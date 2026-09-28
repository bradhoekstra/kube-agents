"""The credential broker's Prometheus surface.

A metrics-only listener serves three families: brokered tool invocations by
tool, subcommand and outcome; their wall-clock latency; and the credentialed
listener's requests by route family and status code. Two properties carry the
security argument and are what these tests hold: nothing a caller sends reaches
a label value, and the listener serves nothing but the exposition.

Run: python3 -m unittest test_credential_proxy_metrics -v
"""

import contextlib
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import credential_proxy
from credential_proxy import (
    CommandExecutor,
    CredentialProxyHandler,
    MetricsHandler,
    Policy,
    ProxyMetrics,
)

# The vocabulary every label value must come from: lower-case words and
# hyphens for tool, subcommand and status; a route prefix or `other` for the
# endpoint; three digits for the status code. A caller-supplied string that
# slipped into a label would fail all three.
_WORD_LABEL = re.compile(r"^[a-z0-9-]{1,32}$")
_ENDPOINT_LABEL = re.compile(r"^(/[a-z0-9/-]+|other)$")
_STATUS_CODE_LABEL = re.compile(r"^[0-9]{3}$")
_SERIES = re.compile(r"^(?P<name>[a-z_]+)(?:\{(?P<labels>[^}]*)\})? (?P<value>-?[0-9.]+(?:e[+-]?[0-9]+)?)$")
_LABEL_PAIR = re.compile(r'([a-z_]+)="((?:[^"\\]|\\.)*)"')
_STUB_FAILING_EXIT = 3


def _parse(exposition):
    """The exposition as {name: {frozenset(label pairs): value}}, checking its shape."""
    families = {}
    typed = set()
    for line in exposition.splitlines():
        if line.startswith("# TYPE "):
            typed.add(line.split()[2])
            continue
        if line.startswith("#"):
            continue
        match = _SERIES.match(line)
        assert match, f"not a Prometheus text line: {line!r}"
        labels = frozenset(_LABEL_PAIR.findall(match.group("labels") or ""))
        families.setdefault(match.group("name"), {})[labels] = float(match.group("value"))
    for name in families:
        base = re.sub(r"_(bucket|sum|count)$", "", name)
        assert base in typed, f"{name} has no # TYPE line"
    return families


def _series(families, name, **labels):
    return families.get(name, {}).get(frozenset(labels.items()))


