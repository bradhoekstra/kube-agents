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

// The shell sandbox: the pod the agent's terminal, file and code-execution tools
// run in once Hermes' `ssh` terminal backend is turned on. Design and rationale
// live in docs/designs/agent-shell-sandboxing.md; the image is deploy/sandbox/.
//
// Reconciled only when spec.harness.experimental.shellSandbox.enabled is true, which
// no install sets by default. It stays experimental until #737 Part C gives the
// credential proxy an address of its own (see the credentialProxyURL parameter
// below): without it the sandbox has no credential path at all, so kubectl, gcloud,
// gh and git report that they are unconfigured. That is a usable state for testing
// the file and code-execution tools and not one to ship an agent in.
//
// On the name: "sandbox" already means something else here. The agent's own
// container is the credential-isolation sandbox — see buildSandboxCredentialCleanup
// and safeSandboxEnvOverrides — and that usage predates this file and is load-bearing
// in docs/credential-isolation-design.md. Everything in here is therefore the *shell*
// sandbox, and its objects are named <agent>-shell so no one has to hold both
// meanings at once while reading a `kubectl get`.
//
// On the workload kind: this was going to be a `Sandbox` custom resource from
// kubernetes-sigs/agent-sandbox. It is a StatefulSet because three of that project's
// four CRDs do not exist in the version that ships, and what does ship maps field for
// field onto this file. The design doc records the evidence, and the interface is
// drawn so that swapping back is this file and nothing else.
package controller

import (
	"fmt"
	"os"
	"path"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// The port sshd listens on in the sandbox image, matching
	// deploy/sandbox/sshd_config. Above 1024 so the daemon does not need
	// CAP_NET_BIND_SERVICE, and not 22 so nothing in the cluster mistakes it for
	// a node.
	shellSandboxPort = 2222

	// Operator-level override for installs mirroring images into a private
	// registry, matching the "override" field of the agent-sandbox entry in
	// images.json. Set on the controller-manager Deployment.
	shellSandboxImageEnvVar = "AGENT_SANDBOX_IMAGE"

	// The login the agent ssh's in as, created by deploy/sandbox/Dockerfile as uid
	// 1000, with an ephemeral home and a durable /opt/data. Not root, and not the
	// agent pod's own uid 10000 — the two pods share nothing but a public key.
	shellSandboxUser = "agent"

	// shellSandboxUser's uid, from the same useradd. Named here because the
	// credential proxy runs under it when it shares this pod — the entrypoint
	// chowns the data volume to this uid, and a workspace only one of the two
	// can write is not a shared workspace. See buildCredentialProxyContainer for
	// why a common uid is safe in this pod and was not in the gateway's.
	shellSandboxUID = 1000

	shellSandboxDataVolume     = "data"
	shellSandboxSshdVolume     = "sshd"
	shellSandboxKeysVolume     = "authorized-keys"
	shellSandboxSettingsVolume = "settings"

	// Where deploy/sandbox/entrypoint.sh expects each of them. Changing either
	// side alone starts a pod that exits with a pointed message rather than one
	// that half works, which is the intended failure mode.
	//
	// The data path is the agent pod's Hermes home path, on purpose and on a
	// different volume: the SOPs, skills and model-written scripts that hardcode
	// /opt/data then resolve wherever they run, instead of failing on a directory
	// that exists in only one of the two pods. Nothing is copied across and
	// nothing can read across — see the marker file entrypoint.sh writes, and the
	// design doc's note that no handoff may assume write-here-read-there.
	shellSandboxDataPath = "/opt/data"
	shellSandboxKeysPath = "/etc/ssh-authorized"

	// sshd's host keys, on a volume the model has no access to. They cannot live
	// on the data volume: uid 1000 owns that mount point, so it can rename any
	// directory inside it and take over whatever the entrypoint writes there
	// next. Both clients pin the host key with StrictHostKeyChecking=accept-new,
	// which is worth nothing if the sandboxed account holds the private half.
	shellSandboxSshdPath = "/var/lib/sandbox-sshd"

	// shellSandboxUser's home, from the useradd in deploy/sandbox/Dockerfile. It
	// is writable alongside the data volume — see HERMES_WRITE_SAFE_ROOT in
	// buildPodTemplateSpec — but it is on the container filesystem and does not
	// survive a restart. That is deliberate: the model owns ~/.bashrc, bash
	// sources it for a non-interactive `ssh host cmd`, and a hijack planted there
	// should not outlive the pod. Durable work goes to the data volume, which is
	// what TERMINAL_CWD points at.
	shellSandboxHomePath = "/home/" + shellSandboxUser

	// The agent pod's side of the same keypair. Two volumes rather than one for
	// a reason spelled out at buildShellSandboxClientKeyInitContainer: the
	// Secret cannot be handed to `ssh -i` directly.
	shellSandboxClientKeySecretVolume = "sandbox-ssh-secret"
	shellSandboxClientKeyVolume       = "sandbox-ssh"
	shellSandboxClientKeySecretPath   = "/etc/sandbox-ssh-secret"
	shellSandboxClientKeyPath         = "/etc/sandbox-ssh"
	shellSandboxClientKeyFile         = "id_ed25519"

	// The key in platform-agent-secrets holding the private half. The public
	// half is beside it as SANDBOX_SSH_PUBLIC_KEY, but the agent pod has no use
	// for it — it is there so a re-running install surface can recover the pair
	// from one place, and so the chart can render the sandbox's Secret from it.
	shellSandboxPrivateKeySecretKey = "SANDBOX_SSH_PRIVATE_KEY"
)

