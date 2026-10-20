"""Unit tests for feedback_summary.py: the copy-pasteable usage and feedback summary.

Run: python3 -m unittest agents/platform/scripts/test_feedback_summary.py
"""

from __future__ import annotations

import datetime as dt
import io
import json
import subprocess
import unittest
from unittest import mock

import feedback_summary as fs

NOW = dt.datetime(2026, 10, 20, 18, 0, tzinfo=dt.timezone.utc)
CLUSTER = "prod-eu-1"
PROJECT = "acme-platform-prod"
NAMESPACE = "kubeagents-system"
TAG = "0.14.0"

CR = {
    "metadata": {
        "name": "platform-agent",
        "namespace": NAMESPACE,
        "creationTimestamp": "2026-10-01T12:00:00Z",
    },
    "spec": {
        "mode": "today",
        "deployment": {"tag": TAG},
        "harness": {"clusterName": CLUSTER, "projectId": PROJECT, "location": "us-central1"},
    },
    "status": {
        "conditions": [{"type": "Ready", "status": "True"}],
        "usage": {
            "clustersRegistered": 3,
            "clustersMonitored": 2,
            "chatMessagesInboundTotal": 41,
            "sessionsTotal": 7,
            "toolExecutionsTotal": 19127,
            "eventsIngestedTotal": 10,
            "remediationsProposedTotal": 4,
            "remediationsMergedTotal": 1,
            "lastActiveTime": "2026-10-20T17:34:06Z",
            "activeInterfaces": ["googlechat"],
        },
    },
}

LITELLM_CONFIG_YAML = """model_list:
  - model_name: model-default
    litellm_params:
      model: vertex_ai/claude-opus-5
      vertex_project: acme-platform-prod
"""

SERVER_VERSION = {"serverVersion": {"gitVersion": "v1.33.4-gke.1234000"}}


def _task(status, created_min_ago, duration_s=None, title="list pods in prod-eu-1"):
    created = int((NOW - dt.timedelta(minutes=created_min_ago)).timestamp())
    completed = created + duration_s if duration_s is not None else None
    return {
        "id": f"t_{created_min_ago}",
        "title": title,
        "assignee": "platform",
        "status": status,
        "priority": 0,
        "session_id": "s1",
        "created_at": created,
        "started_at": created,
        "completed_at": completed,
        "updated_at": completed or created,
        "summary": f"touched {CLUSTER} in {PROJECT}",
        "result": f"token ya29.{'A' * 40} on {CLUSTER}",
        "error": None,
    }


TASKS = [
    _task("done", 10, 95),
    _task("done", 60, 310),
    _task("blocked", 120, 40),
    _task("gave_up", 600, 2000),
    _task("running", 2),
    _task("done", 30 * 24 * 60, 50),  # outside the window
]

LITELLM_METRICS = """# HELP litellm_spend_metric_total Total spend on LLM requests
# TYPE litellm_spend_metric_total counter
litellm_spend_metric_total{api_provider="vertex_ai",model="claude-opus-5",requested_model="model-default"} 2.907
litellm_input_tokens_metric_total{api_provider="vertex_ai",model="claude-opus-5"} 1200000
litellm_output_tokens_metric_total{api_provider="vertex_ai",model="claude-opus-5"} 34000
litellm_input_tokens_metric_total{api_provider="vertex_ai",model="other"} 100
litellm_request_total_latency_metric_bucket{le="1.0",model="claude-opus-5"} 3
litellm_request_total_latency_metric_bucket{le="5.0",model="claude-opus-5"} 9
litellm_request_total_latency_metric_bucket{le="30.0",model="claude-opus-5"} 10
litellm_request_total_latency_metric_bucket{le="+Inf",model="claude-opus-5"} 10
litellm_request_total_latency_metric_count{model="claude-opus-5"} 10
"""


def full_snapshot(**overrides):
    base = dict(
        generated_at=NOW,
        cr=CR,
        server_version=SERVER_VERSION,
        litellm_config=LITELLM_CONFIG_YAML,
        tasks=TASKS,
        litellm_metrics=LITELLM_METRICS,
        sources={"session-kv": True, "litellm-metrics": True},
    )
    base.update(overrides)
    return fs.build_snapshot(**base)


