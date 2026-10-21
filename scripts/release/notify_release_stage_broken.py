#!/usr/bin/env python3
"""Track a failing release pipeline stage as an issue, and say so in Chat.

A release pipeline run that fails is a red row in the Actions tab and nothing
else. On 2026-10-20 and 2026-10-21 `Staging: Promotion Pipeline` failed two
nights in a row on the same test, the GA scheduler skipped both mornings with
"No commits between 0.16.0 and the gate-passing commit", and the first anyone
knew was a person asking why there had been no release. #1978 describes the
gap for the promotion pipeline; the RC and GA pipelines have the same one.

So this writes it down, the way `scripts/notify_broken_main.py` does for a
required check on `main`. For each watched workflow it reads the newest
completed run that said anything and brings the tracking issue into line:

    failing, no open issue      -> open "release pipeline stage broken: <workflow>"
    failing, issue open         -> rewrite the body for the newest run, one comment
    green, issue open           -> comment and close
    anything already recorded   -> nothing

One open issue per workflow at a time, found by a hidden marker in its body
rather than by title. The body names the stage (the first failing job), the
step inside it, the candidate the run tested, the first failure line the
job's log shows, and the runs of the current streak; a hidden stamp lists the
run ids the body already reflects, so handling a run twice writes nothing.
An issue a person closed while the pipeline was still red stays closed for
the runs it names; the next failing run opens a fresh one.

On open and on close it also posts a few lines to the CI-health Chat space,
through the same sender, space and token `scripts/eval_dashboard/post_health.py`
uses: the workflow mints the token as the dashboard publisher and passes it
in `CI_HEALTH_CHAT_TOKEN`; unconfigured, the post is skipped and said so,
and the issue is still filed. The GitHub write comes first, so the message
can cite the issue number.

The candidate is not in the run's API record: the three workflows take it as
a `workflow_dispatch` input, which the runs endpoint does not return. It is
read from the job logs instead, where the runner echoes a reusable
workflow's inputs (`commit_sha:`, `rc_tag:`), a step's `env:` (`RC_TAG:`,
`TARGET_COMMIT:`) and the resolver banners (`Candidate RC Tag:`,
`Gate-passing commit:`). A run whose logs have none of those is reported
with the candidate unknown rather than left unreported.

A sweep rather than a `workflow_run` trigger. Posting to Chat needs
`id-token: write` to mint the token, and `tests/conformance/test_B_write_path.py`
(B4) requires a `workflow_run` job holding that permission to gate on
`workflow_run.conclusion == 'success'`, which a failure notifier cannot. The
sweep runs every fifteen minutes, reconciles every watched workflow, and
writes nothing when the issues already say what the runs say, so it is safe
to run at any time; a dispatch with `run_id` reconciles that run's workflow
alone. The release pipelines run a handful of times a day and take between
forty minutes and six hours, so a bound of fifteen minutes on the delay
costs nothing.

Setup: `GH_TOKEN` (the workflow's own token, with `issues: write` and
`actions: read`) and the `gh` binary. The label is created the first time it
is needed. Chat needs `CI_HEALTH_CHAT_SPACE` and `CI_HEALTH_CHAT_TOKEN`, the
poster's own variables, or nothing to post nowhere.

Run:  python3 scripts/release/notify_release_stage_broken.py --sweep --dry-run
      python3 scripts/release/notify_release_stage_broken.py --run-id 39533999681 --dry-run
Test: cd scripts && python3 -m unittest test_notify_release_stage_broken
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

# The shared helpers live one directory up: the broken-main notifier's
# event-agnostic decision over a run history, and the CI-health poster's
# Chat sender, so this file spells neither a second time.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval_dashboard.post_health import SPACE_ENV, TOKEN_ENV, Sender  # noqa: E402
from notify_broken_main import FAILING_CONCLUSIONS, decide, newest_reporting_run, reporting_history  # noqa: E402

# The release workflows, by the `name:` each declares, which is how the
# Actions API lists them. `test_notify_release_stage_broken.py` holds this
# tuple to the workflow files, and `test_integration_contracts.py` holds it to
# `RELEASE_STAGE_WATCHED_WORKFLOWS`, the roster of release stages that have to
# be watched.
WATCHED_WORKFLOWS = (
    "RC: Release Pipeline",
    "Staging: Promotion Pipeline",
    "Release & Publish (GA)",
)

LABEL = "ci:release-stage-broken"
LABEL_COLOR = "d73a4a"
LABEL_DESCRIPTION = "A release pipeline stage is failing"

DEFAULT_REPO = "gke-labs/kube-agents"
REPO_ENV = "GITHUB_REPOSITORY"

GH_BIN = "gh"
# One call's ceiling. A job log is the largest read, and `gh` streams it;
# a hung call must not eat the job.
CALL_TIMEOUT_S = 120
# The Actions list endpoints page at this size and wrap the page in an object.
PAGE_SIZE = 100
# Completed runs to read back per workflow. The streak the issue lists is cut
# off here; thirty runs of a pipeline that runs at most eight times a day is
# several days of history, past which a streak is not news.
HISTORY_DEPTH = 30
# Issues to read back per state. One open issue per workflow is the
# invariant, so a page covers the open ones; for the closed ones, newest
# first, the one that matters is the most recent that names the run.
ISSUE_PAGE = 50
# The run's first jobs whose logs are read for the candidate. Each pipeline
# names it in its first job (the tag step, the resolver, the gate decision);
# the second is for a run whose first job is a checkout-only prelude.
CANDIDATE_LOG_JOBS = 2
# A commit sha is shown short in prose and in Chat; the body carries it whole.
SHORT_SHA = 7
# A run's time is shown to the minute: `2026-10-21 02:36 UTC`.
WHEN_LENGTH = len("2026-10-21 02:36")
WHEN_UNKNOWN = "time unknown"
# What an issue opened under `--dry-run` is numbered in the printed messages.
DRY_RUN_ISSUE_NUMBER = 0
# The first failure line is cut here; a pytest summary line can run to a
# screenful of assertion text, and the log link has the rest.
FAILURE_LINE_LIMIT = 200

# Job conclusions beyond the run-level ones that mark the job where a run
# stopped: a job cancelled by a timeout or by a sibling's failure is where the
# reader looks when no job failed outright.
JOB_STOPPED_CONCLUSIONS = ("cancelled",)
STEP_FAILED = "failure"

# Hidden lines in the issue body. The marker finds the issue again whatever
# its title; the stamp names the runs the body already reflects.
MARKER = "<!-- release-stage-broken workflow={workflow_id} -->"
STAMP = "<!-- release-stage-broken runs={run_ids} -->"
_MARKER_RE = re.compile(r"<!-- release-stage-broken workflow=(\d+) -->")
_STAMP_RE = re.compile(r"<!-- release-stage-broken runs=([\d,]*) -->")

TITLE = "🔴 release pipeline stage broken: {workflow}"
RUNBOOK = "`scripts/release/README.md`, \"Pipeline Cadence & Execution Flow\""
CANDIDATE_UNKNOWN = "not found in the run's logs"
STAGE_UNKNOWN = "no failing job in the run's job list"

# What the runner writes at the start of every log line, and the colour
# codes a step's own output carries.
_LOG_TIMESTAMP_RE = re.compile(r"^\S+Z ")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# The candidate, as the three pipelines' logs spell it: a reusable workflow's
# echoed inputs, a step's echoed `env:`, and the resolvers' banners.
_CANDIDATE_TAG_RE = re.compile(r"^\s*(?:rc_tag|RC_TAG|Candidate RC Tag|From gate tag):\s*(\S+)\s*$")
_CANDIDATE_SHA_RE = re.compile(
    r"^\s*(?:commit_sha|COMMIT_SHA|Commit SHA|TARGET_COMMIT|Gate-passing commit):\s*([0-9a-f]{40})\s*$"
)
# The first failure, in order of how much it says: a pytest summary line
# names the test, a script's own error line names the check, and the
# runner's annotation says only that the step failed.
_FAILURE_LINE_RES = (
    re.compile(r"^FAILED \S+::.*$"),
    re.compile(r"^.*(?:❌|ERROR:).*$"),
    re.compile(r"^##\[error\].*$"),
)

CHAT_BROKEN = "🔴 *Release pipeline: {workflow} failed* — {stage}: {failure}"
CHAT_BROKEN_DETAIL = "Candidate {candidate}. Tracking #{number}."
CHAT_RECOVERED = "🟢 *Release pipeline: {workflow} passes again* — run #{run_number} on candidate {candidate}. Closed #{number}."
NOT_CONFIGURED = "chat not configured: set CI_HEALTH_CHAT_SPACE and CI_HEALTH_CHAT_TOKEN; nothing posted"


class GhError(RuntimeError):
    """`gh` exited non-zero, or did not answer. A read that fails writes
    nothing; the next sweep is at most fifteen minutes away."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Reading a run