// shellSandboxAuthorizedKeysSecretName is the Secret the sandbox mounts. It holds
// one entry, `authorized_keys`, and nothing else.
//
// Deliberately not platform-agent-secrets with an `items:` selector, which would
// work — kubelet projects only the listed items — and is still wrong: that object
// holds every model API key, and naming it in the sandbox's volume list puts the
// whole thing one careless edit away from being readable inside the pod this
// design exists to keep credential-free. The duplication of the public half
// across two Secrets is the price, and a public key is the cheapest thing in the
// system to duplicate.
func shellSandboxAuthorizedKeysSecretName(agent *agentv1alpha1.PlatformAgent) string {
	return shellSandboxName(agent) + "-authorized-keys"
}

// shellSandboxServiceAccountName is the identity the sandbox pod runs as.
//
// Its own, not the agent's, and the difference is the point. The agent's
// ServiceAccount carries iam.gke.io/gcp-service-account, and GKE resolves
// Workload Identity by pod IP — so running this pod under it would hand the
// shell container a full GSA token from 169.254.169.254 whether or not anything
// mounts a Kubernetes token, and whether or not the credential proxy is even
// there. This one is deliberately unannotated: the metadata server answers both
// containers with the unbound <project>.svc.id.goog principal, which IAM grants
// nothing.
//
// The proxy's cloud identity comes from spec.security.workloadIdentityFederation
// instead — a projected token this ServiceAccount can mint, mounted into the
// proxy container alone. The KSA is still the subject IAM authorizes; what
// changes is that the authorization runs against a token file rather than a pod
// IP, and a file is per-container where an IP is not.
func shellSandboxServiceAccountName(agent *agentv1alpha1.PlatformAgent) string {
	return shellSandboxName(agent)
}

// buildShellSandboxServiceAccount renders it. No annotations at all, so there is
// no place for iam.gke.io/gcp-service-account to arrive by accident: the CR's
// spec.security.serviceAccountAnnotations is deliberately not plumbed through
// here, because the one annotation an operator would reach for is the one that
// undoes the isolation.
func buildShellSandboxServiceAccount(agent *agentv1alpha1.PlatformAgent) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      shellSandboxServiceAccountName(agent),
			Namespace: agent.Namespace,
			Labels:    shellSandboxSelector(agent),
		},
		// Kubelet would otherwise create a legacy token Secret for it on older
		// clusters. Nothing reads one; the projected volume is the only token
		// path this design has.
		AutomountServiceAccountToken: ptr.To(false),
	}
}

// shellSandboxClientKeyFilePath is the path the agent's Hermes config points
// `terminal.ssh.key_path` at once this is wired up.
func shellSandboxClientKeyFilePath() string {
	return shellSandboxClientKeyPath + "/" + shellSandboxClientKeyFile
}

// fallbackShellSandboxImage derives its tag from DefaultPlatformAgentVersion at
// call time, exactly as fallbackPlatformAgentImage does, so a release build
// defaults the sandbox and the agent to the same version. They are built from the
// same commit by the same workflow and a skew between them is a bug, not a
// configuration.
func fallbackShellSandboxImage() string {
	return "ghcr.io/gke-labs/kube-agents/agent-sandbox:" + DefaultPlatformAgentVersion
}

