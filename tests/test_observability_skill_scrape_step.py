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
prose, so this test pins the phrases the fix is made of: a rewording that keeps
the fix keeps them; one that sends the agent back to the Deployment annotations
fails here before it fails a nightly run a day later. Per
`.agents/rules/eval_driven_development.md` this stand-in does not replace the
case; it is the guard the case cannot be, because the agent's behaviour was
already correct.

Run:
  python3 -m unittest discover -s tests -p 'test_observability_skill_scrape_step.py' -v
"""

from __future__ import annotations

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
# the step. The operator renders no such annotation, so any form of this command
# in step 1 is the regression.
DEPLOYMENT_READ = "kubectl get deployment"


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) < 1000:
        raise AssertionError(f"{path} read back suspiciously short ({len(text)} chars)")
    return text


def _metrics_step_one(skill: str) -> str:
    start = skill.index(STEP_HEADING)
    end = skill.index(NEXT_HEADING, start)
    return skill[start:end]


class ObservabilitySkillReadsThePodMonitoring(unittest.TestCase):
    def test_step_one_reads_the_scrape_off_the_podmonitoring(self):
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        for phrase in (READ_OFF_PODMONITORING, GATEWAY_READ, WATCHER_PORT, WATCHER_SERIES, WATCHER_UP):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, step, f"the observability skill's Prometheus step lost {phrase!r}")

    def test_step_one_does_not_send_the_agent_to_the_deployment_annotations(self):
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        self.assertNotIn(
            DEPLOYMENT_READ,
            step,
            "step 1 sends the agent back to the Deployment for scrape annotations (the #2141 regression)",
        )


if __name__ == "__main__":
    unittest.main()
