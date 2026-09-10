#!/usr/bin/env python3
"""chat_delivery_watch.py - notice when scheduled reports stop reaching chat.

Every failed delivery of a scheduled report is recorded: the Hermes scheduler
writes the failure into ``last_delivery_error`` on the job's entry in that
profile's ``cron/jobs.json``. Until this job existed nothing read that field
(#1102). #1094 is what that cost: seven days of six audits composed, dropped,
and recorded as dropped, and the only signal anyone received was that the
daily reports had stopped arriving.

The obvious watcher, a job that posts to chat when deliveries fail, reports
through the leg it is monitoring and is silent in exactly the case it exists
for. This one reports through two channels that do not depend on chat and
need no privilege the pod does not already hold:

* a **GitHub ledger issue** in the install's configured repository, one per
  install, labelled ``agent:delivery-watch``. It is opened when any job crosses
  the failure threshold, edited when the picture changes, and closed with a
  comment when every leg has recovered. The forge call goes the same route the
  GitHub watcher's does (``forge.run_gh`` through the sandbox and the
  credential proxy);
* an **``ALERT chat_delivery_watch`` line appended to
  ``<agent home>/logs/chat_delivery_watch.log``**. The gateway pod's fluent-bit
  sidecar tails ``logs/*.log`` and ships it to the container's stdout and so
  to Cloud Logging. Note that this job's own stdout is *not* that route: the
  scheduler captures a ``no_agent`` job's stdout into ``cron/output/`` and,
  with ``deliver: "local"``, sends it nowhere. Stdout is kept for a person
  running this by hand.

``deliver: "local"`` is the point, and this is the one platform-roster job
allowed to use it (``agents/platform/cron/README.md``): a delivery leg for the
report that a leg is down would be circular.

Why a ledger of its own. ``last_delivery_error`` holds only the latest run
and is set back to ``None`` by the next run that delivered cleanly or, less
obviously, delivered nothing: a run whose answer was ``[SILENT]`` clears it
too. "Failed for N consecutive runs" therefore needs state this job keeps
itself, advancing a job's streak only when a *new* run has appeared, and
treating a silent run as no evidence either way rather than as a recovery.

Grading. A hard failure means the report reached no platform: the relay
answered 502 (``composed but not delivered to <platforms>``), was unreachable,
or had no key. A partial one means it landed somewhere but not everywhere
(``chat relay partial: the report did not reach <platforms>``); a degraded
one means it was posted but the Chat Agent's turn failed. All three count
toward the streak; the grade and the platforms are carried into the issue so
a partial outage of one platform reads differently from a dead relay.

Run by the platform roster every half hour. Cadence bounds how late a failure
is noticed, not how it is counted. Exit code is 0 on every path a cron tick
can reach: a non-zero exit would only make the scheduler build a failure
summary that ``local`` then drops.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Siblings in `$HERMES_HOME/scripts`, this script's own directory and therefore
# `sys.path[0]` when the scheduler runs it.
import forge
import gitops_workspace
import sandbox_exec
from cluster_agent_profile import RESERVED_PROFILES

# --- channels -----------------------------------------------------------------
# Stable prefix for a log filter. Cloud Logging:
#   resource.type="k8s_container" resource.labels.container_name="fluent-bit"
#   jsonPayload.log:"ALERT chat_delivery_watch"
LOG_PREFIX = "ALERT chat_delivery_watch"
LOGS_DIR = "logs"
ALERT_FILE_NAME = "chat_delivery_watch.log"

# --- where the scheduler keeps its state --------------------------------------
PROFILES_DIR = "profiles"
PLATFORM_PROFILE = "platform"
# The chat/default profile's store is the agent home itself.
DEFAULT_PROFILE_LABEL = "default"
CRON_DIR = "cron"
JOBS_FILE = "jobs.json"
OUTPUT_DIR = "output"
OUTPUT_SUFFIX = ".md"

# --- this job's own state -----------------------------------------------------
STATE_FILE_NAME = "chat_delivery_watch.json"
STATE_PATH_ENV = "CHAT_DELIVERY_WATCH_STATE"
STATE_SCHEMA_VERSION = 1
# A second consecutive miss on a daily job is two days of silence; a single miss
# is what a relay restart during the run looks like, and is not worth an issue.
THRESHOLD_ENV = "CHAT_DELIVERY_ALERT_THRESHOLD"
THRESHOLD_DEFAULT = 2
MAX_ERROR_CHARS = 500
# An output file written this much before `last_run_at` is still the latest
# run's: the scheduler stamps the run start and writes the file at the end, and
# the two clocks are the same one, so this only absorbs rounding.
SILENT_OUTPUT_SLACK_S = 300

# --- grades, from the strings deploy/docker/plugins/chat/adapter.py produces ---
GRADE_HARD = "hard"
GRADE_PARTIAL = "partial"
GRADE_DEGRADED = "degraded"
PARTIAL_RE = re.compile(r"chat relay partial: the report did not reach ([^.]+)\.")
HARD_UNDELIVERED_RE = re.compile(r"composed but not delivered to ([^\n]+?)(?: \(target [^)]*\))?$")
DEGRADED_MARKER = "chat relay degraded:"
SILENT_MARKER = "[SILENT]"
SILENT_STATUS_MARKER = "**Status:** silent"

# --- the ledger issue ---------------------------------------------------------
LABEL = "agent:delivery-watch"
LABEL_COLOR = "D73A4A"
LABEL_DESCRIPTION = "Scheduled-report delivery to chat is failing; maintained by chat_delivery_watch.py"
TITLE_PREFIX = "Scheduled-report delivery is failing: "
BODY_MARKER = "<!-- chat-delivery-watch -->"
GH_LIST_LIMIT = "20"
CLOSE_REASON = "completed"
# `gh issue create` prints the new issue's URL; the number is its last segment.
ISSUE_URL_RE = re.compile(r"/issues/(\d+)\s*$")

LEDGER_NONE = "none"
LEDGER_CLOSED = "closed"
LEDGER_DRY_RUN = "dry-run"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def threshold() -> int:
    raw = os.environ.get(THRESHOLD_ENV, "").strip()
    if not raw:
        return THRESHOLD_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return THRESHOLD_DEFAULT
    return value if value >= 1 else THRESHOLD_DEFAULT


# --- reading the schedulers' stores -------------------------------------------


def roster_paths(agent_home: Path) -> list[tuple[str, Path]]:
    """Every cron store on this volume, as (profile label, jobs.json path)."""
    paths = [
        (DEFAULT_PROFILE_LABEL, agent_home / CRON_DIR / JOBS_FILE),
        (PLATFORM_PROFILE, agent_home / PROFILES_DIR / PLATFORM_PROFILE / CRON_DIR / JOBS_FILE),
    ]
    profiles_dir = agent_home / PROFILES_DIR
    if profiles_dir.is_dir():
        for entry in sorted(profiles_dir.iterdir()):
            if entry.is_dir() and entry.name not in RESERVED_PROFILES:
                paths.append((entry.name, entry / CRON_DIR / JOBS_FILE))
    return paths


def load_jobs(path: Path) -> list[dict]:
    """The job dicts in a store; `{"jobs": [...]}` or a bare list, as Hermes accepts."""
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict) and j.get("id")]


def newest_output_is_silent(store_path: Path, job_id: str, last_run_at: str | None) -> bool:
    """Whether the job's latest run delivered nothing on purpose.

    Best effort, and deliberately biased: when the output directory is missing
    or the newest file predates the run, the run is treated as a real delivery,
    so an unreadable history closes an alert rather than holding one open.
    """
    output_dir = store_path.parent / OUTPUT_DIR / job_id
    if not output_dir.is_dir():
        return False
    candidates = [p for p in output_dir.iterdir() if p.is_file() and p.suffix == OUTPUT_SUFFIX]
    if not candidates:
        return False
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    run_at = parse_iso(last_run_at)
    if run_at is not None and newest.stat().st_mtime < run_at.timestamp() - SILENT_OUTPUT_SLACK_S:
        return False
    try:
        text = newest.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (bool(lines) and lines[-1] == SILENT_MARKER) or SILENT_STATUS_MARKER in text


# --- grading ------------------------------------------------------------------


def grade_error(error: str) -> str:
    if PARTIAL_RE.search(error):
        return GRADE_PARTIAL
    if DEGRADED_MARKER in error:
        return GRADE_DEGRADED
    return GRADE_HARD


def platforms_from(error: str) -> list[str]:
    match = PARTIAL_RE.search(error) or HARD_UNDELIVERED_RE.search(error)
    if not match:
        return []
    return [p.strip() for p in match.group(1).split(",") if p.strip()]


# --- the streak ledger --------------------------------------------------------


def empty_state() -> dict:
    return {
        "version": STATE_SCHEMA_VERSION,
        "last_tick_at": None,
        "last_tick_ok": None,
        "last_tick_error": None,
        "ledger": {"repo": None, "issue_number": None, "fingerprint": None},
        "jobs": {},
    }


def load_state(path: Path) -> dict:
    """The previous tick's state, or a fresh one when there is none or it is unreadable.

    A corrupt file is not fatal: the next tick rebuilds streaks from one run of
    evidence, which under-counts for a tick rather than stopping the watcher.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
        return empty_state()
    state = empty_state()
    state.update({k: v for k, v in data.items() if k in state})
    if not isinstance(state.get("ledger"), dict):
        state["ledger"] = empty_state()["ledger"]
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def new_entry() -> dict:
    return {
        "last_seen_run_at": None,
        "streak": 0,
        "grade": None,
        "first_failure_at": None,
        "last_failure_at": None,
        "last_error": None,
        "platforms": [],
        "alerted": False,
    }


