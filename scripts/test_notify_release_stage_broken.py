#!/usr/bin/env python3
"""Unit tests for the release-stage notifier.

Run: cd scripts && python3 -m unittest test_notify_release_stage_broken

The fixtures under `testdata/release_stage_broken/` are recorded from the
runs the notifier exists for: `Staging: Promotion Pipeline` #30 (2026-10-21,
the second of the two silent nights), `RC: Release Pipeline` #404 and
`Release & Publish (GA)` #10, each trimmed to the log lines the script reads.
Four failure modes matter more than the rest: staying quiet on a red run,
which is the gap this closes; opening a second issue for a run already
recorded, which the fifteen-minute sweep would turn into a flood; leaving an
issue open after the pipeline passes; and posting to Chat without an issue
to cite.
"""

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

_HERE = Path(__file__).resolve().parent
_RELEASE = _HERE / "release"
for path in (str(_HERE), str(_RELEASE)):
    if path not in sys.path:
        sys.path.insert(0, path)

import notify_release_stage_broken as notifier  # noqa: E402

FIXTURES = _HERE / "testdata" / "release_stage_broken"
WORKFLOWS = _HERE.parent / ".github" / "workflows"
NOTIFIER_WORKFLOW = WORKFLOWS / "release-stage-broken-notify.yml"
REPO = "gke-labs/kube-agents"

STAGING = {"id": 365450384, "name": "Staging: Promotion Pipeline"}
STAGING_RUNS = json.loads((FIXTURES / "staging-runs.json").read_text())
STAGING_JOBS = json.loads((FIXTURES / "staging-30-jobs.json").read_text())
GA_JOBS = json.loads((FIXTURES / "ga-10-jobs.json").read_text())
RUN_30 = STAGING_RUNS["workflow_runs"][0]
RUN_29 = STAGING_RUNS["workflow_runs"][1]
STEP1_JOB = STAGING_JOBS["jobs"][0]["id"]
E2E_JOB = STAGING_JOBS["jobs"][2]["id"]


def fixture(name):
    return (FIXTURES / name).read_text()


def green_run(number, run_id):
    run = dict(RUN_30)
    run.update(id=run_id, run_number=number, conclusion="success", html_url=f"https://github.com/{REPO}/actions/runs/{run_id}")
    return run


class FakeRunner:
    """Answers `gh api` reads from a path map and records every write.

    `responses` maps the exact API path (query string included) to the JSON
    answer; `logs` maps a job id to its raw log. A read with no answer fails
    the way `gh` does, so a test that reaches a path it did not declare is a
    test with a hole in it.
    """

    def __init__(self, responses=None, logs=None):
        self.responses = dict(responses or {})
        self.logs = dict(logs or {})
        self.writes = []
        self.next_issue = 4000

    def __call__(self, argv, input=None, **kwargs):
        assert argv[0] == "gh"
        if argv[1] == "label":
            self.writes.append(("label", argv[3]))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "--allow-escape-sequences" in argv:
            job_id = int(argv[-1].rsplit("/", 2)[1])
            if job_id not in self.logs:
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 410: Gone")
            return subprocess.CompletedProcess(argv, 0, self.logs[job_id], "")
        method, path = argv[3], argv[4]
        if method == "GET":
            if path not in self.responses:
                return subprocess.CompletedProcess(argv, 1, "", f"HTTP 404: Not Found ({path})")
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.responses[path]), "")
        payload = json.loads(input)
        self.writes.append((method, path, payload))
        if method == "POST" and path.endswith("/issues"):
            self.next_issue += 1
            return subprocess.CompletedProcess(argv, 0, json.dumps({"number": self.next_issue}), "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")


class FakeOpener:
    """Records what the Chat sender posts and answers 200."""

    def __init__(self):
        self.posted = []

    def __call__(self, request, timeout=None):
        self.posted.append(json.loads(request.data.decode())["text"])
        opener = self

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Response()