# --------------------------------------------------------------------------- #


def clean_log_line(line: str) -> str:
    return _ANSI_RE.sub("", _LOG_TIMESTAMP_RE.sub("", line.rstrip("\r")))


def extract_candidate(log_text: str, found: dict | None = None) -> dict:
    """The candidate tag and commit the log names, first mention of each.

    `found` carries what an earlier job's log already gave, so a second log
    fills only the holes.
    """
    found = dict(found or {})
    for raw in log_text.splitlines():
        line = clean_log_line(raw)
        if "tag" not in found:
            match = _CANDIDATE_TAG_RE.match(line)
            if match:
                found["tag"] = match.group(1)
        if "sha" not in found:
            match = _CANDIDATE_SHA_RE.match(line)
            if match:
                found["sha"] = match.group(1)
        if "tag" in found and "sha" in found:
            break
    return found


def extract_first_failure(log_text: str) -> str | None:
    """The most informative failure line the log holds, or None."""
    lines = [clean_log_line(raw) for raw in log_text.splitlines()]
    for pattern in _FAILURE_LINE_RES:
        for line in lines:
            if pattern.match(line):
                return line[:FAILURE_LINE_LIMIT]
    return None


def failing_job(jobs: list) -> dict | None:
    """The job the run stopped at: the first that failed, else the first that
    was cancelled, in the order the run lists them."""
    for job in jobs:
        if job.get("conclusion") in FAILING_CONCLUSIONS:
            return job
    for job in jobs:
        if job.get("conclusion") in JOB_STOPPED_CONCLUSIONS:
            return job
    return None


