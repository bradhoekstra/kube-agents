"""File a confirmed kube-agents report through the public feedback form, exactly once.

The credential broker serves this as ``POST /v1/feedback/submit`` for the
shell sandbox, whose NetworkPolicy allows DNS and the broker and nothing else.
The broker pod has egress, so the whole wire path runs here: resolve the
current form from the docs-site short link, read the form's field ids, POST
once, and find the resulting issue on the tracker. It holds no credential and
nothing across calls; the form needs no account and the tracker is public.

A submission becomes a public GitHub issue on gke-labs/kube-agents, opened by
the form's pipeline (scripts/feedback_form/README.md). Everything defensive
here is one incident encoded: a live agent that POSTed the form by hand missed
the confirmation in the response, retried, and filed the same report twice
(gke-labs/kube-agents#1345 and #1346).

- The form URL is discovered from the short link at request time, never
  hardcoded, so a recreated form (same link, new form id) needs no release.
- Fields are mapped by their question label, not their entry id, because ids
  change when the form is recreated and labels are the contract with the
  form's own builder script (``Code.gs``; test_feedback_broker.py pins them).
- Before a first POST the tracker is read for a recent issue with this exact
  title, so the same confirmed text dispatched twice is filed once. The
  caller keeps its own marker across attempts and asks for ``recheck_only``
  after a lost answer, which reads the tracker and never POSTs.

``submit`` answers one of::

    {"status": "SUBMITTED", "issue_url": "...", "fallback": {...}}
    {"status": "ALREADY_SUBMITTED", "issue_url": "...", "fallback": {...}}
    {"status": "UNCONFIRMED", "issue_url": null, "detail": "...", "fallback": {...}}

and raises ``Blocked`` when nothing was sent and nothing will be (the route
answers 400 with ``blocked_answer``). ``fallback`` carries the two links the
person can use themselves, on every answer, so the agent relays one source.
"""

from __future__ import annotations

import calendar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

# The one link agents hand out; the docs site serves a meta-refresh page here
# that names the current form. tests/test_feedback_reference.py ties it to the
# redirect in docs/site/astro.config.mjs, and a2a/lib/chattext/commands.go
# carries the same two links for the gateway's own answer.
SHORT_LINK = "https://gke-labs.github.io/kube-agents/feedback"
TRACKER_URL = "https://github.com/gke-labs/kube-agents/issues"
# Where the pipeline files the issue, and the label the form's script applies.
TRACKER_REPO = "gke-labs/kube-agents"
TRACKER_API = f"https://api.github.com/repos/{TRACKER_REPO}/issues"
FEEDBACK_LABEL = "external-feedback"
FALLBACK = {"tracker": TRACKER_URL, "form": SHORT_LINK}

ROUTE = "/v1/feedback/submit"

STATUS_SUBMITTED = "SUBMITTED"
STATUS_ALREADY_SUBMITTED = "ALREADY_SUBMITTED"
STATUS_UNCONFIRMED = "UNCONFIRMED"
STATUS_BLOCKED = "BLOCKED"

# The form questions this fills, keyed by the label the form's builder gives
# each one. Labels are the stable contract: entry ids change whenever the form
# is recreated.
QUESTION_LABELS = {
    "title": "One-line summary",
    "kind": "What kind of feedback is this?",
    "happened": "What happened?",
    "expected": "What did you expect instead?",
    "environment": "Version and environment",
}
REQUIRED_FIELDS = ("title", "kind", "happened")
KIND_CHOICES = ("Bug", "Feature request", "Question", "Other")
CONFIRMED_FIELD = "user_confirmed"
RECHECK_FIELD = "recheck_only"

# The pipeline truncates the issue title at this many characters (Code.gs
# TITLE_MAX_CHARS). A longer summary would file fine but the filed issue's
# title would no longer equal the payload's, so find_issue could never return
# its URL; refuse it up front, where the person can shorten it.
TITLE_MAX_CHARS = 120

# A stable substring of the form's confirmation message (CONFIRMATION_MESSAGE
# in Code.gs); its presence in the POST response is what distinguishes
# "submitted" from "the form served an error page with status 200".
CONFIRMATION_SUBSTRING = "filed as a public issue"

# Content that must never reach a public issue, whoever typed it. These block;
# the softer redaction judgment (cluster names, project ids) is the persona's,
# because no regex recognizes a cluster name.
SECRET_PATTERNS = (
    ("private key material", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("a bearer token", re.compile(r"\bBearer [A-Za-z0-9._~+/-]{20,}")),
    ("a signed JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ")),
)