def runs_path(workflow_id=STAGING["id"]):
    return (
        f"repos/{REPO}/actions/workflows/{workflow_id}/runs"
        f"?status=completed&exclude_pull_requests=true&per_page={notifier.HISTORY_DEPTH}"
    )


def jobs_path(run_id):
    return f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page={notifier.PAGE_SIZE}&page=1"


def issues_path(state):
    return f"repos/{REPO}/issues?labels=ci%3Arelease-stage-broken&state={state}&sort=updated&direction=desc&per_page={notifier.ISSUE_PAGE}"


def issue(number, body, state="open"):
    return {"number": number, "body": body, "state": state, "html_url": f"https://github.com/{REPO}/issues/{number}"}


def body_with(run_ids, workflow_id=STAGING["id"]):
    return "\n".join([notifier.marker(workflow_id), notifier.STAMP.format(run_ids=",".join(map(str, run_ids))), "", "body"])


def staging_runner(runs=STAGING_RUNS, open_issues=(), closed_issues=(), logs=None):
    responses = {
        runs_path(): runs,
        jobs_path(RUN_30["id"]): STAGING_JOBS,
        issues_path("open"): list(open_issues),
        issues_path("closed"): list(closed_issues),
    }
    default_logs = {STEP1_JOB: fixture("staging-30-step1.log"), E2E_JOB: fixture("staging-30-e2e.log")}
    return FakeRunner(responses, logs if logs is not None else default_logs)


def reconcile(runner, opener=None, dry_run=False):
    gh = notifier.Gh(REPO, runner=runner, dry_run=dry_run)
    sender = notifier.Sender(space="spaces/AAA", token="t", opener=opener or FakeOpener())
    with mock.patch("sys.stderr", new=io.StringIO()):
        return notifier.reconcile(gh, STAGING, sender, dry_run)


class LogReadingTest(unittest.TestCase):
    """What the script reads out of the logs the three pipelines write."""

    def test_the_staging_resolver_names_tag_and_commit(self):
        candidate = notifier.extract_candidate(fixture("staging-30-step1.log"))
        self.assertEqual(candidate, {"tag": "rc_2610210024_8dc552a_validated", "sha": "8dc552a35eb4fa831ea12058868063e530d1e957"})

    def test_the_rc_pipeline_echoes_its_inputs(self):
        candidate = notifier.extract_candidate(fixture("rc-404-step1.log"))
        self.assertEqual(candidate, {"tag": "rc_2610210122_cbbe254", "sha": "cbbe2541ba1fb8b78770b40c09109bf939bf519c"})

    def test_the_ga_gate_names_the_gate_passing_commit_not_the_empty_input(self):
        """`TARGET_COMMIT:` is echoed empty on a scheduled release; the banner
        below it carries the commit the gate resolved."""
        candidate = notifier.extract_candidate(fixture("ga-10-decide.log"))
        self.assertEqual(candidate, {"tag": "staging_2609301327_d817b3b", "sha": "d817b3b2c4e5f92e84d2a62636d6279b571a82d7"})

    def test_a_second_log_fills_only_the_holes(self):
        first = {"tag": "rc_x"}
        merged = notifier.extract_candidate(fixture("ga-10-publish.log"), first)
        self.assertEqual(merged, {"tag": "rc_x", "sha": "d817b3b2c4e5f92e84d2a62636d6279b571a82d7"})

    def test_a_pytest_summary_line_is_the_first_failure(self):
        line = notifier.extract_first_failure(fixture("staging-30-e2e.log"))
        self.assertTrue(line.startswith("FAILED tests/e2e/test_stockout_investigation.py::test_stockout_ingress_alert_smoke"), line)
        self.assertNotIn("2026-10-21T", line)

    def test_the_chat_suite_failure_is_read_the_same_way(self):
        line = notifier.extract_first_failure(fixture("rc-404-e2e.log"))
        self.assertTrue(line.startswith("FAILED tests/e2e/gchat_agent_test.py::test_gchat_agent_math_response"), line)

    def test_a_script_error_line_beats_the_runner_annotation(self):
        line = notifier.extract_first_failure(fixture("ga-10-publish.log"))
        self.assertTrue(line.startswith("❌ ERROR: Required container images"), line)

    def test_a_log_with_no_failure_line_yields_none(self):
        self.assertIsNone(notifier.extract_first_failure(fixture("staging-30-step1.log")))

    def test_colour_codes_and_timestamps_are_stripped(self):
        self.assertEqual(notifier.clean_log_line("2026-10-21T02:37:11.1014977Z \x1b[36;1mhello\x1b[0m\r"), "hello")


