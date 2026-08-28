package controller

import (
	"fmt"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The credential proxy, in one of two placements.
//
// **Beside the shell sandbox**, when the sandbox is on and
// spec.security.workloadIdentityFederation names a pool. The proxy is a second
// container in the sandbox's StatefulSet, sharing that pod's loopback and its
// data volume with the shell. This is the placement the design targets and the
// only one in which `git` works, because git's state is the working tree and the
// tree has to be visible from both sides of the exec relay.
//
// **In a Deployment of its own** otherwise. A separate pod cannot mount the
// sandbox's ReadWriteOnce claim, so the proxy falls back to a workspace in its
// own emptyDir: kubectl reads work, `git` and every `-f FILE` write path do not.
// It is also unauthenticated on a ClusterIP, which hands the exec endpoint to
// every pod in the cluster — a NetworkPolicy narrows that where the CNI enforces
// one, and on the reference install (GKE Standard, no Dataplane V2) it does not.
//
// Co-location used to be unavailable for a reason worth restating, because it is
// what spec.security.workloadIdentityFederation exists to remove. GKE resolves
// Workload Identity by *pod IP*, so a sidecar of the sandbox would let the shell
// container curl 169.254.169.254 and mint the proxy's own GSA token — every
// credential the proxy holds, with the policy layer bypassed entirely. Neither
// gVisor nor NetworkPolicy nor automountServiceAccountToken:false closes that:
// gVisor's boundary is the host kernel rather than the network, runtimeClassName
// is pod-scoped, NetworkPolicy is pod-scoped, and Workload Identity does not read
// the projected token file. The pod is the smallest unit that has an IP.
//
// What is *not* pod-scoped is a volumeMount. Federation moves the proxy's cloud
// identity off the metadata server and onto a projected token file mounted into
// this container alone: the pod's ServiceAccount carries no
// iam.gke.io/gcp-service-account annotation, so the metadata server answers both
// containers with an unbound <project>.svc.id.goog principal that IAM grants
// nothing, and the only identity in the pod lives behind a mount namespace the
// shell is not in. shareProcessNamespace stays false so /proc/<pid>/root cannot
// route around that; shell_sandbox_manifests.go pins it and a test asserts it.
//
// The split is by role rather than by copy: the same image runs in both places
// with CREDENTIAL_PROXY_ROLE selecting which of its three services start. See
// deploy/shared/start-services.sh, and the design in
// docs/designs/agent-shell-sandboxing.md.

const (
	// Where the federated token and the ADC config derived from it live. Both
	// are inside the proxy container's mount namespace and nowhere else — that
	// containment is the control, so a mount added to the shell container at
	// either path silently undoes this whole design.
	credentialProxyWIFTokenVolume = "credential-proxy-wif-token"
	credentialProxyWIFTokenPath   = "/var/run/secrets/kubeagents/wif"
	credentialProxyWIFTokenFile   = credentialProxyWIFTokenPath + "/token"

	// On credential-proxy-runtime, which is a memory-backed emptyDir: the file
	// names the token path and the impersonation target and is regenerated at
	// every container start, so nothing is gained by letting it reach a disk.
	credentialProxyWIFCredentialFile = "/var/run/credential-proxy/wif-credentials.json"
)

// credentialProxyFederation returns the federation config when it is complete.
//
// Both fields or neither: a pool with no service account to impersonate, or an
// impersonation target with no pool to reach it through, cannot produce a token.
// Treating a half-filled block as absent means the proxy falls back to its own
// pod and the metadata server, which works — rather than co-locating with the
// sandbox and then failing every credentialed command, which is the same
// misconfiguration with the security property removed.
func credentialProxyFederation(agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.WorkloadIdentityFederationSpec {
	if agent == nil || agent.Spec.Security == nil {
		return nil
	}
	wif := agent.Spec.Security.WorkloadIdentityFederation
	if wif == nil || wif.Audience == "" || wif.ServiceAccountEmail == "" {
		return nil
	}
	return wif
}

// credentialProxyColocated reports whether the proxy runs as a container of the
// sandbox's StatefulSet rather than in a Deployment of its own.
//
// Federation is a precondition, not a preference. Co-locating without it puts a
// metadata-server-backed cloud identity in the same network namespace as the
// shell, which is strictly worse than the separate pod it replaces.
func credentialProxyColocated(agent *agentv1alpha1.PlatformAgent) bool {
	return shellSandboxEnabled(agent) && credentialProxyFederation(agent) != nil
}

// credentialProxyName is the Deployment, Service and pod-selector name.
func credentialProxyName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-credential-proxy"
}

// credentialProxyServiceSelector is what the Service sends traffic to. Co-located,
// that is the sandbox pod; otherwise the proxy's own Deployment. Keeping one
// Service name across both placements is what lets credentialProxyURL stay
// constant for the gateway, whose relay clients should not have to know where the
// relays are running.
func credentialProxyServiceSelector(agent *agentv1alpha1.PlatformAgent) map[string]string {
	if credentialProxyColocated(agent) {
		return shellSandboxSelector(agent)
	}
	return credentialProxySelector(agent)
}

// credentialProxySelector reproduces the labels the pre-#368 standalone proxy
// carried, down to the component label nothing reads any more. A Deployment's
// spec.selector is immutable, so an install old enough to still have that
// Deployment — one that has not reconciled since #368's cleanup removed it —
// would otherwise fail the apply and wedge the whole reconcile rather than
// adopting the object.
func credentialProxySelector(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{
		"app":                           credentialProxyName(agent),
		"kubeagents.x-k8s.io/component": "credential-proxy",
	}
}

// credentialProxyURL is the routable address: what the gateway's Google Chat and
// Slack relay clients dial, from a pod that is never the proxy's own. Fully
// qualified so it resolves the same from a pod with a different search path.
func credentialProxyURL(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("http://%s.%s.svc.cluster.local:%d",
		credentialProxyName(agent), agent.Namespace, credentialProxyPort)
}

// credentialProxySandboxURL is what the sandbox's wrapped CLIs post to, which is
// loopback whenever the proxy is in the same pod.
//
// Not a micro-optimisation. credential_proxy_client.shares_filesystem_with_proxy
// keys on the endpoint being a loopback host, and only when it is true does the
// shim forward the caller's `cwd` and `kubeconfig`. That forwarding is the whole
// of git support: without it every `git` call runs in the proxy's own default
// workspace instead of the tree the agent is working in, which is why the
// standalone placement cannot clone-edit-commit at all. Naming the Service here
// would leave the two containers in one pod and still route them as strangers.
func credentialProxySandboxURL(agent *agentv1alpha1.PlatformAgent) string {
	if credentialProxyColocated(agent) {
		return fmt.Sprintf("http://127.0.0.1:%d", credentialProxyPort)
	}
	return credentialProxyURL(agent)
}

func buildCredentialProxyService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	svc := &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyName(agent), Namespace: agent.Namespace},
		Spec: corev1.ServiceSpec{
			Selector: credentialProxyServiceSelector(agent),
			Ports: []corev1.ServicePort{{
				Name:       "cred-proxy",
				Port:       credentialProxyPort,
				TargetPort: intstr.FromString("cred-proxy"),
			}},
		},
	}
	withCommonLabels(svc, agent)
	return svc
}

