#!/usr/bin/env python3
"""Unit tests for the broken-main issue notifier.

Run: cd scripts && python3 -m unittest test_notify_broken_main

The event path cannot be exercised end to end before it is on main -- a
`workflow_run` workflow only runs from the default branch's copy of itself, and
a dispatch from a branch reaches only the sweep -- so everything that can be
decided without a runner is decided here. Four failure
modes are worth more than the rest: staying quiet when main is broken, which
reproduces the gap this exists to close; opening an issue on a green run, which
trains everyone to ignore the label; leaving an issue open after main recovers,
which does the same thing more slowly; and writing to an issue that already says
so, which the scheduled sweep would turn into a comment every fifteen minutes.
"""

import json
import re
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import notify_broken_main as notifier

WORKFLOW_FILE = _HERE.parent / ".github" / "workflows" / "main-broken-notify.yml"


def run(number, conclusion, *, sha=None, subject="a commit", run_id=None, name="Operator Tests"):
    """A workflow run, carrying only the fields the notifier reads."""
    return {
        "id": run_id if run_id is not None else 1000 + number,
        "run_number": number,
        "conclusion": conclusion,
        "name": name,
        "event": "push",
        "head_branch": "main",
        "head_sha": sha or f"{number:040x}",
        "head_commit": {"message": f"{subject}\n\nbody", "author": {"name": "A Contributor"}},
        # What Tide leaves behind on every merge here, and the reason the
        # attribution is read off the commit rather than the run.
        "actor": {"login": "google-oss-prow[bot]"},
        "triggering_actor": {"login": "google-oss-prow[bot]"},
        "html_url": f"https://github.com/gke-labs/kube-agents/actions/runs/{1000 + number}",
        "workflow_id": 77,
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
    }