# The viewform page embeds every question as JSON in this variable.
FB_DATA_PATTERN = re.compile(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\])\s*;", re.DOTALL)
FORM_URL_PATTERN = re.compile(r"https://docs\.google\.com/forms/d/e/([A-Za-z0-9_-]+)/viewform")
VIEWFORM_SUFFIX = "/viewform"
RESPONSE_SUFFIX = "/formResponse"
ENTRY_PREFIX = "entry."

HTTP_TIMEOUT_SECONDS = 30
USER_AGENT = "kube-agents-feedback-broker"
# The pipeline says the issue appears "within a minute"; poll a little longer.
TRACKER_POLL_SECONDS = 90
TRACKER_POLL_INTERVAL_SECONDS = 10
TRACKER_PAGE_SIZE = 20
TRACKER_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Before a first POST, one tracker read looks for this payload's title among
# recent feedback issues: a match inside this window means an earlier call
# already filed this text, and filing it again is the #1345/#1346 duplicate.
DUPLICATE_LOOKBACK_SECONDS = 24 * 60 * 60
# find_issue's `since` is one second before the POST, so an issue the pipeline
# stamps in the same second as the POST still counts.
POST_CLOCK_SLACK_SECONDS = 1

DETAIL_ALREADY_FILED = "an issue with this exact title was filed recently; not filing it again"
DETAIL_RECHECK_LOST = (
    "an earlier attempt for this exact text exists but no issue has appeared on "
    "the tracker; nothing was re-sent -- if this persists, hand it to a human "
    "rather than filing again"
)
DETAIL_POST_UNCLEAN = (
    "the POST to the form did not complete cleanly ({error}); a recheck is safe "
    "and will only read the tracker for the issue"
)
DETAIL_NO_SIGNAL = (
    "the form did not confirm and no issue has appeared yet; a recheck is safe "
    "and will only read the tracker"
)
REASON_UNCONFIRMED_PAYLOAD = (
    f"payload lacks {CONFIRMED_FIELD}: true -- nothing is filed until the person "
    "has seen this exact text and said yes"
)
REASON_FORM_RETIRED = (
    f"{SHORT_LINK} no longer names a Google form; the form may have been retired "
    "(scripts/feedback_form/README.md)"
)
REASON_FORM_UNREACHABLE = "could not reach the feedback form ({error}); nothing was sent"
REASON_NO_FORM_DATA = "the form page carried no FB_PUBLIC_LOAD_DATA_"
REASON_QUESTIONS_DIVERGED = (
    "the form no longer asks: {missing} -- its questions and this module's "
    "QUESTION_LABELS have diverged"
)
REASON_TITLE_TOO_LONG = (
    "the one-line summary is {length} characters; the pipeline truncates issue "
    f"titles at {TITLE_MAX_CHARS}, which would leave the filed issue unfindable "
    "by title -- shorten it"
)

Fetch = Callable[..., str]


class Blocked(Exception):
    """Nothing was sent and nothing will be; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def blocked_answer(reason: str) -> dict[str, Any]:
    return {"status": STATUS_BLOCKED, "reason": reason, "fallback": FALLBACK}


def _answer(status: str, issue_url: str | None, detail: str | None = None) -> dict[str, Any]:
    answer: dict[str, Any] = {"status": status, "issue_url": issue_url}
    if detail is not None:
        answer["detail"] = detail
    answer["fallback"] = FALLBACK
    return answer


def _fetch(url: str, data: bytes | None = None) -> str:
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def resolve_form_url(short_link_html: str) -> str:
    """The current form's viewform URL, from the short link's redirect page."""
    match = FORM_URL_PATTERN.search(short_link_html)
    if not match:
        raise Blocked(REASON_FORM_RETIRED)
    return match.group(0)


def entry_ids_by_field(viewform_html: str) -> dict[str, str]:
    """Map payload field -> form entry id, matching questions by label."""
    match = FB_DATA_PATTERN.search(viewform_html)
    if not match:
        raise Blocked(REASON_NO_FORM_DATA)
    data = json.loads(match.group(1))
    label_to_entry: dict[str, str] = {}
    for question in data[1][1]:
        try:
            label, entry = question[1], question[4][0][0]
        except (TypeError, IndexError):
            continue
        label_to_entry[label] = str(entry)
    mapped, missing = {}, []
    for field, label in QUESTION_LABELS.items():
        if label in label_to_entry:
            mapped[field] = f"{ENTRY_PREFIX}{label_to_entry[label]}"
        else:
            missing.append(label)
    if missing:
        raise Blocked(REASON_QUESTIONS_DIVERGED.format(missing=", ".join(missing)))
    return mapped


def normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload with every form field stripped, exactly as it is POSTed.

    The stripped title is also what find_issue matches, so stripping in one
    place keeps the submission and the lookup identical.
    """
    normalized = dict(payload)
    for field in QUESTION_LABELS:
        normalized[field] = str(payload.get(field, "")).strip()
    return normalized