class RenderTest(unittest.TestCase):
    def test_a_full_snapshot_renders_one_fenced_block_under_the_line_cap(self):
        text = fs.render(full_snapshot())
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("```"))
        self.assertEqual(lines[-1], "```")
        self.assertLessEqual(len(lines), fs.SUMMARY_MAX_LINES)
        self.assertIn("mode: today", text)
        self.assertIn(f"version: {TAG}", text)
        self.assertIn("model: vertex_ai/claude-opus-5", text)
        self.assertIn("kubernetes: v1.33.4-gke.1234000", text)
        self.assertIn("installed: 19 days", text)
        self.assertIn("toolExecutionsTotal: 19127", text)
        self.assertIn("sessionsTotal: 7", text)
        self.assertIn("Rating:", text)
        self.assertIn("What worked well:", text)
        self.assertIn("What failed or was slow:", text)

    def test_the_block_carries_no_identifier_from_the_install(self):
        text = fs.render(full_snapshot())
        for forbidden in (CLUSTER, PROJECT, NAMESPACE, "ya29.", "list pods", "s1", "us-central1"):
            self.assertNotIn(forbidden, text, forbidden)
        self.assertEqual(fs.audit(text, [CLUSTER, PROJECT, NAMESPACE]), [])

    def test_audit_names_a_leaked_identifier(self):
        self.assertEqual(fs.audit(f"all good on {CLUSTER}", [CLUSTER, PROJECT]), [CLUSTER])

    def test_rendering_is_deterministic(self):
        self.assertEqual(fs.render(full_snapshot()), fs.render(full_snapshot()))

    def test_tasks_are_rolled_up_by_status_and_duration_only(self):
        text = fs.render(full_snapshot())
        self.assertIn("tasks (14 days): 5", text)
        self.assertIn("done 2", text)
        self.assertIn("blocked 1", text)
        self.assertIn("gave_up 1", text)
        self.assertIn("active 1", text)
        self.assertIn("completed p50 1m35s / p95 5m10s", text)
        # the newest completed tasks, one line each, without titles
        self.assertRegex(text, r"\n  - 10m ago +done +1m35s\n")
        self.assertNotIn("prod-eu", text)

    def test_tokens_and_spend_come_from_the_gateway_metrics(self):
        text = fs.render(full_snapshot())
        self.assertIn("tokens: in 1200100 / out 34000", text)
        self.assertIn("spend: 2.91 USD", text)
        self.assertIn("latency p50 2.3s / p95 17.5s", text)

    def test_missing_sources_degrade_to_unavailable_lines(self):
        snapshot = full_snapshot(
            tasks=None, litellm_metrics=None, litellm_config=None, server_version=None,
            sources={"session-kv": False, "litellm-metrics": False},
        )
        text = fs.render(snapshot)
        self.assertIn("tasks (14 days): unavailable", text)
        self.assertIn("tokens: unavailable", text)
        self.assertIn("model: unavailable", text)
        self.assertIn("kubernetes: unavailable", text)
        self.assertLessEqual(len(text.splitlines()), fs.SUMMARY_MAX_LINES)

    def test_an_empty_usage_block_prints_zeroes_not_blanks(self):
        cr = json.loads(json.dumps(CR))
        cr["status"] = {}
        text = fs.render(full_snapshot(cr=cr))
        self.assertIn("toolExecutionsTotal: 0", text)
        self.assertIn("lastActive: never", text)
        self.assertIn("ready: unknown", text)

    def test_next_mode_and_interfaces_are_printed_as_enums(self):
        cr = json.loads(json.dumps(CR))
        cr["spec"]["mode"] = "next"
        cr["status"]["usage"]["activeInterfaces"] = ["slack", "googlechat"]
        text = fs.render(full_snapshot(cr=cr))
        self.assertIn("mode: next", text)
        self.assertIn("interfaces: googlechat, slack", text)


class ParsingTest(unittest.TestCase):
    def test_prometheus_text_parses_names_labels_and_values(self):
        samples = fs.parse_prometheus(LITELLM_METRICS)
        spend = [s for s in samples if s.name == "litellm_spend_metric_total"]
        self.assertEqual(len(spend), 1)
        self.assertEqual(spend[0].labels["model"], "claude-opus-5")
        self.assertAlmostEqual(spend[0].value, 2.907)

    def test_histogram_quantiles_interpolate_within_the_bucket(self):
        samples = fs.parse_prometheus(LITELLM_METRICS)
        p50, p95 = fs.histogram_quantiles(samples, "litellm_request_total_latency_metric", (0.5, 0.95))
        self.assertAlmostEqual(p50, 1.0 + (5.0 - 1.0) * ((5 - 3) / 6), places=3)
        self.assertAlmostEqual(p95, 5.0 + (30.0 - 5.0) * ((9.5 - 9) / 1), places=3)

    def test_a_histogram_with_no_samples_yields_nothing(self):
        self.assertIsNone(fs.histogram_quantiles([], "litellm_request_total_latency_metric", (0.5,)))

    def test_the_model_line_is_read_from_the_gateway_config(self):
        self.assertEqual(fs.model_from_litellm_config(LITELLM_CONFIG_YAML), "vertex_ai/claude-opus-5")
        self.assertIsNone(fs.model_from_litellm_config("model_list: []\n"))
        self.assertIsNone(fs.model_from_litellm_config(None))


