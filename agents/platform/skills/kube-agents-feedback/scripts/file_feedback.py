#!/usr/bin/env python3
"""File a confirmed kube-agents report through the credential broker, exactly once.

The shell sandbox reaches DNS and the credential broker and nothing else, so
the form's wire path lives on the broker (`feedback_broker.py`, route
`POST /v1/feedback/submit`). This script is the sandbox side: it asks once per
confirmed text, and remembers that it asked.

The memory is a marker file in the task workspace, written *before* the call
and finalised after it. A rerun that finds the marker with an issue URL
answers from it and asks nothing; one that finds it without a URL (the
broker's answer was lost mid-flight) asks the broker to *recheck only*, which
reads the tracker and never POSTs. A broker that could not be reached at all,
or that refused the payload, sent nothing, so the marker is removed again and
the next attempt is a first attempt. Everything here is the #1345/#1346
incident, encoded: one report filed twice because a retry did not know the
first attempt had landed.

Input is a JSON payload file:

    {"title": "...", "kind": "Bug", "happened": "...",
     "expected": "...", "environment": "...", "user_confirmed": true}

`title`, `kind`, `happened` and `user_confirmed` are required; `user_confirmed`
is the caller's assertion that the person saw this exact text and said yes.
The broker validates the rest (the kinds, the 120-character title, secret-shaped
text). Output is one JSON line on stdout, the broker's object as received:

    {"status": "SUBMITTED", "issue_url": "...", "fallback": {...}}
    {"status": "ALREADY_SUBMITTED", "issue_url": "...", "fallback": {...}}
    {"status": "UNCONFIRMED", "issue_url": null, "detail": "...", "fallback": {...}}
    {"status": "BLOCKED", "reason": "...", "fallback": {...}}   (exit 1; nothing was sent)

`fallback` names the tracker and the form's short link on every answer, so a
report the install could not file can be handed back with where to file it.

Run:
    file_feedback.py submit --payload-file payload.json [--state-dir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# The shared helpers: staged into /opt/defaults/scripts in the images, beside
# this tree in a source checkout.
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
sys.path.append(str(Path(__file__).resolve().parents[3] / "scripts"))

import credential_proxy_client  # noqa: E402
import feedback_broker  # noqa: E402

BROKER_ENDPOINT_VAR = "CREDENTIAL_PROXY_URL"
ROUTE = feedback_broker.ROUTE
RECHECK_FIELD = feedback_broker.RECHECK_FIELD
FALLBACK = feedback_broker.FALLBACK
STATUS_BLOCKED = feedback_broker.STATUS_BLOCKED
STATUS_ALREADY_SUBMITTED = feedback_broker.STATUS_ALREADY_SUBMITTED

MARKER_PREFIX = ".feedback-submission-"
STATE_HASH_LENGTH = 16
BLOCKED_EXIT = 1
# A lost answer exits non-zero too, but on a different code than BLOCKED: the
# agent's next step differs (rerun to recheck, versus fix and resend).
LOST_EXIT = 2
CONTENT_TYPE_JSON = "application/json"

REASON_NO_BROKER = (
    f"{BROKER_ENDPOINT_VAR} is not set, so there is no broker to ask; this "
    "skill runs in the shell sandbox"
)
REASON_BROKER_UNREACHABLE = "the credential broker could not be reached ({error}); nothing was sent"
REASON_BROKER_ANSWER = (
    "the credential broker answered HTTP {code} without a feedback status; nothing was sent"
)
DETAIL_MARKER_SUBMITTED = (
    "a submission for this exact text already went out from this workspace; not filing it again"
)
LOST_MESSAGE = (
    "the broker's answer was lost after the request went out ({error}); the marker "
    "is kept, and rerunning the same command only asks the broker to read the tracker"
)


class BrokerAnswerLost(Exception):
    """The request reached the broker and its answer did not come back whole."""


def _print(answer: dict[str, Any]) -> None:
    print(json.dumps(answer, separators=(",", ":")))


def _blocked(reason: str) -> int:
    _print(feedback_broker.blocked_answer(reason))
    return BLOCKED_EXIT


def _payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {field: payload[field] for field in feedback_broker.QUESTION_LABELS}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:STATE_HASH_LENGTH]


def _marker_path(state_dir: Path, payload: dict[str, Any]) -> Path:
    return state_dir / f"{MARKER_PREFIX}{_payload_hash(payload)}.json"


def _read_marker(marker: Path) -> dict[str, Any]:
    """The marker's state, or a bare attempted-state if it was corrupted.

    A marker that exists but cannot be parsed still means an attempt started;
    treating it as anything less would re-open the door to a second POST.
    """
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"started_at": 0.0}


def _post_to_broker(endpoint: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """One `POST /v1/feedback/submit`: the HTTP status and the JSON body (``{}`` if none).

    Raises ``urllib.error.URLError`` when the broker could not be reached at all
    (nothing was sent) and ``BrokerAnswerLost`` when the connection broke after
    the request went out (the broker may have filed).
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": CONTENT_TYPE_JSON}
    headers.update(credential_proxy_client.authorization_headers())
    request = urllib.request.Request(
        endpoint.rstrip("/") + ROUTE, data=body, headers=headers, method="POST"
    )
    try:
        with credential_proxy_client.open_broker_request(request) as response:
            return response.status, _json_or_empty(response.read())
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, _json_or_empty(exc.read())
    except (http.client.HTTPException, ConnectionResetError, TimeoutError) as exc:
        raise BrokerAnswerLost(f"{type(exc).__name__}: {exc}") from exc