def validate(payload: dict[str, Any]) -> None:
    """Refuse, with ``Blocked``, anything that must not reach the form."""
    if payload.get(CONFIRMED_FIELD) is not True:
        raise Blocked(REASON_UNCONFIRMED_PAYLOAD)
    for field in REQUIRED_FIELDS:
        if not payload[field]:
            raise Blocked(f"required field '{field}' is empty")
    if payload["kind"] not in KIND_CHOICES:
        raise Blocked(f"kind {payload['kind']!r} is not one of {', '.join(KIND_CHOICES)}")
    if len(payload["title"]) > TITLE_MAX_CHARS:
        raise Blocked(REASON_TITLE_TOO_LONG.format(length=len(payload["title"])))
    text = "\n".join(payload[field] for field in QUESTION_LABELS)
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise Blocked(f"the text contains {name}; that must never reach a public issue, so nothing was sent")


def find_issue(
    title: str,
    since_epoch: float,
    deadline_seconds: int,
    fetch: Fetch = _fetch,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """The tracker URL of the filed issue, matched by exact title, or None.

    Unauthenticated read of a public repo. A deadline of 0 means one read.
    """
    query = urllib.parse.urlencode(
        {"labels": FEEDBACK_LABEL, "state": "all", "per_page": TRACKER_PAGE_SIZE}
    )
    deadline = now() + deadline_seconds
    while True:
        try:
            issues = json.loads(fetch(f"{TRACKER_API}?{query}"))
            for issue in issues:
                created = calendar.timegm(time.strptime(issue["created_at"], TRACKER_TIME_FORMAT))
                if issue["title"] == title and created >= since_epoch:
                    return issue["html_url"]
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
            pass  # the tracker being briefly unreachable is not a submit failure
        if now() >= deadline:
            return None
        sleep(TRACKER_POLL_INTERVAL_SECONDS)


def submit(
    payload: dict[str, Any],
    fetch: Fetch = _fetch,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Validate, file once, and answer a status object; raise ``Blocked`` if nothing went out."""
    payload = normalize(payload)
    validate(payload)
    title = payload["title"]

    # The same confirmed text may already have been filed, from this card or
    # another. One tracker read settles it before anything is sent.
    prior_url = find_issue(title, now() - DUPLICATE_LOOKBACK_SECONDS, 0, fetch, now, sleep)
    if prior_url:
        return _answer(STATUS_ALREADY_SUBMITTED, prior_url, DETAIL_ALREADY_FILED)
    if payload.get(RECHECK_FIELD) is True:
        # The caller lost an earlier answer. The tracker is the arbiter; a
        # POST here would be the duplicate this whole module exists to stop.
        return _answer(STATUS_UNCONFIRMED, None, DETAIL_RECHECK_LOST)

    try:
        form_url = resolve_form_url(fetch(SHORT_LINK))
        entries = entry_ids_by_field(fetch(form_url))
    except (urllib.error.URLError, OSError) as exc:
        raise Blocked(REASON_FORM_UNREACHABLE.format(error=exc)) from exc
    form_data = {entries[field]: payload[field] for field in QUESTION_LABELS if payload[field]}
    response_url = form_url.replace(VIEWFORM_SUFFIX, RESPONSE_SUFFIX)

    started_at = now()
    try:
        body = fetch(response_url, urllib.parse.urlencode(form_data).encode("utf-8"))
    except (urllib.error.URLError, OSError) as exc:
        # The POST's fate is unknown: say so rather than retry. The caller's
        # recheck reads the tracker.
        return _answer(STATUS_UNCONFIRMED, None, DETAIL_POST_UNCLEAN.format(error=exc))
    confirmed = CONFIRMATION_SUBSTRING in body

    issue_url = find_issue(
        title, started_at - POST_CLOCK_SLACK_SECONDS, TRACKER_POLL_SECONDS, fetch, now, sleep
    )
    if not confirmed and not issue_url:
        return _answer(STATUS_UNCONFIRMED, None, DETAIL_NO_SIGNAL)
    return _answer(STATUS_SUBMITTED, issue_url)
