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
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// These pin the properties the shell sandbox exists to have, in the order the
// design doc argues for them. They are not coverage for the builders' plumbing:
// a StatefulSet whose replica count or image is wrong announces itself, while a
// StatefulSet that mounts a ServiceAccount token or throws its host keys away on
// a scale-down works perfectly right up until it matters.

func shellSandboxTestAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
}

func TestShellSandboxStatefulSetHasNoKubernetesCredential(t *testing.T) {
	sts := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "", "settings-hash", "policy-hash")
	pod := sts.Spec.Template.Spec

	if pod.AutomountServiceAccountToken == nil || *pod.AutomountServiceAccountToken {
		t.Error("the sandbox must not mount a ServiceAccount token: it is the boundary this workload exists to draw")
	}
	// Its own ServiceAccount, never the agent's. The agent's carries
	// iam.gke.io/gcp-service-account, and Workload Identity resolves by pod IP —
	// so borrowing it would hand the shell container a full GSA token from
	// 169.254.169.254 whatever this pod does about token projection.
	agent := shellSandboxTestAgent()
	if pod.ServiceAccountName != shellSandboxName(agent) {
		t.Errorf("the sandbox must run under its own ServiceAccount %q, got %q",
			shellSandboxName(agent), pod.ServiceAccountName)
	}
	if pod.ShareProcessNamespace == nil || *pod.ShareProcessNamespace {
		t.Error("shareProcessNamespace must be explicitly false: /proc/<pid>/{environ,root} routes around the mount namespace that separates the shell from the credential proxy")
	}
	sa := buildShellSandboxServiceAccount(agent)
	if len(sa.Annotations) != 0 {
		t.Errorf("the sandbox ServiceAccount must carry no annotations — iam.gke.io/gcp-service-account there undoes the whole design — got %#v", sa.Annotations)
	}
	if pod.EnableServiceLinks == nil || *pod.EnableServiceLinks {
		t.Error("the sandbox must not get service-link env vars: they hand it a map of the namespace it has no use for")
	}
	// The whole list, by name, rather than a count: every volume here is a way to
	// put bytes into the pod the agent can run arbitrary commands in, so adding one
	// should be a decision someone makes on purpose. Exactly two are allowed — the
	// authorized-keys Secret and the SETTINGS.md ConfigMap — and neither carries a
	// credential. Anything else fails here and gets argued about in review.
	allowed := map[string]bool{
		shellSandboxKeysVolume:     true,
		shellSandboxSettingsVolume: true,
	}
	byName := map[string]corev1.Volume{}
	for _, v := range pod.Volumes {
		if !allowed[v.Name] {
			t.Errorf("unexpected volume %q in the sandbox pod: %#v", v.Name, v.VolumeSource)
		}
		byName[v.Name] = v
	}
	keys, ok := byName[shellSandboxKeysVolume]
	if !ok {
		t.Fatalf("expected the %q volume, got %#v", shellSandboxKeysVolume, pod.Volumes)
	}
	// One Secret, one key from it, and it is a public key.
	secret := keys.Secret
	if secret == nil {
		t.Fatalf("expected the authorized-keys Secret volume, got %#v", keys.VolumeSource)
	}
	if len(secret.Items) != 1 || secret.Items[0].Key != "authorized_keys" {
		t.Errorf("expected only the authorized_keys item from the Secret, got %#v", secret.Items)
	}
	// The other one is a ConfigMap, which is the part that matters: a Secret named
	// here would be a credential arriving by the same route.
	if settings, ok := byName[shellSandboxSettingsVolume]; ok && settings.ConfigMap == nil {
		t.Errorf("expected %q to be a ConfigMap, got %#v", shellSandboxSettingsVolume, settings.VolumeSource)
	}
}

func TestShellSandboxRetainsItsVolumesOnDeleteAndScale(t *testing.T) {
	// Hermes connects with StrictHostKeyChecking=accept-new and the host keys
	// live on this volume, so a reclaimed claim is not a lost cache — it is every
	// subsequent command failing until known_hosts is edited by hand.
	sts := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "", "settings-hash", "policy-hash")
	policy := sts.Spec.PersistentVolumeClaimRetentionPolicy
	if policy == nil {
		t.Fatal("expected an explicit PersistentVolumeClaimRetentionPolicy; the default is Retain today and is not guaranteed to stay so")
	}
	if policy.WhenDeleted != appsv1.RetainPersistentVolumeClaimRetentionPolicyType {
		t.Errorf("expected WhenDeleted=Retain, got %s", policy.WhenDeleted)
	}
	if policy.WhenScaled != appsv1.RetainPersistentVolumeClaimRetentionPolicyType {
		t.Errorf("expected WhenScaled=Retain, got %s", policy.WhenScaled)
	}
	claims := map[string]bool{}
	for _, c := range sts.Spec.VolumeClaimTemplates {
		claims[c.Name] = true
	}
	// Two, and the split is the point: the host keys must not sit on the volume
	// whose mount point uid 1000 owns. See shellSandboxSshdPath.
	if len(claims) != 2 || !claims[shellSandboxDataVolume] || !claims[shellSandboxSshdVolume] {
		t.Fatalf("expected %q and %q volumeClaimTemplates, got %#v",
			shellSandboxDataVolume, shellSandboxSshdVolume, sts.Spec.VolumeClaimTemplates)
	}
}