// resolveShellSandboxImage returns the sandbox image: the CR's own override if it
// carries one, else AGENT_SANDBOX_IMAGE from the controller, else the public
// ghcr.io default.
//
// Deliberately not derived from the resolved agent image the way
// resolveCredentialProxyImage is. That derivation exists because the proxy is a
// second stage of the same Dockerfile and must not drift from the agent it sits
// beside in one pod; the sandbox is a separate artifact in a separate pod, and
// inferring its registry from a CR's spec.deployment.image would mean a user who
// points the agent at their own mirror silently gets a sandbox image from a
// repository they never populated. Hence the explicit per-agent field.
func resolveShellSandboxImage(agent *agentv1alpha1.PlatformAgent) string {
	if spec := shellSandboxSpec(agent); spec != nil && spec.Image != "" {
		return spec.Image
	}
	if override := os.Getenv(shellSandboxImageEnvVar); override != "" {
		return override
	}
	return fallbackShellSandboxImage()
}

// shellSandboxSpec returns the CR's sandbox block, or nil. Every access to it goes
// through here because the path is four optional levels deep and a nil check missed
// anywhere in it is a panic in the reconcile loop.
func shellSandboxSpec(agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.ShellSandboxSpec {
	if agent == nil || agent.Spec.Harness == nil || agent.Spec.Harness.Experimental == nil {
		return nil
	}
	return agent.Spec.Harness.Experimental.ShellSandbox
}

// shellSandboxEnabled reports whether this agent's shell runs in the sandbox.
// Absent means off: an install that says nothing keeps the local shell every
// existing install has.
func shellSandboxEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	spec := shellSandboxSpec(agent)
	return spec != nil && spec.Enabled != nil && *spec.Enabled
}

// shellSandboxName is the name of every object in this file: the StatefulSet, its
// governing Service, and the NetworkPolicy. One name, because they are one thing,
// and because the DNS record the agent dials is built from it.
func shellSandboxName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-shell"
}

// shellSandboxSelector is the pod label the Service, the StatefulSet and both
// halves of the NetworkPolicy agree on. `app` rather than a kubeagents.x-k8s.io/
// key to match the gateway's existing selector, which the ingress rule below has
// to name anyway.
func shellSandboxSelector(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{"app": shellSandboxName(agent)}
}

// shellSandboxHost is the address Hermes' ssh backend connects to: the stable
// per-pod DNS name a StatefulSet gives its replica through its governing Service.
// It is what buildConfigMapData will render into the agent's terminal.ssh settings
// when this is wired up.
//
// Not the Service name. A headless Service resolves to the pod's address either
// way at one replica, but the pod name is the record that stays correct if this
// ever grows a second replica, and it is what makes the identity in
// "long-running singleton with a stable identity" observable from the client side.
func shellSandboxHost(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("%s-0.%s.%s.svc.cluster.local", shellSandboxName(agent), shellSandboxName(agent), agent.Namespace)
}

// buildShellSandboxService is the StatefulSet's governing Service: headless, so it
// publishes the per-pod DNS record above rather than load-balancing to it.
func buildShellSandboxService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	name := shellSandboxName(agent)
	return &corev1.Service{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: agent.Namespace,
			Labels:    shellSandboxSelector(agent),
		},
		Spec: corev1.ServiceSpec{
			ClusterIP: corev1.ClusterIPNone,
			Selector:  shellSandboxSelector(agent),
			Ports: []corev1.ServicePort{{
				Name:       "ssh",
				Port:       shellSandboxPort,
				TargetPort: intstr.FromInt32(shellSandboxPort),
				Protocol:   corev1.ProtocolTCP,
			}},
			// The pod is addressable while sshd is still generating host keys on a
			// first start. Without this the DNS record does not exist until the
			// readiness probe passes, and a StatefulSet's first pod can wait on its
			// own name.
			PublishNotReadyAddresses: true,
		},
	}
}

