#!/usr/bin/env python3
"""Tests for pin_smoke_status.py -- the re-pinning that keeps a green smoke run valid.

Run: cd scripts && python3 -m unittest test_pin_smoke_status

Every wrong answer here fails green: a status that should have been pinned is
left stale and Tide quietly re-runs a 1.5-3.5h job; a status that should have
been left alone -- a red, an override, a newer `pending` -- is overwritten with
a success. So the guards get one test each, and the description builder is
driven with what crier actually writes, U+2001 padding included.
"""

import sys
import unittest
import unittest.mock
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import pin_smoke_status as pin

MAIN = "a" * 40
OLD = "b" * 40
HEAD = "c" * 40
CRIER = "Job succeeded." + " " * 20 + "BaseSHA:" + OLD
URL = "https://oss.gprow.dev/view/gs/kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/1/pull-kube-agents-smoke-test/1"


def status(**overrides):
    base = {"id": 1, "context": pin.CONTEXT, "state": "success", "description": CRIER, "target_url": URL}
    base.update(overrides)
    return base


class FakeAPI:
    """Just enough of `GitHubAPI` for the functions that call it."""

    def __init__(self, statuses=None, pulls=()):
        self.repo = "gke-labs/kube-agents"
        self.statuses = statuses or {}
        self.pulls = list(pulls)
        self.posts = []

    def get(self, path):
        if path.endswith(f"/git/ref/{pin.MAIN_REF}"):
            return {"object": {"sha": MAIN}}
        sha = path.rsplit("/commits/", 1)[1].split("/")[0]
        return {"statuses": self.statuses.get(sha, [])}

    def get_all(self, path):
        assert path.endswith("/pulls?state=open"), path
        return self.pulls

    def post(self, path, payload):
        self.posts.append((path, payload))


class DescriptionTest(unittest.TestCase):
    def test_crier_padding_is_collapsed_and_the_base_replaced(self):
        got = pin.pinned_description(CRIER, MAIN)
        self.assertEqual(got, f"Job succeeded. {pin.PIN_NOTE} BaseSHA:{MAIN}")
        self.assertLessEqual(len(got), pin.MAX_DESCRIPTION)

    def test_the_base_sha_still_parses_the_way_prow_reads_it(self):
        human, sha = pin.split_description(pin.pinned_description(CRIER, MAIN))
        self.assertEqual(sha, MAIN)
        self.assertEqual(human, "Job succeeded.")

    def test_re_pinning_does_not_stack_the_note(self):
        twice = pin.pinned_description(pin.pinned_description(CRIER, OLD), MAIN)
        self.assertEqual(twice.count(pin.PIN_NOTE), 1)

    def test_a_long_human_part_is_cut_ahead_of_the_suffix(self):
        got = pin.pinned_description("x" * 200, MAIN)
        self.assertEqual(len(got), pin.MAX_DESCRIPTION)
        self.assertTrue(got.endswith(f"BaseSHA:{MAIN}"))

    def test_a_description_without_a_base_gets_one(self):
        self.assertEqual(pin.split_description("Job succeeded.")[1], None)
        self.assertTrue(pin.pinned_description("Job succeeded.", MAIN).endswith(MAIN))

    def test_a_malformed_base_is_not_mistaken_for_one(self):
        self.assertIsNone(pin.split_description("Job succeeded. BaseSHA:ABC")[1])


class GuardTest(unittest.TestCase):
    def test_a_stale_green_is_pinned(self):
        self.assertIsNone(pin.skip_reason(status(), MAIN))

    def test_a_green_already_at_main_is_left_alone(self):
        self.assertIn("already pinned", pin.skip_reason(status(description=f"Job succeeded. BaseSHA:{MAIN}"), MAIN))

    def test_anything_not_green_is_left_alone(self):
        for state in ("pending", "failure", "error"):
            self.assertIn(state, pin.skip_reason(status(state=state), MAIN))

    def test_an_admin_override_is_left_alone(self):
        self.assertIn("override", pin.skip_reason(status(description="Overridden by someone BaseSHA:" + OLD), MAIN))

    def test_a_commit_with_no_smoke_status_is_left_alone(self):
        self.assertIn("no pull-kube-agents-smoke-test status", pin.skip_reason(None, MAIN))


class PinHeadTest(unittest.TestCase):
    def test_pins_and_keeps_the_target_url(self):
        api = FakeAPI(statuses={HEAD: [status()]})
        outcome = pin.pin_head(api, HEAD, MAIN, status_id=1)
        self.assertIn("pinned", outcome)
        (path, payload), = api.posts
        self.assertEqual(path, f"/repos/gke-labs/kube-agents/statuses/{HEAD}")
        self.assertEqual(payload["state"], "success")
        self.assertEqual(payload["target_url"], URL)
        self.assertTrue(payload["description"].endswith(MAIN))

    def test_a_newer_status_on_the_context_wins(self):
        """The event's success was followed by a `pending` from a fresh run."""
        api = FakeAPI(statuses={HEAD: [status(id=2, state="pending", description="Job triggered.")]})
        self.assertIn("no longer the latest", pin.pin_head(api, HEAD, MAIN, status_id=1))
        self.assertEqual(api.posts, [])

    def test_only_the_smoke_context_is_read(self):
        api = FakeAPI(statuses={HEAD: [status(context="tide", state="pending"), status()]})
        pin.pin_head(api, HEAD, MAIN)
        self.assertEqual(len(api.posts), 1)

    def test_dry_run_posts_nothing(self):
        api = FakeAPI(statuses={HEAD: [status()]})
        self.assertIn("pinned", pin.pin_head(api, HEAD, MAIN, dry_run=True))
        self.assertEqual(api.posts, [])


class SweepTest(unittest.TestCase):
    def test_only_stale_greens_across_the_open_pull_requests_are_pinned(self):
        stale, current, red, none = "1" * 40, "2" * 40, "3" * 40, "4" * 40
        api = FakeAPI(
            statuses={
                stale: [status()],
                current: [status(description=f"Job succeeded. BaseSHA:{MAIN}")],
                red: [status(state="failure")],
            },
            pulls=[{"head": {"sha": sha}} for sha in (stale, current, red, none)],
        )
        outcomes = pin.sweep(api, MAIN)
        self.assertEqual(len(outcomes), 4)
        self.assertEqual([path.rsplit("/", 1)[1] for path, _ in api.posts], [stale])


class MainTest(unittest.TestCase):
    def test_refuses_to_run_without_a_token(self):
        env = {k: v for k, v in __import__("os").environ.items() if k not in ("GITHUB_TOKEN",)}
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(pin.main(["--repo", "o/r", "sweep"]), 2)


if __name__ == "__main__":
    unittest.main()