func TestShellSandboxMountsMatchTheImage(t *testing.T) {
	// deploy/sandbox/entrypoint.sh reads both paths and exits if either is wrong.
	// The failure is loud, but it is loud in a pod's logs rather than in CI.
	sts := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "", "settings-hash", "policy-hash")
	containers := sts.Spec.Template.Spec.Containers
	if len(containers) != 1 {
		t.Fatalf("expected a single container, got %d", len(containers))
	}
	mounts := map[string]corev1.VolumeMount{}
	for _, m := range containers[0].VolumeMounts {
		mounts[m.Name] = m
	}
	if got := mounts[shellSandboxKeysVolume]; got.MountPath != shellSandboxKeysPath || !got.ReadOnly {
		t.Errorf("expected %s mounted read-only at %s, got %#v", shellSandboxKeysVolume, shellSandboxKeysPath, got)
	}
	if got := mounts[shellSandboxDataVolume]; got.MountPath != shellSandboxDataPath {
		t.Errorf("expected %s mounted at %s, got %#v", shellSandboxDataVolume, shellSandboxDataPath, got)
	}
	if got := mounts[shellSandboxSshdVolume]; got.MountPath != shellSandboxSshdPath {
		t.Errorf("expected %s mounted at %s, got %#v", shellSandboxSshdVolume, shellSandboxSshdPath, got)
	}
	// A regression guard with a security consequence rather than a cosmetic one:
	// nested under the data path, the host keys are back on a volume the model
	// can rename entries in, and the pinned host key stops meaning anything.
	if strings.HasPrefix(shellSandboxSshdPath, shellSandboxDataPath+"/") {
		t.Errorf("the sshd state path %s is inside the model's data path %s", shellSandboxSshdPath, shellSandboxDataPath)
	}
	if containers[0].Command != nil || containers[0].Args != nil {
		t.Error("the image's entrypoint owns startup; a command or args here bypasses the volume-dependent setup")
	}
	// The baseline quota in kubeagents-system rejects a pod that omits either,
	// and the rejection surfaces as a StatefulSet that never creates a pod.
	if containers[0].Resources.Requests == nil || containers[0].Resources.Limits == nil {
		t.Error("expected both resource requests and limits")
	}
}

func TestShellSandboxGetsTheSameSettingsFileAsTheAgent(t *testing.T) {
	// Six skills read SETTINGS.md by path, and reading a file is a shell tool now.
	// The image cannot carry it — the content is per-install, rendered from the CR
	// — so it is the one part of the delivery set that arrives as a mount. Everything
	// else is baked at /opt/defaults and synced by deploy/sandbox/entrypoint.sh.
	agent := shellSandboxTestAgent()
	sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash", "policy-hash")

	var mount *corev1.VolumeMount
	for i, m := range sts.Spec.Template.Spec.Containers[0].VolumeMounts {
		if m.Name == shellSandboxSettingsVolume {
			mount = &sts.Spec.Template.Spec.Containers[0].VolumeMounts[i]
		}
	}
	if mount == nil {
		t.Fatalf("expected a %s mount on the sandbox container", shellSandboxSettingsVolume)
	}
	// The path the skills name, and subPath so the ConfigMap lands as one file
	// rather than replacing the data volume's whole directory.
	if want := shellSandboxDataPath + "/" + settingsFileName; mount.MountPath != want {
		t.Errorf("expected SETTINGS.md at %s, got %s", want, mount.MountPath)
	}
	if mount.SubPath != settingsFileName {
		t.Errorf("expected subPath %s, got %q — a directory mount here hides the synced tree", settingsFileName, mount.SubPath)
	}
	if !mount.ReadOnly {
		t.Error("expected the settings mount to be read-only")
	}

	var vol *corev1.Volume
	for i, v := range sts.Spec.Template.Spec.Volumes {
		if v.Name == shellSandboxSettingsVolume {
			vol = &sts.Spec.Template.Spec.Volumes[i]
		}
	}
	if vol == nil || vol.ConfigMap == nil {
		t.Fatalf("expected a ConfigMap volume named %s, got %#v", shellSandboxSettingsVolume, vol)
	}
	// The same object the agent container mounts, so the two sides cannot disagree
	// about what the install's scope is.
	if vol.ConfigMap.Name != settingsConfigMapName(agent) {
		t.Errorf("expected the agent's settings ConfigMap %q, got %q", settingsConfigMapName(agent), vol.ConfigMap.Name)
	}
	if vol.ConfigMap.Name != buildSettingsConfigMap(agent).Name {
		t.Errorf("the sandbox mounts %q but the reconciler writes %q", vol.ConfigMap.Name, buildSettingsConfigMap(agent).Name)
	}
	// Optional, unlike the agent container's copy. The reconciler writes the
	// ConfigMap before the StatefulSet, but they are separate objects: a sandbox
	// that will not start because one is briefly missing takes the whole shell down,
	// while a skill reading an absent SETTINGS.md fails on its own terms.
	if vol.ConfigMap.Optional == nil || !*vol.ConfigMap.Optional {
		t.Error("expected the settings ConfigMap to be optional for the sandbox")
	}
}

func TestShellSandboxRollsWhenSettingsChange(t *testing.T) {
	// A subPath mount is resolved once at pod start, so a ConfigMap edit alone does
	// not reach a running sandbox. The agent's Deployment carries the same hash
	// annotation for the same reason; without it here, editing the CR's scope rolls
	// the agent onto the new SETTINGS.md and leaves the sandbox — where the shell
	// actually reads it — serving the old one indefinitely.
	agent := shellSandboxTestAgent()
	const key = "kubeagents.x-k8s.io/settings-config-hash"

	first := buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "hash-one", "policy-hash")
	if got := first.Spec.Template.Annotations[key]; got != "hash-one" {
		t.Fatalf("expected %s=hash-one on the pod template, got %q", key, got)
	}
	second := buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "hash-two", "policy-hash")
	if first.Spec.Template.Annotations[key] == second.Spec.Template.Annotations[key] {
		t.Error("a different settings hash must change the pod template, or nothing restarts")
	}
}

