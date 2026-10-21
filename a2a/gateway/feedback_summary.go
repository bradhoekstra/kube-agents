package gateway

import (
	"context"
	"fmt"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/discovery"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
)

// "/feedback summary" (docs/designs/chat-feedback-command.md, part 7; issue
// #3428): the gateway posts the install's usage and feedback summary itself,
// deterministically, so a person in a restricted network can paste one block
// into an email or a chat message after reading it. Same block as
// scripts/feedback-summary.sh prints from a workstation, from what the
// gateway can see: the PlatformAgent resource (one read-only grant on its
// Role) and its own task records. No model is in the loop, and the block
// names no cluster, project, namespace, task or person: the audit below
// refuses a block that does.

const (
	usageSummaryTitle      = "kube-agents usage & feedback summary"
	usageSummaryMaxLines   = 40
	usageSummaryWindow     = 14 * 24 * time.Hour
	usageSummaryWindowDays = 14
	//: The performance figures the script reads in-pod (tokens, spend,
	//: latency) are not the gateway's to see; the block says where they are.
	usageSummaryPerformanceNote   = "tokens, spend and latency are in scripts/feedback-summary.sh's block"
	usageSummaryUnavailableNotice = "⚠️ The usage summary is unavailable: the PlatformAgent resource could not be read. From a workstation, scripts/feedback-summary.sh prints the same block."
	usageSummaryRefusedNotice     = "⚠️ The usage summary was not posted: the block would have named an identifier of this install. From a workstation, scripts/feedback-summary.sh prints it with the same check."
	usageSummaryFence             = "```"
	usageSummaryUnavailable       = "unavailable"
	usageSummaryUnknown           = "unknown"
	usageSummaryNever             = "never"
	usageSummaryNone              = "none"
	usageSummaryDefaultMode       = "today"
	usageSummaryReadyCondition    = "Ready"
	usageSummaryMinIdentifier     = 3
	usageSummaryDateLayout        = "2006-01-02"
	platformAgentGroup            = "kubeagents.x-k8s.io"
	platformAgentVersion          = "v1alpha1"
	platformAgentResource         = "platformagents"
	//: The gateway's Deployment is <agent>-a2a-gateway (the operator's
	//: a2aGatewayName); the resource name is what precedes the suffix.
	a2aGatewayNameSuffix = "-a2a-gateway"
	platformAgentNameEnv = "PLATFORM_AGENT_NAME"
)

// : status.usage fields, in the order the block prints them; a missing one prints 0.
var usageSummaryCounters = []string{
	"clustersRegistered", "clustersMonitored", "chatMessagesInboundTotal", "sessionsTotal",
	"toolExecutionsTotal", "eventsIngestedTotal", "remediationsProposedTotal", "remediationsMergedTotal",
}

// : Spec subtrees whose string leaves name the install and must not reach the
// : block. deployment is left out on purpose: its env values, annotations and
// : sidecar args are settings, and one spelled "true" would collide with the
// : block's own "ready: true" (the script makes the same choice).
var usageSummaryIdentifierSubtrees = []string{"harness", "scope", "integration", "security", "chat", "telemetry", "networkPolicy"}

// : Spec strings that are settings in any subtree.
var usageSummaryStopwords = map[string]bool{"true": true, "false": true, "yes": true, "no": true, "on": true, "off": true, "none": true, "null": true}

var (
	usageSummaryNumeric = regexp.MustCompile(`^[0-9][0-9._%:-]*$`)
	usageSummaryToken   = regexp.MustCompile(`[A-Za-z0-9_.-]+`)
)

var usageSummaryFeedbackTemplate = []string{
	"feedback:",
	"  Rating: [ 👍 / 👎 / 😐 ]",
	"  What worked well:",
	"  What failed or was slow:",
}

// usageSource is what the summary reads from the cluster: the PlatformAgent
// resource as unstructured content, and the server version.
type usageSource interface {
	PlatformAgent(ctx context.Context) (map[string]any, error)
	ServerVersion(ctx context.Context) (string, error)
}

// k8sUsageSource reads through the gateway's own ServiceAccount: the Role
// grants get on the one named PlatformAgent, and /version needs no grant.
type k8sUsageSource struct {
	dyn       dynamic.Interface
	disc      discovery.DiscoveryInterface
	namespace string
	name      string
}

// newK8sUsageSource builds the in-cluster reader. The resource name is
// PLATFORM_AGENT_NAME when set, else the gateway's own Deployment name less
// its suffix.
func newK8sUsageSource(cfg *Config) (*k8sUsageSource, error) {
	name := strings.TrimSpace(os.Getenv(platformAgentNameEnv))
	if name == "" {
		name = strings.TrimSuffix(cfg.OwnerDeployment, a2aGatewayNameSuffix)
	}
	if name == "" {
		return nil, fmt.Errorf("the PlatformAgent name is unknown: set %s or A2A_OWNER_DEPLOYMENT", platformAgentNameEnv)
	}
	rc, err := rest.InClusterConfig()
	if err != nil {
		return nil, err
	}
	dyn, err := dynamic.NewForConfig(rc)
	if err != nil {
		return nil, err
	}
	disc, err := discovery.NewDiscoveryClientForConfig(rc)
	if err != nil {
		return nil, err
	}
	return &k8sUsageSource{dyn: dyn, disc: disc, namespace: cfg.Namespace, name: name}, nil
}