// buildCredentialProxyDeployment renders the standalone proxy pod.
//
// Recreate, not RollingUpdate. The Google Chat relay pulls from a Pub/Sub
// subscription and buffers what it pulled until the gateway fetches it over this
// Service; two pods pulling the same subscription during a rollout means
// messages land in the buffer of the pod that is going away, and the Service
// then load-balances the gateway's fetch to the other one. A few seconds of
// unavailability is the cheaper failure — the gateway retries its long poll,
// while a dropped chat message is silent.
func buildCredentialProxyDeployment(agent *agentv1alpha1.PlatformAgent, policyHash string) *appsv1.Deployment {
	saName := agent.Name
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		saName = agent.Spec.Security.ServiceAccountName
	}
	fsGroup := int64(10000)

	podLabels := commonLabels(agent)
	for k, v := range credentialProxySelector(agent) {
		podLabels[k] = v
	}
	// What github-token-minter's NetworkPolicy admits on 8080. It follows the
	// credential runtime rather than staying on the gateway: the runtime is what
	// calls TOKEN_BROKER_URL, and the gateway pod no longer has a reason to.
	podLabels["kubeagents.x-k8s.io/has-credential-proxy"] = "true"

	var affinity *corev1.Affinity
	var nodeSelector map[string]string
	var tolerations []corev1.Toleration
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.Availability != nil {
		affinity = agent.Spec.Deployment.Availability.Affinity
		nodeSelector = agent.Spec.Deployment.Availability.NodeSelector
		tolerations = agent.Spec.Deployment.Availability.Tolerations
	}

	dep := &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyName(agent), Namespace: agent.Namespace},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To(int32(1)),
			Strategy: appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType},
			Selector: &metav1.LabelSelector{MatchLabels: credentialProxySelector(agent)},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      podLabels,
					Annotations: map[string]string{"kubeagents.x-k8s.io/proxy-policy-hash": policyHash},
				},
				Spec: corev1.PodSpec{
					ServiceAccountName:           saName,
					AutomountServiceAccountToken: ptr.To(false),
					SecurityContext: &corev1.PodSecurityContext{
						FSGroup:        &fsGroup,
						RunAsUser:      ptr.To(int64(10000)),
						RunAsNonRoot:   ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Affinity:     affinity,
					NodeSelector: nodeSelector,
					Tolerations:  tolerations,
					Containers:   []corev1.Container{buildCredentialProxyContainer(agent, false)},
					Volumes:      buildCredentialProxyRuntimeVolumes(agent),
				},
			},
		},
	}
	withCommonLabels(dep, agent)
	dep.Labels["app"] = credentialProxyName(agent)
	return dep
}