def _json_or_empty(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def submit(payload_path: Path, state_dir: Path) -> int:
    payload = feedback_broker.normalize(json.loads(payload_path.read_text(encoding="utf-8")))
    marker = _marker_path(state_dir, payload)

    if marker.exists():
        state = _read_marker(marker)
        if state.get("issue_url"):
            _print({
                "status": STATUS_ALREADY_SUBMITTED,
                "issue_url": state["issue_url"],
                "detail": DETAIL_MARKER_SUBMITTED,
                "fallback": FALLBACK,
            })
            return 0
        payload = {**payload, RECHECK_FIELD: True}

    endpoint = os.environ.get(BROKER_ENDPOINT_VAR, "").strip()
    if not endpoint:
        return _blocked(REASON_NO_BROKER)

    first_attempt = not marker.exists()
    if first_attempt:
        marker.write_text(
            json.dumps({"started_at": time.time(), "title": payload["title"]}), encoding="utf-8"
        )
    try:
        code, answer = _post_to_broker(endpoint, payload)
    except urllib.error.URLError as exc:
        if first_attempt:
            marker.unlink(missing_ok=True)
        return _blocked(REASON_BROKER_UNREACHABLE.format(error=exc.reason))
    except BrokerAnswerLost as exc:
        print(LOST_MESSAGE.format(error=exc), file=sys.stderr)
        raise SystemExit(LOST_EXIT) from exc

    status = answer.get("status")
    if code == 200 and status and status != STATUS_BLOCKED:
        state = _read_marker(marker)
        state.update({"status": status, "issue_url": answer.get("issue_url")})
        marker.write_text(json.dumps(state), encoding="utf-8")
        _print(answer)
        return 0
    # Refused (400 BLOCKED), or an answer that is not the broker's: nothing was
    # sent, so a first attempt is forgotten and the next one starts fresh.
    if first_attempt:
        marker.unlink(missing_ok=True)
    if status == STATUS_BLOCKED:
        _print(answer)
        return BLOCKED_EXIT
    return _blocked(REASON_BROKER_ANSWER.format(code=code))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit_parser = subparsers.add_parser("submit", help="file the confirmed payload, exactly once")
    submit_parser.add_argument("--payload-file", type=Path, required=True)
    submit_parser.add_argument(
        "--state-dir", type=Path, default=Path.cwd(),
        help="where the submission marker lives; use the task workspace so a "
             "rerun sees its own earlier attempt",
    )
    args = parser.parse_args()
    return submit(args.payload_file, args.state_dir)


if __name__ == "__main__":
    sys.exit(main())
