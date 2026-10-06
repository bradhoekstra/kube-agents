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
	"fmt"
	"math"
	"slices"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// GKE Autopilot's general-purpose compute class admits a container unchanged
// only while its memory sits between 1 GiB and 6.5 GiB per vCPU, and raises the
// smaller side of the pair to reach the band otherwise. The operator declares
// the proxy's defaults at the lower edge of that band (500m and 512Mi) so the
// manifest it writes is the pod Autopilot admits; an override that leaves the
// band is admitted at figures the CR does not show, and the chart's quota
// preflight, which sums the CR's figures, is short by the difference. A
// warning rather than a refusal: the pod still runs, and on Standard nothing
// is resized at all.
const (
	autopilotMinMemoryBytesPerVCPU int64 = 1 << 30
	autopilotMaxMemoryBytesPerVCPU int64 = 6656 << 20 // 6.5 GiB
	bytesPerMiB                    int64 = 1 << 20
	bytesPerGiB                    int64 = 1 << 30
)

const (
	credentialProxyRequestsField = "requests"
	credentialProxyLimitsField   = "limits"
	credentialProxyClaimsField   = "claims"
)

// The refusals ValidateCredentialProxyResources makes that carry no figures
// of their own.
const (
	credentialProxyClaimsRefusal        = "the credential-proxy pod declares no resourceClaims, so a claim named here cannot take effect"                                                                                                                                                                                                                                 // #nosec G101 -- Error message, not a credential
	credentialProxyNegativeRefusal      = "must not be negative; the API server refuses a container that declares one"                                                                                                                                                                                                                                                    // #nosec G101 -- Error message, not a credential
	credentialProxyZeroLimitRefusal     = "a limit of zero leaves the container nothing of this resource; omit the key to keep the operator's default"                                                                                                                                                                                                                    // #nosec G101 -- Error message, not a credential
	credentialProxyUnrepresentableFmt   = "is not a representable byte count: it exceeds the %d bytes an int64 holds, which is what the Downward API hands the broker"                                                                                                                                                                                                    // #nosec G101 -- Error message, not a credential
	credentialProxyFloorRefusalFmt      = "a %s memory limit is under the %dMi floor at which the budget admits %d commands; below it the broker turns the budget off and admits by the slot cap alone, which is the exposure the budget exists to remove"                                                                                                                // #nosec G101 -- Error message, not a credential
	credentialProxyCrossedBesideFmt     = "exceeds the %s %s limit set beside it"                                                                                                                                                                                                                                                                                         // #nosec G101 -- Error message, not a credential
	credentialProxyCrossedDefLimitFmt   = "exceeds the operator's default %s %s limit, which this override does not raise; set limits.%s as well"                                                                                                                                                                                                                         // #nosec G101 -- Error message, not a credential
	credentialProxyCrossedDefRequestFmt = "is below the operator's default %s %s request, which this override does not lower; set requests.%s as well"                                                                                                                                                                                                                    // #nosec G101 -- Error message, not a credential
	credentialProxyBandWarningFmt       = "%s: %s of memory per %s of CPU is %.2f GiB per vCPU, outside the %d to %.1f GiB per vCPU that GKE Autopilot admits unchanged; Autopilot raises the smaller side into that band, so the pod it admits is larger than this CR declares and the chart's quota preflight, which sums the CR's figures, is short by the difference" // #nosec G101 -- Error message, not a credential
)

// credentialProxyRefusalMoreFmt counts the refusals past the first, which the
// condition and the event leave out: the override's keys are the author's and
// unbounded in number, while a condition message is capped.
const credentialProxyRefusalMoreFmt = " (and %d more)" // #nosec G101 -- Message suffix, not a credential

// credentialProxyResourcesPath is where the override sits on the CR.
var credentialProxyResourcesPath = field.NewPath("spec", "deployment", "credentialProxy", "resources")

