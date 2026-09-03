#!/usr/bin/env python3
"""Keep a green `pull-kube-agents-smoke-test` status valid when `main` moves.

Tide credits a presubmit only against the base SHA it ran on. Crier writes that
SHA into the commit status as a `BaseSHA:<sha>` suffix, and Tide reads it back
(`prowJobsFromContexts`) so a result outlives the ProwJob object -- as long as
the SHA still names the head of `main`. Every merge to `main` therefore turns
every other pull request's green smoke run stale, and Tide re-runs the 1.5-3.5h
job for a pull request whose head has not changed (#1179, #1202).

This re-pins the suffix. On every push to `main` it sweeps the open pull
requests and re-posts each green smoke status with `BaseSHA:` set to the new
head; when a green arrives after `main` has already moved past the base it
ran on, the `status` event does the same for that one commit. The description
says what happened, so a reader is not told the run was against a base it was
not. Tide's newer form of this -- a `[prow:skip-retest]` sentinel that
`/override-sticky` writes -- is upstream since July 2026 but not in the Prow
build this repository merges through (its plugin help does not list
`/override-sticky`); when it is, append the sentinel here and stop pinning.

It is best-effort against Tide's own clock. Tide syncs about once a minute,
and a sync that sees the stale statuses before the sweep has re-pinned them
starts the retest -- as a batch, when two or more pull requests qualify --
and crier's `pending` is then the newer word, which this leaves alone. So the
sweep is kept short (no dependencies, one read per open pull request), takes
the pull requests Tide is actually waiting on first, and is still a race that
is sometimes lost. Two merges seconds apart start two sweeps with no ordering
between them, so a pin is always made to the head of `main` as read just
before the write, never to the head the run started with.

What it will not do: touch a status that is not `success`, touch an admin
`/override`, pin a pull request that does not target `main` (Tide keys the
base SHA on the pull request's own base branch), or overwrite a newer status
on the same context. The read and the write are two calls, so a `pending`
that lands between them is buried; that window is a few hundred milliseconds
and its cost is the trade-off above, taken a little early.
"""

import argparse
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from github_api import GitHubAPI, log  # noqa: E402

CONTEXT = "pull-kube-agents-smoke-test"
#: `contextDescriptionBaseSHADelimiter` in kubernetes-sigs/prow, pkg/config/config.go.
BASE_SHA_DELIMITER = " BaseSHA:"
#: What the override plugin writes; its statuses are an admin's decision, not ours.
OVERRIDE_PREFIX = "Overridden by"
#: Left in the human-readable part so the description does not claim a run it did not have.
PIN_NOTE = "(base pinned to main by smoke-test-sticky)"
#: GitHub rejects a longer status description; Prow's `contextDescriptionMaxLen` agrees.
MAX_DESCRIPTION = 140
SUCCESS = "success"
MAIN_BRANCH = "main"
MAIN_REF = f"heads/{MAIN_BRANCH}"
USER_AGENT = "kube-agents-smoke-test-sticky"
#: Tide's pool wants both; a pull request carrying them is the one a stale base costs hours.
POOL_LABELS = frozenset({"lgtm", "approved"})
HOLD_LABEL_PREFIX = "do-not-merge"
SHORT_SHA = 8
_SHA = re.compile(r"[0-9a-f]{40}")


def split_description(description):
    """(human-readable part, base SHA or None) of a crier-shaped description.

    Crier left-pads the suffix with U+2001 so it lines up in the GitHub UI; that
    is Unicode whitespace, which `str.split()` collapses. A previous pin note is
    dropped so re-pinning does not stack them.
    """
    text = " ".join((description or "").split())
    human, _, base_sha = text.partition(BASE_SHA_DELIMITER)
    human = " ".join(human.replace(PIN_NOTE, "").split())
    return human, (base_sha if _SHA.fullmatch(base_sha) else None)


def pinned_description(description, main_sha):
    """The same status, its base pinned to `main_sha`, within GitHub's limit."""
    human, _ = split_description(description)
    human = f"{human} {PIN_NOTE}".strip()
    suffix = f"{BASE_SHA_DELIMITER}{main_sha}"
    room = MAX_DESCRIPTION - len(suffix)
    return human[:room].rstrip() + suffix


def skip_reason(status, main_sha):
    """Why a status is left alone, or None when it should be pinned."""
    if status is None:
        return f"no {CONTEXT} status on the commit"
    if status.get("state") != SUCCESS:
        return f"state is {status.get('state')!r}, not {SUCCESS}"
    description = status.get("description") or ""
    if description.startswith(OVERRIDE_PREFIX):
        return "an admin override, which is not ours to extend"
    _, base_sha = split_description(description)
    if base_sha == main_sha:
        return "already pinned to the head of main"
    return None


