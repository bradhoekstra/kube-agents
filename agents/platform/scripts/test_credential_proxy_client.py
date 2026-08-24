#!/usr/bin/env python3
"""Tests for the credential proxy client shim.

The shim is what every `kubectl`/`gcloud`/`gh`/`git` in the agent container
actually is, so what it puts in the request body decides whether a command
reaches the right cluster - or is rejected outright.

Run:  python3 agents/platform/scripts/test_credential_proxy_client.py
"""

import base64
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import credential_proxy_client


class RecordingResponse(io.BytesIO):
    """Stand-in for the urlopen context manager the client reads."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SubmittedPayloadTestCase(unittest.TestCase):
    # A sidecar proxy. Whether the endpoint is loopback decides whether the
    # client sends paths at all, so it is part of every case below.
    LOCAL_ENDPOINT = "http://127.0.0.1:8765"

    def submit(self, argv, environ, endpoint=LOCAL_ENDPOINT):
        """Run the client against a stubbed proxy, returning the request body."""
        captured = {}

        def fake_urlopen(request, *args, **kwargs):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return RecordingResponse(json.dumps({"exitCode": 0}).encode("utf-8"))

        with patch.dict("os.environ", environ, clear=False):
            with patch.object(credential_proxy_client.urllib.request, "urlopen", fake_urlopen):
                with patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=io.StringIO()):
                    credential_proxy_client.execute(endpoint, argv)
        return captured["payload"]


class TestKubeconfigForwarding(SubmittedPayloadTestCase):
    PINNED = "/opt/data/profiles/cluster-a/kubeconfig.yaml"

    def test_kubectl_carries_the_pin(self):
        # The whole point of the forward: a Cluster Agent's pinned kubeconfig
        # has to reach the sidecar, which does not inherit the caller's env.
        payload = self.submit(["kubectl", "get", "pods"], {"KUBECONFIG": self.PINNED})
        self.assertEqual(payload["kubeconfig"], self.PINNED)

    def test_gcloud_carries_the_pin(self):
        # gcloud writes it: `container clusters get-credentials` renders the
        # kubeconfig at $KUBECONFIG, which is how switch_kube_context works.
        payload = self.submit(["gcloud", "container", "clusters", "get-credentials", "c"],
                              {"KUBECONFIG": self.PINNED})
        self.assertEqual(payload["kubeconfig"], self.PINNED)

    def test_git_and_gh_do_not(self):
        # Neither reads KUBECONFIG, and the server rejects an out-of-workspace
        # path rather than ignoring it - so forwarding it here would 400 a
        # command that has nothing to do with Kubernetes.
        for argv in (["git", "status"], ["gh", "pr", "list"]):
            with self.subTest(argv=argv):
                payload = self.submit(argv, {"KUBECONFIG": "/tmp/somewhere.yaml"})
                self.assertNotIn("kubeconfig", payload)

    def test_absent_when_unset(self):
        payload = self.submit(["kubectl", "get", "pods"], {"KUBECONFIG": ""})
        self.assertNotIn("kubeconfig", payload)

    def test_trailing_newline_is_stripped(self):
        # Profile .env files routinely carry one, and an unstripped value fails
        # the server's containment check on a path that is actually fine.
        payload = self.submit(["kubectl", "get", "pods"], {"KUBECONFIG": self.PINNED + "\n"})
        self.assertEqual(payload["kubeconfig"], self.PINNED)


class TestCrossPodCallerSendsNoPaths(SubmittedPayloadTestCase):
    """A path only means something when both ends share a filesystem.

    The sandbox calls the proxy over a Service, and its `/opt/data` is its own
    volume. Sending either path field would have the server resolve it against
    a filesystem where it names nothing, or something else.
    """

    REMOTE_ENDPOINT = "http://agent-credential-proxy.kubeagents-system.svc.cluster.local:8765"

    def test_no_cwd(self):
        payload = self.submit(["kubectl", "get", "pods"], {}, endpoint=self.REMOTE_ENDPOINT)
        self.assertNotIn("cwd", payload)

    def test_no_kubeconfig(self):
        payload = self.submit(
            ["kubectl", "get", "pods"],
            {"KUBECONFIG": "/opt/data/profiles/cluster-a/kubeconfig.yaml"},
            endpoint=self.REMOTE_ENDPOINT,
        )
        self.assertNotIn("kubeconfig", payload)

    def test_a_sidecar_still_sends_its_cwd(self):
        # The loopback case has to keep working: the workspace containment
        # check and the git lease check are both driven by this field.
        payload = self.submit(["kubectl", "get", "pods"], {})
        self.assertIn("cwd", payload)


class TestSharesFilesystemWithProxy(unittest.TestCase):
    def test_loopback_hosts(self):
        for endpoint in ("http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(credential_proxy_client.shares_filesystem_with_proxy(endpoint))

    def test_a_service_name_is_not_loopback(self):
        for endpoint in ("http://agent-credential-proxy:8765", "http://10.4.0.7:8765"):
            with self.subTest(endpoint=endpoint):
                self.assertFalse(credential_proxy_client.shares_filesystem_with_proxy(endpoint))


class StdinGateTest(unittest.TestCase):
    """`-f -` has never worked in any topology. These bind the narrow fix."""

    def test_recognises_an_explicit_request_for_stdin(self):
        for argv in (
            ["kubectl", "apply", "-f", "-"],
            ["kubectl", "apply", "--filename", "-"],
            ["kubectl", "apply", "--filename=-"],
            ["kubectl", "patch", "deploy/x", "--patch-file", "-"],
            ["gh", "pr", "create", "--title", "t", "--body-file", "-"],
            ["gh", "issue", "create", "--body-file=-"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(credential_proxy_client.reads_stdin(argv))

    def test_leaves_every_other_argv_alone(self):
        """The MCP protocol-stream hazard is why this list stays short."""
        for argv in (
            ["kubectl", "get", "ns"],
            ["kubectl", "apply", "-f", "manifest.yaml"],
            ["gh", "pr", "list"],
            ["git", "log", "-"],
            ["kubectl", "logs", "-f", "pod/x"],
            ["gh", "pr", "create", "--body", "-"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(credential_proxy_client.reads_stdin(argv))

    def test_a_terminal_on_fd_zero_is_not_read(self):
        """Otherwise an interactive `-f -` hangs and reads as the proxy being down."""

        class Tty(io.StringIO):
            def isatty(self):
                return True

        with patch.object(sys, "stdin", Tty("ignored")):
            self.assertIsNone(
                credential_proxy_client.read_stdin_if_requested(
                    ["kubectl", "apply", "-f", "-"]
                )
            )

    def test_a_pipe_on_fd_zero_is_forwarded(self):
        with patch.object(sys, "stdin", io.StringIO("kind: ConfigMap\n")):
            self.assertEqual(
                credential_proxy_client.read_stdin_if_requested(
                    ["kubectl", "apply", "-f", "-"]
                ),
                "kind: ConfigMap\n",
            )

    def test_stdin_reaches_the_request_body(self):
        captured = {}

        def fake_urlopen(request):
            captured["body"] = json.loads(request.data)
            return RecordingResponse(json.dumps({"exitCode": 0}).encode())

        with patch("urllib.request.urlopen", fake_urlopen):
            credential_proxy_client.execute(
                "http://127.0.0.1:8765", ["kubectl", "apply", "-f", "-"], stdin="kind: X\n"
            )
        self.assertEqual(captured["body"]["stdin"], "kind: X\n")


class WorkspaceClientTest(unittest.TestCase):
    """The client half of content-passing. No path crosses this boundary."""

    def setUp(self):
        self.endpoint = "http://127.0.0.1:8765"
        self.calls = []

    def _serve(self, answers):
        def fake_urlopen(request):
            body = json.loads(request.data)
            self.calls.append((request.full_url, body))
            verb = request.full_url.rsplit("/", 1)[-1]
            return RecordingResponse(json.dumps(answers[verb]).encode())

        return patch("urllib.request.urlopen", fake_urlopen)

    def test_open_commit_push_close(self):
        answers = {
            "open": {
                "handle": "a" * 32,
                "repo": "acme/infra",
                "base": "main",
                "baseSha": "b" * 40,
            },
            "commit": {
                "committed": True,
                "branch": "fix/x",
                "base": "main",
                "baseSha": "c" * 40,
                "commit": "d" * 40,
            },
            "push": {"pushed": True, "branch": "fix/x", "commit": "d" * 40},
            "close": {"closed": True},
        }
        with self._serve(answers):
            with credential_proxy_client.Workspace.open(
                self.endpoint, "acme/infra"
            ) as workspace:
                workspace.commit(
                    branch="fix/x",
                    message="m",
                    changes={"a.yaml": b"kind: X\n", "gone.yaml": None},
                    expected_base_sha=workspace.base_sha,
                )
                workspace.push()

        verbs = [url.rsplit("/", 1)[-1] for url, _ in self.calls]
        self.assertEqual(verbs, ["open", "commit", "push", "close"])
        commit_body = self.calls[1][1]
        self.assertEqual(commit_body["expectedBaseSha"], "b" * 40)
        entries = {entry["path"]: entry for entry in commit_body["changes"]}
        self.assertEqual(
            base64.b64decode(entries["a.yaml"]["contentBase64"]), b"kind: X\n"
        )
        self.assertTrue(entries["gone.yaml"]["delete"])
        self.assertNotIn("contentBase64", entries["gone.yaml"])

    def test_a_disabled_broker_is_distinguishable_from_a_refusal(self):
        """Callers that can do either need to tell "off" from "no"."""

        def disabled(request):
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "Not Found",
                {},
                io.BytesIO(
                    json.dumps(
                        {"error": "not enabled", "code": "CONTENT_WORKSPACES_DISABLED"}
                    ).encode()
                ),
            )

        with patch("urllib.request.urlopen", disabled):
            with self.assertRaises(credential_proxy_client.WorkspaceUnavailable):
                credential_proxy_client.Workspace.open(self.endpoint, "acme/infra")
            self.assertFalse(credential_proxy_client.workspaces_available(self.endpoint))

    def test_a_refusal_carries_the_brokers_answer_through(self):
        def conflict(request):
            raise urllib.error.HTTPError(
                request.full_url,
                409,
                "Conflict",
                {},
                io.BytesIO(
                    json.dumps(
                        {
                            "error": "the base branch moved",
                            "code": "BASE_MOVED",
                            "paths": ["manifests/app.yaml"],
                        }
                    ).encode()
                ),
            )

        with patch("urllib.request.urlopen", conflict):
            with self.assertRaises(
                credential_proxy_client.WorkspaceRequestError
            ) as caught:
                credential_proxy_client.Workspace.open(self.endpoint, "acme/infra")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.payload["code"], "BASE_MOVED")
        self.assertEqual(
            caught.exception.payload["paths"], ["manifests/app.yaml"]
        )

    def test_push_before_commit_is_refused_client_side(self):
        answers = {
            "open": {
                "handle": "a" * 32,
                "repo": "acme/infra",
                "base": "main",
                "baseSha": "b" * 40,
            },
            "close": {"closed": True},
        }
        with self._serve(answers):
            workspace = credential_proxy_client.Workspace.open(
                self.endpoint, "acme/infra"
            )
            with self.assertRaises(ValueError):
                workspace.push()


if __name__ == "__main__":
    unittest.main(verbosity=2)
