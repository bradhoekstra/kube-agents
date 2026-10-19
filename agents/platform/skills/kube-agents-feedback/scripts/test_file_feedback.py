"""The filing client calls the broker once per confirmed text and never hides a failure.

The broker (agents/platform/scripts/feedback_broker.py) owns the form's wire
path; this script owns the one thing the broker cannot: the memory that this
workspace already asked, kept in a marker written before the call. These tests
hold the marker's contract and the shape of what the agent reads back.

Run:
  python3 -m unittest discover -s agents/platform/skills/kube-agents-feedback/scripts \
      -p 'test_file_feedback.py' -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import file_feedback  # noqa: E402

PAYLOAD = {
    "title": "credential proxy dies silently",
    "kind": "Bug",
    "happened": "the sidecar crash-loops and nothing surfaces it",
    "user_confirmed": True,
}
ISSUE_URL = "https://github.com/gke-labs/kube-agents/issues/9999"
FALLBACK = {
    "tracker": "https://github.com/gke-labs/kube-agents/issues",
    "form": "https://gke-labs.github.io/kube-agents/feedback",
}
SUBMITTED = {"status": "SUBMITTED", "issue_url": ISSUE_URL, "fallback": FALLBACK}
ALREADY = {"status": "ALREADY_SUBMITTED", "issue_url": ISSUE_URL, "fallback": FALLBACK}
BLOCKED_400 = {"status": "BLOCKED", "reason": "kind 'Complaint' is not one of Bug, ...", "fallback": FALLBACK}
BROKER = "http://127.0.0.1:8765"


class FileFeedbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": BROKER})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, payload: dict, broker) -> tuple[int, dict, list]:
        path = self.state_dir / "payload.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        calls: list[dict] = []

        def post(endpoint, body):
            calls.append(body)
            if isinstance(broker, Exception):
                raise broker
            return broker

        out = io.StringIO()
        with mock.patch.object(file_feedback, "_post_to_broker", side_effect=post):
            with contextlib.redirect_stdout(out):
                code = file_feedback.submit(path, self.state_dir)
        return code, json.loads(out.getvalue().strip()), calls

    def _markers(self) -> list[Path]:
        return sorted(self.state_dir.glob(f"{file_feedback.MARKER_PREFIX}*"))

    def test_first_submit_writes_the_marker_and_prints_the_brokers_answer(self) -> None:
        code, printed, calls = self._run(PAYLOAD, (200, SUBMITTED))
        self.assertEqual((0, SUBMITTED), (code, printed))
        self.assertEqual(1, len(calls))
        self.assertNotIn("recheck_only", calls[0])
        (marker,) = self._markers()
        state = json.loads(marker.read_text())
        self.assertEqual((ISSUE_URL, "SUBMITTED"), (state["issue_url"], state["status"]))

    def test_second_submit_of_the_same_text_does_not_call_the_broker(self) -> None:
        self._run(PAYLOAD, (200, SUBMITTED))
        code, printed, calls = self._run(PAYLOAD, (200, SUBMITTED))
        self.assertEqual(0, code)
        self.assertEqual(("ALREADY_SUBMITTED", ISSUE_URL), (printed["status"], printed["issue_url"]))
        self.assertEqual(FALLBACK, printed["fallback"])
        self.assertEqual([], calls, "the duplicate-filing bug, exactly")

    def test_lost_answer_rechecks_only(self) -> None:
        # The first call died after the marker was written; the rerun asks
        # the broker to read the tracker and never to POST.
        with self.assertRaises(SystemExit):
            # A broker that hangs up mid-answer is not a BLOCKED (the POST may
            # have gone out): the marker stays and the script exits non-zero.
            self._run(PAYLOAD, file_feedback.BrokerAnswerLost("connection reset"))
        self.assertEqual(1, len(self._markers()), "the marker outlives a lost answer")
        code, printed, calls = self._run(PAYLOAD, (200, ALREADY))
        self.assertEqual((0, "ALREADY_SUBMITTED"), (code, printed["status"]))
        self.assertEqual([True], [c.get("recheck_only") for c in calls])

    def test_a_broker_error_is_blocked_with_the_links_and_leaves_no_marker(self) -> None:
        for broker in ((503, {}), (502, {"error": "bad gateway"}), urllib.error.URLError("refused")):
            with self.subTest(broker=broker):
                code, printed, _ = self._run(PAYLOAD, broker)
                self.assertEqual((1, "BLOCKED"), (code, printed["status"]))
                self.assertEqual(FALLBACK, printed["fallback"])
                self.assertEqual([], self._markers(), "nothing was sent, so nothing is remembered")

    def test_a_400_is_printed_as_received_and_exits_1(self) -> None:
        code, printed, _ = self._run(dict(PAYLOAD, kind="Complaint"), (400, BLOCKED_400))
        self.assertEqual((1, BLOCKED_400), (code, printed))
        self.assertEqual([], self._markers())

    def test_a_missing_token_is_blocked_and_leaves_no_marker(self) -> None:
        path = self.state_dir / "payload.json"
        path.write_text(json.dumps(PAYLOAD), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(
            file_feedback.credential_proxy_client, "authorization_headers",
            side_effect=file_feedback.credential_proxy_client.TokenUnavailable("empty"),
        ):
            with contextlib.redirect_stdout(out):
                code = file_feedback.submit(path, self.state_dir)
        printed = json.loads(out.getvalue().strip())
        self.assertEqual((1, "BLOCKED"), (code, printed["status"]))
        self.assertIn("token", printed["reason"])
        self.assertEqual(FALLBACK, printed["fallback"])
        self.assertEqual([], self._markers())

    def test_submitted_without_a_url_is_rechecked_on_the_next_run(self) -> None:
        pending = {"status": "SUBMITTED", "issue_url": None, "detail": "not on the tracker yet", "fallback": FALLBACK}
        code, printed, _ = self._run(PAYLOAD, (200, pending))
        self.assertEqual((0, pending), (code, printed))
        code, printed, calls = self._run(PAYLOAD, (200, ALREADY))
        self.assertEqual((0, "ALREADY_SUBMITTED"), (code, printed["status"]))
        self.assertEqual([True], [c.get("recheck_only") for c in calls])
        (marker,) = self._markers()
        self.assertEqual(ISSUE_URL, json.loads(marker.read_text())["issue_url"])

    def test_missing_endpoint_is_blocked(self) -> None:
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}):
            code, printed, calls = self._run(PAYLOAD, (200, SUBMITTED))
        self.assertEqual((1, "BLOCKED"), (code, printed["status"]))
        self.assertIn("CREDENTIAL_PROXY_URL", printed["reason"])
        self.assertEqual(([], []), (calls, self._markers()))


if __name__ == "__main__":
    unittest.main()