func TestShellSandboxCredentialProxyURLIsOptional(t *testing.T) {
	// Empty is the state until #737 Part C, and it has to be a working state: the
	// entrypoint warns and starts, so file and code-execution tools function while
	// the credentialed wrappers report that they are unconfigured.
	withoutURL := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "", "settings-hash", "policy-hash")
	for _, env := range withoutURL.Spec.Template.Spec.Containers[0].Env {
		if env.Name == "CREDENTIAL_PROXY_URL" {
			t.Errorf("expected no CREDENTIAL_PROXY_URL when none was resolved, got %q", env.Value)
		}
	}

	withURL := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "http://test-agent-credential-proxy:8765", "settings-hash", "policy-hash")
	var found string
	for _, env := range withURL.Spec.Template.Spec.Containers[0].Env {
		if env.Name == "CREDENTIAL_PROXY_URL" {
			found = env.Value
		}
	}
	if found != "http://test-agent-credential-proxy:8765" {
		t.Errorf("expected the resolved credential proxy URL in the pod env, got %q", found)
	}
}

func TestShellSandboxServiceIsHeadlessAndPublishesTheStableName(t *testing.T) {
	agent := shellSandboxTestAgent()
	svc := buildShellSandboxService(agent)

	if svc.Spec.ClusterIP != corev1.ClusterIPNone {
		t.Errorf("the governing Service must be headless or the per-pod DNS record does not exist, got %q", svc.Spec.ClusterIP)
	}
	if !svc.Spec.PublishNotReadyAddresses {
		t.Error("expected PublishNotReadyAddresses: the pod is addressable while sshd generates host keys on a first start")
	}
	if svc.Name != buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash", "policy-hash").Spec.ServiceName {
		t.Errorf("the StatefulSet's serviceName must be this Service, got %q vs %q",
			buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash", "policy-hash").Spec.ServiceName, svc.Name)
	}
	// The host Hermes dials has to be resolvable by this Service, which means
	// <pod>.<service>.<namespace>.svc and nothing else.
	host := shellSandboxHost(agent)
	if want := "test-agent-shell-0.test-agent-shell.test-ns.svc.cluster.local"; host != want {
		t.Errorf("expected %q, got %q", want, host)
	}
	if !strings.Contains(host, "."+svc.Name+".") {
		t.Errorf("host %q does not route through Service %q", host, svc.Name)
	}
}

func TestShellSandboxNetworkPolicyDeniesByDefault(t *testing.T) {
	np := buildShellSandboxNetworkPolicy(shellSandboxTestAgent())

	types := map[networkingv1.PolicyType]bool{}
	for _, t := range np.Spec.PolicyTypes {
		types[t] = true
	}
	if !types[networkingv1.PolicyTypeIngress] || !types[networkingv1.PolicyTypeEgress] {
		t.Fatalf("both policy types must be named or the unnamed direction is unrestricted, got %v", np.Spec.PolicyTypes)
	}

	// Ingress: the agent pod, on sshd's port, and nothing else. A rule with an
	// empty From or empty Ports is an open door that looks like a closed one.
	if len(np.Spec.Ingress) != 1 {
		t.Fatalf("expected exactly one ingress rule, got %d", len(np.Spec.Ingress))
	}
	in := np.Spec.Ingress[0]
	if len(in.From) != 1 || in.From[0].PodSelector == nil ||
		in.From[0].PodSelector.MatchLabels["app"] != "test-agent-gateway" {
		t.Errorf("expected ingress only from the gateway pod, got %#v", in.From)
	}
	if len(in.Ports) != 1 || in.Ports[0].Port.IntValue() != shellSandboxPort {
		t.Errorf("expected ingress only on %d, got %#v", shellSandboxPort, in.Ports)
	}

	// Egress: DNS and the credential proxy. Anything else reachable from here is
	// a path out of the sandbox that the incident this design answers used.
	if len(np.Spec.Egress) != 2 {
		t.Fatalf("expected exactly two egress rules (DNS, credential proxy), got %d", len(np.Spec.Egress))
	}
	for i, rule := range np.Spec.Egress {
		if len(rule.To) == 0 {
			t.Errorf("egress rule %d has no peers, which permits egress to everywhere", i)
		}
		if len(rule.Ports) == 0 {
			t.Errorf("egress rule %d has no ports, which permits every port on its peers", i)
		}
	}
	proxy := np.Spec.Egress[1]
	if proxy.Ports[0].Port.IntValue() != credentialProxyPort {
		t.Errorf("expected the credential proxy port %d, got %#v", credentialProxyPort, proxy.Ports[0].Port)
	}
}