// buildShellSandboxStatefulSet is the sandbox itself.
//
// authorizedKeysSecret holds the public half of the keypair the agent pod connects
// with, under the key "authorized_keys". credentialProxyURL is what the sandbox's
// kubectl/gcloud/gh/git wrappers post to — loopback when the proxy is a container
// of this same pod, the proxy's Service otherwise. Empty is a supported state: the
// entrypoint logs that the wrappers are unconfigured and starts anyway, so file and
// code-execution tools work while the credentialed ones report a clear error
// instead of a stack trace.
//
// policyHash reaches the pod template only when the proxy is here. It is the same
// annotation the standalone Deployment carries, and it exists so that editing the
// exec policy restarts whatever pod is enforcing it.
//
// settingsConfigHash goes on the pod template for the same reason the agent's
// Deployment carries it: SETTINGS.md is mounted with a subPath, and a subPath mount
// is resolved once at pod start and never refreshed. Without the annotation, editing
// the CR's scope rolls the agent pod onto the new file and leaves the sandbox holding
// the old one — and the sandbox is where the shell reads it, so the skills that read
// SETTINGS.md by path would be the ones getting the stale answer.
func buildShellSandboxStatefulSet(agent *agentv1alpha1.PlatformAgent, authorizedKeysSecret, credentialProxyURL, settingsConfigHash, policyHash string) *appsv1.StatefulSet {
	name := shellSandboxName(agent)
	labels := shellSandboxSelector(agent)

	env := []corev1.EnvVar{}
	if credentialProxyURL != "" {
		env = append(env, corev1.EnvVar{Name: "CREDENTIAL_PROXY_URL", Value: credentialProxyURL})
	}

	containers := buildShellSandboxContainers(agent, env)
	volumes := buildShellSandboxVolumes(agent, authorizedKeysSecret)

	annotations := map[string]string{
		"kubeagents.x-k8s.io/settings-config-hash": settingsConfigHash,
	}
	// A superset of the selector, never a replacement for it: a StatefulSet's
	// spec.selector is immutable, so the extra label has to live on the template
	// alone or turning federation on would need the object deleted first.
	//
	// commonLabels is in here explicitly, and that is not belt-and-braces.
	// `labels` is one map shared by ObjectMeta.Labels and Selector.MatchLabels
	// below, and withCommonLabels merges into the object's map in place on the
	// way out — so by the time the StatefulSet reaches the API server its
	// selector carries the four recommended labels too. A template built from
	// `labels` alone would then be narrower than the selector the server has
	// stored, which it rejects with `selector` does not match template `labels`.
	podLabels := commonLabels(agent)
	for k, v := range labels {
		podLabels[k] = v
	}
	if credentialProxyColocated(agent) {
		annotations["kubeagents.x-k8s.io/proxy-policy-hash"] = policyHash
		// github-token-minter's NetworkPolicy admits callers by this label. It
		// follows the credential runtime between placements, which is the whole
		// reason it is a label rather than a pod name.
		podLabels["kubeagents.x-k8s.io/has-credential-proxy"] = "true"
	}

	return &appsv1.StatefulSet{
		TypeMeta: metav1.TypeMeta{APIVersion: "apps/v1", Kind: "StatefulSet"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: agent.Namespace,
			Labels:    labels,
		},
		Spec: appsv1.StatefulSetSpec{
			Replicas:    ptr.To(int32(1)),
			ServiceName: name,
			Selector:    &metav1.LabelSelector{MatchLabels: labels},
			// Retain on both transitions, for both claims. The sshd volume holds
			// the host keys, and Hermes connects with
			// StrictHostKeyChecking=accept-new: a regenerated host key is not a
			// prompt, it is every command from then on failing until known_hosts
			// is edited by hand. The data volume holds whatever the agent has
			// been working on. Deleting the StatefulSet must therefore leave both
			// claims, at the cost of PVCs that outlive their workload.
			PersistentVolumeClaimRetentionPolicy: &appsv1.StatefulSetPersistentVolumeClaimRetentionPolicy{
				WhenDeleted: appsv1.RetainPersistentVolumeClaimRetentionPolicyType,
				WhenScaled:  appsv1.RetainPersistentVolumeClaimRetentionPolicyType,
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      podLabels,
					Annotations: annotations,
				},
				Spec: corev1.PodSpec{
					// Unannotated, so the metadata server has no GSA to hand
					// either container. See shellSandboxServiceAccountName.
					ServiceAccountName: shellSandboxServiceAccountName(agent),
					// The whole point. With a token mounted, the sandbox holds a
					// Kubernetes credential and the boundary this workload exists
					// to draw is decorative.
					//
					// Note what this does *not* do, since it is the obvious thing
					// to reach for and it is not a Workload Identity control:
					// Workload Identity never reads the projected token file, so
					// turning the automount off leaves 169.254.169.254 answering
					// exactly as before. Unbinding the ServiceAccount above is
					// what closes that; this closes the Kubernetes API.
					AutomountServiceAccountToken: ptr.To(false),
					// Explicitly false, never merely unset. With the credential
					// proxy in this pod, a shared PID namespace would put its
					// environment — Slack tokens, API_SERVER_EXTERNAL_KEY — and
					// its whole filesystem behind /proc/<pid>/{environ,root},
					// readable from the shell, which runs as the same uid on
					// purpose. That is the exact finding #720 reproduced on the
					// gateway pod, which did set this. Pinning it rather than
					// leaving it nil is so that a future edit has to argue with a
					// value instead of adding one to a blank.
					ShareProcessNamespace: ptr.To(false),
					// Kubelet otherwise injects a docker-link-style env var for
					// every Service in the namespace. None of them are secrets,
					// but they hand the sandbox a map of the namespace it has no
					// use for: a live pod came up knowing the cluster IP and port
					// of another workload's Service. The sandbox reaches the
					// credential proxy by an explicit URL, so it needs no
					// service discovery at all.
					EnableServiceLinks: ptr.To(false),
					// No securityContext, and that is a decision rather than an
					// omission. sshd's privilege separation forks as uid 0 and
					// drops to the unprivileged `agent` user for the session, and
					// the entrypoint chowns the freshly-mounted data volume before
					// it — so runAsNonRoot cannot be set, and a capability drop
					// has to keep at least CHOWN, SETUID, SETGID, SYS_CHROOT and
					// DAC_OVERRIDE. Which of those is genuinely required is a
					// question deploy/sandbox/smoke-test.sh can answer and nobody
					// has asked it yet; guessing here would produce a pod that
					// fails at login, which reads as a key problem.
					Containers: containers,
					Volumes:    volumes,
				},
			},
			// Two claims, because one of them must be unreachable from the account
			// that can write the other. See shellSandboxSshdPath.
			//
			// VolumeClaimTemplates is immutable, so an install that already has a
			// sandbox needs its StatefulSet deleted (--cascade=orphan keeps the
			// pod up meanwhile) before the operator can lay this down. The feature
			// is experimental and off by default, which is what makes that
			// acceptable rather than a migration.
			VolumeClaimTemplates: []corev1.PersistentVolumeClaim{
				{
					ObjectMeta: metav1.ObjectMeta{Name: shellSandboxDataVolume},
					Spec: corev1.PersistentVolumeClaimSpec{
						AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
						Resources: corev1.VolumeResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceStorage: resource.MustParse(defaultStorageSize),
							},
						},
					},
				},
				{
					// Two host keys and nothing else, so this is a minimum-size
					// request rather than a sized one; the CSI driver rounds it up
					// to whatever the storage class's disk type allows.
					ObjectMeta: metav1.ObjectMeta{Name: shellSandboxSshdVolume},
					Spec: corev1.PersistentVolumeClaimSpec{
						AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
						Resources: corev1.VolumeResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceStorage: resource.MustParse("1Gi"),
							},
						},
					},
				},
			},
		},
	}
}