// byteCountResources are the names whose quantity is a count of bytes, and so
// has to fit the int64 the Downward API and the kubelet carry it in.
var byteCountResources = []corev1.ResourceName{corev1.ResourceMemory, corev1.ResourceEphemeralStorage}

// maxByteCount is the largest byte count an int64 carries, as a quantity.
var maxByteCount = *resource.NewQuantity(math.MaxInt64, resource.BinarySI)

// ValidateCredentialProxyResources checks spec.deployment.credentialProxy.resources
// on the result the operator renders: its defaults with the CR's keys merged
// over them (resolveCredentialProxyResources). The admission webhook calls it
// at apply, and the reconciler calls it before writing the proxy Deployment,
// so an install that runs without the webhook (the chart's default) refuses
// the same override rather than rendering it. The refusals:
//
//   - claims. The proxy pod declares no resourceClaims, so the key cannot
//     take effect and the render drops it.
//   - A negative quantity on either side, a zero limit, or a byte count
//     beyond an int64. The API server refuses the first; the second leaves
//     the container nothing; the third cannot reach the broker as the byte
//     count it reads its limit as.
//   - A memory limit under the floor at which the broker's child memory
//     budget admits two commands. Below it the broker does not run a smaller
//     budget: it turns the budget off and admits by the slot cap alone, so a
//     limit set too low is the unbudgeted exposure, not an error anywhere
//     else.
//   - A request above its limit, on any name the merged result carries. The
//     API server would refuse the Deployment, which the reconciler would read
//     as an immutable-field change; refusing here puts the error on the field
//     that has the problem, and names which side is the operator's default
//     when the CR set only the other one.
//
// And one warning, when the requests pair or the limits pair leaves the
// memory-per-vCPU band Autopilot admits unchanged (the constants above).
//
// Nothing runs when the CR carries no override: the defaults satisfy every
// check by construction, and the sizing test pins that.
func ValidateCredentialProxyResources(deployment *agentv1alpha1.DeploymentSpec, path *field.Path) (field.ErrorList, admission.Warnings) {
	if deployment == nil || deployment.CredentialProxy == nil || deployment.CredentialProxy.Resources == nil {
		return nil, nil
	}
	override := deployment.CredentialProxy.Resources
	merged := resolveCredentialProxyResources(deployment)
	var errs field.ErrorList
	var warnings admission.Warnings

	if len(override.Claims) > 0 {
		errs = append(errs, field.Forbidden(path.Child(credentialProxyClaimsField), credentialProxyClaimsRefusal))
	}

	sides := []struct {
		name string
		list corev1.ResourceList
	}{{credentialProxyRequestsField, merged.Requests}, {credentialProxyLimitsField, merged.Limits}}

	// A quantity refused on its own is left out of the comparisons below,
	// which would only restate it.
	refused := map[string]bool{}
	for _, side := range sides {
		for _, name := range sortedResourceNames(side.list) {
			quantity := side.list[name]
			at := path.Child(side.name, string(name))
			var msg string
			switch {
			case quantity.Sign() < 0:
				msg = credentialProxyNegativeRefusal
			case quantity.IsZero() && side.name == credentialProxyLimitsField:
				msg = credentialProxyZeroLimitRefusal
			case slices.Contains(byteCountResources, name) && quantity.Cmp(maxByteCount) > 0:
				msg = fmt.Sprintf(credentialProxyUnrepresentableFmt, int64(math.MaxInt64))
			default:
				continue
			}
			errs = append(errs, field.Invalid(at, quantity.String(), msg))
			refused[at.String()] = true
		}
	}

	limitPath := path.Child(credentialProxyLimitsField, string(corev1.ResourceMemory))
	limit := merged.Limits[corev1.ResourceMemory]
	floor := credentialProxyMinimumMemoryLimitBytes(credentialProxyOutputCapBytes)
	if !refused[limitPath.String()] && limit.CmpInt64(floor) < 0 {
		errs = append(errs, field.Invalid(limitPath, limit.String(),
			fmt.Sprintf(credentialProxyFloorRefusalFmt, limit.String(), floor/bytesPerMiB, credentialProxyMinimumAdmittedRequests)))
	}

	for _, name := range sortedResourceNames(merged.Requests) {
		request := merged.Requests[name]
		limit, hasLimit := merged.Limits[name]
		requestPath := path.Child(credentialProxyRequestsField, string(name))
		limitPath := path.Child(credentialProxyLimitsField, string(name))
		if !hasLimit || refused[requestPath.String()] || refused[limitPath.String()] || request.Cmp(limit) <= 0 {
			continue
		}
		// The defaults are consistent with each other, so at least one side of
		// a crossed pair is the override's; the error goes on that side, and
		// says when the other is the operator's.
		_, overrodeRequest := override.Requests[name]
		_, overrodeLimit := override.Limits[name]
		switch {
		case overrodeRequest && overrodeLimit:
			errs = append(errs, field.Invalid(requestPath, request.String(),
				fmt.Sprintf(credentialProxyCrossedBesideFmt, limit.String(), name)))
		case overrodeRequest:
			errs = append(errs, field.Invalid(requestPath, request.String(),
				fmt.Sprintf(credentialProxyCrossedDefLimitFmt, limit.String(), name, name)))
		default:
			errs = append(errs, field.Invalid(limitPath, limit.String(),
				fmt.Sprintf(credentialProxyCrossedDefRequestFmt, request.String(), name, name)))
		}
	}

	for _, side := range sides {
		cpu, hasCPU := side.list[corev1.ResourceCPU]
		memory, hasMemory := side.list[corev1.ResourceMemory]
		if !hasCPU || !hasMemory || cpu.Sign() <= 0 || memory.Sign() <= 0 ||
			refused[path.Child(side.name, string(corev1.ResourceMemory)).String()] {
			continue
		}
		// memory against cpu × each edge, in Quantity arithmetic, which falls
		// back to arbitrary precision rather than wrapping: memory × 1000 / cpu
		// in int64 overflows above about 8 PiB.
		low, high := cpu.DeepCopy(), cpu.DeepCopy()
		low.Mul(autopilotMinMemoryBytesPerVCPU)
		high.Mul(autopilotMaxMemoryBytesPerVCPU)
		if memory.Cmp(low) >= 0 && memory.Cmp(high) <= 0 {
			continue
		}
		gibPerVCPU := memory.AsApproximateFloat64() / cpu.AsApproximateFloat64() / float64(bytesPerGiB)
		warnings = append(warnings, fmt.Sprintf(credentialProxyBandWarningFmt,
			path.Child(side.name), memory.String(), cpu.String(), gibPerVCPU,
			autopilotMinMemoryBytesPerVCPU/bytesPerGiB, float64(autopilotMaxMemoryBytesPerVCPU)/float64(bytesPerGiB)))
	}
	return errs, warnings
}

// sortedResourceNames is list's keys in order, so the errors a CR gets back
// read the same on every apply.
func sortedResourceNames(list corev1.ResourceList) []corev1.ResourceName {
	names := make([]corev1.ResourceName, 0, len(list))
	for name := range list {
		names = append(names, name)
	}
	slices.Sort(names)
	return names
}

// credentialProxyResourcesRefusal is the reconciler's reading of
// ValidateCredentialProxyResources: the first refusal with the count of the
// rest, or "" when the override is valid. Warnings are not refusals; the
// caller logs them.
func credentialProxyResourcesRefusal(agent *agentv1alpha1.PlatformAgent) (string, admission.Warnings) {
	errs, warnings := ValidateCredentialProxyResources(agent.Spec.Deployment, credentialProxyResourcesPath)
	if len(errs) == 0 {
		return "", warnings
	}
	refusal := errs[0].Error()
	if len(errs) > 1 {
		refusal += fmt.Sprintf(credentialProxyRefusalMoreFmt, len(errs)-1)
	}
	return refusal, warnings
}