func (s *k8sUsageSource) PlatformAgent(ctx context.Context) (map[string]any, error) {
	gvr := schema.GroupVersionResource{Group: platformAgentGroup, Version: platformAgentVersion, Resource: platformAgentResource}
	obj, err := s.dyn.Resource(gvr).Namespace(s.namespace).Get(ctx, s.name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	return obj.UnstructuredContent(), nil
}

func (s *k8sUsageSource) ServerVersion(context.Context) (string, error) {
	info, err := s.disc.ServerVersion()
	if err != nil {
		return "", err
	}
	return info.GitVersion, nil
}

// taskRollup is what the gateway's records say about the last window: tasks
// that started, split by outcome. No title, requester or text.
type taskRollup struct {
	Total, Finished, Canceled, Active int
}

func rollupTasks(records []*SessionRecord, now time.Time) taskRollup {
	cutoff := now.Add(-usageSummaryWindow)
	var r taskRollup
	for _, rec := range records {
		for _, task := range rec.Tasks {
			if task.StartedAt.IsZero() || task.StartedAt.Before(cutoff) {
				continue
			}
			r.Total++
			switch {
			case task.Canceled:
				r.Canceled++
			case task.TerminalObserved:
				r.Finished++
			default:
				r.Active++
			}
		}
	}
	return r
}

func nested(m map[string]any, path ...string) any {
	var cur any = m
	for _, key := range path {
		next, ok := cur.(map[string]any)
		if !ok {
			return nil
		}
		cur = next[key]
	}
	return cur
}

func asString(v any) string {
	s, _ := v.(string)
	return s
}

func asInt(v any) int64 {
	switch n := v.(type) {
	case int64:
		return n
	case int:
		return int64(n)
	case float64:
		return int64(n)
	case string:
		i, _ := strconv.ParseInt(n, 10, 64)
		return i
	}
	return 0
}

func installAge(cr map[string]any, now time.Time) string {
	created, err := time.Parse(time.RFC3339, asString(nested(cr, "metadata", "creationTimestamp")))
	if err != nil {
		return usageSummaryUnknown
	}
	return fmt.Sprintf("%d days", int(now.Sub(created).Hours()/24))
}

func readyState(cr map[string]any) string {
	conditions, _ := nested(cr, "status", "conditions").([]any)
	for _, c := range conditions {
		cond, _ := c.(map[string]any)
		if asString(cond["type"]) == usageSummaryReadyCondition {
			return strings.ToLower(asString(cond["status"]))
		}
	}
	return usageSummaryUnknown
}

// renderUsageSummary is the block: the script's shape, from what the gateway sees.
func renderUsageSummary(now time.Time, cr map[string]any, serverVersion string, tasks taskRollup, haveTasks bool) string {
	usage, _ := nested(cr, "status", "usage").(map[string]any)
	lines := []string{usageSummaryFence, fmt.Sprintf("%s (%s)", usageSummaryTitle, now.UTC().Format(usageSummaryDateLayout)), "environment:"}
	tag := asString(nested(cr, "spec", "deployment", "tag"))
	if tag == "" {
		tag = usageSummaryUnknown
	}
	mode := asString(nested(cr, "spec", "mode"))
	if mode == "" {
		mode = usageSummaryDefaultMode
	}
	if serverVersion == "" {
		serverVersion = usageSummaryUnavailable
	}
	lines = append(lines,
		"  version: "+tag,
		"  mode: "+mode,
		"  kubernetes: "+serverVersion,
		fmt.Sprintf("  installed: %s; ready: %s", installAge(cr, now), readyState(cr)),
	)
	var interfaces []string
	if raw, ok := usage["activeInterfaces"].([]any); ok {
		for _, item := range raw {
			interfaces = append(interfaces, asString(item))
		}
	}
	sort.Strings(interfaces)
	joined := strings.Join(interfaces, ", ")
	if joined == "" {
		joined = usageSummaryNone
	}
	lines = append(lines, "  interfaces: "+joined, "usage:")
	for _, counter := range usageSummaryCounters {
		lines = append(lines, fmt.Sprintf("  %s: %d", counter, asInt(usage[counter])))
	}
	last := asString(usage["lastActiveTime"])
	if last == "" {
		last = usageSummaryNever
	}
	lines = append(lines, "  lastActive: "+last)
	lines = append(lines, "performance: "+usageSummaryPerformanceNote)
	if !haveTasks {
		lines = append(lines, fmt.Sprintf("tasks (%d days): %s", usageSummaryWindowDays, usageSummaryUnavailable))
	} else {
		var parts []string
		for _, bucket := range []struct {
			name string
			n    int
		}{{"finished", tasks.Finished}, {"canceled", tasks.Canceled}, {"active", tasks.Active}} {
			if bucket.n > 0 {
				parts = append(parts, fmt.Sprintf("%s %d", bucket.name, bucket.n))
			}
		}
		line := fmt.Sprintf("tasks (%d days): %d", usageSummaryWindowDays, tasks.Total)
		if len(parts) > 0 {
			line += " (" + strings.Join(parts, ", ") + ")"
		}
		lines = append(lines, line)
	}
	lines = append(lines, usageSummaryFeedbackTemplate...)
	lines = append(lines, usageSummaryFence)
	return strings.Join(lines, "\n")
}

func stringLeaves(v any, out *[]string) {
	switch x := v.(type) {
	case string:
		*out = append(*out, x)
	case map[string]any:
		for _, item := range x {
			stringLeaves(item, out)
		}
	case []any:
		for _, item := range x {
			stringLeaves(item, out)
		}
	}
}

// usageIdentifiers is what the block must not carry: the resource's name and
// namespace plus the string leaves of the naming subtrees, less settings
// (booleans, numbers, strings shorter than the floor) and the two spec strings
// the block prints by design.
func usageIdentifiers(cr map[string]any) []string {
	spec, _ := cr["spec"].(map[string]any)
	printed := map[string]bool{asString(nested(cr, "spec", "mode")): true, asString(nested(cr, "spec", "deployment", "tag")): true}
	values := []string{asString(nested(cr, "metadata", "name")), asString(nested(cr, "metadata", "namespace"))}
	for _, subtree := range usageSummaryIdentifierSubtrees {
		stringLeaves(spec[subtree], &values)
	}
	seen := map[string]bool{}
	var ids []string
	for _, v := range values {
		if len(v) < usageSummaryMinIdentifier || printed[v] || seen[v] || usageSummaryStopwords[strings.ToLower(v)] || usageSummaryNumeric.MatchString(v) {
			continue
		}
		seen[v] = true
		ids = append(ids, v)
	}
	return ids
}

var usageSummaryTemplateTokens = func() map[string]bool {
	tokens := map[string]bool{}
	for _, tok := range usageSummaryToken.FindAllString(renderUsageSummary(time.Unix(0, 0).UTC(), map[string]any{}, "", taskRollup{}, false), -1) {
		tokens[tok] = true
	}
	return tokens
}()

// auditUsageSummary returns the identifiers that appear in text as whole
// tokens, skipping the template's own vocabulary (a cluster named "today" is
// not told apart from the mode line, and is not refused for it).
func auditUsageSummary(text string, identifiers []string) []string {
	var leaked []string
	for _, id := range identifiers {
		if id == "" || usageSummaryTemplateTokens[id] {
			continue
		}
		pattern := regexp.MustCompile(`(?:^|[^A-Za-z0-9_.-])` + regexp.QuoteMeta(id) + `(?:[^A-Za-z0-9_.-]|$)`)
		if pattern.MatchString(text) {
			leaked = append(leaked, id)
		}
	}
	return leaked
}

// feedbackSummary answers "/feedback summary" in place: the block, or one of
// the two fixed notices. Nothing about the conversation's task state changes,
// and no error text reaches chat.
func (g *Gateway) feedbackSummary(ctx context.Context, rec *SessionRecord) {
	if g.usage == nil {
		g.log.Warn("feedback summary unavailable: no usage source", "conversation", rec.Key)
		g.post(rec.Key, usageSummaryUnavailableNotice)
		return
	}
	cr, err := g.usage.PlatformAgent(ctx)
	if err != nil {
		g.log.Warn("feedback summary unavailable", "conversation", rec.Key, "err", err)
		g.post(rec.Key, usageSummaryUnavailableNotice)
		return
	}
	version, err := g.usage.ServerVersion(ctx)
	if err != nil {
		g.log.Warn("feedback summary: server version unavailable", "err", err)
		version = ""
	}
	now := time.Now()
	records, err := g.reg.Sessions(ctx)
	haveTasks := err == nil
	if err != nil {
		g.log.Warn("feedback summary: task records unavailable", "err", err)
	}
	text := renderUsageSummary(now, cr, version, rollupTasks(records, now), haveTasks)
	if leaked := auditUsageSummary(text, usageIdentifiers(cr)); len(leaked) > 0 {
		g.log.Warn("feedback summary refused: the block named identifiers of the install", "conversation", rec.Key, "count", len(leaked))
		g.post(rec.Key, usageSummaryRefusedNotice)
		return
	}
	g.log.Info("feedback summary", "conversation", rec.Key)
	g.post(rec.Key, text)
}