func TestShellSandboxObjectsShareOneSelector(t *testing.T) {
	// Three objects, one label set. A Service that selects nothing and a
	// NetworkPolicy that constrains nothing both look healthy in `kubectl get`.
	agent := shellSandboxTestAgent()
	sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash", "policy-hash")
	svc := buildShellSandboxService(agent)
	np := buildShellSandboxNetworkPolicy(agent)

	podLabels := sts.Spec.Template.ObjectMeta.Labels
	for name, selector := range map[string]map[string]string{
		"StatefulSet.spec.selector": sts.Spec.Selector.MatchLabels,
		"Service.spec.selector":     svc.Spec.Selector,
		"NetworkPolicy.podSelector": np.Spec.PodSelector.MatchLabels,
	} {
		for k, v := range selector {
			if podLabels[k] != v {
				t.Errorf("%s wants %s=%s, which the pod template does not carry (%v)", name, k, v, podLabels)
			}
		}
		if len(selector) == 0 {
			t.Errorf("%s is empty, which selects every pod in the namespace", name)
		}
	}
}

func TestResolveShellSandboxImageHonoursTheMirrorOverride(t *testing.T) {
	agent := shellSandboxTestAgent()

	t.Setenv(shellSandboxImageEnvVar, "registry.example.com/mirror/agent-sandbox:v1.2.3")
	if got := resolveShellSandboxImage(agent); got != "registry.example.com/mirror/agent-sandbox:v1.2.3" {
		t.Errorf("expected the %s override to win, got %q", shellSandboxImageEnvVar, got)
	}

	// A per-agent image beats the controller-wide one: the override exists for an
	// install mirroring every image, the CR field for one agent being moved.
	withImage := shellSandboxTestAgent()
	withImage.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{
			ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Image: "registry.example.com/team/agent-sandbox:dev"},
		},
	}
	if got := resolveShellSandboxImage(withImage); got != "registry.example.com/team/agent-sandbox:dev" {
		t.Errorf("expected the CR image to win over %s, got %q", shellSandboxImageEnvVar, got)
	}

	t.Setenv(shellSandboxImageEnvVar, "")
	// The default must track the agent's version, not float on :latest: the two
	// images are built from one commit by one workflow.
	got := resolveShellSandboxImage(agent)
	if !strings.HasSuffix(got, ":"+DefaultPlatformAgentVersion) {
		t.Errorf("expected the default sandbox image to carry the build version %q, got %q", DefaultPlatformAgentVersion, got)
	}
	if !strings.Contains(got, "/agent-sandbox:") {
		t.Errorf("expected the agent-sandbox repository from images.json, got %q", got)
	}
}

// The failure this guards against is silent: a Secret volume's files are
// root-owned, the agent pod runs as uid 10000, and `ssh -i` refuses any key with
// a group or other permission bit set. 0400 is unreadable and 0440 is refused, so
// the key has to be copied to a file the agent's own uid owns. If someone
// "simplifies" this to a single Secret mount it will fail at connection time with
// a permissions error that reads like a bad key.
func TestShellSandboxClientKeyIsStagedRatherThanMountedDirectly(t *testing.T) {
	volumes := buildShellSandboxClientKeyVolumes()
	if len(volumes) != 2 {
		t.Fatalf("expected a Secret volume and a writable staging volume, got %d", len(volumes))
	}

	var secretVol, stagingVol *corev1.Volume
	for i := range volumes {
		switch volumes[i].Name {
		case shellSandboxClientKeySecretVolume:
			secretVol = &volumes[i]
		case shellSandboxClientKeyVolume:
			stagingVol = &volumes[i]
		}
	}
	if secretVol == nil || stagingVol == nil {
		t.Fatalf("expected both %q and %q volumes, got %+v", shellSandboxClientKeySecretVolume, shellSandboxClientKeyVolume, volumes)
	}
	if stagingVol.EmptyDir == nil {
		t.Errorf("the staging volume must be writable, so the init container's copy is owned by the pod's uid")
	}
	if secretVol.Secret == nil {
		t.Fatalf("expected %q to be backed by a Secret", shellSandboxClientKeySecretVolume)
	}
	if secretVol.Secret.SecretName != defaultPlatformAgentSecrets {
		t.Errorf("expected the private key to come from %q, got %q", defaultPlatformAgentSecrets, secretVol.Secret.SecretName)
	}
	if secretVol.Secret.Optional == nil || !*secretVol.Secret.Optional {
		t.Errorf("the mount must be optional: an install predating the keypair has to keep starting")
	}
	if mode := secretVol.Secret.DefaultMode; mode == nil || *mode&0444 == 0 {
		t.Errorf("the Secret mount must be readable by the init container's non-root uid, got mode %v", mode)
	}

	// Only the private half. The public half sits in the same Secret and has no
	// business in the agent pod.
	if len(secretVol.Secret.Items) != 1 {
		t.Fatalf("expected exactly one projected item, got %+v", secretVol.Secret.Items)
	}
	if got := secretVol.Secret.Items[0].Key; got != shellSandboxPrivateKeySecretKey {
		t.Errorf("expected only %q to be projected, got %q", shellSandboxPrivateKeySecretKey, got)
	}
}

func TestShellSandboxClientKeyInitContainerStagesWithPrivateMode(t *testing.T) {
	init := buildShellSandboxClientKeyInitContainer("example.com/agent:v1")
	script := strings.Join(init.Args, "\n")

	// 0600 is the only mode ssh accepts; anything with a group bit is refused.
	if !strings.Contains(script, "install -m 0600") {
		t.Errorf("expected the key to be staged with mode 0600, got script:\n%s", script)
	}
	// A missing key must not crash-loop the agent pod over a feature that is off.
	if !strings.Contains(script, "if [ -r ") {
		t.Errorf("expected a missing key to be tolerated, got script:\n%s", script)
	}
	if !strings.Contains(script, shellSandboxClientKeyFilePath()) {
		t.Errorf("expected the staged path %q to match what the Hermes config will point at, got script:\n%s",
			shellSandboxClientKeyFilePath(), script)
	}

	var writable bool
	for _, m := range init.VolumeMounts {
		if m.Name == shellSandboxClientKeyVolume {
			writable = !m.ReadOnly
		}
		if m.Name == shellSandboxClientKeySecretVolume && !m.ReadOnly {
			t.Errorf("the Secret mount must be read-only in the init container")
		}
	}
	if !writable {
		t.Errorf("the init container needs to write to %q", shellSandboxClientKeyVolume)
	}
}