// buildShellSandboxContainers is the shell, plus the credential proxy when this
// pod is where the proxy lives.
//
// Order matters only for readability — kubelet starts both concurrently and
// neither waits on the other. The proxy tolerates a data volume the shell's
// entrypoint has not chowned yet because its workspace root is the mount point
// itself, which already exists; it creates nothing there until the first command
// arrives, by which time the shell has been up long enough to accept an ssh
// session.
func buildShellSandboxContainers(agent *agentv1alpha1.PlatformAgent, env []corev1.EnvVar) []corev1.Container {
	containers := []corev1.Container{buildShellSandboxContainer(agent, env)}
	if credentialProxyColocated(agent) {
		containers = append(containers, buildCredentialProxyContainer(agent, true))
	}
	return containers
}

// buildShellSandboxVolumes is the shell's own set, plus the credential runtime's
// when co-located.
//
// The two sets are disjoint apart from the data volume, and only the data volume
// is mounted by both containers. That is the isolation: the proxy's kubeconfig,
// gcloud configuration and federated token are separated from the shell by a
// mount namespace, which is the only per-container boundary a pod has.
func buildShellSandboxVolumes(agent *agentv1alpha1.PlatformAgent, authorizedKeysSecret string) []corev1.Volume {
	volumes := []corev1.Volume{{
		Name: shellSandboxKeysVolume,
		VolumeSource: corev1.VolumeSource{
			Secret: &corev1.SecretVolumeSource{
				SecretName: authorizedKeysSecret,
				// Only this key. The Secret is the agent's, and the
				// sandbox has no business seeing the private half
				// if it ever ends up stored alongside.
				Items: []corev1.KeyToPath{{Key: "authorized_keys", Path: "authorized_keys"}},
			},
		},
	}, {
		Name: shellSandboxSettingsVolume,
		VolumeSource: corev1.VolumeSource{
			ConfigMap: &corev1.ConfigMapVolumeSource{
				LocalObjectReference: corev1.LocalObjectReference{
					Name: settingsConfigMapName(agent),
				},
				// Optional, unlike the agent container's copy. The
				// reconciler writes this ConfigMap before it builds
				// the StatefulSet, but the two are separate objects
				// and a sandbox that cannot start because one of them
				// is briefly missing takes the agent's whole shell
				// with it. A skill reading an absent SETTINGS.md
				// fails on its own terms.
				Optional: ptr.To(true),
			},
		},
	}}
	if credentialProxyColocated(agent) {
		volumes = append(volumes, buildCredentialProxyRuntimeVolumes(agent)...)
	}
	return volumes
}