def advance(entry: dict, job: dict, *, silent: bool) -> dict:
    """One job's ledger entry after seeing its current store record.

    Pure: the caller decides `silent` (see `newest_output_is_silent`). The
    streak moves only when `last_run_at` differs from the last run this ledger
    saw, so a half-hourly tick over a daily job counts runs, not ticks.
    """
    run_at = job.get("last_run_at")
    if not run_at or run_at == entry.get("last_seen_run_at"):
        return entry
    updated = dict(entry)
    updated["last_seen_run_at"] = run_at
    error = job.get("last_delivery_error")
    if error:
        error = str(error)
        updated["streak"] = int(entry.get("streak") or 0) + 1
        updated["grade"] = grade_error(error)
        updated["platforms"] = platforms_from(error)
        updated["first_failure_at"] = entry.get("first_failure_at") or run_at
        updated["last_failure_at"] = run_at
        updated["last_error"] = error[:MAX_ERROR_CHARS]
        return updated
    if silent:
        # Delivered nothing, so says nothing about the leg.
        return updated
    updated["streak"] = 0
    return updated


# --- the ledger issue ---------------------------------------------------------


class LedgerError(RuntimeError):
    """A GitHub step failed in a way that must not be read as 'nothing to do'."""


def gh(argv: list[str], repo: str, *, stdin: str | None = None, check: bool = True):
    result = forge.run_gh(argv, repo, stdin=stdin)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise LedgerError(f"gh {' '.join(argv[:2])} exited {result.returncode}: {detail[-1] if detail else ''}")
    return result


