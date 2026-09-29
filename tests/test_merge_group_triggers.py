"""Every workflow behind a required check on `main` declares `merge_group`.

A GitHub merge queue runs the required checks on its own temporary branch and
waits for each one to report there. A required check whose workflow has no
`merge_group` trigger never reports, and the queue holds every group until the
status-check timeout. Nothing else notices the trigger going missing: a
workflow edited without it still runs on every pull request exactly as before.
This roster is the ten required contexts as of the Tide-to-merge-queue
migration (gke-labs/kube-agents#1363), less `cla/google`, which is an external
app's commit status rather than a workflow.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

_MERGE_GROUP = "merge_group"
_REQUIRED_CHECK_WORKFLOWS = (
    "actionlint.yml",
    "docker-build.yml",
    "docs-check.yml",
    "k8s-operator-test.yml",
    "prettier.yml",
    "python-tests.yml",
    "validate-pr-title.yml",
    "validate.yml",
)


def _triggers(path: pathlib.Path) -> set[str]:
    doc = yaml.safe_load(path.read_text())
    # PyYAML reads an unquoted `on:` key as the boolean True (YAML 1.1).
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(item) for item in on}
    return {str(key) for key in on}


_PULL_REQUEST = "pull_request"
_RELEASE_LINE_GLOB = "release/**"


def _on(path: pathlib.Path) -> dict:
    doc = yaml.safe_load(path.read_text())
    return doc.get("on", doc.get(True))


class MergeGroupTriggerTest(unittest.TestCase):
    def test_required_check_workflows_run_on_merge_group(self) -> None:
        for name in _REQUIRED_CHECK_WORKFLOWS:
            with self.subTest(workflow=name):
                self.assertIn(_MERGE_GROUP, _triggers(_WORKFLOWS / name))

    def test_required_check_workflows_run_for_pull_requests_against_a_release_line(self) -> None:
        """A backport targets `release/<X.Y>` and needs the same ten contexts.

        Branch protection on a release line requires the contexts `main` does,
        and Tide reads that protection. A workflow whose `pull_request` trigger
        is filtered to `main` never posts on such a pull request, so the context
        stays pending and nothing can merge onto the line. A filter is allowed;
        one that omits the release lines is not.
        """
        for name in _REQUIRED_CHECK_WORKFLOWS:
            with self.subTest(workflow=name):
                on = _on(_WORKFLOWS / name)
                self.assertIn(_PULL_REQUEST, on, f"{name} does not run on pull_request")
                branches = (on[_PULL_REQUEST] or {}).get("branches")
                if branches is not None:
                    self.assertIn(_RELEASE_LINE_GLOB, branches, f"{name} filters pull_request to {branches}")

    def test_merge_group_stays_on_main(self) -> None:
        """The merge queue is `main`'s; a release line merges through Tide alone."""
        for name in _REQUIRED_CHECK_WORKFLOWS:
            with self.subTest(workflow=name):
                on = _on(_WORKFLOWS / name)
                self.assertEqual((on[_MERGE_GROUP] or {}).get("branches"), ["main"])


if __name__ == "__main__":
    unittest.main()
