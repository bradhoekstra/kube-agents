"""Hardening invariants for workflows triggered by `pull_request_target`.

`pull_request_target` runs in the base repository's context with repository
secrets and a writable token scope. Checkout safety and write-permission caps
are covered by `test_B4_no_pull_request_target_workflow_checks_out_the_pull_request`
in `tests/conformance/test_B_write_path.py`. This suite holds the remaining
workflow-level invariants that neither `test_workflow_pins_and_fork_guards.py`
(which skips `GITHUB_TOKEN`-only workflows) nor `test_merge_group_triggers.py`
(which covers required merge-queue checks only) reaches:

  - Only the four allowlisted workflows declare `pull_request_target`, matching
    the per-workflow repository Actions event rules configured on GitHub.
  - Every `pull_request_target` workflow sets top-level `permissions: {}` so
    each job declares only the scopes it uses.
  - Every job in a `pull_request_target` workflow carries
    `github.repository == 'gke-labs/kube-agents'` in `if:` except
    `merge-gate.yml`'s `gate` job, which runs only on `merge_group` where a
    skipped job would satisfy a required check.
  - Every job in a `pull_request_target` workflow sets an explicit, bounded
    `timeout-minutes` so a stalled HTTP call (`urllib.request.urlopen` has no
    default socket timeout) cannot hang for GitHub's 360-minute default or
    wedge a `cancel-in-progress: false` concurrency group.
"""

from __future__ import annotations

import pathlib
import unittest

from tests.test_merge_group_triggers import _timeout_problem
from tests.test_workflow_pins_and_fork_guards import WORKFLOW_GLOBS, _load, _triggers

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

_PULL_REQUEST_TARGET = "pull_request_target"
_REPO_GUARD = "github.repository == 'gke-labs/kube-agents'"

_ALLOWED_WORKFLOWS = (
    "auto-enqueue.yml",
    "hold-unresolved-threads.yml",
    "merge-gate.yml",
    "risk_classify.yml",
)

# `merge-gate.yml`'s `gate` job runs only on `merge_group`; skipping a required
# check reports success on a queue commit, and forks have no merge queue.
_MERGE_GROUP_ONLY_JOBS = {
    ("merge-gate.yml", "gate"): "github.event_name == 'merge_group'",
}


def _pull_request_target_workflows() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for pattern in WORKFLOW_GLOBS:
        for path in sorted(_WORKFLOWS.glob(pattern)):
            _text, doc = _load(path)
            if doc is not None and _PULL_REQUEST_TARGET in _triggers(doc):
                found[path.name] = doc
    return found


class PullRequestTargetWorkflowTest(unittest.TestCase):
    def test_only_allowlisted_workflows_declare_pull_request_target(self) -> None:
        self.assertEqual(
            list(_ALLOWED_WORKFLOWS),
            sorted(_pull_request_target_workflows()),
            "unexpected workflow declares pull_request_target; update repository "
            "Actions event policies before adding a new pull_request_target workflow",
        )

    def test_every_pull_request_target_workflow_clears_top_level_permissions(self) -> None:
        for name, doc in _pull_request_target_workflows().items():
            with self.subTest(workflow=name):
                self.assertEqual(
                    {},
                    doc.get("permissions"),
                    f"{name} must set top-level `permissions: {{}}` and scope grants per job",
                )

    def test_every_pull_request_target_job_carries_the_repository_guard(self) -> None:
        for name, doc in _pull_request_target_workflows().items():
            for job_id, job in (doc.get("jobs") or {}).items():
                with self.subTest(workflow=name, job=job_id):
                    condition = str((job or {}).get("if", ""))
                    expected_merge_group_guard = _MERGE_GROUP_ONLY_JOBS.get((name, job_id))
                    if expected_merge_group_guard is not None:
                        self.assertEqual(expected_merge_group_guard, condition.strip())
                    else:
                        self.assertIn(
                            _REPO_GUARD,
                            condition,
                            f"{name} job {job_id!r} runs in a pull_request_target workflow "
                            f"without `{_REPO_GUARD}`",
                        )

    def test_every_pull_request_target_job_sets_a_bounded_timeout(self) -> None:
        for name, doc in _pull_request_target_workflows().items():
            for job_id, job in (doc.get("jobs") or {}).items():
                with self.subTest(workflow=name, job=job_id):
                    self.assertIsNone(
                        _timeout_problem((job or {}).get("timeout-minutes")),
                        f"{name} job {job_id!r}",
                    )


if __name__ == "__main__":
    unittest.main()