func buildShellSandboxContainer(agent *agentv1alpha1.PlatformAgent, env []corev1.EnvVar) corev1.Container {
	return corev1.Container{
		Name:  "shell",
		Image: resolveShellSandboxImage(agent),
		// No command or args: the image's entrypoint does the
		// volume-dependent setup and execs sshd. An earlier prototype
		// carried all of it as a heredoc in the pod spec, where no
		// linter or test could reach it.
		Ports: []corev1.ContainerPort{{
			Name:          "ssh",
			ContainerPort: shellSandboxPort,
		}},
		Env: env,
		ReadinessProbe: &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{
				TCPSocket: &corev1.TCPSocketAction{Port: intstr.FromInt32(shellSandboxPort)},
			},
			InitialDelaySeconds: 5,
			PeriodSeconds:       5,
		},
		// Requests and limits on every container, always: the
		// platform-baseline-quota in kubeagents-system rejects a pod
		// that omits them, and the rejection surfaces as a StatefulSet
		// that never creates a pod.
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("200m"),
				corev1.ResourceMemory: resource.MustParse("512Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("2"),
				corev1.ResourceMemory: resource.MustParse("2Gi"),
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			{Name: shellSandboxKeysVolume, MountPath: shellSandboxKeysPath, ReadOnly: true},
			{Name: shellSandboxDataVolume, MountPath: shellSandboxDataPath},
			{Name: shellSandboxSshdVolume, MountPath: shellSandboxSshdPath},
			{
				// The one file in the delivery set the image cannot
				// carry: SETTINGS.md is per-install, rendered by the
				// operator from the CR, and six skills read it by
				// path. The image stages skills, SOPs and shared
				// scripts at /opt/defaults for the entrypoint to sync
				// (deploy/sandbox/Dockerfile); this arrives the way
				// the agent container gets the same file, as a subPath
				// mount over its own data volume.
				//
				// subPath, so the ConfigMap lands as a single file
				// rather than replacing the directory. The cost is
				// that it does not track ConfigMap updates — a
				// subPath mount is resolved once at container start —
				// which matches the agent container's behaviour, where
				// a settings change already means a restart.
				Name:      shellSandboxSettingsVolume,
				MountPath: path.Join(shellSandboxDataPath, settingsFileName),
				SubPath:   settingsFileName,
				ReadOnly:  true,
			},
		},
	}
}