// buildCredentialProxyContainer is the credential half of the old sidecar: Envoy
// and the credential runtime, with the chat relays the runtime hosts. The event
// watcher and the agent API authenticator stay in the gateway pod, because both
// talk to processes on that pod's loopback — see buildAgentAPIAuthSidecar.
// colocated selects the sandbox-sidecar variant: the shell's data volume as the
// workspace root, a federated identity in place of the metadata server, and a uid
// matching the sandbox login so the two can write each other's files.
func buildCredentialProxyContainer(agent *agentv1alpha1.PlatformAgent, colocated bool) corev1.Container {
	pullPolicy := corev1.PullAlways
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.ImagePullPolicy != nil {
		pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
	}
	// The role, the listen address and the caller authentication come from
	// buildCredentialProxyEnv, which sets them for either way of moving the
	// broker off the agent's Pod. Still 0.0.0.0 when co-located: the sandbox
	// reaches the proxy on loopback, but the gateway reaches the chat relays
	// hosted in the same process over the Service, and one listener serves both.
	envVars := buildCredentialProxyEnv(agent)
	volumeMounts := buildCredentialProxyVolumeMounts()
	securityContext := &corev1.SecurityContext{
		AllowPrivilegeEscalation: ptr.To(false), ReadOnlyRootFilesystem: ptr.To(true), Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
	}
	if colocated {
		envVars = append(envVars, buildCredentialProxyFederationEnv(agent)...)
		// The shell's own data volume, at the path the shell sees it. One
		// filesystem, two mounts, nothing copied — which is what makes
		// `git clone` land where the agent can then edit it, and what makes
		// `kubectl apply -f manifest.yaml` find the file the agent just wrote.
		volumeMounts = append(volumeMounts,
			corev1.VolumeMount{Name: shellSandboxDataVolume, MountPath: shellSandboxDataPath},
			corev1.VolumeMount{Name: credentialProxyWIFTokenVolume, MountPath: credentialProxyWIFTokenPath, ReadOnly: true},
		)
		envVars = append(envVars, corev1.EnvVar{Name: "CREDENTIAL_PROXY_WORKSPACE_ROOT", Value: shellSandboxDataPath})
		// The sandbox login's uid, from deploy/sandbox/Dockerfile, because the
		// entrypoint chowns the data volume to it and a shared tree neither side
		// can fully write is not shared.
		//
		// Sharing a uid with the shell is safe here and was not safe in the
		// gateway pod, and the difference is one field: that pod set
		// shareProcessNamespace, so /proc/<pid>/environ crossed the boundary
		// (#720). The sandbox pod pins it false. With no shared PID namespace and
		// no shared volume but this one, a common uid grants the shell nothing —
		// every credential the proxy holds is in its environment, on an emptyDir
		// it alone mounts, or behind credentialProxyWIFTokenPath.
		securityContext.RunAsUser = ptr.To(int64(shellSandboxUID))
		securityContext.RunAsGroup = ptr.To(int64(shellSandboxUID))
		securityContext.RunAsNonRoot = ptr.To(true)
		securityContext.SeccompProfile = &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}
	}
	// Standalone, CREDENTIAL_PROXY_WORKSPACE_ROOT is deliberately not set. It
	// would name the sandbox's data volume, which a separate pod cannot mount —
	// that claim is ReadWriteOnce. Unset, credential_proxy.py falls back to
	// <state-dir>/workspace inside this pod's own emptyDir, which is where a
	// `git clone` through the proxy lands and where nothing else can read it.
	return corev1.Container{
		Name:            "envoy-credential-proxy",
		Image:           resolveCredentialProxyImage(agent.Spec.Deployment),
		ImagePullPolicy: pullPolicy,
		Command:         []string{"/usr/local/bin/start-services"},
		Env:             envVars,
		Ports:           []corev1.ContainerPort{{Name: "cred-proxy", ContainerPort: credentialProxyPort}},
		ReadinessProbe: &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
				Path: "/healthz", Port: intstr.FromString("cred-proxy"),
			}},
			InitialDelaySeconds: 5,
			PeriodSeconds:       10,
			TimeoutSeconds:      5,
			FailureThreshold:    3,
		},
		Resources: corev1.ResourceRequirements{
			// Lower than the sidecar's, which sized for the event watcher's
			// informer caches. Nothing here holds cluster state; the memory goes
			// on Envoy and one Python process per in-flight command.
			Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse("256Mi")},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("1Gi"), corev1.ResourceEphemeralStorage: resource.MustParse("2Gi"),
			},
		},
		VolumeMounts:    volumeMounts,
		SecurityContext: securityContext,
	}
}

