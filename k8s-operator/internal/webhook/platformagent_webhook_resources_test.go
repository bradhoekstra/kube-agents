/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package webhook

import (
	"context"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func proxyResourcesAgent(override *corev1.ResourceRequirements) *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
		Spec: agentv1alpha1.PlatformAgentSpec{AgentSpec: agentv1alpha1.AgentSpec{
			Deployment: &agentv1alpha1.DeploymentSpec{
				CredentialProxy: &agentv1alpha1.CredentialProxySpec{Resources: override},
			},
		}},
	}
}

func fieldErrorMessage(t *testing.T, err error, path string) string {
	t.Helper()
	assertFieldError(t, err, path)
	statusErr, ok := err.(*apierrors.StatusError)
	if !ok {
		t.Fatalf("expected *apierrors.StatusError, got %T", err)
	}
	for _, cause := range statusErr.ErrStatus.Details.Causes {
		if cause.Field == path {
			return cause.Message
		}
	}
	return ""
}

// The floor at the operator's defaults: 192Mi + 128Mi + 2 × (128Mi + 6 × 8Mi)
// (docs/designs/credential-proxy-child-memory-budget.md §2.5). A limit of
// 512Mi leaves 192Mi after the fixed reserves, which admits one command.
func TestCredentialProxyMemoryLimitBelowTheFloorIsRefusedWithTheNumbers(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("512Mi")},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.limits.memory")
	for _, want := range []string{"512Mi memory limit admits 1 brokered command", "at least 672Mi to admit 2"} {
		if !strings.Contains(msg, want) {
			t.Errorf("message %q does not say %q", msg, want)
		}
	}
}

// The CPU limit comes down with it: 672Mi against the default 1 CPU is 0.66 GiB
// per vCPU, which the band check would warn on, and that would be a true
// warning rather than this test's subject.
func TestCredentialProxyMemoryLimitAtTheFloorIsAdmitted(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("672Mi"), corev1.ResourceCPU: resource.MustParse("500m")},
	}))
	if err != nil {
		t.Fatalf("a limit exactly at the floor was refused: %v", err)
	}
	if len(warnings) != 0 {
		t.Errorf("672Mi per 500m is inside the Autopilot band, got warnings %v", warnings)
	}
}

// The case the field exists for (#2324): the limit raised to 2Gi and nothing
// else. No error, and no warning either, because 2Gi per 1 CPU and 512Mi per
// 500m are both inside the band.
func TestCredentialProxyTwoGiLimitIsAdmittedWithoutWarnings(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2Gi")},
	}))
	if err != nil {
		t.Fatalf("a 2Gi memory limit was refused: %v", err)
	}
	if len(warnings) != 0 {
		t.Errorf("expected no warnings, got %v", warnings)
	}
}

// A request raised past a limit the CR never wrote: the merged result is what
// is checked, and the error names the operator's default as the other side.
func TestCredentialProxyRequestAboveTheDefaultLimitIsRefused(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2Gi")},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.requests.memory")
	if !strings.Contains(msg, "operator's default 1Gi memory limit") || !strings.Contains(msg, "set limits.memory as well") {
		t.Errorf("message %q does not name the default limit it collides with", msg)
	}
}

// The mirror image: a CPU limit lowered under the request the CR never wrote.
func TestCredentialProxyLimitBelowTheDefaultRequestIsRefused(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("200m")},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.limits.cpu")
	if !strings.Contains(msg, "operator's default 500m cpu request") {
		t.Errorf("message %q does not name the default request it collides with", msg)
	}
}

func TestCredentialProxyEphemeralStorageRequestAboveItsLimitIsRefused(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceEphemeralStorage: resource.MustParse("4Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceEphemeralStorage: resource.MustParse("3Gi")},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.requests.ephemeral-storage")
	if !strings.Contains(msg, "3Gi ephemeral-storage limit set beside it") {
		t.Errorf("message %q does not name the limit set in the same override", msg)
	}
}

// 8Gi of memory against the default 1 CPU limit is 8 GiB per vCPU, past the
// 6.5 GiB Autopilot admits unchanged: a warning on the limits pair, the
// requests pair (512Mi per 500m) still inside the band, and no error.
func TestCredentialProxyMemoryPerCPUOutsideTheAutopilotBandWarns(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("8Gi")},
	}))
	if err != nil {
		t.Fatalf("an out-of-band ratio must warn, not refuse: %v", err)
	}
	if len(warnings) != 1 {
		t.Fatalf("expected one warning, got %v", warnings)
	}
	for _, want := range []string{"spec.deployment.credentialProxy.resources.limits", "8.00 GiB per vCPU", "1 to 6.5 GiB per vCPU", "quota preflight"} {
		if !strings.Contains(warnings[0], want) {
			t.Errorf("warning %q does not say %q", warnings[0], want)
		}
	}
}

// The request pair is checked too: it is the pair Autopilot resizes, and the
// case the issue describes is memory raised at a 500m CPU request.
func TestCredentialProxyRequestPairOutsideTheAutopilotBandWarns(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
	}))
	if err != nil {
		t.Fatalf("expected admission, got: %v", err)
	}
	if len(warnings) != 1 || !strings.Contains(warnings[0], "spec.deployment.credentialProxy.resources.requests") || !strings.Contains(warnings[0], "8.00 GiB per vCPU") {
		t.Errorf("expected one warning on the requests pair at 8 GiB per vCPU, got %v", warnings)
	}
}

// A memory limit of 1Gi against a 4-CPU limit is 0.25 GiB per vCPU, below
// the band's lower edge; the smaller side Autopilot raises is then memory.
func TestCredentialProxyBelowTheAutopilotBandWarns(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("4")},
	}))
	if err != nil {
		t.Fatalf("expected admission, got: %v", err)
	}
	if len(warnings) != 1 || !strings.Contains(warnings[0], "0.25 GiB per vCPU") {
		t.Errorf("expected one warning at 0.25 GiB per vCPU, got %v", warnings)
	}
}

// An empty override block is the field present and saying nothing; the
// defaults are what render, and they pass every check.
func TestCredentialProxyEmptyOverrideIsAdmitted(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	for _, override := range []*corev1.ResourceRequirements{nil, {}} {
		warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(override))
		if err != nil || len(warnings) != 0 {
			t.Errorf("override %v: err=%v warnings=%v, expected neither", override, err, warnings)
		}
	}
}