// buildShellSandboxNetworkPolicy is deny-by-default in both directions, with three
// holes.
//
// Agent Sandbox ships an equivalent as its GKE default; not taking the CRD means
// writing it, and this is the one part of that reversal that is real work rather
// than a rename. Note that it is inert on any cluster without a NetworkPolicy
// implementation — the reference install has none — so it is a control on clusters
// that enforce it and documentation everywhere else.
//
// Co-location changes both directions, and the egress change is the cost of the
// design rather than a detail. A NetworkPolicy selects pods, so every rule the
// credential proxy needs to reach a GKE control plane, the Google Chat and Slack
// APIs, GitHub and the token broker is a rule the *shell* container gets too:
// putting the two in one pod hands the sandbox the proxy's outbound reach. What
// the design buys is not a smaller network — it is that nothing reachable over
// that network will answer the shell, because the shell holds no credential. The
// metadata server is the one exception worth naming, and it answers the whole pod
// with an unbound principal (see shellSandboxServiceAccountName).
func buildShellSandboxNetworkPolicy(agent *agentv1alpha1.PlatformAgent) *networkingv1.NetworkPolicy {
	tcp := corev1.ProtocolTCP
	udp := corev1.ProtocolUDP
	gateway := map[string]string{"app": agent.Name + "-gateway"}
	colocated := credentialProxyColocated(agent)

	ingress := []networkingv1.NetworkPolicyIngressRule{{
		// Only the agent pod may open a shell, and only on sshd's port.
		From: []networkingv1.NetworkPolicyPeer{{
			PodSelector: &metav1.LabelSelector{MatchLabels: gateway},
		}},
		Ports: []networkingv1.NetworkPolicyPort{{
			Protocol: &tcp,
			Port:     ptr.To(intstr.FromInt32(shellSandboxPort)),
		}},
	}}

	egress := []networkingv1.NetworkPolicyEgressRule{{
		// Cluster DNS. Without it the sandbox cannot resolve the credential
		// proxy, and every wrapper fails with a name error that looks like the
		// proxy being down.
		To: []networkingv1.NetworkPolicyPeer{{
			NamespaceSelector: &metav1.LabelSelector{
				MatchLabels: map[string]string{"kubernetes.io/metadata.name": "kube-system"},
			},
			PodSelector: &metav1.LabelSelector{
				MatchLabels: map[string]string{"k8s-app": "kube-dns"},
			},
		}},
		Ports: []networkingv1.NetworkPolicyPort{
			{Protocol: &udp, Port: ptr.To(intstr.FromInt32(53))},
			{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(53))},
		},
	}}

	if colocated {
		// The gateway's chat relay clients, which now dial a process in this
		// pod. Loopback needs no rule — the shell reaches the proxy at
		// 127.0.0.1 and a NetworkPolicy never sees that packet — so this hole
		// exists for the one caller that is genuinely remote.
		ingress = append(ingress, networkingv1.NetworkPolicyIngressRule{
			From: []networkingv1.NetworkPolicyPeer{{
				PodSelector: &metav1.LabelSelector{MatchLabels: gateway},
			}},
			Ports: []networkingv1.NetworkPolicyPort{{
				Protocol: &tcp,
				Port:     ptr.To(intstr.FromInt32(credentialProxyPort)),
			}},
		})
		// Everything the proxy talks to. Left as a single 443 rule rather than
		// an address list because the set is open-ended — a GKE control plane
		// per registered cluster, googleapis.com, chat.googleapis.com,
		// slack.com, github.com, and whatever a skill reaches next — and an
		// enumeration that goes stale fails closed in a way that looks like the
		// agent being broken. 10.0.0.0/8 and friends are excluded so this does
		// not become a lateral-movement rule for the rest of the cluster; the
		// private ranges the proxy does need are the two below it.
		egress = append(egress,
			networkingv1.NetworkPolicyEgressRule{
				To: []networkingv1.NetworkPolicyPeer{{
					IPBlock: &networkingv1.IPBlock{
						CIDR: "0.0.0.0/0",
						Except: []string{
							"10.0.0.0/8",
							"172.16.0.0/12",
							"192.168.0.0/16",
							// The metadata server. The pod's identity is
							// unbound so it answers nothing useful, but the
							// proxy has no reason to ask and the shell must
							// not learn to.
							"169.254.169.254/32",
						},
					},
				}},
				Ports: []networkingv1.NetworkPolicyPort{{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(443))}},
			},
			networkingv1.NetworkPolicyEgressRule{
				// github-token-minter, in this namespace, on 8080. It is what
				// mints the installation token every `gh` and `git` call uses,
				// and it admits this pod by the
				// kubeagents.x-k8s.io/has-credential-proxy label the template
				// carries when co-located.
				To: []networkingv1.NetworkPolicyPeer{{
					PodSelector: &metav1.LabelSelector{
						MatchLabels: map[string]string{"app": "github-token-minter"},
					},
				}},
				Ports: []networkingv1.NetworkPolicyPort{{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(8080))}},
			},
			networkingv1.NetworkPolicyEgressRule{
				// The Kubernetes API. `kubectl` against the local cluster goes
				// to a ClusterIP in the default namespace, which the 0.0.0.0/0
				// rule above deliberately excludes.
				To: []networkingv1.NetworkPolicyPeer{{
					IPBlock: &networkingv1.IPBlock{CIDR: "0.0.0.0/0"},
				}},
				Ports: []networkingv1.NetworkPolicyPort{{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(6443))}},
			},
		)
	} else {
		egress = append(egress, networkingv1.NetworkPolicyEgressRule{
			// The credential proxy in a pod of its own. This is the connection
			// every wrapped CLI in the sandbox makes.
			To: []networkingv1.NetworkPolicyPeer{{
				PodSelector: &metav1.LabelSelector{MatchLabels: credentialProxySelector(agent)},
			}},
			Ports: []networkingv1.NetworkPolicyPort{{
				Protocol: &tcp,
				Port:     ptr.To(intstr.FromInt32(credentialProxyPort)),
			}},
		})
	}

	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      shellSandboxName(agent),
			Namespace: agent.Namespace,
			Labels:    shellSandboxSelector(agent),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: shellSandboxSelector(agent)},
			// Both types listed even though each has rules below: naming a type
			// with no rule is what makes it deny-all, and a later edit that
			// removes the last egress rule must not silently open egress.
			PolicyTypes: []networkingv1.PolicyType{
				networkingv1.PolicyTypeIngress,
				networkingv1.PolicyTypeEgress,
			},
			Ingress: ingress,
			Egress:  egress,
		},
	}
}