// buildCredentialProxyVolumeMounts is the set both placements share.
//
// Every entry is a volume the shell container must never mount. That is not a
// style rule: co-located, the mount namespace is the only thing separating the
// shell from the proxy's kubeconfig, its gcloud config directory and its
// federated token, because the two containers deliberately run as the same uid.
// TestSandboxSharesOnlyTheDataVolumeWithTheProxy holds the line.
func buildCredentialProxyVolumeMounts() []corev1.VolumeMount {
	return []corev1.VolumeMount{
		{Name: "credential-proxy-policy", MountPath: "/etc/credential-proxy/policy.json", SubPath: "policy.json", ReadOnly: true},
		{Name: "credential-proxy-tmp", MountPath: "/tmp"},
		{Name: "credential-proxy-state", MountPath: "/var/lib/credential-proxy"},
		{Name: "credential-proxy-runtime", MountPath: "/var/run/credential-proxy"},
		// Named for the watcher it was introduced for, but what it holds is
		// $KUBECONFIG — the file CREDENTIAL_PROXY_BOOTSTRAP_COMMAND writes with
		// `gcloud container clusters get-credentials`. The watcher moved; the
		// kubeconfig did not.
		{Name: "event-watcher-kubeconfig", MountPath: "/var/run/event-watcher"},
		{Name: "credential-proxy-ksa-token", MountPath: "/var/run/secrets/kubeagents/serviceaccount", ReadOnly: true},
		// The default-audience token and the cluster CA, which is what the
		// TokenReview call needs — a broker off the agent's Pod authenticates
		// its callers, and it authenticates itself to the API server with this.
		// A different token from the one above: that one is audience-bound to
		// the broker and is what the proxy *presents*, and an audience-bound
		// token is not accepted as a client credential by the API server.
		// AutomountServiceAccountToken is false on both Pods, so nothing
		// projects this bundle unless it is asked for.
		{Name: "event-watcher-ksa-token", MountPath: kubeAPIAccessMountPath, ReadOnly: true},
	}
}

// buildCredentialProxyFederationEnv points the proxy's Google clients at a token
// file instead of 169.254.169.254.
//
// Three variables do the work. CREDENTIAL_PROXY_WIF_* are read by
// scripts/wif_credentials.py, which start-services.sh runs before anything else
// and which writes the external_account document. The other two are what make
// that document authoritative: GOOGLE_APPLICATION_CREDENTIALS for the client
// libraries and gke-gcloud-auth-plugin, CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE
// for gcloud itself, which keeps its own credential store and would otherwise
// ignore ADC and fall through to the metadata server. Both names are already on
// credential_proxy.py's forwarding allowlist, so they reach the executed command
// as well as the bootstrap.
func buildCredentialProxyFederationEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	wif := credentialProxyFederation(agent)
	if wif == nil {
		return nil
	}
	return []corev1.EnvVar{
		{Name: "CREDENTIAL_PROXY_WIF_AUDIENCE", Value: wif.Audience},
		{Name: "CREDENTIAL_PROXY_WIF_SERVICE_ACCOUNT", Value: wif.ServiceAccountEmail},
		{Name: "CREDENTIAL_PROXY_WIF_TOKEN_FILE", Value: credentialProxyWIFTokenFile},
		{Name: "CREDENTIAL_PROXY_WIF_CREDENTIAL_FILE", Value: credentialProxyWIFCredentialFile},
		{Name: "GOOGLE_APPLICATION_CREDENTIALS", Value: credentialProxyWIFCredentialFile},
		{Name: "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", Value: credentialProxyWIFCredentialFile},
	}
}

