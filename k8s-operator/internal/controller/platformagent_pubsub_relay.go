package controller

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The gateway's Pub/Sub platform adapter (agentplugins/pubsub-platform) pulls
// its subscriptions through the credential broker: the gateway Pod holds no
// Google credential, so the broker pulls on the install's identity, confined by
// its policy to the subscriptions rendered here. The operator is the one reader
// of both the AgentPlugin configs and the project, so it is the one writer of
// the set: the broker learns it from platformPubsubSubscriptionsEnvVar and the
// gateway learns where the relay is from pubsubRelayURLEnvVar. Neither is set
// when no plugin configures a Pub/Sub platform, and the broker refuses every
// relayed Pub/Sub call then.
const (
	pubsubRelayURLEnvVar              = "PUBSUB_RELAY_URL"
	platformPubsubSubscriptionsEnvVar = "PLATFORM_PUBSUB_SUBSCRIPTIONS"
	// The adapter's own rule for a route that names a topic and no
	// subscription: it subscribes to `<topic>-sub`, the name the installers
	// create (adapter.py, _parse_subscription_config).
	pubsubDerivedSubscriptionSuffix = "-sub"
	pubsubSubscriptionPathFormat    = "projects/%s/subscriptions/%s"
	pubsubSubscriptionPathPrefix    = "projects/"
	pubsubSubscriptionsSeparator    = ","
)

// The shape the broker's policy matches a subscription on the wire by
// (agents/platform/scripts/watcher_policy.py: PUBSUB_PROJECT and SUBSCRIPTION).
// A route that does not render to this shape is left out of the set rather than
// handed to the broker: the broker drops such an entry too, and the route's
// pulls are then refused by the policy, which the adapter logs with the rule.
// Validating here as well keeps the rendered variable honest.
var pubsubSubscriptionPathPattern = regexp.MustCompile(
	`^projects/((?:[a-z0-9][a-z0-9.-]*[a-z0-9]:)?[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]{1,20})/subscriptions/[A-Za-z][A-Za-z0-9._~+%-]{2,254}$`,
)

// pluginPubsubSubscriptions is the sorted, deduplicated set of fully qualified
// subscriptions the agent's plugins configure under
// `platforms.pubsub.extra.subscriptions.<route>`, qualified with the harness
// project the way the adapter qualifies them. A plugin whose config does not
// parse, or a route that names neither a subscription nor a topic, contributes
// nothing: the adapter logs and skips the same route, and the broker's refusal
// of an unlisted subscription is the loud signal if the two ever disagree.
func pluginPubsubSubscriptions(agent *agentv1alpha1.PlatformAgent, agentPlugins []*agentv1alpha1.AgentPlugin) []string {
	if len(agentPlugins) == 0 {
		return nil
	}
	project := ""
	if agent != nil && agent.Spec.Harness != nil {
		project = strings.TrimSpace(agent.Spec.Harness.ProjectID)
	}
	seen := map[string]struct{}{}
	for _, plugin := range agentPlugins {
		if plugin == nil || strings.TrimSpace(plugin.Spec.Config) == "" {
			continue
		}
		var config map[string]any
		if err := yaml.Unmarshal([]byte(plugin.Spec.Config), &config); err != nil {
			continue
		}
		for _, route := range pubsubRoutes(config) {
			if path := pubsubSubscriptionPath(route, project); pubsubSubscriptionPathPattern.MatchString(path) {
				seen[path] = struct{}{}
			}
		}
	}
	paths := make([]string, 0, len(seen))
	for path := range seen {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	return paths
}

// pubsubRoutes is the route map under platforms.pubsub.extra.subscriptions, in
// route-name order, or nothing when the config carries no such map.
func pubsubRoutes(config map[string]any) []map[string]any {
	platforms, _ := config["platforms"].(map[string]any)
	pubsub, _ := platforms["pubsub"].(map[string]any)
	extra, _ := pubsub["extra"].(map[string]any)
	subscriptions, _ := extra["subscriptions"].(map[string]any)
	names := make([]string, 0, len(subscriptions))
	for name := range subscriptions {
		names = append(names, name)
	}
	sort.Strings(names)
	routes := make([]map[string]any, 0, len(names))
	for _, name := range names {
		if route, ok := subscriptions[name].(map[string]any); ok {
			routes = append(routes, route)
		}
	}
	return routes
}

// pubsubSubscriptionPath is the fully qualified subscription one route pulls,
// by the adapter's rule: a `subscription` that is already a path as it is; a
// short one under the project; a `topic` alone as `<topic>-sub` under the
// project. "" when the route names nothing, or names a short name with no
// project to qualify it by. The caller checks the shape; this only composes.
func pubsubSubscriptionPath(route map[string]any, project string) string {
	subscription, _ := route["subscription"].(string)
	subscription = strings.TrimSpace(subscription)
	if strings.HasPrefix(subscription, pubsubSubscriptionPathPrefix) {
		return subscription
	}
	if project == "" {
		return ""
	}
	if subscription != "" {
		return fmt.Sprintf(pubsubSubscriptionPathFormat, project, subscription)
	}
	topic, _ := route["topic"].(string)
	topic = strings.TrimSpace(topic)
	if topic == "" {
		return ""
	}
	if strings.HasPrefix(topic, pubsubSubscriptionPathPrefix) {
		topic = topic[strings.LastIndex(topic, "/")+1:]
	}
	return fmt.Sprintf(pubsubSubscriptionPathFormat, project, topic+pubsubDerivedSubscriptionSuffix)
}

// platformPubsubEnv is the broker's variable for the set, or nothing when the
// set is empty, so an install without a Pub/Sub platform renders no relay.
func platformPubsubEnv(subscriptions []string) []corev1.EnvVar {
	if len(subscriptions) == 0 {
		return nil
	}
	return []corev1.EnvVar{{
		Name:  platformPubsubSubscriptionsEnvVar,
		Value: strings.Join(subscriptions, pubsubSubscriptionsSeparator),
	}}
}

// pubsubRelayEnabled says whether the gateway gets the relay URL: exactly when
// the broker gets a non-empty set, so the two cannot disagree.
func pubsubRelayEnabled(agent *agentv1alpha1.PlatformAgent, agentPlugins []*agentv1alpha1.AgentPlugin) bool {
	return len(pluginPubsubSubscriptions(agent, agentPlugins)) > 0
}
