package controller

import (
	"strings"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

// agentInProject is the test agent with its harness project set; the fixture
// may carry no harness at all.
func agentInProject(project string) *agentv1alpha1.PlatformAgent {
	agent := newTestPlatformAgent()
	if agent.Spec.Harness == nil {
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{}
	}
	agent.Spec.Harness.ProjectID = project
	return agent
}

func pubsubPlugin(name, config string) *agentv1alpha1.AgentPlugin {
	plugin := &agentv1alpha1.AgentPlugin{}
	plugin.Name = name
	plugin.Spec.AgentRef = "test-agent"
	plugin.Spec.Image = "example.com/" + name + ":v1"
	plugin.Spec.Config = config
	return plugin
}

const stockoutPluginConfig = `
platforms:
  pubsub:
    enabled: true
    extra:
      subscriptions:
        gke_stockout_alerts:
          subscription: gke-stockout-alerts-sub
          topic: gke-stockout-alerts-topic
          dispatch: kanban
`

func TestPluginPubsubSubscriptionsQualifiesShortNamesWithTheHarnessProject(t *testing.T) {
	agent := agentInProject("my-gcp-project")
	got := pluginPubsubSubscriptions(agent, []*agentv1alpha1.AgentPlugin{pubsubPlugin("stockout", stockoutPluginConfig)})
	want := []string{"projects/my-gcp-project/subscriptions/gke-stockout-alerts-sub"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestPluginPubsubSubscriptionsFollowsTheAdaptersRules(t *testing.T) {
	agent := agentInProject("my-gcp-project")
	plugins := []*agentv1alpha1.AgentPlugin{
		pubsubPlugin("listener", "platforms:\n  pubsub:\n    enabled: true\n"),
		pubsubPlugin("fullpath", `
platforms:
  pubsub:
    extra:
      subscriptions:
        elsewhere:
          subscription: projects/other-project/subscriptions/their-sub
        topic-only:
          topic: alerts
        topic-path-only:
          topic: projects/other-project/topics/deep
        nothing:
          prompt: "no subscription and no topic"
        same-again:
          subscription: gke-stockout-alerts-sub
`),
		pubsubPlugin("stockout", stockoutPluginConfig),
		pubsubPlugin("broken", "platforms: [not, a, map"),
		pubsubPlugin("empty", "   "),
	}
	got := pluginPubsubSubscriptions(agent, plugins)
	want := []string{
		"projects/my-gcp-project/subscriptions/alerts-sub",
		"projects/my-gcp-project/subscriptions/deep-sub",
		"projects/my-gcp-project/subscriptions/gke-stockout-alerts-sub",
		"projects/other-project/subscriptions/their-sub",
	}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestPluginPubsubSubscriptionsLeavesOutWhatTheBrokerWouldNotAdmit(t *testing.T) {
	// A typo in one route must not reach the broker's variable: an uppercase
	// project, a space in a topic, a two-letter subscription. The well formed
	// route beside them still renders.
	agent := agentInProject("my-gcp-project")
	plugins := []*agentv1alpha1.AgentPlugin{pubsubPlugin("typos", `
platforms:
  pubsub:
    extra:
      subscriptions:
        upper:
          subscription: projects/My-Project/subscriptions/alerts
        spaced:
          topic: "gke alerts"
        short:
          subscription: ab
        good:
          subscription: gke-stockout-alerts-sub
`)}
	got := pluginPubsubSubscriptions(agent, plugins)
	if len(got) != 1 || got[0] != "projects/my-gcp-project/subscriptions/gke-stockout-alerts-sub" {
		t.Fatalf("got %v, want only the well formed route", got)
	}
}

func TestPluginPubsubSubscriptionsSeesOnlyThePluginsTheGatewayLoads(t *testing.T) {
	// A hyphenated name fails the render's plugin-name rule: the plugin is set
	// Degraded and kept out of config.yaml and the pod env. Its routes must not
	// reach the broker either.
	agent := agentInProject("my-gcp-project")
	plugins := []*agentv1alpha1.AgentPlugin{pubsubPlugin("stockout-investigator", stockoutPluginConfig)}
	if got := pluginPubsubSubscriptions(agent, plugins); len(got) != 0 {
		t.Fatalf("a plugin the render refuses reached the broker's set: %v", got)
	}
	if url := pubsubRelayURL(agent, plugins); url != "" {
		t.Fatalf("the relay URL %q would be pinned for a plugin the gateway never loads", url)
	}
}

func TestPluginPubsubSubscriptionsWithoutAProjectKeepsOnlyFullPaths(t *testing.T) {
	agent := agentInProject("")
	plugins := []*agentv1alpha1.AgentPlugin{pubsubPlugin("stockout", stockoutPluginConfig), pubsubPlugin("full", `
platforms:
  pubsub:
    extra:
      subscriptions:
        r:
          subscription: projects/other-project/subscriptions/their-sub
`)}
	got := pluginPubsubSubscriptions(agent, plugins)
	if len(got) != 1 || got[0] != "projects/other-project/subscriptions/their-sub" {
		t.Fatalf("got %v, want the one full path", got)
	}
	if pluginPubsubSubscriptions(agent, nil) != nil && len(pluginPubsubSubscriptions(agent, nil)) != 0 {
		t.Fatalf("no plugins must mean no subscriptions")
	}
}

func TestTheBrokerLearnsTheSubscriptionSetAndTheGatewayTheRelayURL(t *testing.T) {
	agent := agentInProject("my-gcp-project")
	plugins := []*agentv1alpha1.AgentPlugin{pubsubPlugin("stockout", stockoutPluginConfig)}

	brokerEnv := buildCredentialProxyEnvForPlugins(agent, plugins)
	value, count := envValueCount(brokerEnv, platformPubsubSubscriptionsEnvVar)
	if count != 1 || value != "projects/my-gcp-project/subscriptions/gke-stockout-alerts-sub" {
		t.Fatalf("broker env %s: value %q count %d", platformPubsubSubscriptionsEnvVar, value, count)
	}
	if _, count := envValueCount(buildCredentialProxyEnv(agent), platformPubsubSubscriptionsEnvVar); count != 0 {
		t.Fatalf("with no plugins the broker must not carry %s", platformPubsubSubscriptionsEnvVar)
	}

	deployment := buildCredentialProxyDeploymentForPlugins(agent, "hash", plugins)
	if _, count := envValueCount(deployment.Spec.Template.Spec.Containers[0].Env, platformPubsubSubscriptionsEnvVar); count != 1 {
		t.Fatalf("the broker Deployment render must carry the set through to the container")
	}

	gateway := buildDeployment(agent, "cfg", "fb", "settings", "policy", plugins, renderOptions{imageVolumeSupported: true})
	agentContainer, found := findContainer(gateway.Spec.Template.Spec, "platform-agent")
	if !found {
		t.Fatalf("no platform-agent container")
	}
	wantURL := credentialProxyBaseURL(agent)
	if value, count := envValueCount(agentContainer.Env, pubsubRelayURLEnvVar); count != 1 || value != wantURL {
		t.Fatalf("gateway env %s: value %q count %d, want %q once", pubsubRelayURLEnvVar, value, count, wantURL)
	}
	// Empty rather than absent without a Pub/Sub platform: the adapter reads
	// an empty value as "no relay", and a name the operator always emits is one
	// a plugin's spec.env cannot supply.
	bare := buildDeployment(agent, "cfg", "fb", "settings", "policy", nil, renderOptions{imageVolumeSupported: true})
	bareContainer, _ := findContainer(bare.Spec.Template.Spec, "platform-agent")
	if value, count := envValueCount(bareContainer.Env, pubsubRelayURLEnvVar); count != 1 || value != "" {
		t.Fatalf("with no Pub/Sub platform the gateway must carry %s empty once, got %q count %d", pubsubRelayURLEnvVar, value, count)
	}
}

func TestAPluginCannotSupplyTheRelayURLTheOperatorDidNotRender(t *testing.T) {
	// A route outside the grammar renders no relay; a plugin that names the URL
	// in its own spec.env must not become the writer the adapter trusts.
	agent := agentInProject("my-gcp-project")
	plugin := pubsubPlugin("stockout", `
platforms:
  pubsub:
    enabled: true
    extra:
      subscriptions:
        gke_stockout_alerts:
          subscription: projects/my-gcp-project/subscriptions/ab
`)
	plugin.Spec.Env = []corev1.EnvVar{{Name: pubsubRelayURLEnvVar, Value: "http://attacker.example"}}
	plugins := []*agentv1alpha1.AgentPlugin{plugin}
	if url := pubsubRelayURL(agent, plugins); url != "" {
		t.Fatalf("a route outside the grammar rendered a relay URL %q", url)
	}
	gateway := buildDeployment(agent, "cfg", "fb", "settings", "policy", plugins, renderOptions{imageVolumeSupported: true})
	agentContainer, _ := findContainer(gateway.Spec.Template.Spec, "platform-agent")
	if value, count := envValueCount(agentContainer.Env, pubsubRelayURLEnvVar); count != 1 || value != "" {
		t.Fatalf("the plugin's %s reached the gateway: value %q count %d", pubsubRelayURLEnvVar, value, count)
	}
	if !strings.Contains(buildConfigMapData(agent, plugins)[managedEnvKey], "\n"+pubsubRelayURLEnvVar+"=\n") {
		t.Fatalf("%s is not pinned empty in the managed .env:\n%s", pubsubRelayURLEnvVar, buildConfigMapData(agent, plugins)[managedEnvKey])
	}
}

func TestTheManagedEnvPinsTheRelayURLWhenAPluginConfiguresPubsub(t *testing.T) {
	agent := agentInProject("my-gcp-project")
	plugins := []*agentv1alpha1.AgentPlugin{pubsubPlugin("stockout", stockoutPluginConfig)}
	data := buildConfigMapData(agent, plugins)
	if !strings.Contains(data[managedEnvKey], pubsubRelayURLEnvVar+"=") {
		t.Fatalf("%s is not pinned in the managed .env, so the agent could write its own:\n%s", pubsubRelayURLEnvVar, data[managedEnvKey])
	}
	if !strings.Contains(data[managedEnvKey], pubsubRelayURLEnvVar+"="+credentialProxyBaseURL(agent)+"\n") {
		t.Fatalf("%s pinned to something other than the broker:\n%s", pubsubRelayURLEnvVar, data[managedEnvKey])
	}
	if !strings.Contains(buildConfigMapData(agent, nil)[managedEnvKey], "\n"+pubsubRelayURLEnvVar+"=\n") {
		t.Fatalf("%s must be pinned empty with no Pub/Sub platform configured", pubsubRelayURLEnvVar)
	}
}

func TestTheReservedListNamesThePlatformPubsubSubscriptions(t *testing.T) {
	merged := mergeCredentialProxyEnv(nil, []corev1.EnvVar{
		{Name: platformPubsubSubscriptionsEnvVar, Value: "projects/p/subscriptions/theirs"},
		{Name: "HARMLESS_PLUGIN_SETTING", Value: "kept"},
	})
	if _, count := envValueCount(merged, platformPubsubSubscriptionsEnvVar); count != 0 {
		t.Fatalf("%s survived the merge from spec.deployment.env: a CR could point the relay at any subscription", platformPubsubSubscriptionsEnvVar)
	}
	if _, count := envValueCount(merged, "HARMLESS_PLUGIN_SETTING"); count != 1 {
		t.Fatalf("an unreserved name must still merge")
	}
}