class DecideTest(unittest.TestCase):
    """Which of the three kinds a run falls into, or none. This is the whole design."""

    def test_first_failure_announces_a_break(self):
        decision = notifier.decide(run(10, "failure"), [run(9, "success")])
        self.assertEqual(decision["kind"], "broken")
        self.assertEqual(decision["streak_length"], 1)
        self.assertEqual(decision["broke_at"]["run_number"], 10)

    def test_green_after_green_is_still_a_green_to_reconcile(self):
        """Not None. Whether a green run has anything to do depends on whether
        an issue is open, which the run history cannot see -- it cannot see a
        notify run that was dropped, or a red run re-run into a green. `decide`
        reports the state; `reconcile` decides whether it matters."""
        decision = notifier.decide(run(10, "success"), [run(9, "success")])
        self.assertEqual(decision["kind"], "green")
        self.assertEqual(decision["streak_length"], 0)

    def test_a_conclusion_that_says_nothing_says_nothing(self):
        """`report` never passes one of these, but `decide` is a public seam
        and a direct caller could. Read as green, any of them closes the issue
        on a main that is still broken."""
        for conclusion in ("neutral", "stale", "action_required", "cancelled", "skipped", None):
            with self.subTest(conclusion=conclusion):
                self.assertIsNone(notifier.decide(run(10, conclusion), [run(9, "failure")]))

    def test_a_second_failure_is_a_follow_up_not_a_new_break(self):
        decision = notifier.decide(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        self.assertEqual(decision["kind"], "still-broken")
        self.assertEqual(decision["streak_length"], 2)
        self.assertEqual(decision["broke_at"]["run_number"], 10)

    def test_green_after_a_failure_carries_the_streak_it_ended(self):
        decision = notifier.decide(run(12, "success"), [run(11, "failure"), run(10, "failure"), run(9, "success")])
        self.assertEqual(decision["kind"], "green")
        self.assertEqual(decision["streak_length"], 2)
        self.assertEqual(decision["broke_at"]["run_number"], 10)

    def test_the_recovery_is_not_counted_into_the_streak_it_ends(self):
        """`broke_at` is the issue's identity, so a green run joining its own
        streak would look for an issue that was never opened -- the reader would
        see a break with no end and an end with no break."""
        history = [run(11, "failure"), run(10, "failure"), run(9, "success")]
        decision = notifier.decide(run(12, "success"), history)
        self.assertEqual(decision["broke_at"]["run_number"], 10)
        self.assertNotIn(12, [r["run_number"] for r in decision["streak"]])

    def test_the_streak_reads_oldest_first(self):
        """The order the issue's table is in: the commit that broke main at the
        top, the ones that landed on top of it below."""
        decision = notifier.decide(run(12, "failure"), [run(11, "failure"), run(10, "failure"), run(9, "success")])
        self.assertEqual([r["run_number"] for r in decision["streak"]], [10, 11, 12])

    def test_a_timeout_is_a_failure(self):
        self.assertEqual(notifier.decide(run(10, "timed_out"), [run(9, "success")])["kind"], "broken")

    def test_an_unparseable_workflow_is_a_failure(self):
        """`startup_failure` reaches main exactly the way a failing test does,
        and reads as 'nothing ran' if it is filtered out."""
        self.assertEqual(notifier.decide(run(10, "startup_failure"), [run(9, "success")])["kind"], "broken")

    def test_the_first_run_of_a_workflow_can_still_break_main(self):
        """No history at all. `broke_at` has to fall back to the current run
        rather than raising, or a newly added required check could never
        report its first failure."""
        decision = notifier.decide(run(1, "failure"), [])
        self.assertEqual(decision["kind"], "broken")
        self.assertEqual(decision["broke_at"]["run_number"], 1)


class HistoryFilterTest(unittest.TestCase):
    def test_the_current_run_is_removed_from_its_own_history(self):
        """The API list includes it, and left in it would compare against
        itself -- every failure would read as 'still broken'."""
        current = run(10, "failure")
        history = notifier.reporting_history([current, run(9, "success")], current)
        self.assertEqual([r["run_number"] for r in history], [9])

    def test_a_newer_run_is_not_treated_as_history(self):
        """`current` is the newest run that said anything, but the list can hold
        newer runs that said nothing, and those are not its past either."""
        current = run(10, "failure")
        history = notifier.reporting_history([run(11, "success"), current, run(9, "success")], current)
        self.assertEqual([r["run_number"] for r in history], [9])

    def test_a_cancelled_run_does_not_end_a_streak(self):
        """A concurrency-superseded run says nothing about the tree. Counted as
        a non-failure it would make the next failure look like a fresh break and
        open a second issue."""
        current = run(12, "failure")
        history = notifier.reporting_history([run(11, "cancelled"), run(10, "failure")], current)
        decision = notifier.decide(current, history)
        self.assertEqual(decision["kind"], "still-broken")
        self.assertEqual(decision["broke_at"]["run_number"], 10)

    def test_a_skipped_run_does_not_end_a_streak_either(self):
        current = run(12, "failure")
        history = notifier.reporting_history([run(11, "skipped"), run(10, "failure")], current)
        self.assertEqual(notifier.decide(current, history)["kind"], "still-broken")

    def test_a_run_that_reports_success_without_testing_would_fool_this(self):
        """The #812 shape, and the one thing this script cannot defend itself
        against -- recorded as the contract it depends on rather than left for
        someone to rediscover in production.

        `k8s-operator-test.yml` used to skip its steps on a docs commit and
        report `success` at run level anyway: run 2664 on main, `1d68f09`, every
        real step `skipped` and the conclusion `success`. Replayed through
        `decide` that green closes the issue -- and on the real history it does
        so twice, at 2655 and again at 2664, naming a docs commit as the fix and
        saying nothing when 2700 genuinely repaired main. The fix is in the
        workflow: its push trigger filters with `paths:`, so no run is recorded
        at all. This states why a watched workflow may not self-skip into a
        green, and it is why `Prettier Check`, which scopes itself to the files
        a push touched, is not on the watch list.
        """
        docs_commit_that_tested_nothing = run(11, "success")
        decision = notifier.decide(docs_commit_that_tested_nothing, [run(10, "failure")])
        self.assertEqual(
            decision["kind"],
            "green",
            "if this stops reading as a green the guard has moved into the script, and "
            "the paths: filter in k8s-operator-test.yml can be reconsidered",
        )


class NewestReportingRunTest(unittest.TestCase):
    """Which run is the current state of main. Every reconciliation starts here,
    whichever run woke it."""

    def test_the_highest_run_number_wins_whatever_the_list_order(self):
        """The API lists by creation and a burst finishes out of order, so the
        first element is regularly not the newest."""
        runs = [run(11, "success"), run(13, "failure"), run(12, "success")]
        self.assertEqual(notifier.newest_reporting_run(runs)["run_number"], 13)

    def test_a_newer_run_that_said_nothing_is_passed_over(self):
        runs = [run(13, "cancelled"), run(12, "failure"), run(11, "success")]
        self.assertEqual(notifier.newest_reporting_run(runs)["run_number"], 12)

    def test_no_reporting_run_is_none(self):
        self.assertIsNone(notifier.newest_reporting_run([run(13, "cancelled"), run(12, "skipped")]))
        self.assertIsNone(notifier.newest_reporting_run([]))


class EpisodeMarkerTest(unittest.TestCase):
    """The hidden marker is how an update finds the issue it belongs to. Get it
    wrong in either direction and the issue either duplicates or never closes."""

    def test_one_breakage_is_one_issue(self):
        break_decision = notifier.decide(run(10, "failure"), [run(9, "success")])
        follow_up = notifier.decide(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        recovery = notifier.decide(run(12, "success"), [run(11, "failure"), run(10, "failure")])
        markers = {notifier.episode_marker(d, 77) for d in (break_decision, follow_up, recovery)}
        self.assertEqual(len(markers), 1, f"expected one issue, got {markers}")

    def test_the_next_breakage_opens_a_new_issue(self):
        first = notifier.decide(run(10, "failure"), [run(9, "success")])
        second = notifier.decide(run(20, "failure"), [run(19, "success")])
        self.assertNotEqual(notifier.episode_marker(first, 77), notifier.episode_marker(second, 77))

    def test_two_workflows_breaking_at_once_do_not_share_an_issue(self):
        """Run numbers are per workflow, so the workflow id has to be in the
        marker or a coincidence of numbering merges two unrelated breakages."""
        decision = notifier.decide(run(10, "failure"), [run(9, "success")])
        self.assertNotEqual(notifier.episode_marker(decision, 77), notifier.episode_marker(decision, 88))

    def test_the_workflow_prefix_matches_that_workflows_markers_only(self):
        """`reconcile` uses the prefix to find stale issues to close. Matching
        another workflow's issue would close a real breakage."""
        decision = notifier.decide(run(10, "failure"), [run(9, "success")])
        self.assertIn(notifier.workflow_marker(77), notifier.episode_marker(decision, 77))
        self.assertNotIn(notifier.workflow_marker(88), notifier.episode_marker(decision, 77))

    def test_the_prefix_does_not_match_a_workflow_whose_id_extends_it(self):
        """Workflow 7 and workflow 77 are both plausible ids, and a prefix that
        ended at the digits would confuse them."""
        self.assertNotIn(notifier.workflow_marker(7), notifier.workflow_marker(77))


class RenderTest(unittest.TestCase):
    REPO = "gke-labs/kube-agents"

    def _body(self, decision):
        return notifier.render_body(decision, self.REPO, notifier.episode_marker(decision, 77))

    def test_a_break_names_the_workflow_commit_and_run(self):
        decision = notifier.decide(
            run(10, "failure", sha="277de10c43e3b7311bfb159a4016eee2831ca6f7", subject="feat: add PDBs (#733)"),
            [run(9, "success")],
        )
        body = self._body(decision)
        self.assertIn("Operator Tests", body)
        self.assertIn("277de10", body)
        self.assertIn("actions/runs/1010", body)

    def test_the_title_names_the_workflow_so_five_issues_are_distinguishable(self):
        decision = notifier.decide(run(10, "failure", name="Prettier Check"), [run(9, "success")])
        self.assertEqual(notifier.render_title(decision), "🔴 main is broken: Prettier Check")

    def test_the_title_is_stable_across_an_episode(self):
        """It is rewritten on every update, so a changing title would rename the
        issue under anyone reading it."""
        first = notifier.decide(run(10, "failure"), [run(9, "success")])
        later = notifier.decide(run(12, "failure"), [run(11, "failure"), run(10, "failure")])
        self.assertEqual(notifier.render_title(first), notifier.render_title(later))

    def test_the_marker_is_in_the_body(self):
        """Without it the next update cannot find this issue and opens another."""
        decision = notifier.decide(run(10, "failure"), [run(9, "success")])
        self.assertIn(notifier.episode_marker(decision, 77), self._body(decision))

    def test_the_change_is_attributed_to_its_author_not_to_tide(self):
        """`actor` is `google-oss-prow[bot]` on every merge here, so a body
        built from it names the robot on every breakage and nobody else."""
        body = self._body(notifier.decide(run(10, "failure"), [run(9, "success")]))
        self.assertIn("A Contributor", body)
        self.assertNotIn("prow", body)

    def test_the_pull_request_is_named_so_github_autolinks_it(self):
        """Bare `#733` rather than a URL: it renders as a link and leaves a
        back-reference on the pull request that broke main."""
        body = self._body(notifier.decide(run(10, "failure", subject="feat: add PDBs (#733)"), [run(9, "success")]))
        self.assertIn("| #733 |", body)

    def test_a_commit_with_no_pull_request_number_still_renders(self):
        """A direct push, or a merge commit that does not carry the suffix. The
        cell is empty rather than the row being dropped."""
        body = self._body(notifier.decide(run(10, "failure", subject="hotfix, no PR"), [run(9, "success")]))
        self.assertIn("A Contributor", body)
        self.assertNotIn("#", body.split("| run |")[1].split("\n")[2])

    def test_every_commit_in_the_streak_gets_a_row(self):
        decision = notifier.decide(run(12, "failure"), [run(11, "failure"), run(10, "failure"), run(9, "success")])
        body = self._body(decision)
        rows = [line for line in body.splitlines() if line.startswith("| [")]
        self.assertEqual(len(rows), 3)
        self.assertIn("Broken since", body)
        self.assertIn("3 consecutive failures", body)

    def test_the_table_reads_oldest_first(self):
        decision = notifier.decide(run(12, "failure"), [run(11, "failure"), run(10, "failure")])
        rows = [line for line in self._body(decision).splitlines() if line.startswith("| [")]
        self.assertEqual([row.split("]")[0] for row in rows], ["| [10", "| [11", "| [12"])

    def test_a_first_failure_does_not_claim_a_streak(self):
        """"Broken since X -- 1 consecutive failures" reads as a bug in the
        counter. One row is its own explanation."""
        body = self._body(notifier.decide(run(10, "failure"), [run(9, "success")]))
        self.assertNotIn("Broken since", body)
        self.assertNotIn("consecutive", body)

    def test_rebuilding_the_body_for_the_same_run_is_idempotent(self):
        """The body is rewritten, not appended to, so a redelivered event or a
        re-run of the workflow cannot double a row."""
        decision = notifier.decide(run(12, "failure"), [run(11, "failure"), run(10, "failure")])
        self.assertEqual(self._body(decision), self._body(decision))

    def test_a_pipe_in_an_author_name_does_not_break_the_table(self):
        """An unescaped `|` in a cell silently splits the row into five columns,
        and Markdown drops the overflow -- the PR link vanishes."""
        odd = run(10, "failure")
        odd["head_commit"]["author"]["name"] = "A | Contributor"
        body = self._body(notifier.decide(odd, [run(9, "success")]))
        row = [line for line in body.splitlines() if line.startswith("| [")][0]
        self.assertIn(r"A \| Contributor", row)
        separators = len(re.findall(r"(?<!\\)\|", row))
        self.assertEqual(separators, 5, f"expected four cells, got {row}")

    def test_a_commit_with_no_message_still_renders(self):
        """`head_commit` is absent on some run payloads. A KeyError here would
        take down the notification rather than degrade it."""
        bare = run(10, "failure")
        del bare["head_commit"]
        body = self._body(notifier.decide(bare, [run(9, "success")]))
        self.assertIn(bare["head_sha"][:7], body)

    def test_a_new_issue_needs_no_comment(self):
        """Opening the issue is the notification."""
        self.assertIsNone(notifier.render_comment(notifier.decide(run(10, "failure"), [run(9, "success")]), self.REPO))

    def test_a_follow_up_comments_because_a_body_edit_notifies_nobody(self):
        comment = notifier.render_comment(
            notifier.decide(run(12, "failure"), [run(11, "failure"), run(10, "failure")]), self.REPO
        )
        self.assertIn("Still failing", comment)
        self.assertIn("3 consecutive failures", comment)

    def test_the_recovery_comment_is_recognisably_not_another_break(self):
        comment = notifier.render_comment(notifier.decide(run(12, "success"), [run(11, "failure")]), self.REPO)
        self.assertIn("✅", comment)
        self.assertNotIn("🔴", comment)
        self.assertIn("Fixed by", comment)

    def test_a_one_commit_breakage_is_not_pluralised(self):
        comment = notifier.render_comment(notifier.decide(run(12, "success"), [run(11, "failure")]), self.REPO)
        self.assertIn("1 consecutive failure before it.", comment)


def _response(raw):
    """What `urlopen` returns: a context manager yielding something with `read`.

    `__exit__` returns False on purpose. A `MagicMock` there is truthy, which
    swallows any exception raised inside the `with` -- including the ones these
    tests exist to catch -- and surfaces it much later as an unrelated
    `UnboundLocalError`.
    """
    return mock.MagicMock(
        __enter__=mock.Mock(return_value=mock.Mock(read=lambda: raw)),
        __exit__=mock.Mock(return_value=False),
    )


class RequestTest(unittest.TestCase):
    def _api(self, responses):
        """A `GitHubAPI` whose opener raises or returns per call."""
        calls = []

        def opener(request):
            calls.append(request)
            outcome = responses[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return _response(b"{}")

        return notifier.GitHubAPI("o/r", "t", opener=opener, sleep=lambda _: None), calls

    def test_a_5xx_is_retried(self):
        """A dropped write is the exact failure this script exists to prevent --
        an issue that never opened, or one that never closed."""
        api, calls = self._api([urllib.error.HTTPError("u", 503, "busy", {}, None), None])
        api.request("POST", "/x", {"a": 1})
        self.assertEqual(len(calls), 2)

    def test_a_4xx_is_not_retried(self):
        """A permissions failure will not start working on the second attempt,
        and the job should fail now rather than in fifteen seconds."""
        api, calls = self._api([urllib.error.HTTPError("u", 403, "no", {}, None), None])
        with self.assertRaises(urllib.error.HTTPError):
            api.request("POST", "/x", {"a": 1})
        self.assertEqual(len(calls), 1)

    def test_giving_up_raises_rather_than_returning(self):
        api, calls = self._api([urllib.error.HTTPError("u", 503, "busy", {}, None)] * notifier.REQUEST_ATTEMPTS)
        with self.assertRaises(urllib.error.HTTPError):
            api.request("POST", "/x", {"a": 1})
        self.assertEqual(len(calls), notifier.REQUEST_ATTEMPTS)

    def test_a_tolerated_status_is_not_an_error(self):
        """`ensure_label` runs on every first failure and 422 means the label is
        already there, which is the normal case."""
        api, calls = self._api([urllib.error.HTTPError("u", 422, "exists", {}, None)])
        self.assertIsNone(api.request("POST", "/labels", {"name": "x"}, tolerate=(422,)))
        self.assertEqual(len(calls), 1)

    def test_a_secondary_rate_limit_is_retried_even_though_it_is_a_403(self):
        """GitHub throttles writes with 403, not 429. Treating every 403 as
        fatal drops the write; treating every 403 as retryable turns a missing
        `issues: write` into fifteen seconds of pointless retries."""
        throttled = urllib.error.HTTPError("u", 403, "slow down", {"Retry-After": "1"}, None)
        api, calls = self._api([throttled, None])
        api.request("POST", "/x", {"a": 1})
        self.assertEqual(len(calls), 2)

    def test_an_exhausted_quota_is_retried_too(self):
        exhausted = urllib.error.HTTPError("u", 403, "limit", {"x-ratelimit-remaining": "0"}, None)
        api, calls = self._api([exhausted, None])
        api.request("POST", "/x", {"a": 1})
        self.assertEqual(len(calls), 2)

    def test_retry_after_is_honoured_and_capped(self):
        slept = []
        api, _ = self._api([urllib.error.HTTPError("u", 429, "wait", {"Retry-After": "9"}, None), None])
        api.sleep = slept.append
        api.request("POST", "/x", {"a": 1})
        self.assertEqual(slept, [9])
        self.assertEqual(notifier._retry_delay(urllib.error.HTTPError("u", 429, "w", {"Retry-After": "9999"}, None)),
                         notifier.REQUEST_RETRY_CEILING)


class QueryTest(unittest.TestCase):
    """The URLs the API layer builds. Nothing else covers them: `ReconcileTest`
    fakes the API wholesale, so dropping `state=open` from the issue query or
    `event=push` from the history query breaks the notifier while every other
    test still passes."""

    def _api(self, payload):
        calls = []

        def opener(request):
            calls.append(request)
            return _response(json.dumps(payload).encode())

        return notifier.GitHubAPI("gke-labs/kube-agents", "t", opener=opener, sleep=lambda _: None), calls

    def test_the_history_query_asks_only_for_completed_pushes_on_the_branch(self):
        """A pull-request run of the same workflow says nothing about main, and
        they outnumber the push runs by an order of magnitude."""
        api, calls = self._api({"workflow_runs": []})
        api.history(77, "main")
        url = calls[0].full_url
        self.assertIn("/actions/workflows/77/runs?", url)
        for expected in ("branch=main", "event=push", "status=completed", f"per_page={notifier.HISTORY_DEPTH}"):
            self.assertIn(expected, url)

    def test_the_workflow_list_is_paged_to_its_own_total(self):
        """The endpoint wraps its page in an object, so the shared client's
        `get_all` cannot page it. One page at `PER_PAGE` holds every workflow
        today; the paging is there so a watched workflow past the page cannot
        one day read as renamed on every sweep."""
        pages = [
            {"total_count": 3, "workflows": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]},
            {"total_count": 3, "workflows": [{"id": 3, "name": "c"}]},
        ]
        calls = []

        def opener(request):
            calls.append(request)
            return _response(json.dumps(pages[len(calls) - 1]).encode())

        api = notifier.GitHubAPI("gke-labs/kube-agents", "t", opener=opener, sleep=lambda _: None)
        with mock.patch.object(notifier, "PER_PAGE", 2):
            workflows = api.workflows()
        self.assertEqual([w["id"] for w in workflows], [1, 2, 3])
        self.assertEqual(len(calls), 2)
        self.assertIn("per_page=2&page=1", calls[0].full_url)
        self.assertIn("page=2", calls[1].full_url)

    def test_an_empty_page_ends_the_workflow_list_even_under_its_total(self):
        """A `total_count` the pages never add up to must not spin forever."""
        calls = []

        def opener(request):
            calls.append(request)
            payload = {"total_count": 5, "workflows": [{"id": 1, "name": "a"}] if len(calls) == 1 else []}
            return _response(json.dumps(payload).encode())

        api = notifier.GitHubAPI("gke-labs/kube-agents", "t", opener=opener, sleep=lambda _: None)
        self.assertEqual(len(api.workflows()), 1)
        self.assertEqual(len(calls), 2)

    def test_a_history_page_shorter_than_its_own_count_is_refused(self):
        """The endpoint says how many runs match; a page with fewer than that
        (up to the depth asked for) is a read with a hole in it."""
        api, _ = self._api({"total_count": 3, "workflow_runs": [run(3, "success")]})
        self.assertIsNone(api.history(77))
        api, _ = self._api({"total_count": 1, "workflow_runs": [run(3, "success")]})
        self.assertEqual(len(api.history(77)), 1)
        full = [run(n, "success") for n in range(notifier.HISTORY_DEPTH)]
        api, _ = self._api({"total_count": 500, "workflow_runs": full})
        self.assertEqual(len(api.history(77)), notifier.HISTORY_DEPTH)

    def test_a_deleted_run_reads_as_absent(self):
        calls = []

        def opener(request):
            calls.append(request)
            raise urllib.error.HTTPError("u", 404, "gone", {}, None)

        api = notifier.GitHubAPI("gke-labs/kube-agents", "t", opener=opener, sleep=lambda _: None)
        self.assertFalse(api.run_exists(1010))
        self.assertTrue(calls[0].full_url.endswith("/actions/runs/1010"))

    def test_the_labelled_issue_list_is_read_once_per_state(self):
        """A sweep reconciles every watched workflow; the list they filter is
        the same list, so it is fetched once."""
        api, calls = self._api([])
        api.issues_for_workflow(77, "open")
        api.issues_for_workflow(88, "open")
        api.issues_for_workflow(77, "closed")
        self.assertEqual(len(calls), 2)

    def test_the_issue_query_is_scoped_to_the_labelled_issues_in_one_state(self):
        for state in ("open", "closed"):
            with self.subTest(state=state):
                api, calls = self._api([])
                api.issues_for_workflow(77, state)
                url = calls[0].full_url
                self.assertIn(f"state={state}", url)
                self.assertIn(urllib.parse.quote(notifier.LABEL, safe=""), url)

    def test_pull_requests_are_excluded_from_the_issue_list(self):
        """`/issues` returns pull requests too. One carrying the marker -- this
        change's own pull request quotes it -- would be commented on and closed
        as though it were the tracking issue."""
        marker = notifier.workflow_marker(77) + "episode=10 -->"
        api, _ = self._api(
            [
                {"number": 1, "body": marker, "pull_request": {"url": "..."}},
                {"number": 2, "body": marker},
                {"number": 3, "body": "unrelated issue"},
                {"number": 4, "body": None},
            ]
        )
        self.assertEqual([i["number"] for i in api.issues_for_workflow(77, "open")], [2])

    def test_the_token_and_api_version_are_sent(self):
        api, calls = self._api({"workflow_runs": []})
        api.history(77)
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer t")
        self.assertEqual(calls[0].get_header("X-github-api-version"), "2022-11-28")

    def test_closing_an_issue_rereads_the_body_then_patches_once_with_the_stamp(self):
        """`state_reason` is what makes the issue read as completed rather than
        as abandoned in the issue list; the stamp rides in the same write so a
        failure between them cannot leave an open issue already stamped; and
        the body is the one GitHub has now, not the one a minutes-old list
        carried, so a note written in between survives."""
        api, calls = self._api({"number": 901, "body": "text, with a fresh note\n\n<!-- marker -->\n"})
        api.close_issue({"number": 901, "body": "text\n\n<!-- marker -->\n"}, "<!-- main-broken fixed-by=12 run=1012 -->")
        self.assertEqual([call.method for call in calls], ["GET", "PATCH"])
        self.assertTrue(calls[1].full_url.endswith("/repos/gke-labs/kube-agents/issues/901"))
        self.assertEqual(
            json.loads(calls[1].data),
            {
                "body": "text, with a fresh note\n\n<!-- marker -->\n<!-- main-broken fixed-by=12 run=1012 -->",
                "state": "closed",
                "state_reason": notifier.CLOSE_COMPLETED,
            },
        )

    def test_an_older_fixed_by_stamp_without_a_run_id_still_names_the_run(self):
        stamped = {"number": 1, "body": notifier.workflow_marker(77) + "episode=10 -->\n<!-- main-broken fixed-by=11 -->"}
        self.assertEqual(notifier.runs_named(stamped), {10: None, 11: None})

    def test_creating_an_issue_carries_the_label(self):
        """Without it `issues_for_workflow` never finds the issue again."""
        api, calls = self._api({"number": 901})
        api.create_issue("t", "b")
        self.assertEqual(json.loads(calls[0].data)["labels"], [notifier.LABEL])


class FakeAPI:
    """Records what `reconcile` asks of the API and answers from two lists, the
    open and the closed issues, which are the whole of the state it reads. The
    writes land on those lists, so a second reconciliation sees what the first
    one left -- which is how the sweep's idempotency is observed."""

    def __init__(self, open_issues=(), closed_issues=(), deleted_runs=()):
        self.open_issues = list(open_issues)
        self.closed_issues = list(closed_issues)
        self.deleted_runs = set(deleted_runs)
        self.actions = []
        self.next_number = 900

    def run_exists(self, run_id):
        return run_id not in self.deleted_runs

    def issues_for_workflow(self, workflow_id, state):
        prefix = notifier.workflow_marker(workflow_id)
        issues = self.open_issues if state == "open" else self.closed_issues
        return [issue for issue in issues if prefix in (issue.get("body") or "")]

    def ensure_label(self):
        self.actions.append(("label",))

    def create_issue(self, title, body):
        self.next_number += 1
        self.actions.append(("create", self.next_number, title, body))
        created = {"number": self.next_number, "title": title, "body": body, "state": "open"}
        self.open_issues.append(created)
        return created

    def update_issue(self, number, **fields):
        self.actions.append(("update", number, fields))
        for issue in self.open_issues:
            if issue["number"] == number:
                issue.update(fields)

    def comment(self, number, body):
        self.actions.append(("comment", number, body))

    def close_issue(self, closing, stamp):
        number = closing["number"]
        self.actions.append(("close", number))
        for issue in self.open_issues:
            if issue["number"] == number:
                issue["body"] = (issue.get("body") or "").rstrip() + "\n" + stamp
                issue["closed_at"] = "2026-09-04T00:00:00Z"
                issue["closed_by"] = {"login": notifier.OWN_CLOSER_LOGIN}
                self.closed_issues.append(issue)
        self.open_issues = [issue for issue in self.open_issues if issue["number"] != number]

    def kinds(self):
        return [action[0] for action in self.actions]


def decided(current, history):
    """A notification as `report` hands it to `reconcile`: decided over the
    reporting history, with the window of the page it was read from. `history`
    is the page minus `current`, newest first, as the tests write it."""
    page = [current] + list(history)
    notification = notifier.decide(current, notifier.reporting_history(page, current))
    notification["window"] = notifier.history_window(page)
    return notification


def full_page(newest, *reds):
    """A page of `HISTORY_DEPTH` runs ending at `newest`, red where named.
    What a workflow with more runs than the depth returns."""
    return [
        run(number, "failure" if number in reds else "success")
        for number in range(newest, newest - notifier.HISTORY_DEPTH, -1)
    ]


def issue(number, workflow_id, episode, state="open"):
    return {
        "number": number,
        "state": state,
        "body": f"whatever\n\n<!-- main-broken workflow={workflow_id} episode={episode} -->",
    }


class ReconcileTest(unittest.TestCase):
    REPO = "gke-labs/kube-agents"

    def _reconcile(self, api, decision, workflow_id=77):
        return notifier.reconcile(api, decision, self.REPO, workflow_id)

    def test_a_first_failure_opens_an_issue(self):
        api = FakeAPI()
        self._reconcile(api, decided(run(10, "failure"), [run(9, "success")]))
        self.assertEqual(api.kinds(), ["label", "create"])

    def test_a_follow_up_updates_the_existing_issue_rather_than_opening_another(self):
        """The failure that would flood the label with one issue per red run."""
        api = FakeAPI([issue(901, 77, 10)])
        self._reconcile(api, decided(run(11, "failure"), [run(10, "failure"), run(9, "success")]))
        self.assertEqual(api.kinds(), ["update", "comment"])
        self.assertEqual(api.actions[0][1], 901)

    def test_a_recovery_closes_the_issue_and_stamps_the_fixing_run(self):
        api = FakeAPI([issue(901, 77, 10)])
        self._reconcile(api, decided(run(12, "success"), [run(11, "failure"), run(10, "failure")]))
        self.assertEqual(api.kinds(), ["comment", "close"])
        self.assertIn("Fixed by", api.actions[0][2])
        self.assertIn(notifier.FIXED_BY_STAMP.format(number=12, run_id=1012), api.closed_issues[0]["body"])

    def test_a_recovery_with_nothing_open_is_quiet(self):
        """Main was already red when this workflow was added, or someone closed
        the issue by hand. Opening one just to close it would be noise."""
        api = FakeAPI()
        result = self._reconcile(api, decided(run(12, "success"), [run(11, "failure")]))
        self.assertEqual(api.actions, [])
        self.assertIn("no issue is open", result)

    def test_an_ordinary_green_run_writes_nothing(self):
        """Nearly every run. It costs one list request to be sure, and that is
        the price of never leaving an issue open on a green main."""
        api = FakeAPI()
        self._reconcile(api, decided(run(12, "success"), [run(11, "success")]))
        self.assertEqual(api.actions, [])

    def test_a_green_run_closes_an_open_issue_even_with_no_failure_behind_it(self):
        """The self-heal, and the reason `decide` no longer infers "recovered"
        from the history. Three ways to get here, all real: the notify run that
        would have closed the issue was cancelled out of its concurrency group;
        a red run was re-run and passed, so its own history reads green after
        green; or two runs were handled out of order."""
        api = FakeAPI([issue(901, 77, 10)])
        self._reconcile(api, decided(run(12, "success"), [run(11, "success"), run(10, "success")]))
        self.assertEqual(api.kinds(), ["comment", "close"])
        self.assertIn("still open", api.actions[0][2])
        self.assertNotIn("Fixed by", api.actions[0][2], "this run is not the fix and must not claim to be")

    def test_another_workflows_issue_is_left_alone(self):
        """Several workflows are watched and each breaks independently. Closing
        another one's issue would hide a real breakage."""
        api = FakeAPI([issue(901, 88, 10)])
        self._reconcile(api, decided(run(12, "success"), [run(11, "failure")]))
        self.assertEqual(api.actions, [])

    def test_redelivering_the_break_that_opened_the_issue_does_not_duplicate_it(self):
        """GitHub redelivers `workflow_run` events, and a re-run of the notifier
        replays one deliberately. For a `broken` this is free -- there is no
        comment to repeat -- so the guarantee tested here is only that a second
        issue is not opened."""
        api = FakeAPI([issue(901, 77, 10)])
        self._reconcile(api, decided(run(10, "failure"), [run(9, "success")]))
        self.assertEqual(api.kinds(), ["update"], "a new break on an existing issue should not comment")

    def test_redelivering_a_follow_up_writes_nothing_the_second_time(self):
        """The scheduled sweep lands here every fifteen minutes for as long as
        main stays red, and a redelivered `workflow_run` lands here too. The
        first pass rewrote the body and commented; a second pass that finds
        the body already saying so must do neither, or a broken main becomes a
        "Still failing" comment on every tick."""
        api = FakeAPI([issue(901, 77, 10)])
        decision = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        self._reconcile(api, decision)
        result = self._reconcile(api, decision)
        self.assertEqual(api.kinds(), ["update", "comment"])
        self.assertIn("already says so", result)

    def test_a_body_github_hands_back_with_crlf_still_reads_as_current(self):
        """GitHub may return `\r\n` for the `\n` it was sent. The rows are
        what is compared, and the row pattern has to survive that too."""
        decision = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        body = notifier.render_body(decision, self.REPO, notifier.episode_marker(decision, 77))
        stored = issue(901, 77, 10)
        stored["title"] = notifier.render_title(decision)
        stored["body"] = body.replace("\n", "\r\n") + "\r\n"
        api = FakeAPI([stored])
        self._reconcile(api, decision)
        self.assertEqual(api.actions, [])

    def test_a_hand_renamed_issue_is_restored_without_a_comment(self):
        """The title is rewritten so the label stays legible, but the commits
        the body lists are not news and nobody needs a "Still failing" for a
        rename."""
        decision = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        body = notifier.render_body(decision, self.REPO, notifier.episode_marker(decision, 77))
        stored = issue(901, 77, 10)
        stored["title"] = "someone renamed this"
        stored["body"] = body.replace("| run | commit |", "A note.\n\n| run | commit |")
        api = FakeAPI([stored])
        self._reconcile(api, decision)
        self.assertEqual(api.kinds(), ["update"])
        self.assertEqual(api.actions[0][2], {"title": notifier.render_title(decision)}, "title only; the note stays")

    def test_a_new_commit_on_a_broken_main_still_updates_and_comments(self):
        """The idempotency check must not swallow real news: a body that lists
        one commit fewer than the history is a change, and the comment is how
        a subscriber hears about it."""
        earlier = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        stored = issue(901, 77, 10)
        stored["title"] = notifier.render_title(earlier)
        stored["body"] = notifier.render_body(earlier, self.REPO, notifier.episode_marker(earlier, 77))
        api = FakeAPI([stored])
        later = decided(run(12, "failure"), [run(11, "failure"), run(10, "failure"), run(9, "success")])
        self._reconcile(api, later)
        self.assertEqual(api.kinds(), ["update", "comment"])
        self.assertIn("3 consecutive failures", api.actions[1][2])

    def test_the_first_failure_reconciled_twice_opens_one_issue(self):
        """A sweep and an event run can both handle a fresh red. The second
        finds the first's issue current and leaves it alone."""
        api = FakeAPI()
        decision = decided(run(10, "failure"), [run(9, "success")])
        self._reconcile(api, decision)
        self._reconcile(api, decision)
        self.assertEqual(api.kinds(), ["label", "create"])

    def test_a_stale_issue_from_an_earlier_breakage_is_closed(self):
        """Only reachable if a close failed, but two open issues both claiming
        main is broken is worse than the missed close that caused it."""
        api = FakeAPI([issue(901, 77, 5)])
        self._reconcile(api, decided(run(100, "failure"), full_page(99)))
        self.assertEqual(api.kinds(), ["label", "create", "comment", "close"])
        self.assertIn("Superseded by #901", api.actions[2][2])
        self.assertEqual(api.actions[3][1], 901)
        self.assertIn(notifier.SUPERSEDED_PREFIX, api.closed_issues[0]["body"])

    def test_a_recovery_closes_every_open_issue_for_the_workflow(self):
        """Same repair: main is green, so nothing about this workflow should
        still be claiming otherwise."""
        api = FakeAPI([issue(901, 77, 98), issue(902, 77, 5)])
        self._reconcile(api, decided(run(100, "success"), full_page(99, 99, 98)))
        self.assertEqual(api.kinds(), ["comment", "close", "comment", "close"])


class HandCloseAndOrderingTest(unittest.TestCase):
    """Two bounds on what a reconciliation may undo: a close made after the
    streak's last change, whoever made it, and an issue about a red newer than
    the green being handled. Both matter more now that a sweep reconciles
    every fifteen minutes."""

    REPO = "gke-labs/kube-agents"

    def _reconcile(self, api, decision, workflow_id=77):
        return notifier.reconcile(api, decision, self.REPO, workflow_id)

    BEFORE = "2026-08-31T00:00:00Z"  # earlier than every fixture run's updated_at
    AFTER = "2026-09-02T00:00:00Z"  # later than every fixture run's updated_at
    LATER_STILL = "2026-09-03T00:00:00Z"

    def _closed_listing(self, decision, closed_at):
        """An issue the notifier itself wrote for `decision`, then closed."""
        body = notifier.render_body(decision, self.REPO, notifier.episode_marker(decision, 77))
        return {
            "number": 901,
            "state": "closed",
            "title": notifier.render_title(decision),
            "body": body,
            "closed_at": closed_at,
        }

    def test_an_issue_closed_after_the_streaks_last_change_is_not_refiled(self):
        """Without this the sweep reopens a dismissed issue within fifteen
        minutes, under a new number, after every close. Whoever closed it did
        so after run 11, the newest red, last changed."""
        decision = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        api = FakeAPI(closed_issues=[self._closed_listing(decision, self.AFTER)])
        result = self._reconcile(api, decision)
        self.assertEqual(api.actions, [])
        self.assertIn("closed after", result)

    def test_a_red_that_finished_after_the_close_is_filed_again(self):
        """A merged pull request that says `Fixes #901` closes the issue through
        Prow and then goes red itself. That run changed after the close, so
        the close is not a dismissal of it, whoever performed the close."""
        earlier = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        api = FakeAPI(closed_issues=[self._closed_listing(earlier, self.AFTER)])
        fix_that_broke = run(12, "failure")
        fix_that_broke["updated_at"] = self.LATER_STILL
        later = decided(fix_that_broke, [run(11, "failure"), run(10, "failure"), run(9, "success")])
        self._reconcile(api, later)
        self.assertEqual(api.kinds(), ["label", "create"])

    def test_the_workflows_own_green_close_does_not_dismiss_the_same_run_going_red_again(self):
        """Run 10 red opened #901; a re-run of 10 went green and the bot closed
        #901; a further re-run of 10 goes red. The re-run moved run 10's
        timestamp past the close, so it is new evidence and files."""
        rerun_red = run(10, "failure")
        rerun_red["updated_at"] = self.LATER_STILL
        decision = decided(rerun_red, [run(9, "success")])
        api = FakeAPI(closed_issues=[self._closed_listing(decided(run(10, "failure"), [run(9, "success")]), self.AFTER)])
        self._reconcile(api, decision)
        self.assertEqual(api.kinds(), ["label", "create"])

    def test_a_stale_read_after_an_unstamped_close_does_not_refile(self):
        """Main recovered at 12 and #901 was closed without a fixed-by stamp:
        by a person, or by this script before it stamped. A later read returns
        a page from before the recovery: 11 red, 10 red, 9 green. The
        completeness check passes -- the page lacks only the green, which the
        unstamped issue does not name -- so the close postdating every run in
        the streak is what keeps a "main is broken" issue from opening against
        a green main. A stamped close is caught earlier, as a hole."""
        decision = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        api = FakeAPI(closed_issues=[self._closed_listing(decision, self.AFTER)])
        self._reconcile(api, decision)
        self.assertEqual(api.actions, [])

    def test_a_closed_issue_that_predates_the_streak_is_not_a_dismissal(self):
        decision = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        api = FakeAPI(closed_issues=[self._closed_listing(decision, self.BEFORE)])
        self._reconcile(api, decision)
        self.assertEqual(api.kinds(), ["label", "create"])

    def test_a_superseded_close_left_behind_does_not_silence_a_later_red(self):
        """State an older version of this script could leave: #901 (episode
        10) closed as superseded while #902 (episode 11) stayed open, the
        completed history then reading 12, 11, 10 red again. Run 10's re-run
        finished after the close, so it files and #902 is superseded."""
        first = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        api = FakeAPI(closed_issues=[self._closed_listing(first, self.AFTER)])
        api.open_issues.append(issue(902, 77, 11))
        rerun = run(10, "failure")
        rerun["updated_at"] = self.LATER_STILL
        after_rerun = decided(run(12, "failure"), [run(11, "failure"), rerun, run(9, "success")])
        self._reconcile(api, after_rerun)
        self.assertEqual(api.kinds(), ["label", "create", "comment", "close"])
        self.assertEqual(api.actions[3][1], 902)

    def test_the_workflows_own_superseded_close_is_never_a_dismissal(self):
        """A read that omitted a green once made run 10 look like the episode
        again: the notifier opened a new issue and closed the right one, #902,
        as superseded. When the next read is whole, that superseded close --
        made after every run in the streak changed -- must not read as someone
        having dealt with #902's breakage, or main sits red with nothing open."""
        decision = decided(run(12, "failure"), [run(11, "success"), run(10, "failure")])
        superseded = self._closed_listing(decision, self.AFTER)
        superseded["number"] = 902
        superseded["body"] += "\n" + notifier.SUPERSEDED_STAMP.format(number=903)
        api = FakeAPI(closed_issues=[superseded])
        self._reconcile(api, decision)
        self.assertEqual(api.kinds(), ["label", "create"])

    def test_an_unstamped_close_by_the_workflows_own_token_is_not_a_dismissal(self):
        """The script on main before this change closed as superseded without
        a stamp. Such a close, met after the change, could be a supersede or a
        recovery, so it counts for nothing and the breakage files."""
        decision = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        closed = self._closed_listing(decision, self.AFTER)
        closed["closed_by"] = {"login": notifier.OWN_CLOSER_LOGIN}
        api = FakeAPI(closed_issues=[closed])
        self._reconcile(api, decision)
        self.assertEqual(api.kinds(), ["label", "create"])

    def test_a_persons_close_as_not_planned_is_a_dismissal(self):
        """"Not planned" is the reason a person picks for a false alarm, so it
        must count exactly like any other close after the streak's last change."""
        decision = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        closed = self._closed_listing(decision, self.AFTER)
        closed["state_reason"] = "not_planned"
        api = FakeAPI(closed_issues=[closed])
        self._reconcile(api, decision)
        self.assertEqual(api.actions, [])

    def test_a_note_written_into_the_body_survives_a_sweep_without_a_comment(self):
        """The body is rewritten only when the set of rows changes, so a
        person's note stays until there is news, and no "Still failing" is
        posted for a commit that is not news."""
        decision = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        body = notifier.render_body(decision, self.REPO, notifier.episode_marker(decision, 77))
        annotated = issue(901, 77, 10)
        annotated["title"] = notifier.render_title(decision)
        annotated["body"] = body.replace("| run | commit |", "Root cause: X, fix in #1700.\n\n| run | commit |")
        api = FakeAPI([annotated])
        self._reconcile(api, decision)
        self.assertEqual(api.actions, [])

    def test_a_dismissal_still_supersedes_an_older_issue_left_open(self):
        """A stale older-episode issue whose close once failed must not sit
        open behind a dismissed newer episode until the next green."""
        decision = decided(run(100, "failure"), full_page(99, 99))
        api = FakeAPI([issue(880, 77, 3)], closed_issues=[self._closed_listing(decision, self.AFTER)])
        result = self._reconcile(api, decision)
        self.assertEqual(api.kinds(), ["comment", "close"])
        self.assertEqual(api.actions[1][1], 880)
        self.assertIn("superseded #880", result)

    def test_the_next_breakage_after_a_dismissal_is_filed(self):
        """A dismissal is about one episode. A green in between makes the next
        red a new episode with a new marker, and it opens as usual."""
        dismissed = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        api = FakeAPI(closed_issues=[self._closed_listing(dismissed, self.AFTER)])
        later = decided(run(13, "failure"), [run(12, "success"), run(11, "failure"), run(10, "failure"), run(9, "success")])
        self._reconcile(api, later)
        self.assertEqual(api.kinds(), ["label", "create"])

    def test_two_open_issues_with_one_marker_collapse_to_one(self):
        """The sweep and an event run both opened the same episode inside the
        same second. The next reconciliation keeps one and closes the other as
        superseded -- the workflow header promises this, so it is pinned."""
        decision = decided(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        api = FakeAPI([issue(902, 77, 10), issue(901, 77, 10)])  # newest first, as the API lists them
        self._reconcile(api, decision)
        closed = [action[1] for action in api.actions if action[0] == "close"]
        self.assertEqual(closed, [902], "the older issue, the one people were notified of, stays")
        self.assertEqual([i["number"] for i in api.open_issues], [901])

    def test_a_green_does_not_close_an_issue_about_a_newer_red(self):
        """A sweep read a green history at T; a red finished just after and
        its event run opened an issue before the sweep listed open issues. The
        green is older than the red and must leave it alone."""
        api = FakeAPI([issue(901, 77, 13)])
        result = self._reconcile(api, decided(run(12, "success"), [run(11, "success")]))
        self.assertEqual(api.actions, [])
        self.assertIn("left open #901", result)

    def test_an_issue_a_person_reopened_is_left_alone_by_a_green(self):
        """Reopening is a decision. Without this the sweep closes it again
        every fifteen minutes, with a fresh "passes again" comment each time."""
        reopened = issue(901, 77, 10)
        reopened["state_reason"] = notifier.REOPENED
        api = FakeAPI([reopened])
        result = self._reconcile(api, decided(run(12, "success"), [run(11, "success"), run(10, "failure")]))
        self.assertEqual(api.actions, [])
        self.assertIn("reopened by hand", result)

    def test_a_green_still_closes_every_older_episode(self):
        """Episode 5 is older than the full page can show, which is the one
        honest reason for a named run to be absent; episode 101 is newer than
        the green and stays."""
        api = FakeAPI([issue(901, 77, 101), issue(902, 77, 98), issue(903, 77, 5)])
        self._reconcile(api, decided(run(100, "success"), full_page(99, 99, 98)))
        closed = [action[1] for action in api.actions if action[0] == "close"]
        self.assertEqual(sorted(closed), [902, 903])

    def test_an_issue_with_a_damaged_marker_still_closes_on_green(self):
        """`episode_of` reads 0 for a body that lost its episode number, which
        is older than any run -- the issue is closed rather than kept open."""
        damaged = {"number": 904, "state": "open", "body": notifier.workflow_marker(77) + "-->"}
        self.assertEqual(notifier.episode_of(damaged), 0)
        api = FakeAPI([damaged])
        self._reconcile(api, decided(run(12, "success"), [run(11, "success")]))
        self.assertEqual(api.kinds(), ["comment", "close"])


class IncompleteReadTest(unittest.TestCase):
    """The run-history endpoint has answered with pages missing recent runs. A
    reconciliation that can see its read is missing a run the issues name
    writes nothing, whichever way the hole would have pushed it."""

    REPO = "gke-labs/kube-agents"

    def _decision(self, current, runs):
        decision = notifier.decide(current, notifier.reporting_history(runs, current))
        decision["window"] = notifier.history_window(runs)
        return decision

    def _listing(self, number, decision):
        body = notifier.render_body(decision, self.REPO, notifier.episode_marker(decision, 77))
        return {"number": number, "state": "open", "title": notifier.render_title(decision), "body": body}

    def _reconcile(self, api, decision):
        return notifier.reconcile(api, decision, self.REPO, 77)

    def test_runs_named_reads_the_marker_and_every_row_with_its_run_id(self):
        decision = self._decision(run(12, "failure"), [run(12, "failure"), run(11, "failure"), run(10, "failure")])
        self.assertEqual(notifier.runs_named(self._listing(1, decision)), {10: 1010, 11: 1011, 12: 1012})

    def test_a_run_deleted_from_github_is_not_a_hole(self):
        """An administrator deleted run 11 (a leaked secret in its log). It
        will never be listed again; a read without it must still act, or the
        workflow's issues freeze for as long as the deleted run is inside the
        window."""
        full = [run(12, "failure"), run(11, "failure"), run(10, "failure"), run(9, "success")]
        open_issue = self._listing(901, self._decision(run(12, "failure"), full))
        api = FakeAPI([open_issue], deleted_runs={1011})
        holed = [run(12, "failure"), run(10, "failure"), run(9, "success")]
        self._reconcile(api, self._decision(run(12, "failure"), holed))
        self.assertEqual(api.kinds(), ["update"], "the table lost a row, which rewrites it; nothing landed, so no comment")

    def test_a_deleted_fixing_green_is_not_a_permanent_hole(self):
        """The fixed-by stamp carries the green run's id, so a green an
        administrator deleted is excused like a deleted row rather than
        blocking every new episode until it ages past a full page."""
        api = FakeAPI([self._listing(901, self._decision(run(10, "failure"), [run(10, "failure"), run(9, "success")]))])
        self._reconcile(api, self._decision(run(11, "success"), [run(11, "success"), run(10, "failure"), run(9, "success")]))
        api.deleted_runs.add(1011)
        later_red = run(12, "failure")
        later_red["updated_at"] = "2026-09-05T00:00:00Z"  # after the close, as a real relapse is
        relapse = self._decision(later_red, [later_red, run(10, "failure"), run(9, "success")])
        self._reconcile(api, relapse)
        self.assertEqual(api.kinds()[-2:], ["label", "create"])

    def test_a_read_missing_the_episode_does_not_open_a_second_issue(self):
        """2026-09-17 on main: #1677 (episode 5928, rows 5928 and 6008) was
        open; the notify for 6009 read a history without either and opened
        #1679 as a fresh episode, closing #1677 as superseded."""
        full = [run(6009, "failure"), run(6008, "failure"), run(5928, "failure"), run(5927, "success")]
        open_issue = self._listing(1677, self._decision(run(6008, "failure"), full[1:]))
        api = FakeAPI([open_issue])
        result = self._reconcile(api, self._decision(run(6009, "failure"), [run(6009, "failure")]))
        self.assertEqual(api.actions, [])
        self.assertIn("missing runs", result)
        self.assertIn("5928", result)

    def test_the_same_read_done_whole_updates_the_right_issue(self):
        full = [run(6009, "failure"), run(6008, "failure"), run(5928, "failure"), run(5927, "success")]
        open_issue = self._listing(1677, self._decision(run(6008, "failure"), full[1:]))
        api = FakeAPI([open_issue])
        self._reconcile(api, self._decision(run(6009, "failure"), full))
        self.assertEqual(api.kinds(), ["update", "comment"])
        self.assertEqual(api.actions[0][1], 1677)

    def test_a_read_missing_a_middle_row_does_not_shorten_the_streak(self):
        full = [run(12, "failure"), run(11, "failure"), run(10, "failure"), run(9, "success")]
        open_issue = self._listing(901, self._decision(run(12, "failure"), full))
        api = FakeAPI([open_issue])
        holed = [run(12, "failure"), run(10, "failure"), run(9, "success")]
        result = self._reconcile(api, self._decision(run(12, "failure"), holed))
        self.assertEqual(api.actions, [])
        self.assertIn("11", result)

    def test_a_green_read_that_lacks_the_reds_does_not_close(self):
        """Closing would be the right end state, but the comment would name
        the wrong fix and the wrong count; a whole read is minutes away."""
        open_issue = self._listing(901, self._decision(run(11, "failure"), [run(11, "failure"), run(10, "failure")]))
        api = FakeAPI([open_issue])
        self._reconcile(api, self._decision(run(12, "success"), [run(12, "success")]))
        self.assertEqual(api.actions, [])
        self._reconcile(api, self._decision(run(12, "success"), [run(12, "success"), run(11, "failure"), run(10, "failure")]))
        self.assertEqual(api.kinds(), ["comment", "close"])
        self.assertIn("Fixed by", api.actions[0][2])

    def test_a_full_page_may_omit_runs_older_than_itself(self):
        """An issue from before the window is the accepted long-streak case,
        not a hole in the read: it is superseded as before."""
        page = [run(n, "failure") if n == 69 else run(n, "success") for n in range(69, 69 - notifier.HISTORY_DEPTH, -1)]
        self.assertEqual(len(page), notifier.HISTORY_DEPTH)
        api = FakeAPI([issue(880, 77, 5)])
        self._reconcile(api, self._decision(run(69, "failure"), page))
        self.assertEqual(api.kinds(), ["label", "create", "comment", "close"])

    def test_a_closed_issue_naming_an_unlisted_run_blocks_a_fresh_episode(self):
        """Nothing open, and the read is a short page: a closed issue for this
        workflow names a run the page lacks, so the page is a hole and the
        fresh episode it suggests is not opened."""
        old = self._listing(880, self._decision(run(6, "failure"), [run(6, "failure"), run(5, "failure"), run(4, "success")]))
        old["state"] = "closed"
        old["closed_at"] = "2026-08-31T00:00:00Z"
        api = FakeAPI(closed_issues=[old])
        result = self._reconcile(api, self._decision(run(6, "failure"), [run(6, "failure")]))
        self.assertEqual(api.actions, [])
        self.assertIn("missing runs", result)

    def test_a_page_lacking_the_green_that_ended_an_episode_is_a_hole(self):
        """True history: 10 red, 11 green, 12 red. The green close stamped
        #901 with fixed-by=11. A read of 12 red, 10 red, 9 green -- the green
        missing -- would otherwise reopen episode 10 and blame the fixed
        commit; the stamp lets the check see the hole."""
        api = FakeAPI([self._listing(901, self._decision(run(10, "failure"), [run(10, "failure"), run(9, "success")]))])
        self._reconcile(api, self._decision(run(11, "success"), [run(11, "success"), run(10, "failure"), run(9, "success")]))
        self.assertEqual(api.kinds(), ["comment", "close"])
        self.assertEqual(notifier.runs_named(api.closed_issues[0]), {10: 1010, 11: 1011})
        relapse = self._decision(run(12, "failure"), [run(12, "failure"), run(10, "failure"), run(9, "success")])
        result = self._reconcile(api, relapse)
        self.assertEqual(api.kinds(), ["comment", "close"])
        self.assertIn("11", result)
        whole = self._decision(run(12, "failure"), [run(12, "failure"), run(11, "success"), run(10, "failure")])
        self._reconcile(api, whole)
        self.assertEqual(api.kinds()[-2:], ["label", "create"])
        self.assertIn("episode=12", api.actions[-1][3])

    def test_a_short_page_may_omit_nothing(self):
        """Fewer runs than the depth means the workflow's whole history came
        back, so a run the issue names that is absent from it is missing."""
        api = FakeAPI([issue(880, 77, 5)])
        result = self._reconcile(api, self._decision(run(12, "failure"), [run(12, "failure"), run(11, "success")]))
        self.assertEqual(api.actions, [])
        self.assertIn("5", result)


class MainTest(unittest.TestCase):
    """The wiring, with the API and the reconciliation stubbed."""

    def _main(self, current, history, argv, env):
        api = mock.Mock()
        api.run.return_value = current
        api.history.return_value = history
        with mock.patch.object(notifier, "GitHubAPI", return_value=api), mock.patch.dict(
            "os.environ", env, clear=True
        ), mock.patch.object(notifier, "reconcile", return_value="done") as reconcile:
            return notifier.main(argv), reconcile

    def test_a_break_is_reconciled(self):
        status, reconcile = self._main(
            run(10, "failure"), [run(9, "success")], ["--run-id", "1010"], {"GITHUB_TOKEN": "t"}
        )
        self.assertEqual(status, 0)
        reconcile.assert_called_once()
        self.assertEqual(reconcile.call_args[0][1]["kind"], "broken")

    def test_a_green_run_still_reaches_reconcile(self):
        """It has to: whether there is an issue to close is a question only the
        API can answer. `reconcile` is what makes it cheap when there is not."""
        status, reconcile = self._main(
            run(10, "success"), [run(9, "success")], ["--run-id", "1010"], {"GITHUB_TOKEN": "t"}
        )
        self.assertEqual(status, 0)
        self.assertEqual(reconcile.call_args[0][1]["kind"], "green")

    def test_a_conclusion_that_says_nothing_still_reconciles_what_the_history_says(self):
        """A `neutral` run is as good a reason to look as any. Main is broken at
        run 9 whatever run 10 concluded, and a notify run that went quiet here
        could be the only one of a burst that survived."""
        status, reconcile = self._main(
            run(10, "neutral"), [run(10, "neutral"), run(9, "failure")], ["--run-id", "1010"], {"GITHUB_TOKEN": "t"}
        )
        self.assertEqual(status, 0)
        self.assertEqual(reconcile.call_args[0][1]["kind"], "broken")
        self.assertEqual(reconcile.call_args[0][1]["run"]["run_number"], 9)

    def test_a_run_a_later_one_has_overtaken_reconciles_against_the_later_one(self):
        """Out of order, a red run handled after the green that fixed it would
        open a "main is broken" issue against a green main. Rather than going
        quiet -- which lost the whole notification in #1681 -- it acts on what
        the later run says."""
        status, reconcile = self._main(
            run(11, "failure"),
            [run(12, "success"), run(11, "failure"), run(10, "failure")],
            ["--run-id", "1011"],
            {"GITHUB_TOKEN": "t"},
        )
        self.assertEqual(status, 0)
        notification = reconcile.call_args[0][1]
        self.assertEqual(notification["kind"], "green")
        self.assertEqual(notification["run"]["run_number"], 12)
        self.assertEqual(notification["broke_at"]["run_number"], 10)

    def test_a_surviving_green_of_a_burst_files_for_the_red_whose_notify_was_cancelled(self):
        """#1681, with the run numbers from 2026-09-16. Six merges landed in a
        minute; the red run, 5928, failed fast and finished before the
        greens 5926 and 5927 that carry older commits. The concurrency group
        cancelled two notify runs in the seconds after 5928 finished -- its own
        among them, on the timing -- and a surviving notify run for a green
        such as 5926 deferred to 5928 and exited under the old rule, so nothing
        was filed for nearly fifteen hours. Handling 5926 must file against
        5928."""
        burst = [
            run(5923, "success"),
            run(5924, "success"),
            run(5925, "success"),
            run(5926, "success"),
            run(5927, "success"),
            run(5928, "failure", subject="ci(lint): run make shellcheck as a required check (#1651)"),
        ]
        status, reconcile = self._main(run(5926, "success"), burst, ["--run-id", "6926"], {"GITHUB_TOKEN": "t"})
        self.assertEqual(status, 0)
        notification = reconcile.call_args[0][1]
        self.assertEqual(notification["kind"], "broken")
        self.assertEqual(notification["run"]["run_number"], 5928)
        self.assertEqual(notification["broke_at"]["run_number"], 5928)

    def test_a_waking_run_put_back_into_a_lagging_list_keeps_run_order(self):
        """Green 5926 finished last and is not yet listed, while the faster
        5927 and 5928 (both red) are. Prepended without a sort it would sit in
        front of 5927 and end the streak early: episode 5928 instead of 5927,
        a duplicate issue, and the right one superseded."""
        status, reconcile = self._main(
            run(5926, "success"),
            [run(5928, "failure"), run(5927, "failure"), run(5925, "success")],
            ["--run-id", "6926"],
            {"GITHUB_TOKEN": "t"},
        )
        self.assertEqual(status, 0)
        notification = reconcile.call_args[0][1]
        self.assertEqual(notification["kind"], "still-broken")
        self.assertEqual(notification["broke_at"]["run_number"], 5927)

    def test_a_short_history_read_is_left_alone(self):
        status, reconcile = self._main(run(10, "failure"), None, ["--run-id", "1010"], {"GITHUB_TOKEN": "t"})
        self.assertEqual(status, 0)
        reconcile.assert_not_called()

    def test_a_history_that_has_not_caught_up_with_the_run_still_counts_it(self):
        """The list is read seconds after the run completed. If it lags, the
        run that woke this is known to have completed and must not be lost to
        the sweep."""
        status, reconcile = self._main(
            run(10, "failure"), [run(9, "success")], ["--run-id", "1010"], {"GITHUB_TOKEN": "t"}
        )
        self.assertEqual(status, 0)
        self.assertEqual(reconcile.call_args[0][1]["kind"], "broken")
        self.assertEqual(reconcile.call_args[0][1]["run"]["run_number"], 10)

    def test_a_later_run_that_said_nothing_does_not_silence_this_one(self):
        """A newer `cancelled` run is no statement about the tree, so the
        newest *reporting* run is the one reconciled, and it is red. Read as
        the current state, the cancelled run would leave nothing to file."""
        status, reconcile = self._main(
            run(11, "failure"),
            [run(12, "cancelled"), run(11, "failure"), run(10, "success")],
            ["--run-id", "1011"],
            {"GITHUB_TOKEN": "t"},
        )
        self.assertEqual(status, 0)
        self.assertEqual(reconcile.call_args[0][1]["kind"], "broken")

    def test_a_pull_request_run_is_refused(self):
        """Defence in depth behind the workflow's `if:`: a hand-run against the
        wrong run id must not file a pull request as broken main."""
        pr_run = run(10, "failure")
        pr_run["event"] = "pull_request"
        status, reconcile = self._main(pr_run, [], ["--run-id", "1010"], {"GITHUB_TOKEN": "t"})
        self.assertEqual(status, 0)
        reconcile.assert_not_called()

    def test_a_run_of_an_unwatched_workflow_is_refused(self):
        """The trigger list keeps this from happening in the workflow; a
        hand-run on a `Prettier Check` run would file for a workflow the sweep
        never reconciles or closes."""
        unwatched = run(10, "failure", name="Prettier Check")
        status, reconcile = self._main(unwatched, [unwatched], ["--run-id", "1010"], {"GITHUB_TOKEN": "t"})
        self.assertEqual(status, 0)
        reconcile.assert_not_called()

    def test_a_run_on_another_branch_is_refused(self):
        other = run(10, "failure")
        other["head_branch"] = "release-1.2"
        status, reconcile = self._main(other, [], ["--run-id", "1010"], {"GITHUB_TOKEN": "t"})
        self.assertEqual(status, 0)
        reconcile.assert_not_called()

    def test_dry_run_never_writes(self):
        status, reconcile = self._main(
            run(10, "failure"),
            [run(9, "success")],
            ["--run-id", "1010", "--dry-run"],
            {"GITHUB_TOKEN": "t"},
        )
        self.assertEqual(status, 0)
        reconcile.assert_not_called()

    def test_no_token_is_an_error(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(notifier.main(["--run-id", "1"]), 1)

    def test_a_run_id_and_a_sweep_are_not_both_accepted(self):
        with self.assertRaises(SystemExit):
            notifier.parse_args(["--run-id", "1", "--sweep"])
        with self.assertRaises(SystemExit):
            notifier.parse_args([])


class SweepTest(unittest.TestCase):
    """The scheduled path: no run to start from, every watched workflow."""

    def _sweep(self, workflows, histories, argv=("--sweep",)):
        api = mock.Mock()
        api.workflows.return_value = workflows
        api.history.side_effect = lambda workflow_id, branch: histories.get(workflow_id, [])
        with mock.patch.object(notifier, "GitHubAPI", return_value=api), mock.patch.dict(
            "os.environ", {"GITHUB_TOKEN": "t"}, clear=True
        ), mock.patch.object(notifier, "reconcile", return_value="done") as reconcile:
            return notifier.main(list(argv)), api, reconcile

    def _workflows(self, names_to_ids):
        return [
            {"id": workflow_id, "name": name, "path": f".github/workflows/{workflow_id}.yml"}
            for name, workflow_id in names_to_ids.items()
        ]

    def test_every_watched_workflow_is_reconciled_and_nothing_else(self):
        names_to_ids = {name: 100 + index for index, name in enumerate(notifier.WATCHED_WORKFLOWS)}
        names_to_ids["Prettier Check"] = 999
        histories = {workflow_id: [run(2, "success"), run(1, "failure")] for workflow_id in names_to_ids.values()}
        status, api, reconcile = self._sweep(self._workflows(names_to_ids), histories)
        self.assertEqual(status, 0)
        reconciled = sorted(call.args[3] for call in reconcile.call_args_list)
        self.assertEqual(reconciled, sorted(100 + index for index in range(len(notifier.WATCHED_WORKFLOWS))))
        self.assertNotIn(999, [call.args[0] for call in api.history.call_args_list])

    def test_a_red_found_by_the_sweep_is_filed(self):
        """The whole point: a red whose event was never delivered."""
        names_to_ids = {name: 100 + index for index, name in enumerate(notifier.WATCHED_WORKFLOWS)}
        histories = {workflow_id: [run(2, "success"), run(1, "success")] for workflow_id in names_to_ids.values()}
        histories[100] = [run(3, "failure"), run(2, "success")]
        status, _, reconcile = self._sweep(self._workflows(names_to_ids), histories)
        self.assertEqual(status, 0)
        by_workflow = {call.args[3]: call.args[1] for call in reconcile.call_args_list}
        self.assertEqual(by_workflow[100]["kind"], "broken")
        self.assertEqual(by_workflow[100]["run"]["run_number"], 3)
        self.assertTrue(all(n["kind"] == "green" for w, n in by_workflow.items() if w != 100))

    def test_a_watched_name_no_workflow_carries_reds_the_sweep_after_doing_the_rest(self):
        """`workflow_run` matches on `name:`, so a rename silently stops the
        event path. The sweep is the only thing positioned to notice, and it
        says so with its exit status -- after reconciling what it could find,
        because one renamed workflow is no reason to leave the others unread."""
        names_to_ids = {name: 100 + index for index, name in enumerate(notifier.WATCHED_WORKFLOWS[1:])}
        histories = {workflow_id: [run(1, "success")] for workflow_id in names_to_ids.values()}
        status, _, reconcile = self._sweep(self._workflows(names_to_ids), histories)
        self.assertEqual(status, 1)
        self.assertEqual(reconcile.call_count, len(notifier.WATCHED_WORKFLOWS) - 1)

    def test_a_workflow_with_no_reporting_run_is_skipped(self):
        names_to_ids = {name: 100 + index for index, name in enumerate(notifier.WATCHED_WORKFLOWS)}
        histories = {workflow_id: [run(1, "success")] for workflow_id in names_to_ids.values()}
        histories[100] = [run(1, "cancelled")]
        histories[101] = []
        status, _, reconcile = self._sweep(self._workflows(names_to_ids), histories)
        self.assertEqual(status, 0)
        self.assertEqual(reconcile.call_count, len(notifier.WATCHED_WORKFLOWS) - 2)

    def test_two_workflows_carrying_one_name_are_reconciled_as_the_one_that_ran_last(self):
        """The workflow list keeps a file that was renamed or deleted, listed
        as `active` with its history frozen. Reconciled too, a red at the end
        of that frozen history would be filed forever."""
        names_to_ids = {name: 100 + index for index, name in enumerate(notifier.WATCHED_WORKFLOWS)}
        workflows = self._workflows(names_to_ids)
        ghost_name = notifier.WATCHED_WORKFLOWS[0]
        workflows.append({"id": 999, "name": ghost_name, "path": ".github/workflows/old.yml"})
        workflows[0]["path"] = ".github/workflows/new.yml"
        histories = {workflow_id: [run(1, "success")] for workflow_id in names_to_ids.values()}
        ghost_red = run(40, "failure")
        ghost_red["created_at"] = "2026-01-01T00:00:00Z"
        live_green = run(2, "success")
        live_green["created_at"] = "2026-09-01T00:00:00Z"
        histories[999] = [ghost_red]
        histories[100] = [live_green]
        status, _, reconcile = self._sweep(workflows, histories)
        self.assertEqual(status, 0)
        reconciled = [call.args[3] for call in reconcile.call_args_list]
        self.assertIn(100, reconciled)
        self.assertNotIn(999, reconciled)
        self.assertEqual(len(reconciled), len(notifier.WATCHED_WORKFLOWS))

    def test_a_carrier_with_no_runs_loses_to_one_that_has_run(self):
        names_to_ids = {name: 100 + index for index, name in enumerate(notifier.WATCHED_WORKFLOWS)}
        workflows = self._workflows(names_to_ids)
        workflows.append({"id": 999, "name": notifier.WATCHED_WORKFLOWS[0], "path": ".github/workflows/old.yml"})
        histories = {workflow_id: [run(1, "success")] for workflow_id in names_to_ids.values()}
        histories[999] = []
        status, _, reconcile = self._sweep(workflows, histories)
        self.assertEqual(status, 0)
        self.assertIn(100, [call.args[3] for call in reconcile.call_args_list])

    def test_a_short_history_read_is_skipped_without_reading_as_a_missing_workflow(self):
        names_to_ids = {name: 100 + index for index, name in enumerate(notifier.WATCHED_WORKFLOWS)}
        histories = {workflow_id: [run(1, "success")] for workflow_id in names_to_ids.values()}
        histories[100] = None
        status, _, reconcile = self._sweep(self._workflows(names_to_ids), histories)
        self.assertEqual(status, 0)
        self.assertEqual(reconcile.call_count, len(notifier.WATCHED_WORKFLOWS) - 1)

    def test_a_name_with_one_carrier_unread_is_skipped_rather_than_left_to_the_other(self):
        """If the live carrier's read is refused and the ghost's is not, the
        ghost must not be reconciled by default against its frozen history."""
        names_to_ids = {name: 100 + index for index, name in enumerate(notifier.WATCHED_WORKFLOWS)}
        workflows = self._workflows(names_to_ids)
        workflows.append({"id": 999, "name": notifier.WATCHED_WORKFLOWS[0], "path": ".github/workflows/old.yml"})
        histories = {workflow_id: [run(1, "success")] for workflow_id in names_to_ids.values()}
        histories[100] = None
        histories[999] = [run(40, "failure")]
        status, _, reconcile = self._sweep(workflows, histories)
        self.assertEqual(status, 0)
        reconciled = [call.args[3] for call in reconcile.call_args_list]
        self.assertNotIn(999, reconciled)
        self.assertNotIn(100, reconciled)

    def test_one_workflows_failure_does_not_stop_the_rest_of_the_sweep(self):
        """A 5xx that outlasts the retries on one workflow must not leave the
        other five unread until the next sweep; the sweep still goes red."""
        names_to_ids = {name: 100 + index for index, name in enumerate(notifier.WATCHED_WORKFLOWS)}
        histories = {workflow_id: [run(2, "failure"), run(1, "success")] for workflow_id in names_to_ids.values()}
        api = mock.Mock()
        api.workflows.return_value = self._workflows(names_to_ids)
        api.history.side_effect = lambda workflow_id, branch: histories[workflow_id]
        calls = []

        def reconcile(api_, notification, repo, workflow_id):
            calls.append(workflow_id)
            if workflow_id == 100:
                raise RuntimeError("boom")
            return "done"

        with mock.patch.object(notifier, "GitHubAPI", return_value=api), mock.patch.dict(
            "os.environ", {"GITHUB_TOKEN": "t"}, clear=True
        ), mock.patch.object(notifier, "reconcile", side_effect=reconcile):
            status = notifier.main(["--sweep"])
        self.assertEqual(status, 1)
        self.assertEqual(len(calls), len(notifier.WATCHED_WORKFLOWS))

    def test_a_dry_run_sweep_writes_nothing(self):
        names_to_ids = {name: 100 + index for index, name in enumerate(notifier.WATCHED_WORKFLOWS)}
        histories = {workflow_id: [run(2, "failure"), run(1, "success")] for workflow_id in names_to_ids.values()}
        status, _, reconcile = self._sweep(self._workflows(names_to_ids), histories, ("--sweep", "--dry-run"))
        self.assertEqual(status, 0)
        reconcile.assert_not_called()


class WorkflowShapeTest(unittest.TestCase):
    """The burst fix rests on the concurrency key and the job guard, which no
    linter checks for meaning. Pinned here so a simplification that lets a
    pull-request run back into a push run's group fails a test."""

    def setUp(self):
        self.document = yaml.safe_load(WORKFLOW_FILE.read_text())
        self.triggers = self.document.get("on") or self.document.get(True)

    def test_push_to_main_runs_share_a_group_and_nothing_else_joins_it(self):
        group = self.document["concurrency"]["group"]
        for needed in (
            "github.event_name != 'workflow_run'",
            "github.event.workflow_run.event == 'push'",
            "github.event.workflow_run.head_branch == 'main'",
            "github.event.workflow_run.workflow_id",
            "github.run_id",
        ):
            self.assertIn(needed, group)
        self.assertIs(self.document["concurrency"]["cancel-in-progress"], False)

    def test_the_sweep_has_a_schedule_and_a_dispatch(self):
        self.assertIn("schedule", self.triggers)
        self.assertIn("workflow_dispatch", self.triggers)

    def test_the_job_is_guarded_and_filters_nothing_by_conclusion(self):
        job = self.document["jobs"]["notify"]
        self.assertIn("github.repository == 'gke-labs/kube-agents'", job["if"])
        self.assertNotIn("conclusion", job["if"])
        steps = {step["name"]: step for step in job["steps"] if "if" in step}
        self.assertEqual(steps["Report on the run"]["if"], "github.event_name == 'workflow_run'")
        self.assertEqual(steps["Sweep every watched workflow"]["if"], "github.event_name != 'workflow_run'")


class WatchListTest(unittest.TestCase):
    def test_the_script_and_the_workflow_watch_the_same_workflows(self):
        """The `workflow_run` trigger and `WATCHED_WORKFLOWS` are the same list,
        because a workflow cannot read its own triggers; the third copy, the
        required-check roster in `test_integration_contracts.py`, is a subset
        the trigger must contain, enforced there. A name on one of
        these two and not the other is a workflow the event path watches and
        the sweep does not, or the reverse."""
        document = yaml.safe_load(WORKFLOW_FILE.read_text())
        # PyYAML reads a bare `on:` key as the boolean True (YAML 1.1).
        triggers = document.get("on") or document.get(True)
        self.assertEqual(sorted(triggers["workflow_run"]["workflows"]), sorted(notifier.WATCHED_WORKFLOWS))


if __name__ == "__main__":
    unittest.main()
