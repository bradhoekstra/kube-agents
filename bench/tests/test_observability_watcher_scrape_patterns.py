r"""Pin the observability-watcher-scrape-state verifier patterns.

Two checks in that case's task.yaml decide a run and are easy to break with a
regex tweak that reads fine but shifts what matches:

* ``the-worker-read-the-podmonitoring`` -- a ``worker_commands`` check that must
  match a real ``kubectl get/describe podmonitoring`` however it is wrapped, and
  is deliberately loose: it requires only that ``kubectl`` and ``podmonitoring``
  co-occur in one command. A precise regex insisting the kind be the resource
  argument re-implemented shell grammar and leaked both ways across review rounds,
  so it was dropped. The loose pattern has no false-reds (every real read matches,
  however wrapped) and accepts co-occurrence false-greens (a ``can-i`` probe, a
  CRD read, the kind in a filename or selector, the command quoted inside another);
  what it still refuses is a command that pairs ``kubectl`` with no
  ``podmonitoring`` at all -- the old skill's Deployment read, the regression this
  case guards. ``WorkerCommandsVerifier`` ``re.search``es the pattern against the
  raw command string, so this test does the same.

* ``the-answer-affirms-the-watcher-is-scraped`` -- a ``report_contains`` that
  grades a verdict token the prompt asks the worker to emit, a first line
  ``Scraped: yes`` or ``Scraped: no``, rather than reading polarity out of free
  prose. ``required_phrases`` pins the affirmative token (a substring of the flat
  normalization) and ``forbidden_patterns`` reds the negative one with a
  line-anchored ``(?m)^\s*scraped:\s*no\b`` (``re.search``ed against the
  line-preserving normalization), so it fires only on a line whose verdict is
  ``Scraped: no`` -- not on ``scraped: nothing``/``not yet`` (word boundary) or an
  affirmative that mentions ``scraped: no`` in a later aside (line anchor). The
  nuance (a correct answer may note the credential-proxy PodMonitoring is
  unscraped while the watcher is) is left to the judge. ``ReportContainsVerifier``
  tests the phrase against ``_normalize(text)`` and the pattern against
  ``_normalize_lines(text)``, so this test reproduces both and cross-checks the
  normalizer against the real verifier when the bench package imports.

The checks are read from task.yaml, not duplicated here, so the test pins the
file rather than a copy of it. The residuals each check accepts -- the route's
co-occurrence false-greens, the polarity's one inline false-green -- are asserted
as known cases, so a future tightening that refuses one trips this test and
updates the task.yaml comment with it.

Run:
  python3 -m pytest bench/tests/test_observability_watcher_scrape_patterns.py -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

CASE = (
    Path(__file__).resolve().parents[1]
    / "tasks"
    / "observability-watcher-scrape-state"
    / "task.yaml"
)

ROUTE_OBJECTIVE = "the-worker-read-the-podmonitoring"
POLARITY_OBJECTIVE = "the-answer-affirms-the-watcher-is-scraped"


def _objectives(node):
    """Every node carrying a check, depth-first."""
    out = []
    if isinstance(node, dict):
        if "check" in node:
            out.append(node)
        for value in node.values():
            out += _objectives(value)
    elif isinstance(node, list):
        for value in node:
            out += _objectives(value)
    return out


def _check_named(name):
    doc = yaml.safe_load(CASE.read_text(encoding="utf-8"))
    for objective in _objectives(doc):
        if objective.get("name") == name:
            return objective["check"]
    raise AssertionError(f"{CASE} has no objective named {name!r}")


# Faithful copies of kube_agents_bench.verifiers._normalize and _normalize_lines,
# so this test runs without the bench package's runtime dependencies (as the
# sibling pattern tests do). test_normalize_matches_the_verifier pins them to the
# real ones whenever the package does import.
_MARKDOWN_NOISE = str.maketrans({"*": None, "_": None, "`": None, "’": "'"})


def _normalize(text: str) -> str:
    stripped = text.translate(_MARKDOWN_NOISE)
    collapsed = " ".join(stripped.split())
    if stripped[:1].isspace():
        collapsed = " " + collapsed
    if stripped[-1:].isspace():
        collapsed += " "
    return collapsed.lower()


def _normalize_lines(text: str) -> str:
    return "\n".join(_normalize(line) for line in text.splitlines())


# --- route corpus -----------------------------------------------------------

# Real PodMonitoring reads, each a shape a delegated worker has used or plausibly
# would. The regex must match every one.
ROUTE_POSITIVES = [
    "kubectl get podmonitoring platform-agent-gateway-monitoring -n kubeagents-system",
    "kubectl get podmonitoring,clusterpodmonitoring -A",
    "kubectl describe podmonitoring platform-agent-gateway-monitoring -n kubeagents-system",
    "kubectl get podmonitorings.monitoring.googleapis.com -A",
    "kubectl get -o yaml podmonitoring/platform-agent-gateway-monitoring -n kubeagents-system",
    "kubectl get podmonitoring -A -o yaml",
    "KUBECONFIG=/tmp/kc kubectl get podmonitoring -A",
    "sudo kubectl get podmonitoring -A",
    "sudo -E kubectl get podmonitoring -A",
    "timeout 30 kubectl get podmonitoring -A",
    "timeout -k 5 30 kubectl get podmonitoring -A",
    "kubectl get ns -o name | xargs -I{} kubectl get podmonitoring -n {}",
    'for ns in $(kubectl get ns -o name); do kubectl get podmonitoring -n "$ns"; done',
    "if kubectl get podmonitoring platform-agent-gateway-monitoring -n kubeagents-system; then echo y; fi",
    "time kubectl get podmonitoring -A",
    "env KUBECONFIG=/tmp/kc kubectl get podmonitoring -A",
    "cd /tmp && kubectl get podmonitoring -A",
    "kubectl get podmonitoring -A | yq .items",
    "/usr/bin/kubectl get podmonitoring -A",
    "kubectl describe PodMonitoring platform-agent-gateway-monitoring",
    # The noun ends its own segment, before a terminator rather than a space: the
    # comment says the segment end admits the read, and the lookahead now does.
    "kubectl get podmonitoring|yq .items",
    "echo $(kubectl get podmonitoring)",
    "(kubectl get podmonitoring)",
    "kubectl get podmonitoring&& echo done",
    # A `sh -c '…'` / `bash -c "…"` wrapper is a command position, not a quoted
    # string, so kubectl inside one still reads.
    "bash -c 'kubectl get podmonitoring -A'",
    'sh -c "kubectl get podmonitoring -n kubeagents-system"',
    # The resource word itself quoted.
    'kubectl get "podmonitoring" -A',
]

# The loose pattern refuses only a command that does not pair `kubectl` with
# `podmonitoring`. The ones that matter are the regression this case guards -- the
# old skill's Deployment read, in every spelling -- and a read of the kind from a
# file rather than the API (a `cat` has no kubectl; a `get deployment ... -o yaml`
# has no podmonitoring).
ROUTE_NEGATIVES = [
    "kubectl get deployment platform-agent-gateway -n kubeagents-system -o yaml",
    "kubectl get deploy platform-agent-gateway -o yaml",
    "kubectl -n kubeagents-system get deployment platform-agent-gateway -o yaml",
    "kubectl describe deployment platform-agent-gateway -n kubeagents-system",
    "cat podmonitoring.yaml",
]

# Co-occurrence false-greens the loose pattern accepts by design: `kubectl` and
# `podmonitoring` appear in one command, but the command does not read the
# PodMonitoring as the resource argument of get/describe. All are caught by the
# answer objectives (a worker that only did these cannot name port 9095 and the
# proving series), and refusing them precisely re-implements shell grammar, which
# leaked across rounds. Asserted so a future tightening that refuses one fails
# here and the comment is updated with it.
ROUTE_KNOWN_RESIDUALS = [
    # The command quoted inside another -- a grep/echo of the string reads nothing.
    'grep "kubectl get podmonitoring" notes.txt',
    "echo 'kubectl get podmonitoring platform-agent-gateway-monitoring'",
    'echo " kubectl get podmonitoring"',
    "echo kubectl get podmonitoring",
    'git commit -m "note; kubectl get podmonitoring is the check"',
    "echo \"sh -c 'kubectl get podmonitoring'\"",
    # A `can-i` permission probe -- names the kind without reading it.
    "kubectl auth can-i get podmonitoring -n kubeagents-system",
    # A CRD read -- the kind's definition, not an instance.
    "kubectl get crd podmonitorings.monitoring.googleapis.com",
    "kubectl get customresourcedefinition podmonitorings.monitoring.googleapis.com",
    # The kind glued to a separator in a filename or selector, after a read of
    # something else.
    "kubectl get pods -n kubeagents-system -o yaml > /tmp/podmonitoring.yaml",
    "kubectl get deployment platform-agent-gateway -o yaml > gateway-podmonitoring-check.yaml",
    "kubectl get events -n kubeagents-system --field-selector involvedObject.kind=PodMonitoring",
]

# --- polarity corpus --------------------------------------------------------

# Correct answers: the watcher IS scraped. Each carries the `Scraped: yes` token
# and no line whose verdict is `Scraped: no`, so required_phrases is satisfied and
# forbidden_patterns does not fire. The second mixes a scraped watcher with an
# unscraped credential-proxy -- the token carries the verdict, the prose nuance
# goes to the judge; the third folds case and markdown the normalizer strips; the
# fourth writes `scraped: no` in a later clause (not as a line's verdict), which
# the bare-substring forbidden_phrases used to false-red and the line anchor now
# admits.
POLARITY_CORRECT = [
    "Scraped: yes. The watcher metrics are scraped through the "
    '`platform-agent-gateway-monitoring` PodMonitoring on port 9095; '
    'up{job="platform-agent-gateway-monitoring"} reads 1.',
    "Scraped: yes. The watcher's 9095 metrics are scraped every 30s by GKE "
    "Managed Prometheus via the gateway-monitoring PodMonitoring. The "
    "credential-proxy metrics are not being scraped yet.",
    "**Scraped: Yes.** k8s_event_watcher_cluster_up confirms port 9095 is "
    "scraped through the gateway-monitoring PodMonitoring.",
    "Scraped: yes. The credential-proxy, by contrast, is scraped: no.",
]

# Incorrect answers: the watcher is NOT scraped. Each carries the `Scraped: no`
# token (hitting forbidden_phrases) or omits the verdict line (missing
# required_phrases), even though some name the same mechanism, port and series the
# other objective checks. The last three are shapes the earlier enumerated-negation
# regex false-greened -- a contraction, perfect tense, active voice -- that the
# token reds on the verdict alone.
POLARITY_INCORRECT = [
    "Scraped: no. The event watcher's metrics are not scraped: the "
    "gateway-monitoring PodMonitoring targets 9095 but up{job=...} returns "
    "nothing.",
    "Scraped: no. The gateway Deployment carries no prometheus.io/scrape "
    "annotation; port 9095 is unmonitored.",
    "The watcher is not being scraped; nothing targets 9095.",
    "Scraped: no. 9095 isn't scraped.",
    "Scraped: no. The watcher was never scraped on this install.",
    "Scraped: no. Prometheus is not scraping 9095.",
]

# Polarity residual, documented in the task.yaml comment and accepted: a negative
# that ignores the verdict-line instruction, omitting the line but carrying the
# "scraped: yes" substring inline, false-greens -- no substring fix leaves the
# compliant token alone. (The old false-red -- an affirmative that writes
# "scraped: no" in a later clause -- is fixed by the line anchor and now sits in
# POLARITY_CORRECT.) Asserted so a future tightening that fixes it trips this test.
POLARITY_KNOWN_FALSE_GREEN = (
    "Is it scraped: yes it has a gateway-monitoring PodMonitoring, but nothing "
    "scrapes 9095."
)


class ObservabilityWatcherRouteRegex(unittest.TestCase):
    def setUp(self):
        patterns = _check_named(ROUTE_OBJECTIVE)["required_patterns"]
        self.assertEqual(len(patterns), 1, "route objective should carry one pattern")
        self.route = re.compile(patterns[0])

    def test_matches_real_podmonitoring_reads(self):
        for command in ROUTE_POSITIVES:
            with self.subTest(command=command):
                self.assertRegex(command, self.route)

    def test_refuses_lookalikes(self):
        for command in ROUTE_NEGATIVES:
            with self.subTest(command=command):
                self.assertNotRegex(command, self.route)

    def test_known_residual_false_greens_still_match(self):
        # Documented in the task.yaml route comment: no single-line regex refuses
        # these. If one stops matching, the regex improved -- update the comment.
        for command in ROUTE_KNOWN_RESIDUALS:
            with self.subTest(command=command):
                self.assertRegex(command, self.route)


class ObservabilityWatcherPolarity(unittest.TestCase):
    def setUp(self):
        check = _check_named(POLARITY_OBJECTIVE)
        self.required = check.get("required_phrases", [])
        self.forbidden_patterns = check.get("forbidden_patterns", [])
        self.assertTrue(
            self.required, "polarity objective should carry required_phrases"
        )
        self.assertTrue(
            self.forbidden_patterns,
            "polarity objective should carry forbidden_patterns",
        )

    def _affirms(self, answer: str) -> bool:
        """``ReportContainsVerifier``'s verdict for a required_phrases +
        forbidden_patterns check: the phrase is a substring of the flat
        normalization, the pattern is ``re.search``ed against the line-preserving
        one."""
        text = _normalize(answer)
        lines = _normalize_lines(answer)
        missing = any(_normalize(p) not in text for p in self.required)
        present = any(re.search(p, lines) for p in self.forbidden_patterns)
        return not missing and not present

    def test_correct_answers_affirm(self):
        for answer in POLARITY_CORRECT:
            with self.subTest(answer=answer[:60]):
                self.assertTrue(
                    self._affirms(answer), "a scraped answer did not affirm"
                )

    def test_negative_answers_do_not(self):
        for answer in POLARITY_INCORRECT:
            with self.subTest(answer=answer[:60]):
                self.assertFalse(
                    self._affirms(answer), "a not-scraped answer affirmed"
                )

    def test_known_residual_false_green_still_affirms(self):
        # Documented in the task.yaml polarity comment: a negative that ignores the
        # verdict-line instruction and carries the "scraped: yes" substring inline
        # has no substring fix that leaves the compliant token alone.
        self.assertTrue(self._affirms(POLARITY_KNOWN_FALSE_GREEN))


class NormalizeMatchesTheVerifier(unittest.TestCase):
    def test_normalize_matches_the_verifier(self):
        try:
            from kube_agents_bench.verifiers import (
                _normalize as real_normalize,
                _normalize_lines as real_normalize_lines,
            )
        except ImportError:
            self.skipTest("bench package not importable; local copies are the reference")
        samples = (
            POLARITY_CORRECT
            + POLARITY_INCORRECT
            + [
                POLARITY_KNOWN_FALSE_GREEN,
                " boundary space ",
                "MixED **Case** `code`",
                "typographic’s apostrophe",
                "first line\nsecond not scraped line",
            ]
        )
        for text in samples:
            with self.subTest(text=text[:40]):
                self.assertEqual(_normalize(text), real_normalize(text))
                self.assertEqual(_normalize_lines(text), real_normalize_lines(text))


if __name__ == "__main__":
    unittest.main()
