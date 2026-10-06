"""Pin the observability-watcher-scrape-state verifier patterns.

Two checks in that case's task.yaml decide a run and are easy to break with a
regex tweak that reads fine but shifts what matches:

* ``the-worker-read-the-podmonitoring`` -- a ``worker_commands`` route regex that
  must match a real ``kubectl get/describe podmonitoring`` however it is wrapped
  (env assignment, ``sudo``/``timeout`` with flags, ``xargs``, a loop, a path to
  the binary, a ``sh -c '…'`` wrapper, a quoted noun, a read that ends its own
  segment before ``;``/``|``/``)``/``&``) and must NOT match the noun quoted inside
  another command, a CRD read, a ``can-i`` probe, or the kind glued to
  ``=``/``/``/``-`` in a selector value or an output filename.
  ``WorkerCommandsVerifier`` ``re.search``es each pattern against the raw command
  string, so this test does the same.

* ``the-answer-affirms-the-watcher-is-scraped`` -- a ``report_contains`` whose
  ``any_of_patterns`` tie a scrape verb to a watcher anchor (the affirmative) and
  whose ``forbidden_patterns`` red a negated conclusion an affirmative substring
  would still contain. A correct "scraped" answer hits an any_of and no forbidden;
  a "not scraped" answer -- including one that negates the subject ("nothing is
  scraping it") rather than the verb, or buries the affirmative in a hypothetical
  ("whether GMP is scraping it: it is not") -- hits a forbidden or no any_of.
  ``ReportContainsVerifier`` ``re.search``es ``any_of_patterns`` against
  ``_normalize(text)`` (flat) and ``forbidden_patterns`` against
  ``_normalize_lines(text)`` (per line), so this test reproduces both and
  cross-checks them against the real verifier when the bench package imports.

The patterns are read from task.yaml, not duplicated here, so the test pins the
file rather than a copy of it. The residuals that neither slot can refuse without
a shell or English parser are asserted as known false-greens/false-reds, so a
future tightening that fixes one trips this test and updates the task.yaml comment
with it.

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

# Lookalikes the regex must refuse: the command quoted inside another, the kind
# glued to a separator in a filename or selector value, a CRD read, a can-i
# probe, a deployment read, and a plain cat of a file.
ROUTE_NEGATIVES = [
    'grep "kubectl get podmonitoring" notes.txt',
    "echo 'kubectl get podmonitoring platform-agent-gateway-monitoring'",
    "kubectl get pods -n kubeagents-system -o yaml > /tmp/podmonitoring.yaml",
    "kubectl get deployment platform-agent-gateway -o yaml > gateway-podmonitoring-check.yaml",
    "kubectl auth can-i get podmonitoring -n kubeagents-system",
    "kubectl get crd podmonitorings.monitoring.googleapis.com",
    "kubectl get customresourcedefinition podmonitorings.monitoring.googleapis.com",
    "kubectl get events -n kubeagents-system --field-selector involvedObject.kind=PodMonitoring",
    'echo " kubectl get podmonitoring"',
    "kubectl get deployment platform-agent-gateway -n kubeagents-system -o yaml",
    "cat podmonitoring.yaml",
]

# False-greens with no single-line-command regex fix, documented in the task.yaml
# route comment and accepted. Asserted so a future tightening that refuses one
# fails here and the comment is updated with it.
ROUTE_KNOWN_RESIDUALS = [
    "echo kubectl get podmonitoring",
    'git commit -m "note; kubectl get podmonitoring is the check"',
    # A `sh -c '…'` wrapper quoted inside another command: admitting the wrapper
    # as a command position also admits its mention inside a quoted string.
    "echo \"sh -c 'kubectl get podmonitoring'\"",
]

# --- polarity corpus --------------------------------------------------------

# Correct answers: the watcher IS scraped. Each must hit an any_of pattern and no
# forbidden pattern. The last two are the adverb-split and non-pronoun-object
# shapes a contiguous substring missed.
POLARITY_CORRECT = [
    'Yes. The watcher metrics are scraped through the `platform-agent-gateway-monitoring` '
    'PodMonitoring on port 9095; up{job="platform-agent-gateway-monitoring"} reads 1.',
    "The watcher's metrics are being scraped every 30s by GKE Managed Prometheus via the "
    "gateway-monitoring PodMonitoring (9095). Note the credential-proxy metrics are not "
    "being scraped yet.",
    "Yes -- port 9095 is scraped through the gateway-monitoring PodMonitoring; "
    "k8s_event_watcher_cluster_up confirms it.",
    "GKE Managed Prometheus scrapes the watcher's 9095 endpoint through the "
    "gateway-monitoring PodMonitoring.",
    "GKE Managed Prometheus is successfully scraping port 9095 through the "
    "gateway-monitoring PodMonitoring.",
    "The watcher's 9095 metrics are currently being scraped every 30s.",
]

# Incorrect answers: the watcher is NOT scraped. Each must miss every any_of or
# hit a forbidden pattern, even though they name the same mechanism, port and
# series the other objective checks -- including answers that negate the subject
# rather than the verb, mix a scraped proxy with the unscraped watcher, or bury
# the affirmative verb in a hypothetical.
POLARITY_INCORRECT = [
    "No. The event watcher's metrics are not scraped: the gateway-monitoring "
    "PodMonitoring targets 9095 but up{job=...} returns nothing.",
    "The watcher is not being scraped. The gateway Deployment carries no "
    "prometheus.io/scrape annotation; port 9095 is unmonitored.",
    "The metrics at 9095 are not scraped through any PodMonitoring; "
    "gateway-monitoring does not exist on this install.",
    "Nothing is scraping the watcher's 9095 endpoint.",
    "None of the k8s_event_watcher_* series are scraped.",
    "No collector scrapes the gateway-monitoring target.",
    "The credential-proxy PodMonitoring is scraped on 8766; the watcher is not scraped.",
    "I checked whether GMP is scraping it: it is not.",
]

# Polarity residuals, documented in the task.yaml comment and accepted. A
# copula-less watcher negation beside an anchored affirmative false-greens; an
# affirmative whose only scrape clause names no anchor false-reds. Asserted so a
# future tightening that fixes one trips this test and updates the comment.
POLARITY_KNOWN_FALSE_GREEN = "GMP scrapes port 9095. The watcher? not scraped."
POLARITY_KNOWN_FALSE_RED = (
    "The gateway-monitoring PodMonitoring targets 9095. Yes, it is scraped."
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
        self.any_of = [re.compile(p) for p in check.get("any_of_patterns", [])]
        self.forbidden = [re.compile(p) for p in check.get("forbidden_patterns", [])]
        self.assertTrue(self.any_of, "polarity objective should carry any_of_patterns")
        self.assertTrue(
            self.forbidden, "polarity objective should carry forbidden_patterns"
        )

    def _affirms(self, answer: str) -> bool:
        """``ReportContainsVerifier``'s verdict for an any_of + forbidden check."""
        flat = _normalize(answer)
        lines = _normalize_lines(answer)
        any_hit = any(p.search(flat) for p in self.any_of)
        forbidden_hit = any(p.search(lines) for p in self.forbidden)
        return any_hit and not forbidden_hit

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
        # Documented in the task.yaml polarity comment: a copula-less watcher
        # negation beside an anchored affirmative has no single-regex fix that
        # leaves the legitimate "credential-proxy is not scraped" mention alone.
        self.assertTrue(self._affirms(POLARITY_KNOWN_FALSE_GREEN))

    def test_known_residual_false_red_still_fails(self):
        # Documented in the task.yaml polarity comment: an affirmative whose only
        # scrape clause names no anchor fails closed (the safe direction).
        self.assertFalse(self._affirms(POLARITY_KNOWN_FALSE_RED))


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
                POLARITY_KNOWN_FALSE_RED,
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
