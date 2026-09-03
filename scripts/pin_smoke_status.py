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
build this repository merges through (its plugin help lists neither); when it
is, append the sentinel here and stop pinning.

What it will not do: touch a status that is not `success`, touch an admin
`/override`, or overwrite a newer status on the same context -- a `pending`
that Tide or `/test` posted meanwhile is the newer word, and it wins.
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
MAIN_REF = "heads/main"
USER_AGENT = "kube-agents-smoke-test-sticky"
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


def pin_head(api, sha, main_sha, status_id=None, dry_run=False):
    """Re-pin one commit's smoke status. Returns what happened, for the log."""
    status = latest_status(api, sha)
    if status_id is not None and status is not None and str(status.get("id")) != str(status_id):
        return f"{sha[:8]}: status {status_id} is no longer the latest (now {status.get('id')}); leaving it"
    reason = skip_reason(status, main_sha)
    if reason:
        return f"{sha[:8]}: {reason}"
    payload = {
        "state": SUCCESS,
        "context": CONTEXT,
        "target_url": status.get("target_url") or "",
        "description": pinned_description(status.get("description"), main_sha),
    }
    if not dry_run:
        api.post(f"/repos/{api.repo}/statuses/{sha}", payload)
    return f"{sha[:8]}: pinned to {main_sha[:8]}: {payload['description']}"


def sweep(api, main_sha, dry_run=False):
    """Every open pull request's head, after a push to main."""
    outcomes = []
    for pull_request in api.get_all(f"/repos/{api.repo}/pulls?state=open"):
        outcomes.append(pin_head(api, pull_request["head"]["sha"], main_sha, dry_run=dry_run))
    return outcomes


def main_head(api):
    return api.get(f"/repos/{api.repo}/git/ref/{MAIN_REF}")["object"]["sha"]


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name")
    parser.add_argument("--main-sha", help="head of main; resolved from the API when omitted")
    parser.add_argument("--dry-run", action="store_true", help="log what would be posted, post nothing")
    sub = parser.add_subparsers(dest="mode", required=True)
    status = sub.add_parser("status", help="one commit, from a status event")
    status.add_argument("--sha", required=True)
    status.add_argument("--status-id", required=True, help="the event's status id; a newer one wins")
    sub.add_parser("sweep", help="every open pull request, after a push to main")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not args.repo or not token:
        print("GITHUB_REPOSITORY (or --repo) and GITHUB_TOKEN are required", file=sys.stderr)
        return 2
    api = GitHubAPI(args.repo, token, user_agent=USER_AGENT)
    main_sha = args.main_sha or main_head(api)
    if args.mode == "status":
        outcomes = [pin_head(api, args.sha, main_sha, status_id=args.status_id, dry_run=args.dry_run)]
    else:
        outcomes = sweep(api, main_sha, dry_run=args.dry_run)
    for outcome in outcomes:
        log(outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
