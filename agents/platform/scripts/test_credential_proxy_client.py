#!/usr/bin/env python3
"""Tests for the credential proxy client shim.

The shim is what every `kubectl`/`gcloud`/`gh`/`git` in the agent container
actually is, so what it puts in the request body decides whether a command
reaches the right cluster - or is rejected outright.

Run:  python3 agents/platform/scripts/test_credential_proxy_client.py
"""

import io
import json
import sys
import unittest
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