// buildShellSandboxClientKeyVolumes returns the agent pod's half of the keypair:
// the Secret holding the private key, and an emptyDir the init container below
// copies it into.
//
// Two volumes because one does not work, and the reason is worth stating rather
// than rediscovering. `ssh -i` refuses a private key with any group or other
// permission bit set, and a Secret volume's files are owned by root — the agent
// pod runs as uid 10000 under runAsNonRoot. That leaves no mode that satisfies
// both: 0400 is unreadable by the agent, and 0440 is refused by ssh. Every
// combination fails at connection time with a message about permissions, which
// reads like a bad key and sends the reader to the sandbox.
//
// So the Secret is mounted world-readable *within this pod* — which changes
// nothing, since the pod is the key's legitimate holder — and copied to an
// emptyDir where the copy is owned by the uid that made it.
func buildShellSandboxClientKeyVolumes() []corev1.Volume {
	return []corev1.Volume{
		{
			Name: shellSandboxClientKeySecretVolume,
			VolumeSource: corev1.VolumeSource{
				Secret: &corev1.SecretVolumeSource{
					SecretName: defaultPlatformAgentSecrets,
					Items: []corev1.KeyToPath{{
						Key:  shellSandboxPrivateKeySecretKey,
						Path: shellSandboxClientKeyFile,
					}},
					DefaultMode: ptr.To(int32(0444)),
					// Optional so that an install predating the keypair keeps
					// starting: it gets an empty directory, the init container
					// says so, and the agent runs with local tools as before.
					// A required mount would hold the whole pod in
					// CreateContainerConfigError over a dormant feature.
					Optional: ptr.To(true),
				},
			},
		},
		{
			Name:         shellSandboxClientKeyVolume,
			VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		},
	}
}

// buildShellSandboxClientKeyInitContainer copies the private key into place with
// the ownership and mode ssh insists on. See buildShellSandboxClientKeyVolumes
// for why a plain Secret mount cannot do this.
//
// It runs as the pod's uid, so `install` produces a file owned by the account
// that will read it. Missing key is not an error: the container logs and exits 0,
// leaving an empty directory behind, because the sandbox is opt-in and an install
// that has not provisioned a keypair is not broken.
func buildShellSandboxClientKeyInitContainer(image string) corev1.Container {
	return corev1.Container{
		Name:            "sandbox-ssh-key",
		Image:           image,
		ImagePullPolicy: corev1.PullIfNotPresent,
		Command:         []string{"/bin/sh", "-c"},
		Args: []string{fmt.Sprintf(
			`set -eu
if [ -r %[1]s/%[3]s ]; then
  install -m 0600 %[1]s/%[3]s %[2]s/%[3]s
  echo "sandbox ssh key staged at %[2]s/%[3]s"
else
  echo "no %[4]s in the agent credentials Secret; the shell sandbox will be unreachable"
fi`,
			shellSandboxClientKeySecretPath,
			shellSandboxClientKeyPath,
			shellSandboxClientKeyFile,
			shellSandboxPrivateKeySecretKey,
		)},
		VolumeMounts: []corev1.VolumeMount{
			{Name: shellSandboxClientKeySecretVolume, MountPath: shellSandboxClientKeySecretPath, ReadOnly: true},
			{Name: shellSandboxClientKeyVolume, MountPath: shellSandboxClientKeyPath},
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("10m"),
				corev1.ResourceMemory: resource.MustParse("16Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("100m"),
				corev1.ResourceMemory: resource.MustParse("64Mi"),
			},
		},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			ReadOnlyRootFilesystem:   ptr.To(true),
			Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
	}
}

// buildShellSandboxClientKeyMount is the read-only view of the staged key that
// the agent container gets. Only the emptyDir: the container that talks to the
// sandbox has no reason to see the Secret mount the init container read.
func buildShellSandboxClientKeyMount() corev1.VolumeMount {
	return corev1.VolumeMount{
		Name:      shellSandboxClientKeyVolume,
		MountPath: shellSandboxClientKeyPath,
		ReadOnly:  true,
	}
}
