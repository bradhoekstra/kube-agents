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
	sts := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "")
	pod := sts.Spec.Template.Spec

	if pod.AutomountServiceAccountToken == nil || *pod.AutomountServiceAccountToken {
		t.Error("the sandbox must not mount a ServiceAccount token: it is the boundary this workload exists to draw")
	}
	if pod.ServiceAccountName != "" {
		t.Errorf("the sandbox must not name a ServiceAccount, got %q", pod.ServiceAccountName)
	}
	if pod.EnableServiceLinks == nil || *pod.EnableServiceLinks {
		t.Error("the sandbox must not get service-link env vars: they hand it a map of the namespace it has no use for")
	}
	// One Secret, one key from it, and it is a public key. Anything else here is
	// a credential in the pod the agent can run arbitrary commands in.
	if len(pod.Volumes) != 1 {
		t.Fatalf("expected exactly one volume in the pod spec, got %d", len(pod.Volumes))
	}
	secret := pod.Volumes[0].Secret
	if secret == nil {
		t.Fatalf("expected the authorized-keys Secret volume, got %#v", pod.Volumes[0].VolumeSource)
	}
	if len(secret.Items) != 1 || secret.Items[0].Key != "authorized_keys" {
		t.Errorf("expected only the authorized_keys item from the Secret, got %#v", secret.Items)
	}
}

func TestShellSandboxRetainsItsWorkspaceOnDeleteAndScale(t *testing.T) {
	// Hermes connects with StrictHostKeyChecking=accept-new and the host keys
	// live on this volume, so a reclaimed claim is not a lost cache — it is every
	// subsequent command failing until known_hosts is edited by hand.
	sts := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "")
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
	if len(sts.Spec.VolumeClaimTemplates) != 1 || sts.Spec.VolumeClaimTemplates[0].Name != shellSandboxWorkspaceVolume {
		t.Fatalf("expected a single %q volumeClaimTemplate, got %#v", shellSandboxWorkspaceVolume, sts.Spec.VolumeClaimTemplates)
	}
}

func TestShellSandboxMountsMatchTheImage(t *testing.T) {
	// deploy/sandbox/entrypoint.sh reads both paths and exits if either is wrong.
	// The failure is loud, but it is loud in a pod's logs rather than in CI.
	sts := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "")
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
	if got := mounts[shellSandboxWorkspaceVolume]; got.MountPath != shellSandboxWorkspacePath {
		t.Errorf("expected %s mounted at %s, got %#v", shellSandboxWorkspaceVolume, shellSandboxWorkspacePath, got)
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

func TestShellSandboxCredentialProxyURLIsOptional(t *testing.T) {
	// Empty is the state until #737 Part C, and it has to be a working state: the
	// entrypoint warns and starts, so file and code-execution tools function while
	// the credentialed wrappers report that they are unconfigured.
	withoutURL := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "")
	for _, env := range withoutURL.Spec.Template.Spec.Containers[0].Env {
		if env.Name == "CREDENTIAL_PROXY_URL" {
			t.Errorf("expected no CREDENTIAL_PROXY_URL when none was resolved, got %q", env.Value)
		}
	}

	withURL := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "http://test-agent-credential-proxy:8765")
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
	if svc.Name != buildShellSandboxStatefulSet(agent, "sandbox-ssh", "").Spec.ServiceName {
		t.Errorf("the StatefulSet's serviceName must be this Service, got %q vs %q",
			buildShellSandboxStatefulSet(agent, "sandbox-ssh", "").Spec.ServiceName, svc.Name)
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
	sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", "")
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

	sts := buildShellSandboxStatefulSet(agent, name, "")
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
	want := shellSandboxWorkspacePath + ":" + shellSandboxHomePath
	if got != want {
		t.Errorf("write safe root = %q, want %q", got, want)
	}
	// The agent's own home is what the file tools must no longer be able to name.
	for _, p := range strings.Split(got, ":") {
		if p == "/opt/data" {
			t.Error("the sandboxed write safe root still permits the agent's own home")
		}
	}
}
