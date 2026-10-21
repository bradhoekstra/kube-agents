package gateway

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

var summaryNow = time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)

func summaryCR() map[string]any {
	return map[string]any{
		"metadata": map[string]any{
			"name": "platform-agent", "namespace": "kubeagents-system",
			"creationTimestamp": "2026-10-02T12:00:00Z",
		},
		"spec": map[string]any{
			"mode":        "next",
			"deployment":  map[string]any{"tag": "0.14.0", "env": []any{map[string]any{"name": "OTEL_SDK_DISABLED", "value": "true"}}},
			"harness":     map[string]any{"clusterName": "prod-eu-1", "projectId": "acme-platform-prod", "location": "us-central1"},
			"scope":       map[string]any{"projects": []any{"acme-scope-dev"}, "folders": []any{"123456789"}},
			"integration": map[string]any{"gitops": map[string]any{"repository": "acme/gitops"}},
		},
		"status": map[string]any{
			"conditions": []any{map[string]any{"type": "Ready", "status": "True"}},
			"usage": map[string]any{
				"clustersRegistered": int64(3), "clustersMonitored": int64(2),
				"chatMessagesInboundTotal": int64(41), "sessionsTotal": int64(7),
				"toolExecutionsTotal": int64(19127), "eventsIngestedTotal": int64(10),
				"remediationsProposedTotal": int64(4), "remediationsMergedTotal": int64(1),
				"lastActiveTime":   "2026-10-21T11:34:06Z",
				"activeInterfaces": []any{"slack", "googlechat"},
			},
		},
	}
}

func summaryRecords() []*SessionRecord {
	ago := func(d time.Duration) time.Time { return summaryNow.Add(-d) }
	return []*SessionRecord{
		// The ordinary relay path leaves no end marker on a TaskRef (only the
		// heal path sets TerminalObserved): what tells a running task from an
		// ended one is the record's ActiveTask.
		{Key: "a", ActiveTask: &ActiveTask{TaskID: "t4"}, Tasks: []TaskRef{
			{ID: "t1", StartedAt: ago(10 * time.Minute)},
			{ID: "t2", StartedAt: ago(time.Hour), TerminalObserved: true}, // healed: ended all the same
			{ID: "t3", StartedAt: ago(2 * time.Hour), Canceled: true},
			{ID: "t4", StartedAt: ago(2 * time.Minute)},
		}},
		{Key: "b", Tasks: []TaskRef{
			{ID: "t5", StartedAt: ago(3 * 24 * time.Hour)},
			{ID: "t6", StartedAt: ago(30 * 24 * time.Hour)}, // outside the window
			{ID: "t7"}, // never started: not counted
		}},
	}
}

func TestRollupTasksBucketsByRecordStateWithinTheWindow(t *testing.T) {
	got := rollupTasks(summaryRecords(), summaryNow, usageSummaryWindow)
	want := taskRollup{Total: 5, Ended: 3, Cancelled: 1, Active: 1}
	if got != want {
		t.Fatalf("rollupTasks = %+v, want %+v", got, want)
	}
}

func TestUsageWindowClampsToTheSessionTTL(t *testing.T) {
	if got := usageWindow(0); got != usageSummaryWindow {
		t.Fatalf("usageWindow(0) = %v, want the full window", got)
	}
	if got := usageWindow(30 * 24 * time.Hour); got != usageSummaryWindow {
		t.Fatalf("usageWindow(30d) = %v, want the full window", got)
	}
	if got := usageWindow(7 * 24 * time.Hour); got != 7*24*time.Hour {
		t.Fatalf("usageWindow(7d) = %v, want 7d", got)
	}
	// t5 started three days ago: a two-day window drops it.
	got := rollupTasks(summaryRecords(), summaryNow, 2*24*time.Hour)
	if want := (taskRollup{Total: 4, Ended: 2, Cancelled: 1, Active: 1}); got != want {
		t.Fatalf("rollupTasks(2d) = %+v, want %+v", got, want)
	}
}