def latest_status(api, sha):
    """The most recent status for CONTEXT on `sha`, from the combined status."""
    combined = api.get(f"/repos/{api.repo}/commits/{sha}/status")
    for status in combined.get("statuses") or []:
        if status.get("context") == CONTEXT:
            return status
    return None


def open_pulls_against_main(api):
    return api.get_all(f"/repos/{api.repo}/pulls?state=open&base={MAIN_BRANCH}")


def targets_main(api, sha):
    """Whether `sha` is the head of an open pull request against main.

    Matched against the open list rather than `/commits/{sha}/pulls`: that
    endpoint returns nothing for a head that lives in a fork, which is where
    nearly every pull request here comes from.
    """
    return any(p["head"]["sha"] == sha for p in open_pulls_against_main(api))


def pin_head(api, sha, main_sha, status_id=None, dry_run=False, check_base=True):
    """Re-pin one commit's smoke status. Returns what happened, for the log.

    `main_sha` is a SHA or a callable returning one; the callable is read just
    before the write, so a sweep that overlaps a newer one cannot pin backwards.
    """
    if check_base and not targets_main(api, sha):
        return f"{sha[:SHORT_SHA]}: not the head of an open pull request against {MAIN_BRANCH}"
    status = latest_status(api, sha)
    if status_id is not None and status is not None and str(status.get("id")) != str(status_id):
        return f"{sha[:SHORT_SHA]}: status {status_id} is no longer the latest (now {status.get('id')}); leaving it"
    if status is not None and status.get("state") == SUCCESS:
        main_sha = main_sha() if callable(main_sha) else main_sha
    reason = skip_reason(status, main_sha)
    if reason:
        return f"{sha[:SHORT_SHA]}: {reason}"
    payload = {
        "state": SUCCESS,
        "context": CONTEXT,
        "target_url": status.get("target_url") or "",
        "description": pinned_description(status.get("description"), main_sha),
    }
    if not dry_run:
        api.post(f"/repos/{api.repo}/statuses/{sha}", payload)
    return f"{sha[:SHORT_SHA]}: pinned to {main_sha[:SHORT_SHA]}: {payload['description']}"


def in_tide_pool(pull_request):
    """Carries the labels Tide merges on and no hold: the ones a stale base costs hours."""
    labels = {label.get("name") for label in pull_request.get("labels") or []}
    return POOL_LABELS <= labels and not any(str(name).startswith(HOLD_LABEL_PREFIX) for name in labels)


def sweep(api, main_sha, dry_run=False):
    """Every open pull request's head, after a push to main; the pool first."""
    outcomes = []
    pulls = sorted(open_pulls_against_main(api), key=lambda p: not in_tide_pool(p))
    for pull_request in pulls:
        outcomes.append(pin_head(api, pull_request["head"]["sha"], main_sha, dry_run=dry_run, check_base=False))
    return outcomes


def main_head(api):
    return api.get(f"/repos/{api.repo}/git/ref/{MAIN_REF}")["object"]["sha"]


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name")
    sub = parser.add_subparsers(dest="mode", required=True)
    status = sub.add_parser("status", help="one commit, from a status event")
    status.add_argument("--sha", required=True)
    status.add_argument("--status-id", required=True, help="the event's status id; a newer one wins")
    sweep_ = sub.add_parser("sweep", help="every open pull request against main, after a push to main")
    # On the subcommands, not the parser: argparse rejects a parent option
    # given after the subcommand, and the workflow writes them after it.
    for command in (status, sweep_):
        command.add_argument("--main-sha", help="pin to this instead of the head of main as read before each write")
        command.add_argument("--dry-run", action="store_true", help="log what would be posted, post nothing")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not args.repo or not token:
        print("GITHUB_REPOSITORY (or --repo) and GITHUB_TOKEN are required", file=sys.stderr)
        return 2
    api = GitHubAPI(args.repo, token, user_agent=USER_AGENT)
    main_sha = args.main_sha or (lambda: main_head(api))
    if args.mode == "status":
        outcomes = [pin_head(api, args.sha, main_sha, status_id=args.status_id, dry_run=args.dry_run)]
    else:
        outcomes = sweep(api, main_sha, dry_run=args.dry_run)
    for outcome in outcomes:
        log(outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