class JobReadingTest(unittest.TestCase):
    def test_the_stage_is_the_first_failed_job_in_run_order(self):
        job = notifier.failing_job(STAGING_JOBS["jobs"])
        self.assertEqual(job["name"], "Step 3 - Run Nightly E2E Matrix / Run E2E (rc)")
        self.assertEqual(notifier.failing_step(job), "Execute Blocking E2E Gate")

    def test_the_ga_publish_job_and_its_step(self):
        job = notifier.failing_job(GA_JOBS["jobs"])
        self.assertEqual(job["name"], "Publish GA Release")
        self.assertEqual(notifier.failing_step(job), "Verify Release Eligibility")

    def test_a_cancelled_job_is_the_stage_only_when_none_failed(self):
        jobs = [{"name": "a", "conclusion": "success"}, {"name": "b", "conclusion": "cancelled"}]
        self.assertEqual(notifier.failing_job(jobs)["name"], "b")
        self.assertIsNone(notifier.failing_job([{"name": "a", "conclusion": "success"}]))


class RenderTest(unittest.TestCase):
    def test_the_stamp_round_trips(self):
        body = body_with([1, 22, 333])
        self.assertEqual(notifier.stamped_runs(body), {1, 22, 333})
        self.assertEqual(notifier.stamped_runs(""), set())
        self.assertEqual(notifier.stamped_runs(None), set())

    def test_the_body_names_stage_candidate_failure_and_streak(self):
        details = {
            "run": RUN_30,
            "stage": "Step 3 - Run Nightly E2E Matrix / Run E2E (rc)",
            "stage_url": "https://example/job",
            "step": "Execute Blocking E2E Gate",
            "failure": "FAILED tests/e2e/x.py::test_y - AssertionError",
            "candidate": {"tag": "rc_1_abc", "sha": "a" * 40},
        }
        body = notifier.render_body(STAGING["name"], STAGING["id"], details, [RUN_29, RUN_30])
        self.assertIn(notifier.marker(STAGING["id"]), body)
        self.assertEqual(notifier.stamped_runs(body), {RUN_29["id"], RUN_30["id"]})
        for expected in ("Step 3 - Run Nightly E2E Matrix / Run E2E (rc)", "Execute Blocking E2E Gate", "rc_1_abc", "a" * 40, "FAILED tests/e2e/x.py::test_y", RUN_30["html_url"], "[#29]", "[#30]", "2026-10-21 02:36 UTC"):
            self.assertIn(expected, body)

    def test_an_unknown_candidate_is_said_not_omitted(self):
        details = {"run": RUN_30, "stage": None, "stage_url": None, "step": None, "failure": None, "candidate": {}}
        body = notifier.render_body(STAGING["name"], STAGING["id"], details, [RUN_30])
        self.assertIn(notifier.CANDIDATE_UNKNOWN, body)
        self.assertIn(notifier.STAGE_UNKNOWN, body)
        self.assertNotIn("Failing runs in this streak", body)

    def test_the_chat_messages_are_short_and_carry_the_links(self):
        details = {"run": RUN_30, "stage": "Step 3", "stage_url": None, "step": "s", "failure": "FAILED t::x", "candidate": {"tag": "rc_1_abc", "sha": "a" * 40}}
        broken = notifier.render_chat_broken(STAGING["name"], details, 4001)
        self.assertEqual(len(broken.splitlines()), 3)
        self.assertIn("🔴", broken)
        self.assertIn("Staging: Promotion Pipeline", broken)
        self.assertIn("Step 3: FAILED t::x", broken)
        self.assertIn("rc_1_abc (aaaaaaa)", broken)
        self.assertIn("#4001", broken)
        self.assertIn(RUN_30["html_url"], broken)
        recovered = notifier.render_chat_recovered(STAGING["name"], details, 4001)
        self.assertEqual(len(recovered.splitlines()), 2)
        self.assertIn("🟢", recovered)
        self.assertIn("#4001", recovered)
        self.assertIn(f"#{RUN_30['run_number']}", recovered)