def failing_step(job: dict | None) -> str | None:
    for step in (job or {}).get("steps") or []:
        if step.get("conclusion") == STEP_FAILED:
            return step.get("name")
    return None


def candidate_text(candidate: dict) -> str:
    tag, sha = candidate.get("tag"), candidate.get("sha")
    if tag and sha:
        return f"`{tag}` (`{sha}`)"
    if tag:
        return f"`{tag}`"
    if sha:
        return f"`{sha}`"
    return CANDIDATE_UNKNOWN


def candidate_short(candidate: dict) -> str:
    tag, sha = candidate.get("tag"), candidate.get("sha")
    if tag and sha:
        return f"{tag} ({sha[:SHORT_SHA]})"
    if tag:
        return tag
    if sha:
        return sha[:SHORT_SHA]
    return CANDIDATE_UNKNOWN


def run_details(gh: Gh, run: dict, with_failure: bool = True) -> dict:
    """What the issue says about one run: stage, step, first failure, candidate.

    Reads the run's jobs once, the first jobs' logs for the candidate, and
    the failing job's log for the failure line. A log that cannot be read
    leaves its field unknown rather than failing the reconciliation: the
    issue with a hole in it is still the signal.
    """
    jobs = gh.jobs(run["id"])
    candidate: dict = {}
    for job in jobs[:CANDIDATE_LOG_JOBS]:
        if "tag" in candidate and "sha" in candidate:
            break
        text = gh.job_log(job["id"])
        if text is not None:
            candidate = extract_candidate(text, candidate)
    stage = failing_job(jobs) if with_failure else None
    failure = None
    if stage is not None:
        text = gh.job_log(stage["id"])
        failure = extract_first_failure(text) if text is not None else None
    return {
        "run": run,
        "stage": stage.get("name") if stage else None,
        "stage_url": stage.get("html_url") if stage else None,
        "step": failing_step(stage),
        "failure": failure,
        "candidate": candidate,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def marker(workflow_id: int) -> str:
    return MARKER.format(workflow_id=workflow_id)


def stamped_runs(body: str | None) -> set:
    match = _STAMP_RE.search(body or "")
    if not match or not match.group(1):
        return set()
    return {int(item) for item in match.group(1).split(",") if item}


def run_link(run: dict) -> str:
    return f"[#{run['run_number']}]({run['html_url']})"


def run_when(run: dict) -> str:
    """`2026-10-21 02:36 UTC` from the run's ISO creation time."""
    created = run.get("created_at") or ""
    if not created:
        return WHEN_UNKNOWN
    return created.replace("T", " ")[:WHEN_LENGTH] + " UTC"


def failure_text(details: dict) -> str:
    if details.get("failure"):
        return f"`{details['failure']}`"
    if details.get("step"):
        return f"step `{details['step']}` failed; the log has no failure line this reads"
    return "no failure line found; read the run"


def stage_text(details: dict) -> str:
    if not details.get("stage"):
        return STAGE_UNKNOWN
    if details.get("stage_url"):
        return f"[`{details['stage']}`]({details['stage_url']})"
    return f"`{details['stage']}`"


def render_title(workflow_name: str) -> str:
    return TITLE.format(workflow=workflow_name)


def render_body(workflow_name: str, workflow_id: int, details: dict, streak: list) -> str:
    """The issue as it stands now, rebuilt from the newest failing run and
    the streak behind it; edited in place rather than appended to."""
    run = details["run"]
    lines = [
        marker(workflow_id),
        STAMP.format(run_ids=",".join(str(each["id"]) for each in streak)),
        "",
        f"`{workflow_name}` is failing. Newest failing run: {run_link(run)} ({run_when(run)}).",
        "",
        "| | |",
        "| --- | --- |",
        f"| Stage | {stage_text(details)} |",
        f"| Step | {'`' + details['step'] + '`' if details.get('step') else 'none recorded'} |",
        f"| Candidate | {candidate_text(details['candidate'])} |",
        f"| First failure | {failure_text(details)} |",
        f"| Run | {run['html_url']} |",
        "",
    ]
    if len(streak) > 1:
        lines.append("Failing runs in this streak, oldest first:")
        lines.append("")
        lines.extend(f"- {run_link(each)} ({run_when(each)})" for each in streak)
        lines.append("")
    lines += [
        "This issue closes itself when the next completed run of the workflow passes. "
        f"What each stage does and how to re-run it: {RUNBOOK}.",
    ]
    return "\n".join(lines)


def render_still_failing(details: dict) -> str:
    run = details["run"]
    return (
        f"Still failing: {run_link(run)} ({run_when(run)}), stage {stage_text(details)}, "
        f"candidate {candidate_text(details['candidate'])}. First failure: {failure_text(details)}"
    )


def render_recovered(details: dict) -> str:
    run = details["run"]
    return f"{run_link(run)} ({run_when(run)}) passed on candidate {candidate_text(details['candidate'])}. Closing."


def render_chat_broken(workflow_name: str, details: dict, number: int) -> str:
    run = details["run"]
    failure = details.get("failure") or (f"step `{details['step']}` failed" if details.get("step") else "see the run")
    lines = [
        CHAT_BROKEN.format(workflow=workflow_name, stage=details.get("stage") or STAGE_UNKNOWN, failure=failure),
        CHAT_BROKEN_DETAIL.format(candidate=candidate_short(details["candidate"]), number=number),
        run["html_url"],
    ]
    return "\n".join(lines)


def render_chat_recovered(workflow_name: str, details: dict, number: int) -> str:
    run = details["run"]
    lines = [
        CHAT_RECOVERED.format(
            workflow=workflow_name,
            run_number=run["run_number"],
            candidate=candidate_short(details["candidate"]),
            number=number,
        ),
        run["html_url"],
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# GitHub, through `gh`
# --------------------------------------------------------------------------- #


class Gh:
    """`gh api` with an injectable runner. Reads raise `GhError`; writes are
    printed and skipped under `dry_run`."""

    def __init__(self, repo: str, runner=subprocess.run, dry_run: bool = False):
        self.repo = repo
        self.runner = runner
        self.dry_run = dry_run

    def _call(self, argv: list, input_text: str | None = None) -> str:
        try:
            proc = self.runner(
                [GH_BIN, *argv],
                input=input_text,
                capture_output=True,
                text=True,
                timeout=CALL_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GhError(f"gh {' '.join(argv[:3])}: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            first = (proc.stderr or "").strip().splitlines()
            raise GhError(f"gh {' '.join(argv[:3])} failed (exit {proc.returncode}): {first[0] if first else 'no stderr'}")
        return proc.stdout or ""

    def api(self, path: str, method: str = "GET", payload: dict | None = None):
        argv = ["api", "-X", method, path]
        if payload is not None:
            argv += ["--input", "-"]
        text = self._call(argv, json.dumps(payload) if payload is not None else None)
        return json.loads(text) if text.strip() else None

    def api_pages(self, path: str, key: str) -> list:
        """Every page of an endpoint that answers `{"total_count": n, <key>: [...]}`."""
        separator = "&" if "?" in path else "?"
        found: list = []
        page = 1
        while True:
            batch = self.api(f"{path}{separator}per_page={PAGE_SIZE}&page={page}")
            items = (batch or {}).get(key) or []
            found.extend(items)
            if not items or len(found) >= (batch or {}).get("total_count", 0):
                return found
            page += 1

    def workflows(self) -> list:
        return self.api_pages(f"repos/{self.repo}/actions/workflows", "workflows")

    def run(self, run_id: int) -> dict:
        return self.api(f"repos/{self.repo}/actions/runs/{run_id}")

    def completed_runs(self, workflow_id: int) -> list:
        batch = self.api(
            f"repos/{self.repo}/actions/workflows/{workflow_id}/runs"
            f"?status=completed&exclude_pull_requests=true&per_page={HISTORY_DEPTH}"
        )
        return (batch or {}).get("workflow_runs") or []

    def jobs(self, run_id: int) -> list:
        return self.api_pages(f"repos/{self.repo}/actions/runs/{run_id}/jobs", "jobs")

    def job_log(self, job_id: int) -> str | None:
        """The job's raw log, or None when it cannot be read: a log expires,
        and a reconciliation must not fail on a missing one."""
        try:
            # The flag: a step's coloured output trips gh's escape-sequence
            # refusal on a redirected stdout, and the lines are cleaned here.
            return self._call(["api", "--allow-escape-sequences", f"repos/{self.repo}/actions/jobs/{job_id}/logs"])
        except GhError as exc:
            log(f"warning: {exc}; log of job {job_id} left unread")
            return None

    def issues(self, state: str) -> list:
        query = urllib.parse.urlencode(
            {"labels": LABEL, "state": state, "sort": "updated", "direction": "desc", "per_page": ISSUE_PAGE}
        )
        return self.api(f"repos/{self.repo}/issues?{query}") or []

    def ensure_label(self) -> None:
        if self.dry_run:
            log(f"dry-run: would ensure label {LABEL}")
            return
        # `--force` updates an existing label instead of failing, so this is
        # a no-op from the second run onwards.
        self._call(["label", "create", LABEL, "--repo", self.repo, "--force", "--color", LABEL_COLOR, "--description", LABEL_DESCRIPTION])

    def create_issue(self, title: str, body: str) -> dict:
        if self.dry_run:
            log(f"dry-run: would open an issue titled {title!r}:\n{body}")
            return {"number": DRY_RUN_ISSUE_NUMBER}
        return self.api(f"repos/{self.repo}/issues", "POST", {"title": title, "body": body, "labels": [LABEL]})

    def update_issue(self, number: int, **fields) -> None:
        if self.dry_run:
            log(f"dry-run: would update #{number} with {sorted(fields)}" + (f":\n{fields['body']}" if "body" in fields else ""))
            return
        self.api(f"repos/{self.repo}/issues/{number}", "PATCH", fields)

    def comment(self, number: int, body: str) -> None:
        if self.dry_run:
            log(f"dry-run: would comment on #{number}:\n{body}")
            return
        self.api(f"repos/{self.repo}/issues/{number}/comments", "POST", {"body": body})


# --------------------------------------------------------------------------- #
# Reconciling one workflow
# --------------------------------------------------------------------------- #


def issues_for(gh: Gh, workflow_id: int, state: str) -> list:
    """The labelled issues in `state` that carry this workflow's marker,
    newest-updated first, pull requests excluded (the issues endpoint lists
    both)."""
    return [
        issue
        for issue in gh.issues(state)
        if "pull_request" not in issue and marker(workflow_id) in (issue.get("body") or "")
    ]


def post(sender: Sender, text: str, dry_run: bool) -> None:
    if dry_run:
        log(f"dry-run: would post to chat ({sender.describe()}):\n{text}")
        return
    if not sender.configured:
        log(NOT_CONFIGURED)
        return
    if sender.send(text):
        log("posted to chat")


def reconcile(gh: Gh, workflow: dict, sender: Sender, dry_run: bool) -> str:
    """Bring the workflow's issue into line with its newest reporting run.
    Returns a one-word outcome for the log and the tests."""
    name, workflow_id = workflow["name"], workflow["id"]
    runs = gh.completed_runs(workflow_id)
    current = newest_reporting_run(runs)
    if current is None:
        log(f"{name}: no completed run has said anything yet")
        return "nothing"
    decision = decide(current, reporting_history(runs, current))
    open_issues = issues_for(gh, workflow_id, "open")
    issue = open_issues[0] if open_issues else None

    if decision["kind"] == "green":
        if issue is None:
            log(f"{name}: green (run #{current['run_number']}), no issue open")
            return "nothing"
        details = run_details(gh, current, with_failure=False)
        gh.comment(issue["number"], render_recovered(details))
        gh.update_issue(issue["number"], state="closed")
        log(f"{name}: run #{current['run_number']} passed; closed #{issue['number']}")
        post(sender, render_chat_recovered(name, details, issue["number"]), dry_run)
        return "closed"

    streak = decision["streak"]
    if issue is not None:
        if current["id"] in stamped_runs(issue.get("body")):
            log(f"{name}: #{issue['number']} already records run #{current['run_number']}")
            return "nothing"
        details = run_details(gh, current)
        gh.update_issue(issue["number"], body=render_body(name, workflow_id, details, streak))
        gh.comment(issue["number"], render_still_failing(details))
        log(f"{name}: run #{current['run_number']} failed again; updated #{issue['number']}")
        return "updated"

    closed = issues_for(gh, workflow_id, "closed")
    if closed and current["id"] in stamped_runs(closed[0].get("body")):
        log(f"{name}: run #{current['run_number']} is recorded on closed #{closed[0]['number']}; someone dealt with it")
        return "nothing"
    details = run_details(gh, current)
    gh.ensure_label()
    created = gh.create_issue(render_title(name), render_body(name, workflow_id, details, streak))
    log(f"{name}: run #{current['run_number']} failed; opened #{created['number']}")
    post(sender, render_chat_broken(name, details, created["number"]), dry_run)
    return "opened"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def watched_workflows(gh: Gh) -> list:
    by_name = {workflow["name"]: workflow for workflow in gh.workflows()}
    missing = [name for name in WATCHED_WORKFLOWS if name not in by_name]
    if missing:
        raise GhError(f"watched workflows not found in the repository (renamed?): {', '.join(missing)}")
    return [by_name[name] for name in WATCHED_WORKFLOWS]


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--run-id", type=int, help="reconcile the workflow this completed run belongs to")
    which.add_argument("--sweep", action="store_true", help="reconcile every watched workflow")
    parser.add_argument("--dry-run", action="store_true", help="print what would be written and posted; write nothing")
    parser.add_argument("--repo", default=os.environ.get(REPO_ENV, DEFAULT_REPO))
    return parser.parse_args(argv)


def main(argv=None, runner=subprocess.run, environ=os.environ, opener=None) -> int:
    args = parse_args(argv)
    gh = Gh(args.repo, runner=runner, dry_run=args.dry_run)
    sender = Sender.from_env(environ, opener) if opener else Sender.from_env(environ)
    if not sender.configured:
        log(f"{NOT_CONFIGURED} ({SPACE_ENV}={'set' if environ.get(SPACE_ENV) else 'unset'}, {TOKEN_ENV}={'set' if environ.get(TOKEN_ENV) else 'unset'})")
    try:
        if args.sweep:
            targets = watched_workflows(gh)
        else:
            run = gh.run(args.run_id)
            if run.get("name") not in WATCHED_WORKFLOWS:
                log(f"run {args.run_id} belongs to {run.get('name')!r}, which is not watched; nothing to do")
                return 0
            targets = [{"id": run["workflow_id"], "name": run["name"]}]
        for workflow in targets:
            outcome = reconcile(gh, workflow, sender, args.dry_run)
            log(f"{workflow['name']}: {outcome}")
    except GhError as exc:
        log(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
