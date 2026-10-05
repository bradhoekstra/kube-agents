"""Pin the observability-watcher-scrape-state verifier patterns.

Two checks in that case's task.yaml decide a run and are easy to break with a
regex tweak that reads fine but shifts what matches:

* ``the-worker-read-the-podmonitoring`` -- a ``worker_commands`` route regex that
  must match a real ``kubectl get/describe podmonitoring`` however it is wrapped
  (env assignment, ``sudo``/``timeout`` with flags, ``xargs``, a loop, a path to
  the binary) and must NOT match the noun quoted inside another command, a CRD
  read, a ``can-i`` probe, or the kind glued to ``=``/``/``/``-`` in a selector
  value or an output filename. ``WorkerCommandsVerifier`` ``re.search``es each
  pattern against the raw command string, so this test does the same.

* ``the-answer-affirms-the-watcher-is-scraped`` -- a ``report_contains``
  ``any_of_phrases`` that carries the conclusion's polarity: a correct "scraped"
  answer hits one phrase, a "not scraped" answer hits none. ``ReportContainsVerifier``
  substring-matches each phrase against ``_normalize(text)`` (lowercased, Markdown
  emphasis dropped, whitespace collapsed), so this test reproduces ``_normalize``
  and cross-checks it against the real one when the bench package imports.

The patterns and phrases are read from task.yaml, not duplicated here, so the
test pins the file rather than a copy of it. Two false-greens the route regex
cannot refuse without a shell parser are asserted as known residuals, so a future
tightening that fixes them trips this test and updates the task.yaml comment with
it.

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


# A faithful copy of kube_agents_bench.verifiers._normalize, so this test runs
# without the bench package's runtime dependencies (as the sibling pattern tests
# do). test_normalize_matches_the_verifier pins it to the real one whenever the
# package does import.
_MARKDOWN_NOISE = str.maketrans({"*": None, "_": None, "`": None, "’": "'"})


def _normalize(text: str) -> str:
    stripped = text.translate(_MARKDOWN_NOISE)
    collapsed = " ".join(stripped.split())
    if stripped[:1].isspace():
        collapsed = " " + collapsed
    if stripped[-1:].isspace():
        collapsed += " "
    return collapsed.lower()


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
]

# --- polarity corpus --------------------------------------------------------

# Correct answers: the watcher IS scraped. Each must hit at least one phrase.
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
]

# Incorrect answers: the watcher is NOT scraped. Each must hit zero phrases, even
# though they name the same mechanism, port and series the other objective checks.
POLARITY_INCORRECT = [
    "No. The event watcher's metrics are not scraped: the gateway-monitoring "
    "PodMonitoring targets 9095 but up{job=...} returns nothing.",
    "The watcher is not being scraped. The gateway Deployment carries no "
    "prometheus.io/scrape annotation; port 9095 is unmonitored.",
    "The metrics at 9095 are not scraped through any PodMonitoring; "
    "gateway-monitoring does not exist on this install.",
]


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


class ObservabilityWatcherPolarityPhrases(unittest.TestCase):
    def setUp(self):
        self.phrases = _check_named(POLARITY_OBJECTIVE)["any_of_phrases"]
        self.assertTrue(self.phrases, "polarity objective should carry any_of_phrases")

    def _hits(self, answer: str) -> bool:
        text = _normalize(answer)
        return any(_normalize(p) in text for p in self.phrases)

    def test_correct_answers_affirm(self):
        for answer in POLARITY_CORRECT:
            with self.subTest(answer=answer[:60]):
                self.assertTrue(self._hits(answer), "a scraped answer hit no polarity phrase")

    def test_negative_answers_do_not(self):
        for answer in POLARITY_INCORRECT:
            with self.subTest(answer=answer[:60]):
                self.assertFalse(self._hits(answer), "a not-scraped answer hit a polarity phrase")

    def test_phrases_are_affirmative(self):
        # A negator in a phrase would let the negation match it. The affirmative
        # forms carry the polarity; "not"/"no"/"n't" must not appear.
        for phrase in self.phrases:
            with self.subTest(phrase=phrase):
                self.assertNotRegex(phrase.lower(), re.compile(r"\bno\b|\bnot\b|n't"))


class NormalizeMatchesTheVerifier(unittest.TestCase):
    def test_normalize_matches_the_verifier(self):
        try:
            from kube_agents_bench.verifiers import _normalize as real_normalize
        except ImportError:
            self.skipTest("bench package not importable; local _normalize is the reference")
        samples = (
            POLARITY_CORRECT
            + POLARITY_INCORRECT
            + _check_named(POLARITY_OBJECTIVE)["any_of_phrases"]
            + [" boundary space ", "MixED **Case** `code`", "typographic’s apostrophe"]
        )
        for text in samples:
            with self.subTest(text=text[:40]):
                self.assertEqual(_normalize(text), real_normalize(text))


if __name__ == "__main__":
    unittest.main()
