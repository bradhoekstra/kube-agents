"""fleet_scope_targets: the declared scope's resolved projects for an audit.

Run: python3 -m unittest discover -s agents/platform/scripts -p 'test_fleet_scope_targets.py' -v
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fleet_scope_targets as fst  # noqa: E402

DECLARED = {"projects": ["payments-prod"], "folders": [], "organizations": [], "sharedVpcHosts": [], "metricsScopes": [], "exclude": {}}


def _snapshot(projects, declared=DECLARED, resolved_at="2026-10-09T12:00:00Z", present=None, readable=None):
    snapshot = {"resolvedAt": resolved_at, "declared": declared, "maxProjects": 100, "projects": projects}
    if present is not None:
        snapshot["present"] = present
    if readable is not None:
        snapshot["readable"] = readable
    return snapshot


class DeclaredScopeTargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)

    def _write(self, snapshot) -> None:
        (self.home / fst.SNAPSHOT_FILE).write_text(json.dumps(snapshot), encoding="utf-8")

    def test_no_snapshot_is_none_so_the_caller_lists_as_before(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fst.SCOPE_FILE_ENV, None)
            self.assertIsNone(fst.declared_scope_targets(self.home))

    def test_no_snapshot_but_a_render_with_a_block_is_the_host_alone_until_the_first_tick(self):
        # A fresh install has the operator's render from the first second and
        # no snapshot until the reconcile's first tick; the audits must not
        # list every visible project in that window.
        render = self.home / "scope.json"
        render.write_text(json.dumps({"present": True, "projects": ["payments-prod"], "exclude": {"projects": [], "clusters": []}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(render), "GCP_PROJECT_ID": "ops-mgmt"}):
            targets = fst.declared_scope_targets(self.home)
            self.assertEqual((targets.projects, targets.unread, targets.resolved_at), (("ops-mgmt",), (), None))
            self.assertEqual(targets.collector_args(), "--scope-projects ops-mgmt")
            self.assertIn("until the next reconcile", targets.note)
        render.write_text(json.dumps({"present": False}), encoding="utf-8")
        with mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(render), "GCP_PROJECT_ID": "ops-mgmt"}):
            self.assertIsNone(fst.declared_scope_targets(self.home))

    def test_the_boundary_key_decides_when_the_reconcile_wrote_it(self):
        # A never-declared install on an unreadable tick: present false, readable
        # false, empty lists, boundary false. Nothing is carried; no scope.
        empty = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"exclude": {"projects": []}}
        snap = _snapshot([{"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"}], declared=empty, present=False, readable=False)
        snap["boundary"] = False
        self._write(snap)
        self.assertIsNone(fst.declared_scope_targets(self.home))
        # The stock install on the same tick: the reconcile carried its boundary.
        snap["boundary"] = True
        self._write(snap)
        self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt",))

    def test_a_file_that_is_not_a_snapshot_is_none(self):
        (self.home / fst.SNAPSHOT_FILE).write_text("<not json>", encoding="utf-8")
        self.assertIsNone(fst.declared_scope_targets(self.home))
        (self.home / fst.SNAPSHOT_FILE).write_text(json.dumps({"projects": "no"}), encoding="utf-8")
        self.assertIsNone(fst.declared_scope_targets(self.home))

    def test_an_install_that_declares_no_scope_is_none(self):
        # No scope block at all: no boundary was drawn, so the audit keeps the
        # listing it had. The reconcile records that as present: false; a
        # snapshot that predates the flag is read by its empty lists.
        empty = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"exclude": {"projects": ["*-sandbox"]}}
        self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=empty, present=False))
        self.assertIsNone(fst.declared_scope_targets(self.home))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fst.SCOPE_FILE_ENV, None)
            self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=empty))
            self.assertIsNone(fst.declared_scope_targets(self.home))

    def test_a_present_block_with_empty_lists_is_the_host_only_boundary(self):
        # spec.scope: {projects: [], exclude: {projects: ["*-sandbox"]}} bounds
        # discovery to the management project; the audits follow it rather
        # than listing every project the identity can see.
        empty = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"exclude": {"projects": ["*-sandbox"]}}
        self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=empty, present=True))
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual(targets.projects, ("ops-mgmt",))
        self.assertEqual(targets.collector_args(), "--scope-projects ops-mgmt")

    def test_a_declared_scope_yields_the_ok_projects_in_snapshot_order(self):
        self._write(_snapshot([
            {"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"},
            {"id": "payments-prod", "via": ["explicit"], "outcome": "ok", "state": "in-scope"},
            {"id": "payments-staging", "via": ["explicit"], "outcome": "denied", "state": "in-scope"},
            {"id": "legacy-api", "via": ["folders/1"], "outcome": "api-disabled", "state": "in-scope"},
            {"id": "payments-legacy", "via": [], "outcome": "ok", "state": "retiring"},
        ]))
        targets = fst.declared_scope_targets(self.home)
        self.assertIsNotNone(targets)
        # api-disabled is swept (counted empty by the GKE collectors, read by the
        # Compute and networking ones); denied is a declared project unread.
        self.assertEqual(targets.projects, ("ops-mgmt", "payments-prod", "legacy-api"))
        self.assertEqual(targets.unread, (("payments-staging", "denied"),))
        self.assertEqual(targets.collector_args(), "--scope-projects ops-mgmt,payments-prod,legacy-api --scope-unread payments-staging=denied")
        self.assertEqual(targets.resolved_at, "2026-10-09T12:00:00Z")
        self.assertEqual(targets.path, str(self.home / fst.SNAPSHOT_FILE))

    def test_a_folder_alone_is_a_declared_scope(self):
        declared = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"folders": ["123456789012"]}
        self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=declared))
        self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt",))

    def test_collector_args_without_an_unread_project_carries_the_sweep_alone(self):
        self._write(_snapshot([{"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"}]))
        self.assertEqual(fst.declared_scope_targets(self.home).collector_args(), "--scope-projects ops-mgmt")

    def test_a_carried_declaration_under_an_unreadable_render_is_still_a_boundary(self):
        # A tick that cannot read the render keeps the last declaration and
        # writes present: false, readable: false; the audits keep the boundary
        # too rather than listing every visible project.
        self._write(_snapshot([
            {"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"},
            {"id": "payments-prod", "outcome": "ok", "state": "in-scope"},
        ], present=False, readable=False))
        self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt", "payments-prod"))
        # A snapshot from before the readable key is read by its lists.
        self._write(_snapshot([{"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"}], present=False))
        self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt",))

    def test_a_carried_host_only_boundary_under_an_unreadable_render_stays_host_only(self):
        # A snapshot from before the boundary key, on the stock install's
        # unreadable tick: present false, readable false, empty lists. Read as
        # a carried boundary; the reconcile now writes `boundary` to settle it.
        empty = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"exclude": {"projects": []}}
        self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=empty, present=False, readable=False))
        self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt",))

    def test_a_block_removed_from_a_readable_cr_is_no_boundary_whatever_is_carried(self):
        # The operator removed spec.scope: the render is readable and carries
        # no block. The reconcile keeps re-persisting the last declaration
        # (removing the block retires nothing), so the lists stay non-empty for
        # good; the audits must not read that as a boundary, or they would
        # shrink to the host and pin every run partial indefinitely.
        self._write(_snapshot([
            {"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"},
            {"id": "payments-prod", "outcome": "unreachable", "state": "in-scope"},
        ], present=False, readable=True))
        self.assertIsNone(fst.declared_scope_targets(self.home))

    def test_nothing_readable_still_hands_the_collector_the_unread_flag(self):
        # The unread flag alone tells the collector a scope was declared, so a
        # verbatim paste of collector_args cannot widen the run to the listing.
        self._write(_snapshot([{"id": "payments-prod", "outcome": "denied", "state": "in-scope"}]))
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual(targets.projects, ())
        self.assertEqual(targets.collector_args(), "--scope-unread payments-prod=denied")
        self._write(_snapshot([]))
        self.assertEqual(fst.declared_scope_targets(self.home).collector_args(), "")

    def test_rows_without_an_id_or_without_a_state_are_handled(self):
        self._write(_snapshot([{"outcome": "ok"}, {"id": "ops-mgmt", "outcome": "ok"}, "junk"]))
        self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt",))

    def test_the_data_root_is_read_from_platform_agent_home_not_hermes_home(self):
        # A platform worker runs with HERMES_HOME at the profile home beneath the
        # data root; the snapshot sits at the root, which PLATFORM_AGENT_HOME names.
        self._write(_snapshot([{"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"}]))
        profile_home = self.home / "profiles" / "platform"
        profile_home.mkdir(parents=True)
        with mock.patch.dict(os.environ, {fst.AGENT_HOME_ENV: str(self.home), "HERMES_HOME": str(profile_home)}):
            self.assertEqual(fst.declared_scope_targets().projects, ("ops-mgmt",))
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=False):
            os.environ.pop(fst.AGENT_HOME_ENV, None)
            self.assertIsNone(fst.declared_scope_targets(), "HERMES_HOME must not be the key: on the pod it names the profile home")
        self.assertEqual(fst.snapshot_path("/elsewhere"), pathlib.Path("/elsewhere") / fst.SNAPSHOT_FILE)


if __name__ == "__main__":
    unittest.main()