def ensure_label(repo: str) -> None:
    gh(
        ["label", "create", LABEL, "-R", repo, "--color", LABEL_COLOR, "--description", LABEL_DESCRIPTION, "--force"],
        repo,
        check=False,
    )


def find_ledger_issue(repo: str) -> int | None:
    """The open ledger issue, if any: highest-numbered open issue carrying the marker."""
    result = gh(
        ["issue", "list", "-R", repo, "--label", LABEL, "--state", "open", "--json", "number,body", "--limit", GH_LIST_LIMIT],
        repo,
    )
    try:
        issues = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise LedgerError(f"gh issue list returned unparseable JSON: {exc}") from exc
    numbers = [int(i["number"]) for i in issues if isinstance(i, dict) and BODY_MARKER in str(i.get("body") or "")]
    return max(numbers) if numbers else None


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("`", "'").replace("\n", " ")


def render_issue(degraded: list[tuple[str, dict]], now: str, threshold_value: int) -> tuple[str, str]:
    """(title, body) for the ledger issue covering every degraded job."""
    profiles = sorted({key.split("/", 1)[0] for key, _ in degraded})
    title = f"{TITLE_PREFIX}{len(degraded)} job(s) on {', '.join(profiles)}"
    rows = ["| Profile | Job | Grade | Consecutive | Did not reach | First failure | Last failure | Last error |", "|---|---|---|---|---|---|---|---|"]
    for key, entry in sorted(degraded):
        profile, job_id = key.split("/", 1)
        rows.append(
            "| "
            + " | ".join(
                [
                    _cell(profile),
                    f"`{_cell(job_id)}`",
                    _cell(entry.get("grade") or ""),
                    str(entry.get("streak") or 0),
                    _cell(", ".join(entry.get("platforms") or []) or "—"),
                    _cell(entry.get("first_failure_at") or ""),
                    _cell(entry.get("last_failure_at") or ""),
                    f"`{_cell(entry.get('last_error') or '')}`",
                ]
            )
            + " |"
        )
    body = "\n".join(
        [
            BODY_MARKER,
            f"Scheduled reports have failed to reach chat on {len(degraded)} job(s) for at least "
            f"{threshold_value} consecutive run(s). The runs themselves completed; what failed is the "
            "delivery. Each report is kept under the profile's `cron/output/<job>/` directory, so do "
            "not re-run a job to resend it.",
            "",
            *rows,
            "",
            "**Grades.** `hard`: the report reached no platform (the relay answered 502, was unreachable, "
            "or had no key). `partial`: it landed on some platforms and not the ones listed. `degraded`: "
            "it was posted but the Chat Agent's turn failed.",
            "",
            "**What to check.**",
            "- The relay route: `logs/session_kv_server.log` in the agent home, around each last-failure time.",
            "- The profile's own view: `hermes cron list` in the profile shows `last_delivery_error`.",
            "- For a `partial`: whether the platform named has a home channel configured and a working connection.",
            "- For a `hard`: whether every enabled chat platform is really configured on this install.",
            "",
            f"_Maintained by `chat_delivery_watch.py`; updated {now}. It closes this issue itself once every leg has recovered._",
        ]
    )
    return title, body