// The agent container sees the staged copy and not the Secret it came from.
func TestShellSandboxClientKeyMountHidesTheSecretFromTheAgent(t *testing.T) {
	mount := buildShellSandboxClientKeyMount()
	if mount.Name != shellSandboxClientKeyVolume {
		t.Errorf("expected the agent to mount the staged copy %q, got %q", shellSandboxClientKeyVolume, mount.Name)
	}
	if !mount.ReadOnly {
		t.Errorf("the agent only reads the key; the init container is what writes it")
	}
}

// The sandbox mounts a Secret that holds one public key and nothing else. Naming
// platform-agent-secrets here — even with an items selector — would put every
// model API key one edit away from the pod this design keeps credential-free.
func TestShellSandboxAuthorizedKeysSecretIsNotTheCredentialSecret(t *testing.T) {
	agent := shellSandboxTestAgent()
	name := shellSandboxAuthorizedKeysSecretName(agent)
	if name == defaultPlatformAgentSecrets {
		t.Fatalf("the sandbox must not mount the agent credential Secret")
	}
	if !strings.HasPrefix(name, shellSandboxName(agent)) {
		t.Errorf("expected the Secret to be named after the sandbox, got %q", name)
	}

	sts := buildShellSandboxStatefulSet(agent, name, "", "settings-hash", "policy-hash")
	for _, v := range sts.Spec.Template.Spec.Volumes {
		if v.Secret != nil && v.Secret.SecretName == defaultPlatformAgentSecrets {
			t.Errorf("the sandbox pod must not reference %q, found volume %q", defaultPlatformAgentSecrets, v.Name)
		}
	}
}

// shellSandboxAgent returns a test agent with the sandbox toggle set.
func shellSandboxAgent(enabled bool) *agentv1alpha1.PlatformAgent {
	agent := shellSandboxTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{
			ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(enabled)},
		},
	}
	return agent
}

// Absent means off. Every install that exists today says nothing about the
// sandbox, and each of these shapes is one of them — a nil check missed anywhere
// in the four-level path is a panic in the reconcile loop, not a default.
func TestShellSandboxIsOffUnlessAskedFor(t *testing.T) {
	off := map[string]*agentv1alpha1.PlatformAgent{
		"nil agent":           nil,
		"no harness":          shellSandboxTestAgent(),
		"no experimental":     {Spec: agentv1alpha1.PlatformAgentSpec{Harness: &agentv1alpha1.HarnessSpec{}}},
		"no sandbox block":    {Spec: agentv1alpha1.PlatformAgentSpec{Harness: &agentv1alpha1.HarnessSpec{Experimental: &agentv1alpha1.ExperimentalSpec{}}}},
		"sandbox without set": {Spec: agentv1alpha1.PlatformAgentSpec{Harness: &agentv1alpha1.HarnessSpec{Experimental: &agentv1alpha1.ExperimentalSpec{ShellSandbox: &agentv1alpha1.ShellSandboxSpec{}}}}},
		"explicitly false":    shellSandboxAgent(false),
	}
	for name, agent := range off {
		if shellSandboxEnabled(agent) {
			t.Errorf("%s must leave the shell local", name)
		}
	}
	if !shellSandboxEnabled(shellSandboxAgent(true)) {
		t.Error("an explicit true must turn the sandbox on")
	}
}

// The managed scope is what makes the backend something the agent cannot write its
// way out of. An agent that saves `backend: local` into its own config.yaml has not
// changed a preference, it has left the sandbox — so these keys have to be in the
// rendering that Hermes treats as immutable, and absent from it entirely when the
// feature is off so that no existing install sees a new key.
func TestManagedConfigCarriesTheTerminalBackendOnlyWhenSandboxed(t *testing.T) {
	if got := renderConfigYAML(shellSandboxAgent(false), nil); strings.Contains(got, "terminal:") {
		t.Errorf("the managed scope must say nothing about the terminal when the sandbox is off:\n%s", got)
	}

	agent := shellSandboxAgent(true)
	got := renderConfigYAML(agent, nil)
	for _, want := range []string{
		"backend: ssh",
		"ssh_host: " + shellSandboxHost(agent),
		"ssh_user: " + shellSandboxUser,
		fmt.Sprintf("ssh_port: %d", shellSandboxPort),
		"ssh_key: " + shellSandboxClientKeyFilePath(),
	} {
		if !strings.Contains(got, want) {
			t.Errorf("expected the managed terminal block to carry %q:\n%s", want, got)
		}
	}
	// cwd is the profile-shaped part of the block, and a leaf here REPLACES each
	// profile's own value rather than merging with it.
	if strings.Contains(got, "cwd:") {
		t.Errorf("the managed scope must not pin terminal.cwd:\n%s", got)
	}
}

