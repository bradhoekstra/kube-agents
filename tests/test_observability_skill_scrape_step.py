"""The observability skill reads the scrape config off the PodMonitoring, not the Deployment.

The kube-agents operator renders no Prometheus scrape annotations on the gateway
Deployment; the managed collector scrapes through the chart's two `PodMonitoring`
resources instead. Step 1 of the skill's Metrics workflow used to send the agent
to the Deployment looking for those annotations, so on a scraped install it
reported "not scraped" (gke-labs/kube-agents#2141). The step now names the
`PodMonitoring` resources, their ports and the proving series.

The bench case `observability-watcher-scrape-state` grades the agent's behaviour,
and on the live-test install the worker read the PodMonitoring without the skill's
help, so the case does not red against the old step. Nothing executes the skill
prose, so this test pins the phrases the fix is made of and the invariant that
guards it: every `kubectl` command step 1 issues reads the `PodMonitoring` (or the
`gmp-system` collector namespace), so a revert that points a command at the
Deployment or any other resource fails here, whatever nouns the surrounding prose
uses. Four denylist patterns back that up for a revert written as prose rather than
a command, but each keys on an annotation word, so they catch an annotation-worded
revert, not one phrased in entirely other words -- the kubectl allowlist, not the
denylist, is what holds each command's resource read to the PodMonitoring. Per
`.agents/rules/eval_driven_development.md` this stand-in does not replace the case;
it is the guard the case cannot be, because the agent's behaviour was already
correct.

Run:
  python3 -m unittest discover -s tests -p 'test_observability_skill_scrape_step.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OBSERVABILITY_SKILL = REPO_ROOT / "agents/platform/skills/kube-agents-observability/SKILL.md"

# The Metrics workflow's first step, by its exact heading. Three other "### 1."
# headings in the file (Logging, Traces, Agent Status) make the full string the
# only unambiguous anchor; the step ends at the next "### 2." heading.
STEP_HEADING = "### 1. Verify Cloud Monitoring & Prometheus State"
NEXT_HEADING = "### 2. Inspect CPU and Memory Metrics"

# The fix, in the skill's own words: read the scrape config off the
# PodMonitoring, not off the Deployment the operator leaves unannotated.
READ_OFF_PODMONITORING = (
    "Read the scrape configuration off the `PodMonitoring` resources, not off Deployment annotations"
)
# The PodMonitoring read the step spells out, and the listener and proving series
# that make the watcher's answer.
GATEWAY_READ = "kubectl get podmonitoring <name>-gateway-monitoring"
WATCHER_PORT = "9095"
WATCHER_SERIES = 'up{job="<name>-gateway-monitoring"}'
WATCHER_UP = "k8s_event_watcher_cluster_up"
# The #2141 bug, reverted: the Deployment-annotation read must not come back into
# the step. The operator renders no such annotation, so any `kubectl get`/
# `describe` of a deployment in step 1 is the regression, in any spelling of the
# noun (`deploy`, `deployment`, `deployments`, `deployments.apps`) and with any
# flags between `kubectl`, the verb and the noun (`kubectl -n ns get deployment`,
# `kubectl get -o yaml deployment`): the gaps are `[^\n]*`, not whitespace, so the
# match describes "a deployment read on one line" rather than one command shape.
# Held to one line (`[^\n]*`, never `.`), so the prose that names the Deployment
# only to forbid it ("not off Deployment annotations", "whatever the Deployment
# says") carries no `kubectl` on its line and does not trip.
DEPLOYMENT_READ = re.compile(r"(?i)kubectl\b[^\n]*\b(get|describe)\b[^\n]*\bdeploy(ments?)?(\.apps)?\b")
# The same bug by its own noun, not the resource it read: the scrape opt-in
# annotation. A revert that reads it off the pod template instead of the
# Deployment is the same regression, and no `deploy` pattern would catch it.
PROM_SCRAPE_ANNOTATION = re.compile(r"(?i)prometheus\.io/scrape")
# The same bug in prose, naming neither the literal annotation key nor a
# `kubectl ... deploy`: a step that tells the agent to read "scrape annotations"
# or "annotations for Prometheus scraping" (off any resource) is the regression
# the two patterns above miss. The current step names Deployment annotations only
# to forbid them ("not off Deployment annotations"), which is "deployment", not
# "scrape", annotations and does not trip this.
SCRAPE_ANNOTATION_PROSE = re.compile(
    r"(?i)annotations?\s+for\s+prometheus\s+scrap|scrap\w*\s+annotations?"
)
# The three patterns above are a denylist -- each keyed to one spelling of the
# revert (a deployment read, the literal annotation key, the prose "scrape
# annotations"). A revert spelled in none of them ("read the gateway pod's
# Prometheus annotations", a jsonpath over `.annotations`) slips all three. This
# last check widens the net to the annotation word itself: step 1 names annotations
# exactly once, in the sentence that forbids reading them (READ_OFF_PODMONITORING);
# strip that sentence and any surviving `annotat` is an annotation read the three
# patterns above may not spell out. It is not the "in any words" backstop it reads
# like -- it keys on the substring `annotat`, so a revert phrased without that word
# slips it too; the step-1 kubectl allowlist below is what holds each command's
# resource read to the PodMonitoring, within the residuals documented there. The
# cost of this one is that a future non-revert mention of the word would also trip
# it and have to update the guard; for a step whose only correct mention of
# annotations is to forbid them, that is the right default.
ANY_ANNOTATION = re.compile(r"(?i)annotat")
# The positive guard the resource-read invariant actually rests on, where the
# denylist above (every pattern keyed on an annotation word) cannot. Collect every
# `kubectl` command step 1 issues from its fenced code blocks, and require each to
# name the PodMonitoring read -- the `podmonitoring` kind, or the `gmp-system`
# namespace the managed collector's pods live in. The check splits each command at
# shell separators (`;`, `|`, `&`, `>`, `#`, `$(`) and holds the segment that runs
# the read to the allowlist, so a revert cannot borrow the word from a chained
# command (`... && kubectl get podmonitoring -A`), a downstream filter (`| grep
# podmonitoring`), an output filename (`> podmonitoring.yaml`), a comment or a
# substitution: each puts `podmonitoring` in a segment that does not run the read,
# while the read segment names the Deployment. Step 1's three reads each name the
# kind or namespace in the read segment itself. Three residuals stand, each the
# price of a substring check over shell grammar rather than a parse of it:
#   1. a command that names `podmonitoring`/`gmp-system` in a label-selector or
#      jsonpath value in the same segment as a read of another resource still
#      passes -- the check cannot tell the resource from an argument;
#   2. a revert that issues no kubectl command and uses no annotation word -- a
#      step that runs no command to read the wrong resource has not reverted what
#      the agent does, so there is nothing here to catch;
#   3. a command written in an inline span rather than a fenced block is not
#      collected; step 1's reads are all fenced, and collecting from inline spans
#      would read a prose `kubectl` mention as a command and false-red the step.
FENCED_CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
# `<name>`/`<namespace>` placeholders, neutralized before the split so their angle
# brackets are not read as a `>` redirect and fragment a real command short.
PLACEHOLDER = re.compile(r"<[\w-]+>")
# The shell separators a one-line command can hide a second resource behind.
COMMAND_SEPARATORS = re.compile(r"[;|&>#]|\$\(")
STEP_ONE_KUBECTL_ALLOW = re.compile(r"(?i)\bpodmonitoring\b|\bgmp-system\b")


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) < 1000:
        raise AssertionError(f"{path} read back suspiciously short ({len(text)} chars)")
    return text


def _metrics_step_one(skill: str) -> str:
    start = skill.index(STEP_HEADING)
    end = skill.index(NEXT_HEADING, start)
    return skill[start:end]


def _step_one_kubectl_commands(step: str) -> list[str]:
    """Every kubectl command step 1 issues, read from its fenced code blocks.
    Step 1's three reads all sit in ```bash blocks; collecting a line that starts
    with `kubectl` from the blocks, rather than every `kubectl` substring on any
    line, is what keeps a prose mention -- an inline `kubectl` span in a sentence
    -- from reading as a command to guard. The inverse is a documented residual:
    a command written in an inline span rather than a fenced block is not
    collected."""
    commands = []
    for block in FENCED_CODE_BLOCK.findall(step):
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("kubectl"):
                commands.append(line)
    return commands


def _command_reads_podmonitoring(command: str) -> bool:
    """A kubectl command names the PodMonitoring read when every one of its
    `kubectl` segments does. Split the command at shell separators (`;`, `|`, `&`,
    `>`, `#`, `$(`) so a chained or redirected revert -- `kubectl get deployment
    ... && kubectl get podmonitoring ...`, `... | grep podmonitoring`, `... >
    podmonitoring.yaml` -- is judged on the segment that runs the read, not on the
    word a later segment borrows. `<name>`/`<namespace>` placeholders are
    neutralized first so their angle brackets are not read as a redirect. The
    residual: a segment that names `podmonitoring` in a label-selector or jsonpath
    value while reading another resource still passes -- a substring check cannot
    tell the resource from an argument."""
    neutralized = PLACEHOLDER.sub("X", command)
    segments = COMMAND_SEPARATORS.split(neutralized)
    kubectl_segments = [s for s in segments if "kubectl" in s]
    return bool(kubectl_segments) and all(
        STEP_ONE_KUBECTL_ALLOW.search(s) for s in kubectl_segments
    )


class ObservabilitySkillReadsThePodMonitoring(unittest.TestCase):
    def test_step_one_reads_the_scrape_off_the_podmonitoring(self):
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        for phrase in (READ_OFF_PODMONITORING, GATEWAY_READ, WATCHER_PORT, WATCHER_SERIES, WATCHER_UP):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, step, f"the observability skill's Prometheus step lost {phrase!r}")

    def test_step_one_does_not_send_the_agent_to_the_deployment_annotations(self):
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        self.assertNotRegex(
            step,
            DEPLOYMENT_READ,
            "step 1 sends the agent back to the Deployment for scrape annotations (the #2141 regression)",
        )

    def test_step_one_does_not_read_scrape_opt_in_annotations(self):
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        self.assertNotRegex(
            step,
            PROM_SCRAPE_ANNOTATION,
            "step 1 tells the agent to read the prometheus.io/scrape annotation (the #2141 regression, by any resource)",
        )

    def test_step_one_does_not_describe_scrape_annotations_in_prose(self):
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        self.assertNotRegex(
            step,
            SCRAPE_ANNOTATION_PROSE,
            "step 1 describes reading scrape annotations in prose (the #2141 regression, without the literal key or a deployment read)",
        )

    def test_step_one_mentions_annotations_only_to_forbid_reading_them(self):
        # The invariant behind the three patterns above: step 1 names annotations
        # only in the sentence that forbids reading them. Strip that sentence and any
        # surviving "annotat" is an annotation read the denylist patterns may not
        # enumerate -- the #2141 regression wherever the word `annotation` appears.
        # This keys on that word, not on any wording of the revert; the kubectl
        # allowlist test below is the guard that holds each command's read to the
        # PodMonitoring.
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        residue = step.replace(READ_OFF_PODMONITORING, "")
        self.assertNotRegex(
            residue,
            ANY_ANNOTATION,
            "step 1 mentions annotations outside the sentence that forbids reading them (the #2141 regression, by the annotation word)",
        )

    def test_step_one_kubectl_commands_all_read_the_podmonitoring(self):
        # The positive guard the resource-read invariant rests on: every kubectl
        # command step 1 issues names the PodMonitoring read (the `podmonitoring`
        # kind or the `gmp-system` collector namespace) in the segment that runs
        # it. A revert that points a command at the Deployment or any other
        # resource names neither and fails here, whatever nouns the surrounding
        # prose uses and wherever on the line it borrows the word from (see the
        # separator-splitting guard the two injection tests below exercise).
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        commands = _step_one_kubectl_commands(step)
        self.assertTrue(commands, "step 1 issues no kubectl command to guard")
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(
                    _command_reads_podmonitoring(command),
                    f"step 1 runs a kubectl command that names neither the "
                    f"PodMonitoring kind nor the gmp-system namespace in the "
                    f"segment that runs the read (the #2141 regression: a read "
                    f"pointed at the Deployment or another resource): {command!r}",
                )

    def test_step_one_guard_reds_a_wrong_resource_read_however_it_borrows_the_word(self):
        # The structural check, exercised against reverts that name `podmonitoring`
        # somewhere on the command line while reading another resource. Each is
        # rejected: the guard splits at shell separators and judges the segment
        # that runs the read, so the word cannot be borrowed from a chained
        # command, a downstream filter, an output filename, a comment or a
        # substitution. The old `kubectl[^\n`]*` collector took the whole line and
        # admitted all six (the finding this replaces).
        reverts = (
            "kubectl get deployment <name>-gateway && kubectl get podmonitoring -A",
            "kubectl get deployment <name>-gateway ; echo podmonitoring",
            "kubectl get deployment <name>-gateway | grep podmonitoring",
            "kubectl get deployment <name>-gateway -o yaml > podmonitoring.yaml",
            "kubectl get deployment <name>-gateway  # read the podmonitoring instead",
            "kubectl get deployment <name>-gateway $(echo podmonitoring)",
        )
        for command in reverts:
            with self.subTest(command=command):
                self.assertFalse(
                    _command_reads_podmonitoring(command),
                    f"a wrong-resource read borrowed the podmonitoring word and passed: {command!r}",
                )

    def test_step_one_guard_admits_the_real_podmonitoring_reads(self):
        reads = (
            "kubectl get pods -n gmp-system",
            "kubectl get podmonitoring <name>-gateway-monitoring -n kubeagents-system -o yaml",
            "kubectl get podmonitoring <name>-credential-proxy-monitoring -n kubeagents-system -o yaml",
            # A podmonitoring read whose output goes to a file named for something
            # else still passes: the segment that runs the read names the kind.
            "kubectl get podmonitoring <name>-gateway-monitoring -o yaml > /tmp/out.yaml",
        )
        for command in reads:
            with self.subTest(command=command):
                self.assertTrue(
                    _command_reads_podmonitoring(command),
                    f"a real podmonitoring read was rejected: {command!r}",
                )

    def test_step_one_collector_ignores_prose_kubectl_mentions(self):
        # The collector reads commands from fenced code blocks, so a `kubectl`
        # written in prose (an inline span in a sentence) is not mistaken for a
        # command to guard. Were it collected, a sentence that mentions `kubectl`
        # without naming the PodMonitoring would false-red the real step.
        step = (
            "### 1. Verify Cloud Monitoring & Prometheus State\n"
            "- Run `kubectl` against the collector, not the Deployment.\n"
            "  ```bash\n"
            "  kubectl get podmonitoring <name>-gateway-monitoring -n kubeagents-system -o yaml\n"
            "  ```\n"
            "### 2. Inspect CPU and Memory Metrics\n"
        )
        commands = _step_one_kubectl_commands(_metrics_step_one(step))
        self.assertEqual(
            commands,
            ["kubectl get podmonitoring <name>-gateway-monitoring -n kubeagents-system -o yaml"],
            "the collector picked up a prose kubectl mention or dropped the real command",
        )


if __name__ == "__main__":
    unittest.main()