def fingerprint(title: str, body: str) -> str:
    digest = hashlib.sha256()
    digest.update(title.encode("utf-8"))
    digest.update(b"\0")
    # The timestamp line changes every tick; everything above it is the state.
    digest.update(body.rsplit("\n_Maintained by", 1)[0].encode("utf-8"))
    return digest.hexdigest()


def reconcile_issue(repo: str, degraded: list[tuple[str, dict]], state: dict, now: str, threshold_value: int) -> str:
    """Bring the ledger issue in line with `degraded`; returns the `ledger=` value for the ALERT lines."""
    ledger = state["ledger"]
    if degraded:
        ensure_label(repo)
        number = ledger.get("issue_number") if ledger.get("repo") == repo else None
        if number is None:
            number = find_ledger_issue(repo)
        title, body = render_issue(degraded, now, threshold_value)
        digest = fingerprint(title, body)
        if number is None:
            result = gh(
                ["issue", "create", "-R", repo, "--title", title, "--body-file", forge.BODY_STDIN, "--label", LABEL],
                repo,
                stdin=body,
            )
            match = ISSUE_URL_RE.search(result.stdout or "")
            number = int(match.group(1)) if match else None
        elif digest != ledger.get("fingerprint"):
            gh(["issue", "edit", str(number), "-R", repo, "--title", title, "--body-file", forge.BODY_STDIN], repo, stdin=body)
        state["ledger"] = {"repo": repo, "issue_number": number, "fingerprint": digest}
        return f"{repo}#{number}" if number else repo
    number = ledger.get("issue_number")
    if number is not None and ledger.get("repo") == repo:
        gh(
            ["issue", "comment", str(number), "-R", repo, "--body-file", forge.BODY_STDIN],
            repo,
            stdin=f"Every scheduled report reached chat again as of {now}; closing.",
        )
        gh(["issue", "close", str(number), "-R", repo, "--reason", CLOSE_REASON], repo)
        state["ledger"] = {"repo": None, "issue_number": None, "fingerprint": None}
        return LEDGER_CLOSED
    return LEDGER_NONE


def ledger_repo() -> str | None:
    repos = gitops_workspace.get_managed_github_repos()
    return sorted(repos)[0] if repos else None


# --- the ALERT lines ----------------------------------------------------------


def format_alert(key: str, entry: dict, threshold_value: int, ledger: str) -> str:
    profile, job_id = key.split("/", 1)
    return " ".join(
        [
            LOG_PREFIX,
            f"job={job_id}",
            f"profile={profile}",
            f"grade={entry.get('grade')}",
            f"streak={entry.get('streak')}",
            f"threshold={threshold_value}",
            f"platforms={','.join(entry.get('platforms') or []) or '-'}",
            f"since={entry.get('first_failure_at')}",
            f"ledger={ledger}",
            f"error={json.dumps(entry.get('last_error') or '')}",
        ]
    )


def format_recovery(key: str, ledger: str) -> str:
    profile, job_id = key.split("/", 1)
    return f"{LOG_PREFIX} job={job_id} profile={profile} recovered=true streak=0 ledger={ledger}"


