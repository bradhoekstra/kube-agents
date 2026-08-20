package controller

import (
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The credential proxy in a Deployment of its own, reachable over a Service.
//
// This is a temporary arrangement and #720 replaces it. It exists because the
// shell sandbox cannot reach a sidecar: the proxy used to run beside the agent
// container in the gateway pod, bound to that pod's loopback, and the sandbox is
// a different pod. #720 is the durable answer; until it merges, nothing in the
// sandbox can run kubectl, gcloud, git or gh at all, and the whole sandboxing
// design is untestable end to end.
//
// What moving it costs, stated plainly: the proxy authenticates no caller. While
// it was on loopback that was the access control — only a container in the
// gateway pod could reach it. A ClusterIP hands the same unauthenticated
// endpoint to every pod in the cluster. The credential property survives, since
// the proxy's whole job is to run credentialed commands without ever handing the
// raw credential back, and its redaction and policy still apply to every caller.
// What is lost is the containment of *who may execute* — a NetworkPolicy is
// written for it below and, on a cluster with a network-policy implementation,
// restores most of that. On the reference install (GKE Standard, no Dataplane
// V2) it is inert, so the exposure there is real. Do not run this arrangement on
// a cluster with untrusted workloads in it.
//
// The split is by role rather than by copy: the same image runs in both places
// with CREDENTIAL_PROXY_ROLE selecting which of its three services start. See
// deploy/shared/start-services.sh, and the design in
// docs/designs/credential-proxy-placement.md under "What shipped ahead of #720".

// credentialProxyName is the Deployment, Service and pod-selector name.
func credentialProxyName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-credential-proxy"
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

// credentialProxyURL is what every client is pointed at: the sandbox's wrapped
// CLIs, and the gateway's Google Chat and Slack relay clients. Fully qualified
// so it resolves the same from a pod with a different search path.
func credentialProxyURL(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("http://%s.%s.svc.cluster.local:%d",
		credentialProxyName(agent), agent.Namespace, credentialProxyPort)
}

func buildCredentialProxyService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	svc := &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyName(agent), Namespace: agent.Namespace},
		Spec: corev1.ServiceSpec{
			Selector: credentialProxySelector(agent),
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
					Containers:   []corev1.Container{buildCredentialProxyContainer(agent)},
					Volumes:      buildStandaloneCredentialProxyVolumes(agent),
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
func buildCredentialProxyContainer(agent *agentv1alpha1.PlatformAgent) corev1.Container {
	pullPolicy := corev1.PullAlways
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.ImagePullPolicy != nil {
		pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
	}
	envVars := buildCredentialProxyEnv(agent)
	envVars = append(envVars,
		corev1.EnvVar{Name: "CREDENTIAL_PROXY_ROLE", Value: "credentials"},
		// The reason this pod exists. Loopback is the image default; see the
		// exposure caveat at the top of this file.
		corev1.EnvVar{Name: "CREDENTIAL_PROXY_LISTEN_ADDRESS", Value: "0.0.0.0"},
	)
	// CREDENTIAL_PROXY_WORKSPACE_ROOT is deliberately not set. In the sidecar it
	// named the agent's data volume, which this pod cannot mount — that claim is
	// ReadWriteOnce and belongs to the gateway. Unset, credential_proxy.py falls
	// back to <state-dir>/workspace inside this pod's own emptyDir, which is
	// where a `git clone` through the proxy now lands. Nothing else reads those
	// trees; the sandbox never could.
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
		VolumeMounts: []corev1.VolumeMount{
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
		},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false), ReadOnlyRootFilesystem: ptr.To(true), Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
	}
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

// buildStandaloneCredentialProxyVolumes and buildAgentAPIAuthVolumes split
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
	standaloneCredentialProxyVolumeNames = map[string]bool{
		"credential-proxy-policy":    true,
		"credential-proxy-tmp":       true,
		"credential-proxy-state":     true,
		"credential-proxy-runtime":   true,
		"event-watcher-kubeconfig":   true,
		"credential-proxy-ksa-token": true,
	}
)

func buildStandaloneCredentialProxyVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	return filterVolumes(buildCredentialProxyVolumes(agent), standaloneCredentialProxyVolumeNames)
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