// The builders were tested in isolation long before anything called them. This is
// the join: with the toggle on, the agent pod has to carry the init container, both
// volumes and the read-only mount, and with it off it must carry none of them —
// an install that has never heard of the sandbox should not grow a reference to a
// Secret key it does not have.
func TestAgentPodStagesTheClientKeyOnlyWhenSandboxed(t *testing.T) {
	has := func(pod corev1.PodSpec) (init, volume, staged, mount bool) {
		for _, c := range pod.InitContainers {
			if c.Name == "sandbox-ssh-key" {
				init = true
			}
		}
		for _, v := range pod.Volumes {
			switch v.Name {
			case shellSandboxClientKeySecretVolume:
				volume = true
			case shellSandboxClientKeyVolume:
				staged = true
			}
		}
		for _, c := range pod.Containers {
			for _, m := range c.VolumeMounts {
				if m.Name == shellSandboxClientKeyVolume {
					mount = m.ReadOnly && m.MountPath == shellSandboxClientKeyPath
				}
				// The Secret mount is the init container's alone: it is the
				// world-readable copy, and the agent container reads the staged one.
				if m.Name == shellSandboxClientKeySecretVolume {
					t.Errorf("container %q must not see the raw Secret volume", c.Name)
				}
			}
		}
		return
	}

	off := buildPodTemplateSpec(shellSandboxAgent(false), "", "", "", "", nil, renderOptions{})
	if init, volume, staged, mount := has(off.Spec); init || volume || staged || mount {
		t.Errorf("an agent with the sandbox off must carry no key staging (init=%v secret=%v staged=%v mount=%v)", init, volume, staged, mount)
	}

	on := buildPodTemplateSpec(shellSandboxAgent(true), "", "", "", "", nil, renderOptions{})
	init, volume, staged, mount := has(on.Spec)
	if !init {
		t.Error("expected the sandbox-ssh-key init container")
	}
	if !volume || !staged {
		t.Errorf("expected both key volumes, got secret=%v staged=%v", volume, staged)
	}
	if !mount {
		t.Errorf("expected the staged key mounted read-only at %s in the agent container", shellSandboxClientKeyPath)
	}
}

// The Hermes base image ships HERMES_WRITE_SAFE_ROOT=/opt/data. Left alone with the
// sandbox on, agent/file_safety.py refuses every sandbox path and permits only one
// that does not exist there, so write_file and patch fail for everything — observed
// on a live install before this was added.
func TestSandboxRepointsTheWriteSafeRoot(t *testing.T) {
	safeRoot := func(pod corev1.PodSpec) (string, bool) {
		for _, c := range pod.Containers {
			if c.Name != "platform-agent" {
				continue
			}
			for _, e := range c.Env {
				if e.Name == "HERMES_WRITE_SAFE_ROOT" {
					return e.Value, true
				}
			}
		}
		return "", false
	}

	// Off, the operator says nothing and the image's own default stands.
	off := buildPodTemplateSpec(shellSandboxAgent(false), "", "", "", "", nil, renderOptions{})
	if got, found := safeRoot(off.Spec); found {
		t.Errorf("an agent with the sandbox off must not override the write safe root, got %q", got)
	}

	on := buildPodTemplateSpec(shellSandboxAgent(true), "", "", "", "", nil, renderOptions{})
	got, found := safeRoot(on.Spec)
	if !found {
		t.Fatal("expected HERMES_WRITE_SAFE_ROOT on the sandboxed agent container")
	}
	want := shellSandboxDataPath + ":" + shellSandboxHomePath
	if got != want {
		t.Errorf("write safe root = %q, want %q", got, want)
	}
	// The sandbox's data volume carries the agent pod's /opt/data path on purpose,
	// so the old check — that the safe root no longer names /opt/data — no longer
	// distinguishes anything. What still has to hold is that every entry resolves
	// inside the sandbox: file_safety.py compares the prefix in the agent process,
	// and a path that exists only in the agent pod would let write_file accept a
	// write the ssh backend then makes on the far side, or refuse one it should
	// allow.
	for _, p := range strings.Split(got, ":") {
		if p != shellSandboxDataPath && p != shellSandboxHomePath {
			t.Errorf("write safe root entry %q is not a sandbox path", p)
		}
	}
}

// TERMINAL_CWD is the difference between the model's work surviving a pod recycle
// and not. Hermes' ssh backend defaults cwd to `~` (tools/terminal_tool.py), which
// is the sandbox's ephemeral home, so without this every relative path the model
// wrote was on the container overlay while the volume beside it stayed empty —
// observed on a live install, 44K on a five-day-old PVC.
func TestSandboxPointsTheTerminalAtTheDataVolume(t *testing.T) {
	cwd := func(pod corev1.PodSpec) (string, bool) {
		for _, c := range pod.Containers {
			if c.Name != "platform-agent" {
				continue
			}
			for _, e := range c.Env {
				if e.Name == "TERMINAL_CWD" {
					return e.Value, true
				}
			}
		}
		return "", false
	}

	// Off, the shell is local and `~` is the agent's own durable home. Setting a
	// cwd there would change behaviour for installs that are not sandboxed at all.
	off := buildPodTemplateSpec(shellSandboxAgent(false), "", "", "", "", nil, renderOptions{})
	if got, found := cwd(off.Spec); found {
		t.Errorf("an agent with the sandbox off must not set TERMINAL_CWD, got %q", got)
	}

	on := buildPodTemplateSpec(shellSandboxAgent(true), "", "", "", "", nil, renderOptions{})
	got, found := cwd(on.Spec)
	if !found {
		t.Fatal("expected TERMINAL_CWD on the sandboxed agent container")
	}
	if got != shellSandboxDataPath {
		t.Errorf("TERMINAL_CWD = %q, want the sandbox data volume %q", got, shellSandboxDataPath)
	}
	// The home is the failure this exists to prevent, and it is a silent one: the
	// shell works, the files are written, and they are gone on the next restart.
	if got == shellSandboxHomePath {
		t.Error("TERMINAL_CWD points at the ephemeral home; model writes will not survive a restart")
	}
}