class _BrokerFixture(unittest.TestCase):
    """A real broker over TCP with a stub kubectl, and a fresh registry per test."""

    def setUp(self):
        for attribute in ("policy", "executor", "enforce_read_only", "max_request_bytes", "authenticator", "metrics"):
            self.addCleanup(
                self._restore,
                attribute,
                attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        policy_path = Path(self.temp_dir.name) / "policy.json"
        policy_path.write_text(json.dumps({"blockedMessage": "blocked", "rules": []}), encoding="utf-8")
        CredentialProxyHandler.policy = Policy.load(str(policy_path))
        CredentialProxyHandler.executor = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=4096,
            state_dir=str(Path(self.temp_dir.name) / "state"),
            scoped_pool=None,
        )
        stub_dir = Path(self.temp_dir.name) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "kubectl"
        # `kubectl get failing` exits non-zero through an allowed verb, so the
        # command runs and fails rather than being refused before it starts.
        stub.write_text(
            "#!/bin/bash\n"
            f'case "$*" in *failing*) exit {_STUB_FAILING_EXIT} ;; esac\n'
            "echo pods\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        CredentialProxyHandler.executor.executables["kubectl"] = str(stub)
        CredentialProxyHandler.max_request_bytes = 65536
        CredentialProxyHandler.enforce_read_only = True
        CredentialProxyHandler.authenticator = credential_proxy.NullAuthenticator()
        CredentialProxyHandler.metrics = ProxyMetrics()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    @staticmethod
    def _restore(attribute, present, value):
        if present:
            setattr(CredentialProxyHandler, attribute, value)
        else:
            with contextlib.suppress(AttributeError):
                delattr(CredentialProxyHandler, attribute)

    def post(self, argv, **extra):
        payload = {"requestId": "req-1", "argv": argv, **extra}
        request = urllib.request.Request(
            self.endpoint + "/v1/exec",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def get(self, path):
        try:
            with urllib.request.urlopen(self.endpoint + path) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def families(self):
        return _parse(CredentialProxyHandler.metrics.render())


class ToolInvocationCountingTest(_BrokerFixture):
    def test_a_completed_command_counts_as_success_and_is_timed(self):
        status, body = self.post(["kubectl", "get", "pods"])
        self.assertEqual((200, "completed", 0), (status, body["status"], body["exitCode"]))
        families = self.families()
        self.assertEqual(
            1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="success")
        )
        self.assertEqual(1, _series(families, "kubeagents_tool_execution_duration_seconds_count", tool="kubectl"))
        self.assertEqual(1, _series(families, "kubeagents_tool_execution_duration_seconds_bucket", tool="kubectl", le="+Inf"))

    def test_a_non_zero_exit_counts_as_error(self):
        # The response still says `completed`: it reports that the broker ran
        # the command, the counter reports how the command went.
        status, body = self.post(["kubectl", "get", "failing"])
        self.assertEqual((200, "completed", _STUB_FAILING_EXIT), (status, body["status"], body["exitCode"]))
        families = self.families()
        self.assertEqual(
            1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="error")
        )
        self.assertIsNone(
            _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="success")
        )

    def test_a_refused_command_counts_as_blocked_under_its_verb_and_is_not_timed(self):
        status, body = self.post(["kubectl", "delete", "pod", "x"])
        self.assertEqual((403, "blocked"), (status, body["status"]))
        families = self.families()
        self.assertEqual(
            1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="delete", status="blocked")
        )
        self.assertNotIn("kubeagents_tool_execution_duration_seconds_count", families)

    def test_an_unserved_executable_is_counted_as_other_and_never_named(self):
        status, body = self.post(["bash", "-c", "id"])
        self.assertEqual((403, "executable.allowlist"), (status, body["rule"]))
        exposition = CredentialProxyHandler.metrics.render()
        self.assertNotIn("bash", exposition)
        self.assertEqual(
            1, _series(_parse(exposition), "kubeagents_tool_invocations_total", tool="other", subcommand="other", status="blocked")
        )

    def test_caller_text_never_reaches_a_label(self):
        # A verb the vocabulary does not list, a namespace with a quote in it,
        # and a flag the policy does not know: each ends up under `other` and
        # none of the caller's own strings appears in the exposition.
        self.post(["kubectl", 'weirdverb"x', "pods"])
        self.post(["kubectl", "get", "pods", "--namespace", 'evil"ns'])
        self.post(["kubectl", "--nosuchflag", "value", "get", "pods"])
        exposition = CredentialProxyHandler.metrics.render()
        for text in ("weirdverb", "evil", "nosuchflag"):
            self.assertNotIn(text, exposition)
        families = _parse(exposition)
        for labels in families["kubeagents_tool_invocations_total"]:
            for key, value in labels:
                self.assertRegex(value, _WORD_LABEL, f"{key}={value!r} is not vocabulary")
        self.assertGreaterEqual(
            _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="other", status="blocked"), 2
        )


class RequestCountingTest(_BrokerFixture):
    def test_requests_are_counted_by_route_family_and_status_code(self):
        self.post(["kubectl", "get", "pods"])
        self.post(["kubectl", "delete", "pod", "x"])
        self.assertEqual(200, self.get("/healthz"))
        self.assertEqual(404, self.get("/no/such/route"))
        families = self.families()
        counts = families["kubeagents_credential_proxy_requests_total"]
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="/v1/exec", status_code="200"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="/v1/exec", status_code="403"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="/healthz", status_code="200"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="other", status_code="404"))
        for labels in counts:
            pairs = dict(labels)
            self.assertRegex(pairs["endpoint"], _ENDPOINT_LABEL)
            self.assertRegex(pairs["status_code"], _STATUS_CODE_LABEL)

    def test_the_path_itself_is_never_a_label(self):
        self.get("/v1/gcp/monitoring.googleapis.com/v3/projects/secret-project/timeSeries?filter=x")
        exposition = CredentialProxyHandler.metrics.render()
        self.assertNotIn("secret-project", exposition)
        self.assertNotIn("timeSeries", exposition)
        self.assertIn('endpoint="/v1/gcp"', exposition)


