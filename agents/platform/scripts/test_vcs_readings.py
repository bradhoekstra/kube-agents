#!/usr/bin/env python3
"""Tests for the readings sweep (`vcs_readings.py`) against a fake forge."""

from __future__ import annotations

import datetime
import math
import os
import threading
import unittest
from unittest import mock

import providers
from providers import IdentityUnknown, WorkspaceError

import vcs_readings
from vcs_readings import (
    ProposalRecord,
    ReadingsSweep,
    Touch,
    bucket_counts,
    classify_touch,
    install_commit_emails,
)

INSTALL = "kube-agents[bot]"
INSTALL_BARE = "kube-agents"
INSTALL_EMAIL = "platform-agent@kube-agents.invalid"
REPO = "github.com/acme/infra"
OTHER_REPO = "github.com/acme/apps"
GITEA_REPO = "gitea.example.com/acme/infra"
LOGGER_NAME = "credential-proxy.vcs-readings"


def iso(seconds_ago: float) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


RECENT = iso(3600)
OLD = "2020-01-01T00:00:00Z"


def rate_limited() -> WorkspaceError:
    return WorkspaceError("slow down", status=429, code="FORGE_RATE_LIMITED")


class FakeTransport:
    def __init__(self, forge: "FakeForge"):
        self.forge = forge

    def whoami(self) -> str:
        self.forge.whoami_calls += 1
        if isinstance(self.forge.login, Exception):
            raise self.forge.login
        return self.forge.login

    def api(self, method, path, **kwargs):
        self.forge.calls.append(path)
        if path == "user":
            return {"id": 7, "login": self.forge.login}
        return None


class FakeForge:
    """Answers the sweep's six reads from in-memory proposals and events."""

    def __init__(self, host="github.com", login=INSTALL, clamp=None, splits=True, http_user=False):
        self.name = "fake"
        self.hosts = (host,)
        self.whoami_graphql = "" if http_user else f"https://{host}/graphql"
        self.whoami_route = ("user", "login") if http_user else None
        self.login = login
        self.clamp = clamp
        self.splits = splits
        self.proposals: dict[int, dict] = {}
        self.reviews: dict[int, list] = {}
        self.comments: dict[int, list] = {}
        self.commits: dict[int, list] = {}
        self.closers: dict[int, dict] = {}
        self.fail: dict[str, object] = {}
        self.calls: list[str] = []
        self.whoami_calls = 0
        self.list_payloads: list[dict] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def add(self, number, state="open", author=INSTALL_BARE, created=RECENT, updated=None, **events):
        self.proposals[number] = {
            "number": number,
            "state": state,
            "author": author,
            "authorBot": True,
            "created": created,
            "updated": updated or f"2026-10-{number % 28 + 1:02d}T00:00:00Z#{number}",
            "closed": "",
        }
        for kind in ("reviews", "comments", "commits"):
            getattr(self, kind)[number] = list(events.get(kind, []))
        if "closer" in events:
            self.closers[number] = events["closer"]

    def _call(self, api, verb, number=None):
        api("GET", verb)
        failure = self.fail.get(verb)
        if callable(failure):
            failure = failure(number)
        if isinstance(failure, Exception):
            raise failure

    def proposal_list_by_author(self, api, repo, payload):
        self.list_payloads.append(dict(payload))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait()
        self._call(api, "list")
        items = sorted(self.proposals.values(), key=lambda p: p["updated"], reverse=True)
        served = min(payload["limit"], self.clamp or payload["limit"])
        start = (payload["page"] - 1) * served
        batch = [dict(p) for p in items[start : start + served]]
        return {
            "proposals": batch,
            "count": len(batch),
            "truncated": start + len(batch) < len(items),
            "total": len(items),
        }

    def proposal_tally(self, api, repo, payload):
        self._call(api, "tally")
        states = [p["state"] for p in self.proposals.values()]
        if not self.splits:
            return {"open": states.count("open"), "merged": None, "closed": None}
        return {state: states.count(state) for state in ("open", "merged", "closed")}

    def proposal_reviews(self, api, repo, payload):
        self._call(api, "reviews", payload["number"])
        reviews = self.reviews.get(payload["number"], [])
        return {"reviews": reviews, "count": len(reviews), "truncated": False}

    def proposal_view(self, api, repo, payload):
        self._call(api, "view", payload["number"])
        assert payload["comments"] is True
        comments = self.comments.get(payload["number"], [])
        return {
            "proposal": dict(self.proposals[payload["number"]]),
            "comments": comments,
            "commentCount": len(comments),
            "commentsTruncated": False,
        }

    def proposal_commits(self, api, repo, payload):
        self._call(api, "commits", payload["number"])
        rows = self.commits.get(payload["number"], [])[: vcs_readings.COMMIT_CAP]
        start = (payload["page"] - 1) * payload["limit"]
        batch = rows[start : start + payload["limit"]]
        return {"commits": batch, "count": len(batch), "truncated": len(batch) >= payload["limit"]}

    def proposal_closer(self, api, repo, payload):
        self._call(api, "closer", payload["number"])
        return self.closers.get(payload["number"], {"actor": "", "bot": False})

    def detail_calls(self) -> int:
        return sum(1 for path in self.calls if path in ("reviews", "view", "commits", "closer"))