func TestRenderUsageSummaryHasTheScriptsShapeAndNoIdentifier(t *testing.T) {
	text := renderUsageSummary(summaryNow, summaryCR(), "v1.33.4-gke.1234000", usageSummaryWindow, rollupTasks(summaryRecords(), summaryNow, usageSummaryWindow), true)
	lines := strings.Split(text, "\n")
	if lines[0] != "```" || lines[len(lines)-1] != "```" {
		t.Fatalf("the block must be fenced, got first %q last %q", lines[0], lines[len(lines)-1])
	}
	if len(lines) > usageSummaryMaxLines {
		t.Fatalf("block is %d lines, cap %d", len(lines), usageSummaryMaxLines)
	}
	for _, want := range []string{
		"kube-agents usage & feedback summary (2026-10-21)",
		"  version: 0.14.0", "  mode: next", "  kubernetes: v1.33.4-gke.1234000",
		"  installed: 19 days; ready: true", "  interfaces: googlechat, slack",
		"  toolExecutionsTotal: 19127", "  sessionsTotal: 7", "  remediationsMergedTotal: 1",
		"  lastActive: 2026-10-21T11:34:06Z",
		"tasks (14 days): 5 (ended 3, cancelled 1, active 1)",
		"performance: " + usageSummaryPerformanceNote,
		"  Rating:", "  What worked well:", "  What failed or was slow:",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("block lacks %q:\n%s", want, text)
		}
	}
	for _, forbidden := range []string{"prod-eu-1", "acme-platform-prod", "kubeagents-system", "us-central1", "acme-scope-dev", "acme/gitops", "123456789", "t1"} {
		if strings.Contains(text, forbidden) {
			t.Errorf("block carries %q", forbidden)
		}
	}
	if leaked := auditUsageSummary(text, usageIdentifiers(summaryCR())); len(leaked) != 0 {
		t.Errorf("audit of a clean block reported %v", leaked)
	}
}

func TestRenderUsageSummaryDegradesWhenSourcesAreMissing(t *testing.T) {
	text := renderUsageSummary(summaryNow, map[string]any{}, "", 7*24*time.Hour, taskRollup{}, false)
	for _, want := range []string{"  version: unknown", "  mode: today", "  kubernetes: unavailable", "  installed: unknown; ready: unknown", "  interfaces: none", "  toolExecutionsTotal: 0", "  lastActive: never", "tasks (7 days): unavailable"} {
		if !strings.Contains(text, want) {
			t.Errorf("block lacks %q:\n%s", want, text)
		}
	}
	if len(strings.Split(text, "\n")) > usageSummaryMaxLines {
		t.Fatal("block over the cap")
	}
}

func TestUsageIdentifiersAreTheNamingSubtreesStringsButNotSettings(t *testing.T) {
	ids := usageIdentifiers(summaryCR())
	for _, want := range []string{"platform-agent", "kubeagents-system", "prod-eu-1", "acme-platform-prod", "us-central1", "acme-scope-dev", "acme/gitops"} {
		if !contains(ids, want) {
			t.Errorf("identifiers lack %q: %v", want, ids)
		}
	}
	for _, unwanted := range []string{"0.14.0", "next", "true", "OTEL_SDK_DISABLED", "123456789"} {
		if contains(ids, unwanted) {
			t.Errorf("identifiers carry the setting %q", unwanted)
		}
	}
}

func TestAuditUsageSummaryMatchesWholeTokensAndSkipsTheTemplate(t *testing.T) {
	if got := auditUsageSummary("on prod-eu-1 today", []string{"prod-eu-1", "today", "kube-agents"}); len(got) != 1 || got[0] != "prod-eu-1" {
		t.Fatalf("audit = %v, want [prod-eu-1]", got)
	}
	if got := auditUsageSummary("version: 0.14.0-rc", []string{"0.14.0"}); len(got) != 0 {
		t.Fatalf("audit matched inside a token: %v", got)
	}
}

