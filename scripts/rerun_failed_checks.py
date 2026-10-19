#!/usr/bin/env python3
"""Re-run the failed GitHub Actions checks on a pull request, for `/retest`.

GitHub lets Write and above re-run a workflow; Triage cannot, and most of this
repository's collaborators hold Triage. Prow's `/retest` re-runs only Prow jobs.
So a GitHub Actions check that flaked on someone else's pull request stayed red
until an admin clicked re-run, and `docs/pull-request-workflow.md` ("Re-running
a check that failed") gave a `gh run rerun` command most readers could not run.

`.github/workflows/retest-actions.yml` runs this on a `/retest` comment from an
owner, member or collaborator, with a token that can re-run workflows. The same
comment reaches Prow, which re-runs its own failed jobs, so one word covers
both kinds of check.

What is re-run is what Tide sees red, read the way Tide reads it. Tide takes
every check run at the head commit and keeps the best state per check name:
success (`success`, `neutral`, `skipped`) over pending (not completed) over
failure (any other completed conclusion). A name whose best state is failure
blocks the merge; nothing else does. So the script lists the check runs at the
head, finds the names that stay red, takes the newest red check run of each,
and re-runs the GitHub Actions workflow run that produced it: through its
failed jobs when the run concluded `failure`, whole otherwise (a `cancelled`
or `timed_out` run has no failed job for that endpoint to find). A red check
run from another app (the review bot's `AI Review`) is not a workflow run and
is named in the log and left alone.

Reading check runs rather than workflow runs is what keeps the command from
spending runners on nothing. A workflow run cancelled while still queued -- its
concurrency group displaced it -- never created a check run, and a run an
`edited` event superseded left a check run whose name a newer success covers;
both read `cancelled` on the runs list and neither blocks anything.

A re-run executes the original run again, with the privileges of the actor
who first triggered it, so the person typing the command gains no ability to
run pull-request code with a write token; the workflow itself checks out the
default branch only.

The ceiling (`RERUN_ATTEMPT_CEILING`) is what keeps this a flake retry rather
than a way to grind a real break to green: a run already on that attempt is
left alone and the person is told so. `scripts/notify_flaky_check.py` records
each re-run that goes green as a `ci:flaky` occurrence; one made here lists
the Actions bot as who re-ran, and the `/retest` comment on the pull request
names the person.

Run:  python3 scripts/rerun_failed_checks.py --pr 3245 --dry-run
Test: cd scripts && python3 -m unittest test_rerun_failed_checks
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import urllib.error
import urllib.parse

from github_api import PER_PAGE, GitHubAPI, is_refusal, log

DEFAULT_REPO = "gke-labs/kube-agents"

# How Tide reads a check run (prow `tide`, `checkRunToContext`): not completed
# is pending; these conclusions are success; every other completed conclusion
# (`failure`, `cancelled`, `timed_out`, `action_required`, `stale`) is failure.
COMPLETED_STATUS = "completed"
SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
# The per-name order Tide keeps (`isStateBetter`): one success clears the name,
# one pending holds it, failure only when every check run of the name failed.
STATE_SUCCESS, STATE_PENDING, STATE_FAILURE = "success", "pending", "failure"

# Check runs GitHub Actions itself posts carry this app slug. Any other app's
# red check run is not a workflow run and this command cannot re-run it.
ACTIONS_APP_SLUG = "github-actions"

# A run that concluded `failure` has failed jobs to re-run; any other red
# conclusion has none, so those re-run whole.
FAILED_CONCLUSION = "failure"
RERUN_FAILED_JOBS = "rerun-failed-jobs"
RERUN_WHOLE = "rerun"

# A run already on this attempt is not re-run again by the command. The doc
# calls a flake "worth one re-run"; the second is for a run that a cancelled
# attempt (a runner lost, a superseding event) put in the way of that one.
# Past it, a person with Write reads the log and decides.
RERUN_ATTEMPT_CEILING = 3

# Reactions on the `/retest` comment: 👀 when something was re-run, 😕 when the
# command found nothing to do. Without the second, a declined command looks
# identical to the workflow never firing.
REACTION_ACKNOWLEDGED = "eyes"
REACTION_DECLINED = "confused"

# A declined command is written where the person who typed it will see it: a
# warning annotation on the workflow run (the `::warning::` command goes to
# stdout) and the job's step summary, when Actions provides one.
WORKFLOW_WARNING_PREFIX = "::warning::"
STEP_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"


@dataclasses.dataclass
class Rerun:
    run: dict
    action: str
    check_names: list


@dataclasses.dataclass
class Skipped:
    name: str
    reason: str


@dataclasses.dataclass
class Plan:
    rerun: list  # Rerun, one per workflow run behind a red check name
    skipped: list  # Skipped, one per red check name left alone, with why
    pending: int  # check names whose best state is pending


def _paged_object(api, path: str, key: str, query: dict) -> list:
    """Every page of an endpoint that wraps its list in `{total_count, <key>}`.

    `get_all` pages a bare list and cannot read these. An empty page ends the
    loop as well as reaching the total, so a total that overstates the list
    cannot spin this.
    """
    found = []
    page = 1
    while True:
        encoded = urllib.parse.urlencode({**query, "per_page": PER_PAGE, "page": page})
        batch = api.get(f"{path}?{encoded}")
        found.extend(batch[key])
        if not batch[key] or len(found) >= batch["total_count"]:
            return found
        page += 1


def list_runs(api, repo: str, head_sha: str) -> list:
    """Every workflow run at `head_sha`, each as its latest attempt."""
    return _paged_object(api, f"/repos/{repo}/actions/runs", "workflow_runs", {"head_sha": head_sha})


def list_check_runs(api, repo: str, head_sha: str) -> list:
    """Every check run at `head_sha`, every attempt: `filter=all`, since the
    default `latest` keeps one per check suite and Tide reads them all."""
    return _paged_object(api, f"/repos/{repo}/commits/{head_sha}/check-runs", "check_runs", {"filter": "all"})


def check_state(check_run: dict) -> str:
    if check_run.get("status") != COMPLETED_STATUS:
        return STATE_PENDING
    if check_run.get("conclusion") in SUCCESS_CONCLUSIONS:
        return STATE_SUCCESS
    return STATE_FAILURE


def best_state(states) -> str:
    states = set(states)
    if STATE_SUCCESS in states:
        return STATE_SUCCESS
    if STATE_PENDING in states:
        return STATE_PENDING
    return STATE_FAILURE


def red_check_runs(check_runs) -> tuple:
    """The newest check run of each name whose best state is failure, by name,
    and how many names are pending."""
    by_name = {}
    for check_run in check_runs:
        by_name.setdefault(check_run["name"], []).append(check_run)
    red = {}
    pending = 0
    for name, entries in by_name.items():
        state = best_state(check_state(entry) for entry in entries)
        if state == STATE_PENDING:
            pending += 1
        elif state == STATE_FAILURE:
            red[name] = max(entries, key=lambda entry: entry["id"])
    return red, pending


def plan_reruns(check_runs, runs) -> Plan:
    """Which workflow runs to re-run and how, and which red names to leave alone and why."""
    red, pending = red_check_runs(check_runs)
    by_suite = {run.get("check_suite_id"): run for run in runs}
    plan = Plan(rerun=[], skipped=[], pending=pending)
    by_run = {}
    for name in sorted(red):
        check_run = red[name]
        app = (check_run.get("app") or {}).get("slug")
        if app != ACTIONS_APP_SLUG:
            plan.skipped.append(Skipped(name, f"posted by {app or 'an unknown app'}, not a workflow run"))
            continue
        run = by_suite.get((check_run.get("check_suite") or {}).get("id"))
        if run is None:
            plan.skipped.append(Skipped(name, "no workflow run at the head commit carries it"))
            continue
        if run.get("status") != COMPLETED_STATUS:
            plan.skipped.append(Skipped(name, f"its workflow run {run['id']} is still running"))
            continue
        attempt = run.get("run_attempt") or 1
        if attempt >= RERUN_ATTEMPT_CEILING:
            plan.skipped.append(Skipped(name, f"run {run['id']} is already on attempt {attempt}; a person with Write access decides"))
            continue
        if run["id"] not in by_run:
            action = RERUN_FAILED_JOBS if run.get("conclusion") == FAILED_CONCLUSION else RERUN_WHOLE
            by_run[run["id"]] = Rerun(run, action, [])
            plan.rerun.append(by_run[run["id"]])
        by_run[run["id"]].check_names.append(name)
    return plan


def describe(entry: Rerun) -> str:
    run = entry.run
    return f"{run.get('name')} (run {run.get('id')}, attempt {run.get('run_attempt')}, {run.get('conclusion')}): {', '.join(entry.check_names)}"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, required=True, help="pull request number")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO),
        help="owner/name (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--react-to",
        type=int,
        metavar="COMMENT_ID",
        help=f"the /retest comment: reacts {REACTION_ACKNOWLEDGED} when something is re-run and {REACTION_DECLINED} when nothing is",
    )
    parser.add_argument("--dry-run", action="store_true", help="print what would be re-run")
    return parser.parse_args(argv)


def react(api, args, content):
    if not args.react_to:
        return
    if args.dry_run:
        log(f"--dry-run: would react {content} to comment {args.react_to}")
        return
    api.post(f"/repos/{args.repo}/issues/comments/{args.react_to}/reactions", {"content": content})


def summarize(lines):
    summary_path = os.environ.get(STEP_SUMMARY_ENV)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def warn(lines):
    for line in lines:
        print(f"{WORKFLOW_WARNING_PREFIX}{line}", flush=True)


def decline(api, args, reason, details=()):
    """Record that nothing was re-run, and why, where the person will see it."""
    message = f"Not re-running anything: {reason}"
    log(message)
    warn([message, *details])
    summarize([message, *details])
    react(api, args, REACTION_DECLINED)


def main(argv=None):
    args = parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("GITHUB_TOKEN (or GH_TOKEN) is not set")
        return 1

    api = GitHubAPI(args.repo, token, user_agent="kube-agents-rerun-failed-checks")

    pull_request = api.get(f"/repos/{args.repo}/pulls/{args.pr}")
    head_sha = pull_request["head"]["sha"]
    log(f"#{args.pr} at {head_sha[:12]}: {pull_request.get('title')}")
    if pull_request.get("state") != "open":
        decline(api, args, f"#{args.pr} is {pull_request.get('state')}")
        return 0

    plan = plan_reruns(list_check_runs(api, args.repo, head_sha), list_runs(api, args.repo, head_sha))
    skipped_lines = [f"Left alone: {entry.name}: {entry.reason}" for entry in plan.skipped]
    if not plan.rerun:
        reason = "no red check at the head commit"
        if plan.skipped:
            reason = "no red check at the head commit that this command can re-run"
        if plan.pending:
            reason += f"; {plan.pending} still running"
        decline(api, args, reason, skipped_lines)
        return 0

    done = []
    refused = []
    for entry in plan.rerun:
        path = f"/repos/{args.repo}/actions/runs/{entry.run['id']}/{entry.action}"
        if args.dry_run:
            log(f"--dry-run: would re-run {describe(entry)} via {entry.action}")
            done.append(entry)
            continue
        try:
            api.post(path)
        except urllib.error.HTTPError as error:
            # GitHub refuses a re-run it cannot make (a run older than thirty
            # days, one already re-running): that one is named and the rest
            # still go. Anything else is the API failing, and raises.
            if not is_refusal(error):
                raise
            log(f"{describe(entry)}: GitHub refused the re-run ({error.code})")
            refused.append(entry)
            continue
        log(f"Re-ran {describe(entry)} via {entry.action}")
        done.append(entry)

    lines = [f"Re-ran {describe(entry)} ({entry.action})" for entry in done]
    noted = [f"GitHub refused to re-run {describe(entry)}" for entry in refused] + skipped_lines
    warn(noted)
    summarize(lines + noted)
    react(api, args, REACTION_ACKNOWLEDGED if done else REACTION_DECLINED)
    return 0


if __name__ == "__main__":
    sys.exit(main())