class MetricsListenerTest(unittest.TestCase):
    def setUp(self):
        previous = CredentialProxyHandler.__dict__.get("metrics")
        self.addCleanup(setattr, CredentialProxyHandler, "metrics", previous)
        CredentialProxyHandler.metrics = ProxyMetrics()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MetricsHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def test_metrics_are_served_unauthenticated_in_the_text_exposition(self):
        CredentialProxyHandler.metrics.record_tool("kubectl", "get", "success")
        CredentialProxyHandler.metrics.observe_duration("kubectl", 0.24)
        with urllib.request.urlopen(self.endpoint + "/metrics") as response:
            self.assertEqual(200, response.status)
            self.assertEqual(credential_proxy.METRICS_CONTENT_TYPE, response.headers["Content-Type"])
            self.assertNotIn("Python", response.headers.get("Server", ""))
            body = response.read().decode("utf-8")
        families = _parse(body)
        self.assertEqual(1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="success"))
        # Cumulative buckets, in bound order, ending at the count.
        buckets = [
            _series(families, "kubeagents_tool_execution_duration_seconds_bucket", tool="kubectl", le=str(bound))
            for bound in credential_proxy.TOOL_DURATION_BUCKETS
        ]
        self.assertEqual(buckets, sorted(buckets))
        self.assertEqual(0, buckets[0])
        self.assertEqual(1, buckets[-1])
        self.assertEqual(1, _series(families, "kubeagents_tool_execution_duration_seconds_bucket", tool="kubectl", le="+Inf"))
        self.assertAlmostEqual(0.24, _series(families, "kubeagents_tool_execution_duration_seconds_sum", tool="kubectl"))
        self.assertIn("# HELP kubeagents_credential_proxy_requests_total", body)

    def test_the_listener_serves_nothing_else(self):
        for path in ("/", "/healthz", "/v1/exec", "/metrics/../v1/exec"):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(self.endpoint + path)
                self.assertEqual(404, caught.exception.code)

    def test_a_scrape_writes_no_access_log_line(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured), self.assertNoLogs(credential_proxy.LOGGER, level="DEBUG"):
            urllib.request.urlopen(self.endpoint + "/metrics").read()
        self.assertEqual("", captured.getvalue())

    def test_two_scrapes_of_an_idle_registry_are_identical(self):
        first = urllib.request.urlopen(self.endpoint + "/metrics").read()
        second = urllib.request.urlopen(self.endpoint + "/metrics").read()
        self.assertEqual(first, second)


class ListenerStartTest(unittest.TestCase):
    def test_an_occupied_port_is_logged_not_raised(self):
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            port = holder.getsockname()[1]
            with self.assertLogs(credential_proxy.LOGGER, level="ERROR") as logs:
                self.assertIsNone(credential_proxy.start_metrics_listener("127.0.0.1", port))
        self.assertTrue(any("ALERT" in line and "/metrics" in line for line in logs.output), logs.output)

    def test_a_free_port_is_served_on_a_daemon_thread(self):
        server = credential_proxy.start_metrics_listener("127.0.0.1", 0)
        self.assertIsNotNone(server)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/metrics") as response:
            self.assertEqual(200, response.status)

    def test_the_port_comes_from_the_operators_variable_and_defaults_off(self):
        with mock.patch.object(sys, "argv", ["credential_proxy.py"]):
            with mock.patch.dict(os.environ, {credential_proxy.METRICS_PORT_ENV: ""}):
                self.assertEqual(0, credential_proxy.parse_args().metrics_port)
            with mock.patch.dict(os.environ, {credential_proxy.METRICS_PORT_ENV: "8766"}):
                self.assertEqual(8766, credential_proxy.parse_args().metrics_port)
            os.environ.pop(credential_proxy.METRICS_PORT_ENV, None)
            self.assertEqual(0, credential_proxy.parse_args().metrics_port)