// The co-located placement: the credential proxy as a second container of this
// pod. These are the properties that make that safe rather than a regression,
// and each one is a single field away from being silently untrue.

func colocatedTestAgent() *agentv1alpha1.PlatformAgent {
	agent := shellSandboxTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{
			ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(true)},
		},
	}
	agent.Spec.Security = &agentv1alpha1.SecuritySpec{
		WorkloadIdentityFederation: &agentv1alpha1.WorkloadIdentityFederationSpec{
			Audience: "//iam.googleapis.com/projects/123456789012/locations/global/" +
				"workloadIdentityPools/kubeagents/providers/test-cluster",
			ServiceAccountEmail: "kubeagents-platform-gsa@example.iam.gserviceaccount.com",
		},
	}
	return agent
}

func TestCredentialProxyColocatesOnlyWithFederation(t *testing.T) {
	// Fail-safe in both directions. Co-locating without federation puts a
	// metadata-server identity in the shell's network namespace, which is
	// strictly worse than the separate pod it would replace; federation without
	// the sandbox has no pod to move into.
	sandboxOnly := shellSandboxTestAgent()
	sandboxOnly.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{
			ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(true)},
		},
	}
	if credentialProxyColocated(sandboxOnly) {
		t.Error("the sandbox alone must not co-locate the proxy: it would have a metadata-server identity")
	}

	federationOnly := colocatedTestAgent()
	federationOnly.Spec.Harness = nil
	if credentialProxyColocated(federationOnly) {
		t.Error("federation alone must not co-locate the proxy: there is no sandbox pod to move into")
	}

	// A half-filled block is absent, not a partial opt-in.
	half := colocatedTestAgent()
	half.Spec.Security.WorkloadIdentityFederation.ServiceAccountEmail = ""
	if credentialProxyColocated(half) {
		t.Error("a federation block with no impersonation target cannot produce a token and must not co-locate")
	}

	if !credentialProxyColocated(colocatedTestAgent()) {
		t.Error("expected co-location when the sandbox is on and federation is complete")
	}
}

func TestSandboxSharesOnlyTheDataVolumeWithTheProxy(t *testing.T) {
	// The two containers deliberately run as the same uid, so the mount
	// namespace is the entire boundary between the shell and the proxy's
	// kubeconfig, gcloud configuration and federated token. Every volume the
	// shell mounts is therefore a decision, and this is where it gets made.
	agent := colocatedTestAgent()
	sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", credentialProxySandboxURL(agent), "settings-hash", "policy-hash")
	pod := sts.Spec.Template.Spec

	if len(pod.Containers) != 2 {
		t.Fatalf("expected the shell and the credential proxy, got %d containers", len(pod.Containers))
	}
	shell, proxy := pod.Containers[0], pod.Containers[1]
	if shell.Name != "shell" || proxy.Name != "envoy-credential-proxy" {
		t.Fatalf("unexpected containers %q and %q", shell.Name, proxy.Name)
	}

	shellMounts := map[string]bool{}
	for _, m := range shell.VolumeMounts {
		shellMounts[m.Name] = true
	}
	for _, m := range proxy.VolumeMounts {
		if m.Name == shellSandboxDataVolume {
			continue
		}
		if shellMounts[m.Name] {
			t.Errorf("the shell mounts %q, which is the credential proxy's: the mount namespace is the only boundary these two containers have", m.Name)
		}
	}
	if !shellMounts[shellSandboxDataVolume] {
		t.Error("the shell must mount the data volume; it is the shared working tree that makes git work")
	}

	// The federated token by name, because it is the one file in the pod worth
	// stealing and an accidental mount of it is invisible at runtime.
	for _, m := range shell.VolumeMounts {
		if m.Name == credentialProxyWIFTokenVolume || m.MountPath == credentialProxyWIFTokenPath {
			t.Fatalf("the shell mounts the federated token at %q — the pod's whole cloud identity", m.MountPath)
		}
	}
}

func TestColocatedProxyRunsAsTheSandboxLogin(t *testing.T) {
	// The entrypoint chowns the data volume to uid 1000, so a proxy running as
	// anything else cannot write the tree the agent works in, and files it does
	// create are unwritable from the shell.
	agent := colocatedTestAgent()
	proxy := buildCredentialProxyContainer(agent, true)
	sc := proxy.SecurityContext
	if sc == nil || sc.RunAsUser == nil || *sc.RunAsUser != int64(shellSandboxUID) {
		t.Fatalf("expected the proxy to run as uid %d, got %#v", shellSandboxUID, sc)
	}
	if sc.RunAsNonRoot == nil || !*sc.RunAsNonRoot {
		t.Error("expected runAsNonRoot on the co-located proxy")
	}

	var root string
	federation := map[string]string{}
	for _, e := range proxy.Env {
		switch e.Name {
		case "CREDENTIAL_PROXY_WORKSPACE_ROOT":
			root = e.Value
		case "CREDENTIAL_PROXY_WIF_AUDIENCE", "CREDENTIAL_PROXY_WIF_SERVICE_ACCOUNT",
			"CREDENTIAL_PROXY_WIF_TOKEN_FILE", "GOOGLE_APPLICATION_CREDENTIALS",
			"CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE":
			federation[e.Name] = e.Value
		}
	}
	if root != shellSandboxDataPath {
		t.Errorf("the proxy's workspace root must be the shared tree %q, got %q", shellSandboxDataPath, root)
	}
	if len(federation) != 5 {
		t.Errorf("expected the full federation environment, got %#v", federation)
	}
	// gcloud keeps its own credential store and ignores ADC, so this one is what
	// stops `gcloud container clusters get-credentials` falling through to the
	// metadata server.
	if federation["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] != credentialProxyWIFCredentialFile {
		t.Errorf("gcloud must be pinned to the federated credential file, got %q",
			federation["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"])
	}
}