class FakeRegistry:
    def __init__(self, forges: dict[str, FakeForge]):
        self.forges = forges

    def resolve(self, key):
        host, _, path = key.partition("/")
        return self.forges[key], path


class FakeMetrics:
    def __init__(self):
        self.readings: list[tuple] = []
        self.failures: list[str] = []

    def set_readings(self, outcomes, details):
        self.readings.append((outcomes, details))

    def record_reading_failure(self, reason):
        self.failures.append(reason)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def review(state, author="alice", bot=False):
    return {"id": 1, "state": state, "author": author, "bot": bot, "created": RECENT}


def comment(kind, author="alice", bot=False, body="looks fine"):
    return {"kind": kind, "author": author, "bot": bot, "body": body}


def commit(author, email, bot=False):
    return {"author": author, "email": email, "bot": bot}


def install_commit():
    # The install's own commit, as a real forge returns it: no linked login,
    # so the translator falls back to the git name, and the install's address.
    return commit("Platform Agent", INSTALL_EMAIL)


class Harness:
    def __init__(
        self, forges: dict[str, FakeForge], interval=vcs_readings.VCS_READINGS_TTL_SECONDS, app_credential=None
    ):
        self.forges = forges
        self.metrics = FakeMetrics()
        self.clock = FakeClock()
        self.sweep = ReadingsSweep(
            registry=FakeRegistry(forges),
            transport_for=lambda forge, repo: FakeTransport(forge),
            metrics=self.metrics,
            managed_repositories=lambda: frozenset(forges),
            mint=mock.Mock(side_effect=AssertionError("the fake transport never mints")),
            clock=self.clock,
            interval=interval,
            app_credential=app_credential,
        )

    def last(self):
        return self.metrics.readings[-1]


# An interval long enough that the per-sweep share of the hourly detail
# budget covers a thousand proposals, for tests that are not about the budget.
WIDE = 3600 * 10