class LabelDerivationTest(unittest.TestCase):
    def test_tool_and_subcommand_labels(self):
        cases = {
            ("kubectl", "get", "pods"): ("kubectl", "get"),
            ("kubectl", "--namespace", "foo", "get", "pods"): ("kubectl", "get"),
            ("kubectl", "rollout", "status", "deploy/x"): ("kubectl", "rollout"),
            ("kubectl", "apply", "-f", "x.yaml"): ("kubectl", "apply"),
            ("kubectl", "--nosuchflag", "x", "get"): ("kubectl", "other"),
            ("kubectl", "gett", "pods"): ("kubectl", "other"),
            ("kubectl",): ("kubectl", "none"),
            ("gcloud", "container", "clusters", "get-credentials", "c"): ("gcloud", "container"),
            ("gcloud", "beta", "compute", "instances", "list"): ("gcloud", "compute"),
            ("gcloud",): ("gcloud", "none"),
            ("git", "-C", "/tmp/x", "status"): ("git", "status"),
            ("git", "rev-parse", "HEAD"): ("git", "rev-parse"),
            ("git", "not-a-verb"): ("git", "other"),
            ("gh", "pr", "list"): ("gh", "pr"),
            ("gh", "--version"): ("gh", "none"),
            ("bash", "-c", "id"): ("other", "other"),
        }
        for argv, want in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(want, credential_proxy._tool_labels(list(argv)))

    def test_every_vocabulary_word_is_a_valid_label(self):
        for tool in ("kubectl", "gcloud", "git", "gh"):
            for word in credential_proxy._subcommand_vocabulary(tool):
                with self.subTest(tool=tool, word=word):
                    self.assertRegex(word, _WORD_LABEL)

    def test_endpoint_labels(self):
        cases = {
            "/v1/exec": "/v1/exec",
            "/v1/chat/events": "/v1/chat",
            "/v1/chat/a2a/events": "/v1/chat/a2a",
            "/v1/chat/api": "/v1/chat/api",
            "/v1/gcp/monitoring.googleapis.com/v3/x?y=z": "/v1/gcp",
            "/v1/vcs/push": "/v1/vcs",
            "/v1/workspace/acquire": "/v1/workspace",
            "/v1/forge/refresh": "/v1/forge",
            "/healthz": "/healthz",
            "/metrics": "other",
            "": "other",
            "/v1/chatter": "other",
        }
        for path, want in cases.items():
            with self.subTest(path=path):
                self.assertEqual(want, credential_proxy._endpoint_label(path))


class RegistryTest(unittest.TestCase):
    def test_concurrent_increments_are_not_lost(self):
        metrics = ProxyMetrics()
        per_thread, threads = 500, 8

        def work():
            for _ in range(per_thread):
                metrics.record_tool("kubectl", "get", "success")
                metrics.observe_duration("kubectl", 0.01)
                metrics.record_request("/v1/exec", "200")

        workers = [threading.Thread(target=work) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        families = _parse(metrics.render())
        self.assertEqual(per_thread * threads, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="success"))
        self.assertEqual(per_thread * threads, _series(families, "kubeagents_tool_execution_duration_seconds_count", tool="kubectl"))
        self.assertEqual(per_thread * threads, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="/v1/exec", status_code="200"))

    def test_label_values_are_escaped(self):
        metrics = ProxyMetrics()
        metrics.record_request('quote"back\\slash\nnewline', "200")
        rendered = metrics.render()
        self.assertIn('endpoint="quote\\"back\\\\slash\\nnewline"', rendered)
        self.assertEqual(1, len([line for line in rendered.splitlines() if line.startswith("kubeagents_credential_proxy_requests_total{")]))


if __name__ == "__main__":
    unittest.main()