class ReconcileTest(unittest.TestCase):
    """The state machine against recorded runs: open, update, close, nothing."""

    def test_a_red_run_with_no_issue_opens_one_and_posts(self):
        runner = staging_runner()
        opener = FakeOpener()
        self.assertEqual(reconcile(runner, opener), "opened")
        self.assertEqual(runner.writes[0], ("label", notifier.LABEL))
        method, path, payload = runner.writes[1]
        self.assertEqual((method, path), ("POST", f"repos/{REPO}/issues"))
        self.assertEqual(payload["title"], "🔴 release pipeline stage broken: Staging: Promotion Pipeline")
        self.assertEqual(payload["labels"], [notifier.LABEL])
        self.assertEqual(notifier.stamped_runs(payload["body"]), {RUN_29["id"], RUN_30["id"]})
        self.assertIn("rc_2610210024_8dc552a_validated", payload["body"])
        self.assertIn("test_stockout_ingress_alert_smoke", payload["body"])
        self.assertEqual(len(runner.writes), 2)
        self.assertEqual(len(opener.posted), 1)
        self.assertIn("Tracking #4001", opener.posted[0])
        self.assertIn("Step 3 - Run Nightly E2E Matrix / Run E2E (rc)", opener.posted[0])
        self.assertIn(RUN_30["html_url"], opener.posted[0])

    def test_a_red_run_already_recorded_writes_nothing(self):
        runner = staging_runner(open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        opener = FakeOpener()
        self.assertEqual(reconcile(runner, opener), "nothing")
        self.assertEqual(runner.writes, [])
        self.assertEqual(opener.posted, [])

    def test_a_new_red_run_rewrites_the_body_and_comments_once_without_chat(self):
        runner = staging_runner(open_issues=[issue(4001, body_with([RUN_29["id"]]))])
        opener = FakeOpener()
        self.assertEqual(reconcile(runner, opener), "updated")
        self.assertEqual([(w[0], w[1]) for w in runner.writes], [("PATCH", f"repos/{REPO}/issues/4001"), ("POST", f"repos/{REPO}/issues/4001/comments")])
        self.assertEqual(notifier.stamped_runs(runner.writes[0][2]["body"]), {RUN_29["id"], RUN_30["id"]})
        comment = runner.writes[1][2]["body"]
        self.assertIn("Still failing", comment)
        self.assertIn("[#30]", comment)
        self.assertIn("rc_2610210024_8dc552a_validated", comment)
        self.assertEqual(opener.posted, [])

    def test_a_green_run_closes_the_open_issue_and_posts(self):
        run_31 = green_run(31, 39600000000)
        runs = {"total_count": 7, "workflow_runs": [run_31, *STAGING_RUNS["workflow_runs"]]}
        runner = staging_runner(runs=runs, open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        runner.responses[jobs_path(run_31["id"])] = {"total_count": 1, "jobs": [{"id": 1, "name": "Step 1", "conclusion": "success", "steps": []}]}
        runner.logs[1] = fixture("staging-30-step1.log")
        opener = FakeOpener()
        self.assertEqual(reconcile(runner, opener), "closed")
        self.assertEqual([(w[0], w[1]) for w in runner.writes], [("POST", f"repos/{REPO}/issues/4001/comments"), ("PATCH", f"repos/{REPO}/issues/4001")])
        self.assertIn("[#31]", runner.writes[0][2]["body"])
        self.assertIn("passed", runner.writes[0][2]["body"])
        self.assertEqual(runner.writes[1][2], {"state": "closed"})
        self.assertEqual(len(opener.posted), 1)
        self.assertIn("passes again", opener.posted[0])
        self.assertIn("Closed #4001", opener.posted[0])
        self.assertIn("rc_2610210024_8dc552a_validated", opener.posted[0])

    def test_a_green_run_with_no_issue_writes_nothing(self):
        runs = {"total_count": 7, "workflow_runs": [green_run(31, 39600000000), *STAGING_RUNS["workflow_runs"]]}
        runner = staging_runner(runs=runs)
        opener = FakeOpener()
        self.assertEqual(reconcile(runner, opener), "nothing")
        self.assertEqual(runner.writes, [])
        self.assertEqual(opener.posted, [])

    def test_a_red_run_a_person_closed_the_issue_for_is_not_refiled(self):
        closed = issue(3999, body_with([RUN_29["id"], RUN_30["id"]]), state="closed")
        runner = staging_runner(closed_issues=[closed])
        opener = FakeOpener()
        self.assertEqual(reconcile(runner, opener), "nothing")
        self.assertEqual(runner.writes, [])
        self.assertEqual(opener.posted, [])

    def test_a_newer_red_run_after_a_closed_issue_opens_a_fresh_one(self):
        closed = issue(3999, body_with([RUN_29["id"]]), state="closed")
        runner = staging_runner(closed_issues=[closed])
        self.assertEqual(reconcile(runner), "opened")
        self.assertEqual(runner.writes[1][0], "POST")

    def test_a_cancelled_newest_run_says_nothing(self):
        cancelled = dict(green_run(31, 39600000000), conclusion="cancelled")
        runs = {"total_count": 7, "workflow_runs": [cancelled, *STAGING_RUNS["workflow_runs"]]}
        runner = staging_runner(runs=runs, open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        self.assertEqual(reconcile(runner), "nothing")
        self.assertEqual(runner.writes, [])

    def test_an_unreadable_log_still_files_with_the_candidate_unknown(self):
        runner = staging_runner(logs={})
        self.assertEqual(reconcile(runner), "opened")
        body = runner.writes[1][2]["body"]
        self.assertIn(notifier.CANDIDATE_UNKNOWN, body)
        self.assertIn("Execute Blocking E2E Gate", body)

    def test_an_unconfigured_sender_files_the_issue_and_posts_nothing(self):
        runner = staging_runner()
        gh = notifier.Gh(REPO, runner=runner)
        sender = notifier.Sender(opener=FakeOpener())
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(notifier.reconcile(gh, STAGING, sender, dry_run=False), "opened")
        self.assertEqual(runner.writes[1][0], "POST")
        self.assertIn(notifier.NOT_CONFIGURED, err.getvalue())

    def test_a_dry_run_writes_and_posts_nothing(self):
        runner = staging_runner()
        opener = FakeOpener()
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            gh = notifier.Gh(REPO, runner=runner, dry_run=True)
            sender = notifier.Sender(space="spaces/AAA", token="t", opener=opener)
            self.assertEqual(notifier.reconcile(gh, STAGING, sender, dry_run=True), "opened")
        self.assertEqual(runner.writes, [])
        self.assertEqual(opener.posted, [])
        self.assertIn("would open an issue", err.getvalue())
        self.assertIn("would post to chat", err.getvalue())

    def test_a_failed_read_writes_nothing_and_reds_the_run(self):
        runner = FakeRunner({})
        with mock.patch("sys.stderr", new=io.StringIO()):
            code = notifier.main(["--sweep"], runner=runner, environ={})
        self.assertEqual(code, 1)
        self.assertEqual(runner.writes, [])


class MainTest(unittest.TestCase):
    def test_a_run_of_an_unwatched_workflow_is_declined(self):
        runner = FakeRunner({f"repos/{REPO}/actions/runs/5": {"id": 5, "name": "Docker Build", "workflow_id": 9}})
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(notifier.main(["--run-id", "5"], runner=runner, environ={}), 0)
        self.assertIn("not watched", err.getvalue())
        self.assertEqual(runner.writes, [])

    def test_a_run_id_reconciles_that_workflow(self):
        runner = staging_runner()
        runner.responses[f"repos/{REPO}/actions/runs/{RUN_30['id']}"] = {"id": RUN_30["id"], "name": STAGING["name"], "workflow_id": STAGING["id"]}
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(notifier.main(["--run-id", str(RUN_30["id"])], runner=runner, environ={}, opener=FakeOpener()), 0)
        self.assertEqual(runner.writes[1][0], "POST")

    def test_the_sweep_reds_on_a_watched_name_that_matches_no_workflow(self):
        runner = FakeRunner({f"repos/{REPO}/actions/workflows?per_page={notifier.PAGE_SIZE}&page=1": {"total_count": 1, "workflows": [{"id": 1, "name": "Docker Build"}]}})
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(notifier.main(["--sweep"], runner=runner, environ={}), 1)
        self.assertIn("renamed?", err.getvalue())


class WorkflowContractTest(unittest.TestCase):
    """The watched list matches the workflow files, and the notifier
    workflow has the shape the script and the rules assume."""

    def _document(self, path):
        document = yaml.safe_load(path.read_text())
        return document, (document.get("on") or document.get(True) or {})

    def test_every_watched_name_is_a_release_workflow_in_the_tree(self):
        names = {}
        for path in WORKFLOWS.glob("*.yml"):
            document, _ = self._document(path)
            names[document.get("name")] = path.name
        self.assertEqual(
            {names.get(name) for name in notifier.WATCHED_WORKFLOWS},
            {"rc-release-pipeline.yml", "staging-promotion-pipeline.yml", "release-publish.yml"},
        )

    def test_the_notifier_sweeps_on_a_schedule_and_never_on_workflow_run(self):
        """B4 in tests/conformance forbids the alternative: a workflow_run
        job holding id-token: write has to gate on a successful run."""
        document, triggers = self._document(NOTIFIER_WORKFLOW)
        self.assertIn("schedule", triggers)
        self.assertIn("workflow_dispatch", triggers)
        self.assertNotIn("workflow_run", triggers)
        self.assertEqual(set(triggers["workflow_dispatch"]["inputs"]), {"dry_run", "run_id"})
        self.assertTrue(triggers["workflow_dispatch"]["inputs"]["dry_run"]["default"])

    def test_every_job_is_guarded_and_runs_the_script(self):
        document, _ = self._document(NOTIFIER_WORKFLOW)
        jobs = document["jobs"]
        self.assertTrue(jobs)
        for name, job in jobs.items():
            self.assertIn("github.repository == 'gke-labs/kube-agents'", job["if"], name)
            self.assertEqual(job["permissions"]["issues"], "write", name)
            self.assertEqual(job["permissions"]["id-token"], "write", name)
        text = NOTIFIER_WORKFLOW.read_text()
        self.assertIn("scripts/release/notify_release_stage_broken.py", text)
        self.assertTrue((_RELEASE / "notify_release_stage_broken.py").exists())

    def test_the_chat_variables_match_the_ci_health_poster(self):
        """One space, one identity: the env names and defaults are the
        ones ci-health.yml declares, so the two posters cannot drift."""
        document, _ = self._document(NOTIFIER_WORKFLOW)
        health, _ = self._document(WORKFLOWS / "ci-health.yml")
        for key in ("WIF_PROVIDER", "CI_HEALTH_SA", "CI_HEALTH_CHAT_SPACE", "CI_HEALTH_MUTE"):
            self.assertEqual(document["env"][key], health["env"][key], key)
        self.assertEqual(notifier.SPACE_ENV, "CI_HEALTH_CHAT_SPACE")
        self.assertEqual(notifier.TOKEN_ENV, "CI_HEALTH_CHAT_TOKEN")


if __name__ == "__main__":
    unittest.main()