class ReadingsSweepTest(unittest.TestCase):
    def test_a_full_sweep_publishes_every_series_and_bucket(self):
        forge = FakeForge()
        forge.add(1, "open", reviews=[review("commented", author="carol")], commits=[install_commit()])
        forge.add(
            2,
            "merged",
            comments=[comment("issue", author=INSTALL_BARE, bot=True)],
            commits=[install_commit()],
        )
        forge.add(
            3,
            "merged",
            reviews=[review("approved"), review("changes_requested", author="renovate", bot=True)],
            comments=[
                comment("issue", author="dependabot", bot=True),
                comment("review_comment", author="bob"),
                comment("review", author="alice", body="a review's body is not a second touch"),
            ],
            commits=[install_commit()],
        )
        forge.add(
            4,
            "closed",
            commits=[commit("dave", "dave@example.com")],
            closer={"actor": "erin", "bot": False},
        )
        harness = Harness({REPO: forge})

        harness.sweep.run_once()

        outcomes, details = harness.last()
        self.assertEqual((outcomes.open, outcomes.merged, outcomes.closed), (1, 2, 1))
        self.assertEqual(details.closed_by, {"install": 0, "human": 1, "bot": 0})
        self.assertEqual(
            details.touches,
            {
                Touch("review_commented", "human"): 1,
                Touch("review_approved", "human"): 1,
                Touch("review_changes_requested", "bot"): 1,
                Touch("comment", "bot"): 1,
                Touch("review_comment", "human"): 1,
                Touch("commit", "human"): 1,
            },
        )
        inf = float("inf")
        self.assertEqual(
            details.buckets["merged"],
            {0.0: 1, 1.0: 1, 2.0: 2, 3.0: 2, 5.0: 2, 8.0: 2, 13.0: 2, 21.0: 2, inf: 2},
        )
        self.assertEqual(details.buckets["closed"][0.0], 0)
        self.assertEqual(details.buckets["closed"][1.0], 1)
        self.assertEqual(details.buckets["closed"][inf], 1)
        self.assertEqual(details.ignored, 0)
        self.assertEqual(list(details.buckets["merged"])[-1], inf)
        self.assertEqual(harness.metrics.failures, [])
        self.assertGreater(outcomes.taken_at, 0)

    def test_the_installs_unlinked_commit_is_not_a_touch(self):
        emails = install_commit_emails("github.com", INSTALL, None)
        self.assertIsNone(
            classify_touch({"type": "commit", "author": None, "email": INSTALL_EMAIL, "bot": False}, INSTALL, emails)
        )
        self.assertIsNone(
            classify_touch(
                {"type": "commit", "author": "Platform Agent", "email": "platform-agent@users.noreply.github.com", "bot": False},
                INSTALL,
                emails,
            )
        )
        self.assertEqual(
            classify_touch({"type": "commit", "author": None, "email": "dave@example.com", "bot": False}, INSTALL, emails),
            Touch("commit", "human"),
        )
        with mock.patch.dict(os.environ, {"GITHUB_APP_ID": "42", "CREDENTIAL_PROXY_GIT_AUTHOR_EMAIL": "Agent@Corp.example"}):
            emails = install_commit_emails("github.com", INSTALL, os.environ["GITHUB_APP_ID"])
        self.assertIn("42+kube-agents[bot]@users.noreply.github.com", emails)
        self.assertIn("agent@corp.example", emails)
        self.assertIsNone(
            classify_touch({"type": "commit", "author": "x", "email": "agent@corp.example", "bot": False}, INSTALL, emails)
        )

        forge = FakeForge()
        forge.add(1, "merged", commits=[install_commit(), install_commit()])
        harness = Harness({REPO: forge})
        harness.sweep.run_once()
        _, details = harness.last()
        self.assertEqual(details.touches, {})
        self.assertEqual(details.buckets["merged"][0.0], 1)

    def test_a_slash_agent_comment_is_agent_address_not_comment(self):
        emails = install_commit_emails("github.com", INSTALL, None)
        self.assertEqual(
            classify_touch({"type": "comment", "kind": "issue", "author": "alice", "bot": False, "body": "  /agent rebase"}, INSTALL, emails),
            Touch("agent_address", "human"),
        )
        self.assertEqual(
            classify_touch({"type": "comment", "kind": "issue", "author": "alice", "bot": False, "body": "please /agent"}, INSTALL, emails),
            Touch("comment", "human"),
        )
        self.assertEqual(
            classify_touch({"type": "comment", "kind": "review_comment", "author": "alice", "bot": False, "body": "/agent x"}, INSTALL, emails),
            Touch("review_comment", "human"),
        )
        self.assertIsNone(
            classify_touch({"type": "comment", "kind": "issue", "author": INSTALL_BARE, "bot": True, "body": "/agent"}, INSTALL, emails)
        )

        forge = FakeForge()
        forge.add(1, "open", comments=[comment("issue", body="/agent please retry")])
        harness = Harness({REPO: forge})
        harness.sweep.run_once()
        _, details = harness.last()
        self.assertEqual(details.touches, {Touch("agent_address", "human"): 1})

    def test_an_open_proposal_older_than_fourteen_days_with_no_touch_is_ignored(self):
        forge = FakeForge()
        forge.add(1, "open", created=OLD)
        forge.add(2, "open", created=OLD, reviews=[review("approved", author="renovate", bot=True)])
        forge.add(3, "open", created=OLD, comments=[comment("issue")])
        forge.add(4, "open", created=RECENT)
        forge.add(5, "closed", created=OLD)
        harness = Harness({REPO: forge})

        harness.sweep.run_once()

        _, details = harness.last()
        # 1 untouched and 2 touched by a bot only; 3 has a human, 4 is young, 5 is closed.
        self.assertEqual(details.ignored, 2)

    def test_a_foreign_author_in_the_listing_ends_the_repository_with_reason_forge(self):
        forge = FakeForge()
        forge.add(1, "merged")
        forge.add(2, "merged", author="someone-else")
        harness = Harness({REPO: forge})

        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            harness.sweep.run_once()

        self.assertEqual(harness.metrics.readings, [])
        self.assertEqual(harness.metrics.failures, ["forge"])
        self.assertEqual(forge.detail_calls(), 0)

    def test_a_truncated_github_repository_publishes_exact_state_counts_from_the_tally(self):
        forge = FakeForge()
        for number in range(1, 1102):
            forge.add(number, "merged" if number % 3 else "closed", updated=f"2026-01-01T00:00:00Z{number:05d}")
        forge.add(1102, "open", updated="2026-01-01T00:00:00Z00000")
        harness = Harness({REPO: forge}, interval=WIDE)

        harness.sweep.run_once()

        self.assertEqual(forge.calls.count("list"), vcs_readings.VCS_READINGS_MAX_PAGES)
        self.assertEqual(forge.calls.count("tally"), 1)
        outcomes, details = harness.last()
        states = [p["state"] for p in forge.proposals.values()]
        self.assertEqual(
            (outcomes.open, outcomes.merged, outcomes.closed),
            (states.count("open"), states.count("merged"), states.count("closed")),
        )
        self.assertEqual(outcomes.open, 1)
        # Details cover the thousand most recently updated proposals.
        inf = float("inf")
        self.assertEqual(details.buckets["merged"][inf] + details.buckets["closed"][inf], 1000)
        self.assertEqual(harness.metrics.failures, ["truncated"])

        # The record of a proposal that fell off the end of the walk is kept.
        newest = max(forge.proposals)
        forge.add(newest + 1, "merged", updated="2026-12-31T00:00:00Z")
        harness.sweep.run_once()
        _, details = harness.last()
        self.assertEqual(details.buckets["merged"][inf] + details.buckets["closed"][inf], 1001)

    def test_a_truncated_gitea_repository_withholds_the_outcomes_reading(self):
        forge = FakeForge(host="gitea.example.com", login="kube-agents", clamp=50, splits=False, http_user=True)
        for number in range(1, 1102):
            forge.add(number, "merged", updated=f"2026-01-01T00:00:00Z{number:05d}")
        harness = Harness({GITEA_REPO: forge}, interval=WIDE)

        harness.sweep.run_once()

        # Bounded in proposals under a server that clamps the page size.
        self.assertEqual(forge.calls.count("list"), vcs_readings.VCS_READINGS_MAX_PROPOSALS_GITEA // 50)
        self.assertEqual(forge.list_payloads[0]["authorId"], 7)
        outcomes, _ = harness.last()
        self.assertIsNone(outcomes)
        self.assertEqual(harness.metrics.failures, ["truncated"])

    def test_a_warming_cache_takes_outcomes_and_not_details_until_every_proposal_is_current(self):
        forge = FakeForge()
        for number in range(1, 41):
            forge.add(number, "merged")
        harness = Harness({REPO: forge})

        harness.sweep.run_once()

        outcomes, details = harness.last()
        self.assertEqual(outcomes.merged, 40)
        self.assertIsNone(details)
        self.assertEqual(harness.metrics.failures, ["warming"])

        for _ in range(5):
            harness.clock.now += vcs_readings.VCS_READINGS_TTL_SECONDS
            harness.sweep.run_once()
            if harness.last()[1] is not None:
                break
        outcomes, details = harness.last()
        self.assertIsNotNone(details)
        self.assertEqual(details.buckets["merged"][float("inf")], 40)
        self.assertEqual(harness.metrics.failures[-1], "warming")
        self.assertEqual(harness.metrics.failures.count("warming"), len(harness.metrics.readings) - 1)

    def test_the_cache_is_written_per_proposal_and_a_cut_short_attempt_continues(self):
        forge = FakeForge()
        for number in range(1, 6):
            forge.add(number, "open")
        # The listing is newest first; the third proposal read is cut short.
        order = sorted(forge.proposals, key=lambda n: forge.proposals[n]["updated"], reverse=True)
        forge.fail["reviews"] = lambda number: rate_limited() if number == order[2] else None
        harness = Harness({REPO: forge}, interval=WIDE)

        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            harness.sweep.run_once()

        self.assertEqual(harness.metrics.failures, ["rate_limited"])
        self.assertEqual(len(harness.sweep._cache), 2)
        del forge.fail["reviews"]
        forge.calls.clear()

        harness.sweep.run_once()

        # Only the three not yet read cost calls: reviews, view, commits each.
        self.assertEqual(forge.calls.count("reviews"), 3)
        self.assertEqual(len(harness.sweep._cache), 5)
        self.assertIsNotNone(harness.last()[1])

    def test_a_failing_transport_leaves_the_cache_intact(self):
        forge = FakeForge()
        forge.add(1, "merged")
        forge.add(2, "open")
        harness = Harness({REPO: forge})
        harness.sweep.run_once()
        self.assertEqual(len(harness.sweep._cache), 2)
        harness.sweep._transport_for = mock.Mock(side_effect=WorkspaceError("mint failed", status=502))

        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            harness.sweep.run_once()

        self.assertEqual(len(harness.sweep._cache), 2)
        self.assertEqual(harness.metrics.failures, ["forge"])
        self.assertEqual(len(harness.metrics.readings), 1)

        # A repository no longer managed still takes its records out.
        harness.sweep._managed = lambda: frozenset({OTHER_REPO})
        harness.sweep._registry.forges[OTHER_REPO] = FakeForge()
        harness.sweep._transport_for = lambda forge, repo: FakeTransport(forge)
        harness.sweep.run_once()
        self.assertEqual(harness.sweep._cache, {})

    def test_a_proposal_whose_updated_did_not_move_costs_no_detail_call(self):
        forge = FakeForge()
        forge.add(1, "merged")
        forge.add(2, "open")
        harness = Harness({REPO: forge})
        harness.sweep.run_once()
        forge.calls.clear()

        harness.sweep.run_once()

        self.assertEqual(forge.calls, ["list"])
        forge.proposals[2]["updated"] = "2027-01-01T00:00:00Z"
        forge.reviews[2] = [review("approved")]
        forge.calls.clear()

        harness.sweep.run_once()

        self.assertEqual(forge.calls, ["list", "reviews", "view", "commits"])
        self.assertEqual(harness.last()[1].touches, {Touch("review_approved", "human"): 1})

        # Within the walk bound, a proposal absent from the listing was deleted.
        del forge.proposals[1]
        harness.sweep.run_once()
        self.assertNotIn(("github.com", "acme/infra", 1), harness.sweep._cache)
        self.assertEqual(harness.last()[1].buckets["merged"][float("inf")], 0)

    def test_the_hourly_detail_budget_bounds_calls_per_sweep(self):
        forge = FakeForge()
        for number in range(1, 501):
            forge.add(number, "open")
        harness = Harness({REPO: forge})
        per_sweep = vcs_readings.VCS_READINGS_DETAIL_CALLS_PER_HOUR * vcs_readings.VCS_READINGS_TTL_SECONDS / 3600
        per_proposal = 3

        harness.sweep.run_once()
        first = forge.detail_calls()
        self.assertGreater(first, 0)
        self.assertLessEqual(first, per_sweep + per_proposal)

        forge.calls.clear()
        harness.sweep.run_once()
        self.assertEqual(forge.detail_calls(), 0, "no time has passed, so nothing has refilled")

        total = first
        for _ in range(15):
            harness.clock.now += vcs_readings.VCS_READINGS_TTL_SECONDS
            forge.calls.clear()
            harness.sweep.run_once()
            spent = forge.detail_calls()
            self.assertLessEqual(spent, per_sweep + per_proposal)
            total += spent
        # Sixteen sweeps across one hour spend the hour's refill plus the
        # bucket's starting share, and overshoot by at most one proposal.
        self.assertLessEqual(total, vcs_readings.VCS_READINGS_DETAIL_CALLS_PER_HOUR + per_sweep + per_proposal)

    def test_a_nameless_credential_leaves_the_other_repositories_whole(self):
        nameless = FakeForge(login="")
        nameless.add(1, "merged")
        refusing = FakeForge(login=IdentityUnknown("fake"))
        refusing.add(1, "merged")
        named = FakeForge()
        named.add(1, "merged")
        named.add(2, "open")
        harness = Harness(
            {REPO: nameless, OTHER_REPO: named, "github.com/acme/third": refusing}
        )

        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            harness.sweep.run_once()
            harness.sweep.run_once()

        outcomes, details = harness.last()
        self.assertEqual((outcomes.open, outcomes.merged, outcomes.closed), (1, 1, 0))
        self.assertIsNotNone(details)
        self.assertEqual(harness.metrics.failures, ["identity", "identity"])
        self.assertEqual(nameless.whoami_calls, 1)
        self.assertEqual(refusing.whoami_calls, 1)
        self.assertEqual(nameless.calls, [])
        self.assertEqual(len([line for line in logs.output if "cannot name itself" in line]), 1)

    def test_a_rate_limited_step_two_keeps_the_outcomes_reading_current(self):
        forge = FakeForge()
        forge.add(1, "merged")
        forge.add(2, "closed", closer={"actor": INSTALL_BARE, "bot": True})
        harness = Harness({REPO: forge})
        harness.sweep.run_once()
        self.assertEqual(harness.last()[1].closed_by, {"install": 1, "human": 0, "bot": 0})

        forge.proposals[1]["updated"] = "2027-01-01T00:00:00Z"
        forge.add(3, "open")
        forge.fail["view"] = rate_limited()
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            harness.sweep.run_once()

        outcomes, details = harness.last()
        self.assertEqual((outcomes.open, outcomes.merged, outcomes.closed), (1, 1, 1))
        self.assertIsNone(details)
        self.assertEqual(harness.metrics.failures, ["rate_limited"])

        forge.fail["list"] = rate_limited()
        before = len(harness.metrics.readings)
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            harness.sweep.run_once()
        self.assertEqual(len(harness.metrics.readings), before, "a rate-limited listing publishes nothing")
        self.assertEqual(harness.metrics.failures[-1], "rate_limited")

    def test_a_closed_proposal_reopened_has_no_closer_and_counts_open(self):
        forge = FakeForge()
        forge.add(1, "closed", closer={"actor": "erin", "bot": False})
        harness = Harness({REPO: forge})
        harness.sweep.run_once()
        outcomes, details = harness.last()
        self.assertEqual((outcomes.open, outcomes.closed), (0, 1))
        self.assertEqual(details.closed_by["human"], 1)

        forge.proposals[1]["state"] = "open"
        forge.proposals[1]["updated"] = "2027-01-01T00:00:00Z"
        forge.calls.clear()
        harness.sweep.run_once()

        outcomes, details = harness.last()
        self.assertEqual((outcomes.open, outcomes.closed), (1, 0))
        self.assertEqual(details.closed_by, {"install": 0, "human": 0, "bot": 0})
        self.assertNotIn("closer", forge.calls)
        record = harness.sweep._cache[("github.com", "acme/infra", 1)]
        self.assertIsInstance(record, ProposalRecord)
        self.assertEqual(record.closer, "")

    def test_a_proposal_past_the_commit_cap_counts_from_the_cap_with_detail_truncated(self):
        forge = FakeForge()
        forge.add(1, "merged", commits=[commit("dave", "dave@example.com")] * 300)
        harness = Harness({REPO: forge})

        harness.sweep.run_once()

        _, details = harness.last()
        self.assertEqual(details.touches, {Touch("commit", "human"): vcs_readings.COMMIT_CAP})
        self.assertEqual(harness.metrics.failures, ["detail_truncated"])
        self.assertEqual(forge.calls.count("commits"), 3)

    def test_run_once_never_overlaps_itself(self):
        forge = FakeForge()
        forge.add(1, "open")
        forge.gate = threading.Event()
        harness = Harness({REPO: forge})
        first = threading.Thread(target=harness.sweep.run_once)
        first.start()
        self.assertTrue(forge.entered.wait(5))

        harness.sweep.run_once()

        self.assertEqual(len(forge.list_payloads), 1)
        forge.gate.set()
        first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual(len(harness.metrics.readings), 1)

        thread = harness.sweep.start()
        try:
            self.assertTrue(thread.daemon)
            self.assertEqual(thread.name, "vcs-readings")
        finally:
            harness.sweep.stop()
            thread.join(5)


class SweepIdentityTest(unittest.TestCase):
    def test_the_sweeps_login_comes_from_the_sweep_transport_not_binding_whoami(self):
        import credential_proxy
        import vcs_broker
        from test_credential_proxy import _SweepExecutor
        from test_providers_http_transport import Opener

        github = next(cls for cls in providers.AVAILABLE if cls.name == "github")
        forge = github(refresh=mock.Mock(side_effect=AssertionError("the write credential is never refreshed")))
        executor = _SweepExecutor()
        opener = Opener({"data": {"viewer": {"login": INSTALL}}})
        sweep = ReadingsSweep(
            registry=FakeRegistry({REPO: forge}),
            transport_for=lambda f, r: credential_proxy.sweep_transport_for(executor, f, r, opener=opener),
            metrics=FakeMetrics(),
            managed_repositories=lambda: frozenset({REPO}),
            mint=executor.mint_sweep_credential,
            app_credential=credential_proxy.sweep_app_credential,
        )

        with mock.patch.object(vcs_broker.Binding, "whoami", side_effect=AssertionError("Binding.whoami")):
            sweep._bind(REPO)
            self.assertEqual(INSTALL, sweep._login(REPO))

        (request,) = opener.requests
        self.assertEqual("https://api.github.com/graphql", request.full_url)
        self.assertEqual("Bearer ghs_sweep_1", request.get_header("Authorization"))
        self.assertEqual({"author": "app/kube-agents"}, sweep._author_payload(REPO))

    def test_an_app_credential_is_searched_as_app_slug_with_or_without_the_marking(self):
        # The credential's kind is the primary signal: GraphQL `viewer` may name
        # an App installation without the `[bot]` marking.
        for login in (INSTALL, INSTALL_BARE):
            with self.subTest(login=login):
                forge = FakeForge(login=login)
                forge.add(1, "open", author=INSTALL_BARE)
                harness = Harness({REPO: forge}, app_credential=lambda f: True)
                harness.sweep.run_once()
                self.assertEqual("app/kube-agents", forge.list_payloads[0]["author"])

        # Not an App: the bare login, unless the marking says otherwise.
        for login, author in ((INSTALL_BARE, INSTALL_BARE), (INSTALL, "app/kube-agents")):
            with self.subTest(login=login, app=False):
                forge = FakeForge(login=login)
                forge.add(1, "open", author=INSTALL_BARE)
                harness = Harness({REPO: forge}, app_credential=lambda f: False)
                harness.sweep.run_once()
                self.assertEqual(author, forge.list_payloads[0]["author"])


class PureFunctionTest(unittest.TestCase):
    def test_bucket_counts_are_cumulative_with_inf_last(self):
        counts = bucket_counts([0, 0, 1, 4, 30])
        self.assertEqual(list(counts), list(vcs_readings.VCS_READINGS_BUCKETS))
        self.assertEqual(counts[0.0], 2)
        self.assertEqual(counts[3.0], 3)
        self.assertEqual(counts[5.0], 4)
        self.assertEqual(counts[21.0], 4)
        self.assertEqual(counts[math.inf], 5)


if __name__ == "__main__":
    unittest.main()
