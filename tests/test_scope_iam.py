"""What a project in `spec.scope` is granted, and what it must never be.

The multi-project scope (docs/designs/multi-project-scope.md §6) binds the
agent's service account into every project `scope.projects` names. The roles
it carries there are a fixed read allowlist intersected with the roles the host
project got, never `project_roles` itself: a `custom` list that carries
roles/container.admin for the host project must not carry
container.clusters.impersonate into every other project, and a quota-consuming
role must not consume quota where the agent only reads.

The Terraform is read as text, structurally, the way tests/test_scoped_sa_pool_iam.py
reads it -- `terraform` is not a dependency of this suite.

Run:
  python3 -m unittest discover -s tests -p 'test_scope_iam.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
IAM_MODULE = REPO_ROOT / "terraform" / "modules" / "kube-agents-iam"
FULL_INSTALL = REPO_ROOT / "terraform" / "examples" / "full-install"
CHART = REPO_ROOT / "charts" / "kube-agents"

# The design's allowlist, verbatim (multi-project-scope.md §6). A change to
# scope.tf that widens it is a design change first.
SCOPE_ROLE_ALLOWLIST = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/iam.securityReviewer",
]

# Default project roles the scope must never carry, and why (§6).
ROLES_KEPT_HOST_ONLY = {
    "roles/iam.serviceAccountUser": "actAs on every service account beneath a container",
    "roles/mcp.toolUser": "unmeasured whether the check runs in the caller's or the target project",
    "roles/serviceusage.serviceUsageConsumer": "consumes quota in projects the agent only reads",
}


# The two HCL readers are test_scoped_sa_pool_iam's; one copy, whichever way the suite is run.
try:
    from tests.test_scoped_sa_pool_iam import _hcl_string_list, _hcl_variable_default_list  # noqa: E402
except ImportError:  # run from inside tests/
    from test_scoped_sa_pool_iam import _hcl_string_list, _hcl_variable_default_list  # noqa: E402


def _resource_block(source: str, kind: str, name: str) -> str:
    block = re.search(
        rf'^resource\s+"{re.escape(kind)}"\s+"{re.escape(name)}"\s*\{{(.*?)^\}}',
        source,
        re.MULTILINE | re.DOTALL,
    )
    if block is None:
        raise AssertionError(f'resource "{kind}" "{name}" moved or was renamed')
    return block.group(1)


def _uncommented(source: str) -> str:
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))


class ScopeAllowlistTest(unittest.TestCase):
    def setUp(self):
        self.scope_tf = (IAM_MODULE / "scope.tf").read_text(encoding="utf-8")
        self.module_vars = (IAM_MODULE / "variables.tf").read_text(encoding="utf-8")

    def test_the_allowlist_is_the_designs(self):
        self.assertEqual(SCOPE_ROLE_ALLOWLIST, _hcl_string_list(self.scope_tf, "scope_role_allowlist"))

    def test_every_allowlisted_role_is_a_default_project_role(self):
        # The allowlist cannot name a role the agent does not otherwise hold:
        # the scope narrows the home grant, it never widens it.
        default_roles = set(_hcl_variable_default_list(self.module_vars, "project_roles"))
        self.assertEqual(set(), set(SCOPE_ROLE_ALLOWLIST) - default_roles)

    def test_the_host_only_roles_stay_out(self):
        for role, why in ROLES_KEPT_HOST_ONLY.items():
            with self.subTest(role=role):
                self.assertNotIn(role, SCOPE_ROLE_ALLOWLIST, why)

    def test_the_scope_binds_the_intersection_and_never_project_roles(self):
        body = _uncommented(self.scope_tf)
        expression = re.search(r"^\s*scope_roles\s*=\s*(.+)$", body, re.MULTILINE)
        self.assertIsNotNone(expression, "local.scope_roles moved or was renamed")
        self.assertIn("scope_role_allowlist", expression.group(1))
        self.assertIn("contains(var.project_roles", expression.group(1))
        binding = _resource_block(body, "google_project_iam_member", "scope_roles")
        self.assertIn("for_each = local.scope_bindings", binding)
        self.assertNotIn("var.project_roles", binding, "the scope binding reads the intersection, not the host list")
        bindings = re.search(r"scope_bindings\s*=\s*\{(.*?)^\s*\}", body, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(bindings)
        self.assertIn("local.scope_roles", bindings.group(1))
        self.assertNotIn("var.project_roles", bindings.group(1))

    def test_the_host_project_is_never_bound_twice(self):
        body = _uncommented(self.scope_tf)
        projects = re.search(r"^\s*scope_projects\s*=\s*(.+)$", body, re.MULTILINE)
        self.assertIsNotNone(projects, "local.scope_projects moved or was renamed")
        self.assertIn("!= var.project_id", projects.group(1))

    def test_a_scope_with_nothing_to_bind_fails_the_plan(self):
        # A `custom` project_roles with no read role leaves scope_bindings empty
        # and every scoped project `denied`; the precondition on the service
        # account, which always exists, is what turns that into a plan error.
        main = (IAM_MODULE / "main.tf").read_text(encoding="utf-8")
        account = _resource_block(main, "google_service_account", "agent")
        self.assertIn("precondition", account)
        self.assertIn("length(local.scope_projects) == 0 || length(local.scope_roles) > 0", account)

    def test_the_variable_caps_the_list_where_the_crd_does(self):
        block = re.search(r'^variable\s+"scope"\s*\{(.*?)^\}', self.module_vars, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(block, "variable scope moved or was renamed")
        self.assertIn("length(var.scope.projects) <= 100", block.group(1))
        self.assertIn("length(var.scope.exclude.projects) <= 100", block.group(1))
        self.assertIn("length(var.scope.exclude.clusters) <= 100", block.group(1))
        self.assertIn('{0,62}$", cluster.cluster_name)', block.group(1))

    def test_the_variable_refuses_what_the_crd_would_refuse_at_admission(self):
        # A repeat (the CRD lists are sets, the cluster list a map) or an
        # exclude glob outside the CRD's class would bind IAM and then fail the
        # CR after the apply; both fail the plan instead.
        block = re.search(r'^variable\s+"scope"\s*\{(.*?)^\}', self.module_vars, re.MULTILINE | re.DOTALL)
        body = block.group(1)
        self.assertIn("length(distinct(var.scope.projects)) == length(var.scope.projects)", body)
        self.assertIn("length(distinct(var.scope.exclude.projects)) == length(var.scope.exclude.projects)", body)
        self.assertIn("for c in var.scope.exclude.clusters", body)
        self.assertIn('for entry in var.scope.exclude.projects : can(regex("^[a-z0-9*?', body)


class ScopeReachesBothHalvesTest(unittest.TestCase):
    """One value, two consumers: the IAM module and the CR the chart renders."""

    def setUp(self):
        self.main = (FULL_INSTALL / "main.tf").read_text(encoding="utf-8")
        self.variables = (FULL_INSTALL / "variables.tf").read_text(encoding="utf-8")

    def test_the_composition_declares_the_variable(self):
        self.assertRegex(self.variables, r'(?m)^variable\s+"scope"\s*\{', "variable scope moved or was renamed")

    def test_the_module_and_the_chart_read_the_same_variable(self):
        module = re.search(r'^module\s+"kube_agents_iam"\s*\{(.*?)^\}', self.main, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(module)
        self.assertRegex(module.group(1), r"(?m)^\s*scope\s*=\s*var\.scope\s*$", "the IAM module no longer receives var.scope")
        values = re.search(r"platformAgent\s*=\s*\{(.*?)^\s*credentials\s*=", self.main, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(values, "the platformAgent Helm values block moved")
        self.assertIn("projects = var.scope.projects", values.group(1))
        self.assertIn("projects = var.scope.exclude.projects", values.group(1))
        for key in ("projectId", "location", "clusterName"):
            self.assertIn(key, values.group(1), f"exclude.clusters no longer maps {key} for the CR")

    def test_the_chart_always_renders_a_present_block(self):
        # docs/designs/multi-project-scope.md §7: an omitted block must not
        # stand in for an emptied one, so the template does not go through
        # compactFields or a `with`.
        template = (CHART / "templates" / "platform-agent-cr.yaml").read_text(encoding="utf-8")
        scope = re.search(r"^  scope:\n(.*?)^  \{\{-", template, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(scope, "the scope block moved or is now conditional")
        self.assertIn("projects: {{ $scope.projects | default list | toJson }}", scope.group(1))
        self.assertIn("clusters: {{ $scopeExclude.clusters | default list | toJson }}", scope.group(1))
        # The one condition on the block is the omit switch the retag path sets;
        # nothing else (a `with`, an `if` on the values) may gate it.
        before = template[: scope.start()].rstrip().splitlines()[-1]
        self.assertEqual(before.strip(), "{{- if not $scope.omit }}", "scope is gated by nothing but scope.omit")


if __name__ == "__main__":
    unittest.main()