// buildCredentialProxyFederationVolume is the projected token STS validates.
//
// A second projection rather than a re-audienced credential-proxy-ksa-token: that
// one is presented to github-token-minter, which checks for its own audience, and
// STS checks for the provider's. One token cannot satisfy both, and widening
// either audience to cover the other would let a token minted for one verifier be
// replayed at the other.
// Gated on the placement rather than on the field being set: standalone, the
// proxy has a pod to itself and the metadata server is the simpler correct
// answer, so a federation block left in the CR while the sandbox is off should
// produce no unmounted volume and no behaviour change.
func buildCredentialProxyFederationVolume(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	wif := credentialProxyFederation(agent)
	if wif == nil || !credentialProxyColocated(agent) {
		return nil
	}
	return []corev1.Volume{{
		Name: credentialProxyWIFTokenVolume,
		VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0400)),
			Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
				Audience: wif.Audience,
				// The floor kubelet accepts. Short because the exchange is
				// re-run on demand by the auth library from the file, so a
				// rotation costs nothing, and because this token is the one
				// thing in the pod that is worth stealing.
				ExpirationSeconds: ptr.To(int64(3600)),
				Path:              "token",
			}}},
		}},
	}}
}

// buildCredentialProxyNetworkPolicy narrows who may reach the unauthenticated
// endpoint back down to the two callers that have a reason to: the sandbox,
// whose wrapped CLIs are the proxy's purpose, and the gateway, which pulls chat
// events from the relay hosted here.
//
// Ingress only. Egress is left open because this pod is the one that talks to
// the world — GKE control planes, the Google Chat and Slack APIs, the token
// broker — and enumerating that is #720's problem, not a temporary bridge's.
//
// Inert without a NetworkPolicy implementation, which the reference install
// (GKE Standard, no Dataplane V2) does not have. It is a control where it is
// enforced and a statement of intent where it is not.
func buildCredentialProxyNetworkPolicy(agent *agentv1alpha1.PlatformAgent) *networkingv1.NetworkPolicy {
	tcp := corev1.ProtocolTCP
	np := &networkingv1.NetworkPolicy{
		TypeMeta:   metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyName(agent), Namespace: agent.Namespace},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: credentialProxySelector(agent)},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
			Ingress: []networkingv1.NetworkPolicyIngressRule{{
				From: []networkingv1.NetworkPolicyPeer{
					{PodSelector: &metav1.LabelSelector{MatchLabels: shellSandboxSelector(agent)}},
					{PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": agent.Name + "-gateway"}}},
				},
				Ports: []networkingv1.NetworkPolicyPort{{
					Protocol: &tcp,
					Port:     ptr.To(intstr.FromInt32(credentialProxyPort)),
				}},
			}},
		},
	}
	withCommonLabels(np, agent)
	return np
}

// buildCredentialProxyRuntimeVolumes and buildAgentAPIAuthVolumes split
// buildCredentialProxyVolumes between the two pods the sidecar became. Both
// filter the same source list rather than restating it, so a volume added there
// has to be assigned to a side here and cannot be silently dropped from both.
var (
	// The watcher's default-audience token and the agent's data volume went with
	// the watcher; everything else is the credential runtime's.
	agentAPIAuthVolumeNames = map[string]bool{
		"credential-proxy-tmp":     true,
		"event-watcher-kubeconfig": true,
		"event-watcher-ksa-token":  true,
	}
	// Volumes the agent container must never mount, whatever the CR says.
	// `credential-proxy-state` is the sharp one: it holds $HOME/.gitconfig, the
	// regenerated kubeconfigs the agent is specifically not supposed to hold,
	// and the content workspaces. Three separate controls in credential_proxy.py
	// rest on the agent not seeing that directory, and each of their comments
	// says the protection is deployment geometry rather than a check — this is
	// the check. The CR is authored by an operator rather than by the agent, so
	// this is a configuration hazard and not an escape; it is guarded because
	// nothing else would notice.
	agentForbiddenVolumeNames = map[string]bool{
		"credential-proxy-state":     true,
		"credential-proxy-policy":    true,
		"credential-proxy-runtime":   true,
		"credential-proxy-ksa-token": true,
	}
	credentialProxyRuntimeVolumeNames = map[string]bool{
		"credential-proxy-policy":    true,
		"credential-proxy-tmp":       true,
		"credential-proxy-state":     true,
		"credential-proxy-runtime":   true,
		"event-watcher-kubeconfig":   true,
		"credential-proxy-ksa-token": true,
		// On both sides, and deliberately: it is a projection rather than a
		// shared object, so each Pod gets its own. The watcher authenticates to
		// the API server with it in the agent Pod, and the broker makes its
		// TokenReview call with it here.
		"event-watcher-ksa-token": true,
	}
)