def format_self_error(exc: BaseException) -> str:
    return f"{LOG_PREFIX} self=error kind={type(exc).__name__} detail={json.dumps(str(exc)[:MAX_ERROR_CHARS])}"


def emit(lines: list[str], agent_home: Path, now: str) -> None:
    """Print each line and append it, timestamped, to the file fluent-bit ships."""
    if not lines:
        return
    for line in lines:
        print(line)
    log_path = agent_home / LOGS_DIR / ALERT_FILE_NAME
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("".join(f"{now} {line}\n" for line in lines))
    except OSError as exc:
        print(f"{LOG_PREFIX} self=warning detail={json.dumps(f'could not append to {log_path}: {exc}')}")


# --- the tick -----------------------------------------------------------------


def parse_roster_arg(value: str) -> tuple[str, Path]:
    profile, sep, path = value.partition("=")
    if not sep or not profile or not path:
        raise argparse.ArgumentTypeError("expected PROFILE=PATH")
    return profile, Path(path)


def tick(agent_home: Path, state_path: Path, rosters: list[tuple[str, Path]], *, dry_run: bool) -> list[str]:
    """One pass: advance every streak, reconcile the issue, return the ALERT lines."""
    now = now_iso()
    limit = threshold()
    state = load_state(state_path)
    previous = state["jobs"]
    current: dict[str, dict] = {}
    unreadable: list[str] = []

    for profile, store in rosters:
        try:
            jobs = load_jobs(store)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            unreadable.append(f"{profile}: {exc}")
            continue
        for job in jobs:
            key = f"{profile}/{job['id']}"
            entry = previous.get(key) or new_entry()
            silent = not job.get("last_delivery_error") and newest_output_is_silent(store, job["id"], job.get("last_run_at"))
            current[key] = advance(entry, job, silent=silent)

    degraded = [(k, e) for k, e in current.items() if int(e.get("streak") or 0) >= limit]
    recovered = [k for k, e in current.items() if e.get("alerted") and int(e.get("streak") or 0) == 0]

    ledger_ref = LEDGER_DRY_RUN if dry_run else LEDGER_NONE
    lines: list[str] = []
    if not dry_run:
        repo = ledger_repo()
        if repo:
            try:
                ledger_ref = reconcile_issue(repo, degraded, state, now, limit)
            except (LedgerError, sandbox_exec.SandboxUnavailable) as exc:
                ledger_ref = f"error:{type(exc).__name__}"
                lines.append(format_self_error(exc))
    for key, entry in sorted(degraded):
        lines.append(format_alert(key, entry, limit, ledger_ref))
        entry["alerted"] = True
    for key in sorted(recovered):
        lines.append(format_recovery(key, ledger_ref if ledger_ref == LEDGER_CLOSED else LEDGER_NONE))
        current[key]["alerted"] = False
    for note in unreadable:
        lines.append(f"{LOG_PREFIX} self=warning detail={json.dumps(f'unreadable cron store {note}')}")

    state["jobs"] = current
    state["last_tick_at"] = now
    state["last_tick_ok"] = True
    state["last_tick_error"] = None
    if not dry_run:
        save_state(state_path, state)
    emit(lines, agent_home, now)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="compute and print; write no state and touch no issue")
    parser.add_argument("--state", type=Path, help=f"ledger path (default: <home>/profiles/platform/cron/{STATE_FILE_NAME}, or ${STATE_PATH_ENV})")
    parser.add_argument("--roster", type=parse_roster_arg, action="append", help="PROFILE=PATH; scan only these stores (repeatable)")
    args = parser.parse_args(argv)

    agent_home = Path(gitops_workspace.agent_home())
    state_path = args.state or Path(os.environ.get(STATE_PATH_ENV) or agent_home / PROFILES_DIR / PLATFORM_PROFILE / CRON_DIR / STATE_FILE_NAME)
    rosters = args.roster or roster_paths(agent_home)
    try:
        tick(agent_home, state_path, rosters, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - the cron path must exit 0 and say why on the log channel
        now = now_iso()
        emit([format_self_error(exc)], agent_home, now)
        try:
            state = load_state(state_path)
            state["last_tick_at"] = now
            state["last_tick_ok"] = False
            state["last_tick_error"] = str(exc)[:MAX_ERROR_CHARS]
            if not args.dry_run:
                save_state(state_path, state)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
