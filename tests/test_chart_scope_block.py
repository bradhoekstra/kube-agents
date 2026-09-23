"""The chart renders `spec.scope` as a present block, empty lists included.

docs/designs/multi-project-scope.md §7: the reconcile reads a present block
whose `projects` is empty as the declaration that drops projects, and an absent
block as no declaration at all. A chart that dropped the group when every list
was empty, the way its other optional groups are dropped, would turn "remove the
last scoped project and upgrade" into a no-op.

Run:
  python3 -m unittest discover -s tests -p 'test_chart_scope_block.py' -v
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=test-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=test-project",
]
_SCOPE_VALUES = {
    "platformAgent": {
        "scope": {
            "projects": ["payments-prod", "payments-staging"],
            "exclude": {
                "projects": ["*-sandbox"],
                "clusters": [
                    {"projectId": "payments-staging", "location": "us-central1", "clusterName": "scratch"},
                ],
            },
        }
    }
}


def _platform_agent(rendered: str) -> dict:
    for document in yaml.safe_load_all(rendered):
        if isinstance(document, dict) and document.get("kind") == "PlatformAgent":
            return document
    raise AssertionError("no PlatformAgent rendered")


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ChartScopeBlockTest(unittest.TestCase):
    def _render(self, *extra: str) -> dict:
        proc = subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra],
            capture_output=True,
            text=True,
            check=True,
        )
        return _platform_agent(proc.stdout)

    def test_the_default_render_carries_an_empty_present_block(self):
        spec = self._render()["spec"]
        self.assertIn("scope", spec, "spec.scope must be present even when every list is empty")
        self.assertEqual({"projects": [], "exclude": {"projects": [], "clusters": []}}, spec["scope"])

    def test_values_reach_the_block_verbatim(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            yaml.safe_dump(_SCOPE_VALUES, handle)
            values_file = handle.name
        try:
            spec = self._render("-f", values_file)["spec"]
        finally:
            pathlib.Path(values_file).unlink()
        self.assertEqual(_SCOPE_VALUES["platformAgent"]["scope"], spec["scope"])

    def test_an_unknown_scope_key_fails_the_render(self):
        # values.schema.json closes the object, so a misspelt key fails here
        # rather than rendering a CR that silently declares less than asked.
        proc = subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED,
             "--set", "platformAgent.scope.folders={123}"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("scope", proc.stderr)


if __name__ == "__main__":
    unittest.main()