func contains(list []string, want string) bool {
	for _, item := range list {
		if item == want {
			return true
		}
	}
	return false
}

type fakeUsageSource struct {
	cr      map[string]any
	version string
	err     error
}

func (f fakeUsageSource) PlatformAgent(context.Context) (map[string]any, error) {
	return f.cr, f.err
}

func (f fakeUsageSource) ServerVersion(context.Context) (string, error) { return f.version, nil }

// TestFeedbackSummaryPostsTheBlockAndStartsNoTask: "/feedback summary" is the
// gateway's own answer, like the bare form: one fenced block, no task, no
// change to the conversation's state.
func TestFeedbackSummaryPostsTheBlockAndStartsNoTask(t *testing.T) {
	r := startRig(t)
	r.g.usage = fakeUsageSource{cr: summaryCR(), version: "v1.33.4-gke.1234000"}
	conv := "discord:g1/thread-fbs1"
	sessionRigTurn(r, conv, "fbs-100", "/feedback summary")
	waitFor(t, "summary", postedContaining(r, usageSummaryTitle))
	if !postedContaining(r, "toolExecutionsTotal: 19127")() {
		t.Fatal("the block must carry the usage counters")
	}
	// The tasks line is the live scan of the rig's bucket under the rig's
	// SessionTTL, which this turn added nothing to.
	if line := fmt.Sprintf("tasks (%d days): 0", windowDays(usageWindow(r.g.cfg.SessionTTL))); !postedContaining(r, line)() {
		t.Fatalf("the block must carry %q from the record scan", line)
	}
	if postedContaining(r, "prod-eu-1")() || postedContaining(r, "kubeagents-system")() {
		t.Fatal("the block named an identifier of the install")
	}
	if n := len(inSubjectEnvelopes(t, r.url, "platform")); n != 0 {
		t.Fatalf("/feedback summary submitted %d envelope(s); it must submit none", n)
	}
	if rec, _ := r.g.reg.Get(context.Background(), conv); rec != nil && rec.ActiveTask != nil {
		t.Fatalf("/feedback summary left an active task: %+v", rec.ActiveTask)
	}
}

func TestFeedbackSummaryRefusesALeakAndSaysWhyWithoutNamingIt(t *testing.T) {
	r := startRig(t)
	cr := summaryCR()
	// A status whose interface list carries the cluster's name: the one
	// dynamic field a leak could ride into the block.
	cr["status"].(map[string]any)["usage"].(map[string]any)["activeInterfaces"] = []any{"slack", "prod-eu-1"}
	r.g.usage = fakeUsageSource{cr: cr, version: "v1"}
	conv := "discord:g1/thread-fbs2"
	sessionRigTurn(r, conv, "fbs-110", "/feedback summary")
	waitFor(t, "refusal", postedContaining(r, usageSummaryRefusedNotice))
	if postedContaining(r, usageSummaryTitle)() {
		t.Fatal("the block was posted although it named an identifier")
	}
}

func TestFeedbackSummaryUnavailableWhenTheResourceCannotBeRead(t *testing.T) {
	r := startRig(t)
	r.g.usage = fakeUsageSource{err: errors.New("platformagents.kubeagents.x-k8s.io is forbidden")}
	conv := "discord:g1/thread-fbs3"
	sessionRigTurn(r, conv, "fbs-120", "/feedback summary")
	waitFor(t, "unavailable", postedContaining(r, usageSummaryUnavailableNotice))
	if postedContaining(r, "forbidden")() {
		t.Fatal("the error text reached chat")
	}
	if n := len(inSubjectEnvelopes(t, r.url, "platform")); n != 0 {
		t.Fatalf("submitted %d envelope(s); it must submit none", n)
	}
}