// buildCredentialProxyRuntimeVolumes is the credential runtime's own set, in
// either placement. Co-located it is added to the sandbox pod's volume list
// alongside the federated token; the shell container mounts none of it.
func buildCredentialProxyRuntimeVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	volumes := filterVolumes(buildCredentialProxyVolumes(agent), credentialProxyRuntimeVolumeNames)
	volumes = append(volumes, buildCredentialProxyFederationVolume(agent)...)
	if credentialProxyColocated(agent) {
		volumes = relaxProjectedTokenMode(volumes)
	}
	return volumes
}

// relaxProjectedTokenMode widens the projected tokens from 0400 to 0444 in the
// sandbox pod.
//
// Kubelet writes a projected file as root:root and applies fsGroup only where a
// pod sets one. The gateway and standalone proxy pods do, so 0400 there arrives
// group-readable; the sandbox pod deliberately does not, because fsGroup is
// pod-wide and would take the data volume the shell owns with it. 0400 in that
// pod is therefore readable by nobody: the container runs as 1000, and gcloud's
// first read of the federated token fails with EACCES, which surfaces as the
// proxy crash-looping with no other symptom.
//
// Not a downgrade. What keeps these tokens away from the shell is that the shell
// container does not mount the volumes at all — a volumeMount is per-container
// where a pod's identity is not. Inside the proxy container there is no second
// uid for the extra read bits to reach.
func relaxProjectedTokenMode(volumes []corev1.Volume) []corev1.Volume {
	for i := range volumes {
		if volumes[i].Projected != nil {
			volumes[i].Projected.DefaultMode = ptr.To(int32(0444))
		}
	}
	return volumes
}

// buildAgentAPIAuthVolumes is the gateway pod's remaining share. The data volume
// the watcher reads is not here: the gateway pod already declares it.
func buildAgentAPIAuthVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	return filterVolumes(buildCredentialProxyVolumes(agent), agentAPIAuthVolumeNames)
}

func filterVolumes(volumes []corev1.Volume, keep map[string]bool) []corev1.Volume {
	var out []corev1.Volume
	for _, vol := range volumes {
		if keep[vol.Name] {
			out = append(out, vol)
		}
	}
	return out
}

// validateExtraVolumeMounts refuses a CR that would mount a broker-owned volume
// into the agent container, returning the message for a degraded condition or
// "" when the CR is acceptable.
//
// It reports rather than silently dropping the mount. A dropped mount is a CR
// whose author believes it took effect, and the failure this guards against is
// one nobody looks for: the manifest applies cleanly and the agent quietly gains
// read access to the credentials the proxy exists to keep from it.
func validateExtraVolumeMounts(agent *agentv1alpha1.PlatformAgent) string {
	if agent.Spec.Deployment == nil {
		return ""
	}
	var forbidden []string
	for _, mount := range agent.Spec.Deployment.ExtraVolumeMounts {
		if agentForbiddenVolumeNames[mount.Name] {
			forbidden = append(forbidden, fmt.Sprintf("%s (at %s)", mount.Name, mount.MountPath))
		}
	}
	if len(forbidden) == 0 {
		return ""
	}
	return fmt.Sprintf(
		"spec.deployment.extraVolumeMounts names volumes owned by the credential proxy: %s. "+
			"Those hold the broker's home directory, its generated kubeconfigs and its git "+
			"workspaces, and mounting them into the agent container defeats the credential "+
			"isolation the proxy provides. Remove them, or use a volume of your own.",
		strings.Join(forbidden, ", "),
	)
}
