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
	"context"
	"fmt"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
)

// existingClaim is a bound claim from a previous install, at `size`.
func existingClaim(name, namespace, size string) *corev1.PersistentVolumeClaim {
	return &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse(size)},
			},
		},
	}
}

func claimSize(t *testing.T, cl client.Client, name, namespace string) string {
	t.Helper()
	got := &corev1.PersistentVolumeClaim{}
	if err := cl.Get(context.Background(), client.ObjectKey{Name: name, Namespace: namespace}, got); err != nil {
		t.Fatalf("reading claim %s: %v", name, err)
	}
	q := got.Spec.Resources.Requests[corev1.ResourceStorage]
	return q.String()
}

// TestReconcilePVCGrowsAnExistingDataClaim pins that the agent's data claim
// follows agentDataStorageSize on an existing install, not only on a new one:
// the claim is create-only otherwise, and a size raised to stop the volume
// filling would reach no install that already has the problem.
func TestReconcilePVCGrowsAnExistingDataClaim(t *testing.T) {
	agent := brokerPodAgent()
	dataClaim := buildPVC(agent)
	systemClaim := buildSystemPVC(agent)
	cases := []struct {
		name     string
		have     string
		wantSize string
	}{
		{"a smaller claim is widened to the constant", "10Gi", agentDataStorageSize},
		{"a claim already at the constant is left alone", agentDataStorageSize, agentDataStorageSize},
		{"a larger claim is never shrunk", "80Gi", "80Gi"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(agent,
					existingClaim(dataClaim.Name, agent.Namespace, tc.have),
					existingClaim(systemClaim.Name, agent.Namespace, "1Gi")).
				Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

			if err := r.reconcilePVC(context.Background(), agent); err != nil {
				t.Fatalf("reconcilePVC: %v", err)
			}
			if got := claimSize(t, cl, dataClaim.Name, agent.Namespace); got != tc.wantSize {
				t.Errorf("data claim is %s, want %s", got, tc.wantSize)
			}
			if got := claimSize(t, cl, systemClaim.Name, agent.Namespace); got != "1Gi" {
				t.Errorf("the system claim moved to %s; only the data claim follows the constant", got)
			}
		})
	}
}

// TestReconcilePVCCreatesTheDataClaimAtTheConstant is the new-install half:
// no claim yet, so the reconcile creates one at the constant and the grow
// has nothing to do.
func TestReconcilePVCCreatesTheDataClaimAtTheConstant(t *testing.T) {
	agent := brokerPodAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.reconcilePVC(context.Background(), agent); err != nil {
		t.Fatalf("reconcilePVC: %v", err)
	}
	if got := claimSize(t, cl, buildPVC(agent).Name, agent.Namespace); got != agentDataStorageSize {
		t.Errorf("new data claim is %s, want %s", got, agentDataStorageSize)
	}
}

// TestReconcilePVCSurvivesARefusedExpansion pins the best-effort contract: a
// StorageClass without allowVolumeExpansion refuses the patch, the claim keeps
// its size, and the reconcile still succeeds rather than taking the agent down
// over a volume that is merely smaller than wanted.
func TestReconcilePVCSurvivesARefusedExpansion(t *testing.T) {
	agent := brokerPodAgent()
	dataClaim := buildPVC(agent)
	refusals := 0
	refuse := interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if _, isPVC := obj.(*corev1.PersistentVolumeClaim); !isPVC {
				return cl.Patch(ctx, obj, patch, opts...)
			}
			refusals++
			return errors.NewForbidden(
				schema.GroupResource{Resource: "persistentvolumeclaims"}, obj.GetName(),
				fmt.Errorf("only dynamically provisioned pvc can be resized and the storageclass that provisions the pvc must support resize"))
		},
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, existingClaim(dataClaim.Name, agent.Namespace, "10Gi")).
		WithInterceptorFuncs(refuse).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.reconcilePVC(context.Background(), agent); err != nil {
		t.Fatalf("a refused expansion must not fail the reconcile: %v", err)
	}
	if refusals == 0 {
		t.Fatal("the fake never refused a patch, so this exercised the accepting path and proves nothing")
	}
	if got := claimSize(t, cl, dataClaim.Name, agent.Namespace); got != "10Gi" {
		t.Errorf("data claim is %s after a refused patch, want 10Gi", got)
	}
}