func TestColocatedTokenProjectionIsAudienceScoped(t *testing.T) {
	agent := colocatedTestAgent()
	volumes := buildCredentialProxyRuntimeVolumes(agent)

	var projected *corev1.Volume
	for i := range volumes {
		if volumes[i].Name == credentialProxyWIFTokenVolume {
			projected = &volumes[i]
		}
	}
	if projected == nil {
		t.Fatalf("expected the federated token volume, got %#v", volumes)
	}
	sources := projected.VolumeSource.Projected.Sources
	if len(sources) != 1 || sources[0].ServiceAccountToken == nil {
		t.Fatalf("expected exactly one ServiceAccountToken projection, got %#v", sources)
	}
	// Scoped to the federation provider, and not to github-token-minter's
	// audience: one token cannot satisfy both verifiers, and widening either
	// audience lets a token minted for one be replayed at the other.
	if got := sources[0].ServiceAccountToken.Audience; got != agent.Spec.Security.WorkloadIdentityFederation.Audience {
		t.Errorf("token audience = %q, want the federation provider", got)
	}

	// Standalone, the same call must not add a volume nothing mounts.
	standalone := colocatedTestAgent()
	standalone.Spec.Harness = nil
	for _, v := range buildCredentialProxyRuntimeVolumes(standalone) {
		if v.Name == credentialProxyWIFTokenVolume {
			t.Error("the federated token must not be projected into the standalone proxy pod: nothing mounts it")
		}
	}
}

func TestSandboxWrappersPostToLoopbackWhenColocated(t *testing.T) {
	// credential_proxy_client.shares_filesystem_with_proxy keys on the endpoint
	// being loopback, and only then forwards the caller's cwd. That forwarding is
	// the whole of git support — routing two containers of one pod through the
	// Service would leave them as strangers.
	if got := credentialProxySandboxURL(colocatedTestAgent()); got != "http://127.0.0.1:8765" {
		t.Errorf("co-located sandbox URL = %q, want loopback", got)
	}
	if got := credentialProxySandboxURL(shellSandboxTestAgent()); got != credentialProxyURL(shellSandboxTestAgent()) {
		t.Errorf("standalone sandbox URL = %q, want the proxy Service", got)
	}
	// The gateway's relay clients keep the Service in both placements; only its
	// selector moves.
	if got := buildCredentialProxyService(colocatedTestAgent()).Spec.Selector["app"]; got != "test-agent-shell" {
		t.Errorf("co-located, the proxy Service must select the sandbox pod, got %q", got)
	}
}

func TestColocatedSandboxAdmitsTheGatewayOnTheProxyPort(t *testing.T) {
	// The relay clients are the one caller that is genuinely remote: the shell
	// reaches the proxy on loopback, which no NetworkPolicy sees.
	np := buildShellSandboxNetworkPolicy(colocatedTestAgent())
	var proxyPort bool
	for _, rule := range np.Spec.Ingress {
		for _, p := range rule.Ports {
			if p.Port != nil && p.Port.IntVal == credentialProxyPort {
				proxyPort = true
			}
		}
	}
	if !proxyPort {
		t.Error("co-located, the sandbox must admit the gateway on the credential proxy port or the chat relays are unreachable")
	}

	// And the pod has to carry the label github-token-minter's own policy admits,
	// or `gh` and `git` lose their installation token.
	agent := colocatedTestAgent()
	sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", credentialProxySandboxURL(agent), "settings-hash", "policy-hash")
	if sts.Spec.Template.Labels["kubeagents.x-k8s.io/has-credential-proxy"] != "true" {
		t.Errorf("expected the has-credential-proxy pod label, got %#v", sts.Spec.Template.Labels)
	}
	// The selector is immutable, so the extra label must be on the template only.
	if _, present := sts.Spec.Selector.MatchLabels["kubeagents.x-k8s.io/has-credential-proxy"]; present {
		t.Error("the label must not reach spec.selector: a StatefulSet's selector is immutable and turning federation on would need the object deleted")
	}

	// ObjectMeta.Labels and Selector.MatchLabels are one map, and withCommonLabels
	// merges into it in place — so the selector the API server stores carries the
	// recommended labels whether or not the template does. A template narrower
	// than that selector is rejected outright: `selector` does not match template
	// `labels`, on every reconcile, with the StatefulSet left as it was.
	withCommonLabels(sts, agent)
	for k, v := range sts.Spec.Selector.MatchLabels {
		if sts.Spec.Template.Labels[k] != v {
			t.Errorf("pod template is missing selector label %s=%s (%#v); the API server rejects the StatefulSet", k, v, sts.Spec.Template.Labels)
		}
	}
}
