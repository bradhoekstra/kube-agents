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

package controller

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func proxyAgentWithResources(override *corev1.ResourceRequirements) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec:       agentv1alpha1.PlatformAgentSpec{AgentSpec: agentv1alpha1.AgentSpec{Deployment: &agentv1alpha1.DeploymentSpec{}}},
	}
	if override != nil {
		agent.Spec.Deployment.CredentialProxy = &agentv1alpha1.CredentialProxySpec{Resources: override}
	}
	return agent
}

func assertQuantity(t *testing.T, list corev1.ResourceList, name corev1.ResourceName, want string) {
	t.Helper()
	got, ok := list[name]
	if !ok {
		t.Fatalf("%s is missing from %v", name, list)
	}
	if got.Cmp(resource.MustParse(want)) != 0 {
		t.Errorf("%s = %s, want %s", name, got.String(), want)
	}
}

// TestCredentialProxyResourcesDefaultToTheOperatorsValues pins the values the
// goldens, footprint.yaml and the chart's quota preflight all carry: a nil CR
// override renders exactly what the literals rendered before the field existed.
func TestCredentialProxyResourcesDefaultToTheOperatorsValues(t *testing.T) {
	for _, deployment := range []*agentv1alpha1.DeploymentSpec{nil, {}, {CredentialProxy: &agentv1alpha1.CredentialProxySpec{}}} {
		got := resolveCredentialProxyResources(deployment)
		assertQuantity(t, got.Requests, corev1.ResourceCPU, "500m")
		assertQuantity(t, got.Requests, corev1.ResourceMemory, "512Mi")
		assertQuantity(t, got.Limits, corev1.ResourceCPU, "1")
		assertQuantity(t, got.Limits, corev1.ResourceMemory, "1Gi")
		assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "2Gi")
		if len(got.Requests) != 2 || len(got.Limits) != 3 {
			t.Errorf("default render carries %d requests and %d limits, want 2 and 3", len(got.Requests), len(got.Limits))
		}
	}
	container := buildCredentialProxyContainer(proxyAgentWithResources(nil))
	if container.Resources.Limits.Memory().Cmp(resource.MustParse("1Gi")) != 0 {
		t.Errorf("container memory limit = %s, want the 1Gi default", container.Resources.Limits.Memory())
	}
}

// TestCredentialProxyMemoryLimitOverrideKeepsTheOtherDefaults is the case the
// field exists for (#2324): a CR raises limits.memory and nothing else, and the
// CPU request Autopilot sizes the pod by, the CPU limit and the
// ephemeral-storage limit that bounds the content workspace all survive.
func TestCredentialProxyMemoryLimitOverrideKeepsTheOtherDefaults(t *testing.T) {
	container := buildCredentialProxyContainer(proxyAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2Gi")},
	}))
	got := container.Resources
	assertQuantity(t, got.Limits, corev1.ResourceMemory, "2Gi")
	assertQuantity(t, got.Requests, corev1.ResourceCPU, "500m")
	assertQuantity(t, got.Requests, corev1.ResourceMemory, "512Mi")
	assertQuantity(t, got.Limits, corev1.ResourceCPU, "1")
	assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "2Gi")
}

// TestCredentialProxyFullOverrideReplacesEveryKey: a CR that states every key
// gets every key, with nothing of the operator's left underneath.
func TestCredentialProxyFullOverrideReplacesEveryKey(t *testing.T) {
	container := buildCredentialProxyContainer(proxyAgentWithResources(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{
			corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("1Gi"),
		},
		Limits: corev1.ResourceList{
			corev1.ResourceCPU: resource.MustParse("2"), corev1.ResourceMemory: resource.MustParse("4Gi"), corev1.ResourceEphemeralStorage: resource.MustParse("8Gi"),
		},
	}))
	got := container.Resources
	assertQuantity(t, got.Requests, corev1.ResourceCPU, "1")
	assertQuantity(t, got.Requests, corev1.ResourceMemory, "1Gi")
	assertQuantity(t, got.Limits, corev1.ResourceCPU, "2")
	assertQuantity(t, got.Limits, corev1.ResourceMemory, "4Gi")
	assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "8Gi")
}

// TestCredentialProxyOverrideDoesNotAliasTheCR: the render copies quantities
// out of the CR rather than sharing them, so a later mutation of the rendered
// container cannot reach back into the object the reconciler was handed.
func TestCredentialProxyOverrideDoesNotAliasTheCR(t *testing.T) {
	override := &corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2Gi")}}
	got := resolveCredentialProxyResources(&agentv1alpha1.DeploymentSpec{CredentialProxy: &agentv1alpha1.CredentialProxySpec{Resources: override}})
	rendered := got.Limits[corev1.ResourceMemory]
	rendered.Add(resource.MustParse("1Gi"))
	got.Limits[corev1.ResourceMemory] = rendered
	if override.Limits.Memory().Cmp(resource.MustParse("2Gi")) != 0 {
		t.Errorf("mutating the rendered limit changed the CR's override to %s", override.Limits.Memory())
	}
}

// TestCredentialProxyBudgetArithmeticAtTheDefaults pins the numbers the design
// quotes (docs/designs/credential-proxy-child-memory-budget.md §2.2 and §2.5):
// a request costs 176 MiB at the 8 MiB cap, the 1Gi default admits four, and
// the floor that admits two is 672 MiB.
func TestCredentialProxyBudgetArithmeticAtTheDefaults(t *testing.T) {
	const mib = 1 << 20
	if credentialProxyOutputCapBytes != 8*mib {
		t.Fatalf("output cap parsed as %d bytes, want 8 MiB", credentialProxyOutputCapBytes)
	}
	if got := credentialProxyRequestCostBytes(credentialProxyOutputCapBytes); got != 176*mib {
		t.Errorf("request cost = %d MiB, want 176", got/mib)
	}
	defaultLimit := resource.MustParse(credentialProxyMemoryLimit)
	if got := CredentialProxyAdmittedRequests(defaultLimit.Value()); got != 4 {
		t.Errorf("the default limit admits %d requests, want 4", got)
	}
	if got := CredentialProxyMemoryLimitFloorBytes(); got != 672*mib {
		t.Errorf("floor = %d MiB, want 672", got/mib)
	}
	if got := CredentialProxyAdmittedRequests(CredentialProxyMemoryLimitFloorBytes()); got != credentialProxyMinimumAdmittedRequests {
		t.Errorf("the floor admits %d requests, want %d", got, credentialProxyMinimumAdmittedRequests)
	}
	if got := CredentialProxyAdmittedRequests(credentialProxyResidentReserveBytes); got != 0 {
		t.Errorf("a limit below the fixed reserves admits %d requests, want 0", got)
	}
}
