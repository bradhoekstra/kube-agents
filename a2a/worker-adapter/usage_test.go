package workeradapter

import (
	"context"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// usageParts is the usage artifact's parts read as Usage, in stream order.
func usageParts(t *testing.T, task *lib.Task) []lib.Usage {
	t.Helper()
	a := task.Artifact(lib.ArtifactUsage)
	if a == nil {
		return nil
	}
	var out []lib.Usage
	for _, p := range a.Parts {
		u, ok := lib.UsageOf(p)
		if !ok {
			t.Fatalf("usage part is not usage: %s", p.Data)
		}
		out = append(out, u)
	}
	return out
}

// The harness result line's usage, total_cost_usd and duration_ms reach the
// bus as one usage part on the turn, with the model the init line named;
// cached prompt tokens count as input, since the model read them.
func TestLifecycle_UsageArtifact(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-otter-u1", "task-usage-1"
	submit(t, c, session, taskID, "how many pods are pending")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-u1","model":"claude-opus-5-5"}'
read first || exit 1
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"three"}]}}'
echo '{"type":"result","subtype":"success","result":"three pods are pending","duration_ms":4321,"duration_api_ms":4000,"num_turns":1,"total_cost_usd":0.0456,"usage":{"input_tokens":100,"cache_creation_input_tokens":20,"cache_read_input_tokens":30,"output_tokens":7}}'
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatalf("artifacts: %v", err)
	}
	got := usageParts(t, task)
	want := []lib.Usage{{Model: "claude-opus-5-5", InputTokens: 150, OutputTokens: 7, CostUsd: 0.0456, DurationMs: 4321}}
	if len(got) != 1 || got[0] != want[0] {
		t.Fatalf("usage = %+v, want %+v", got, want)
	}
	// The usage lands before the terminal: nothing follows the final event
	// (assertion 10), so a usage part in the fold is one that preceded it.
	events := replayEvents(t, url, session, taskID)
	last := events[len(events)-1]
	if last.Kind != lib.KindStatusUpdate {
		t.Fatalf("last event is %s, not the terminal", last.Kind)
	}
}

// A result line with no usage figures publishes no usage artifact: the
// reader sees absence, never zeroes.
func TestUsageArtifactAbsentWithoutFigures(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-otter-u2", "task-usage-2"
	submit(t, c, session, taskID, "say hi")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-u2"}'
read first || exit 1
echo '{"type":"result","subtype":"success","result":"hi"}'
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	if task.Artifact(lib.ArtifactUsage) != nil {
		t.Fatalf("usage artifact published with no figures: %+v", task.Artifact(lib.ArtifactUsage))
	}
}

// A turn that errored still cost tokens: its usage is on the stream ahead of
// the failed terminal.
func TestUsageArtifactOnHarnessError(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-otter-u3", "task-usage-3"
	submit(t, c, session, taskID, "do the thing")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-u3","model":"claude-opus-5-5"}'
read first || exit 1
echo '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"ran out of turns","duration_ms":99,"usage":{"input_tokens":5,"output_tokens":1}}'
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.res.State != lib.StateFailed {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatalf("artifacts: %v", err)
	}
	got := usageParts(t, task)
	if len(got) != 1 || got[0].InputTokens != 5 || got[0].OutputTokens != 1 || got[0].DurationMs != 99 || got[0].Model != "claude-opus-5-5" {
		t.Fatalf("usage = %+v", got)
	}
}