class CollectTest(unittest.TestCase):
    """The collector runs kubectl; every optional source failing leaves the CR-only summary."""

    def fake_run(self, outputs):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            key = " ".join(args)
            for needle, result in outputs.items():
                if needle in key:
                    if isinstance(result, Exception):
                        raise result
                    return subprocess.CompletedProcess(args, 0, stdout=result, stderr="")
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")

        return run, calls

    def test_the_cr_is_required_and_the_rest_is_optional(self):
        run, calls = self.fake_run({
            "get platformagent": json.dumps(CR),
            "version -o json": json.dumps(SERVER_VERSION),
            "get configmap": json.dumps({"items": [
                {"metadata": {"name": "other"}, "data": {"config.yaml": "model: x/y\n"}},
                {"metadata": {"name": "litellm-config-k4b9ht22c7"}, "data": {"config.yaml": LITELLM_CONFIG_YAML}},
            ]}),
            "get pods": json.dumps({"items": [{"metadata": {"name": "platform-agent-gateway-x"}, "status": {"phase": "Running"}}]}),
            "/v1/tasks": json.dumps({"tasks": TASKS, "truncated": False, "board": True}),
            "/metrics": LITELLM_METRICS,
        })
        with mock.patch.object(fs.subprocess, "run", run):
            snapshot = fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW))
        self.assertEqual(snapshot.sources, {"session-kv": True, "litellm-metrics": True})
        text = fs.render(snapshot)
        self.assertIn("tasks (14 days): 5", text)
        self.assertIn("model: vertex_ai/claude-opus-5", text)
        self.assertEqual(fs.audit(text, [CLUSTER, PROJECT, NAMESPACE]), [])
        exec_calls = [c for c in calls if "exec" in c]
        self.assertEqual(len(exec_calls), 2)
        self.assertTrue(all("-c" in c and "platform-agent" in c for c in exec_calls))
        self.assertTrue(all(c[0] == "kubectl" for c in calls))

    def test_without_the_cr_the_collector_fails_loudly(self):
        run, _ = self.fake_run({})
        with mock.patch.object(fs.subprocess, "run", run):
            with self.assertRaises(fs.CollectError) as ctx:
                fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW))
        self.assertIn("platformagent", str(ctx.exception))

    def test_optional_sources_failing_leave_unavailable_lines(self):
        run, _ = self.fake_run({
            "get platformagent": json.dumps(CR),
            "get pods": subprocess.TimeoutExpired(cmd="kubectl", timeout=1),
        })
        with mock.patch.object(fs.subprocess, "run", run):
            snapshot = fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW))
        self.assertEqual(snapshot.sources, {"session-kv": False, "litellm-metrics": False})
        text = fs.render(snapshot)
        self.assertIn("tasks (14 days): unavailable", text)
        self.assertIn("tokens: unavailable", text)

    def test_no_exec_skips_the_in_pod_sources(self):
        run, calls = self.fake_run({"get platformagent": json.dumps(CR)})
        with mock.patch.object(fs.subprocess, "run", run):
            fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW, in_pod=False))
        self.assertFalse(any("exec" in c for c in calls))
        self.assertFalse(any("get pods" in " ".join(c) for c in calls))

    def test_context_and_kubeconfig_reach_every_kubectl_call(self):
        run, calls = self.fake_run({"get platformagent": json.dumps(CR)})
        with mock.patch.object(fs.subprocess, "run", run):
            fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW,
                                  context="ctx", kubeconfig="/tmp/kc", in_pod=False))
        for c in calls:
            self.assertIn("--context", c)
            self.assertIn("--kubeconfig", c)


class MainTest(unittest.TestCase):
    def test_main_prints_the_block_and_refuses_a_leaked_identifier(self):
        snapshot = full_snapshot()
        out = io.StringIO()
        with mock.patch.object(fs, "collect", return_value=snapshot):
            code = fs.main(["--namespace", NAMESPACE], stdout=out)
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("```"))
        leaked = mock.patch.object(fs, "render", return_value=f"```\noops {CLUSTER}\n```")
        err = io.StringIO()
        with mock.patch.object(fs, "collect", return_value=snapshot), leaked:
            code = fs.main(["--namespace", NAMESPACE], stdout=io.StringIO(), stderr=err)
        self.assertEqual(code, fs.EXIT_LEAK)
        self.assertIn("refused", err.getvalue())


if __name__ == "__main__":
    unittest.main()
