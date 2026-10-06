"""The chart forwards `platformAgent.deployment.credentialProxy.resources` to the CR.

The value is the chart's route to `spec.deployment.credentialProxy.resources`, the
PlatformAgent field that sizes the credential-proxy container. Three things have to hold:
a default render writes no block at all (an empty one would be pruned by an API server
serving an older CRD and read as an override by everyone else); a set value reaches the
CR under the path the operator reads; and the schema, which is closed under
`platformAgent.deployment`, declares the key and refuses one it does not know beneath it.

The quota preflight's arithmetic over the same value is in test_quota_preflight.py.

Run: python3 -m unittest discover -s tests -p 'test_chart_credential_proxy_resources.py' -v
"""

import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_SCHEMA = _CHART / "values.schema.json"
_VALUES = _CHART / "values.yaml"
_CR_TEMPLATE = "templates/platform-agent-cr.yaml"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_VALUE_PATH = "platformAgent.deployment.credentialProxy.resources"
_CR_PATH = ("spec", "deployment", "credentialProxy", "resources")


def _proxy_values(resources):
    return {"platformAgent": {"deployment": {"credentialProxy": {"resources": resources}}}}


def _cr_resources(cr):
    node = cr
    for key in _CR_PATH:
        node = node[key]
    return node


def _schema_node(path):
    node = json.loads(_SCHEMA.read_text())
    for key in path:
        node = node["properties"][key]
    return node


class SchemaDeclaresTheKeyTest(unittest.TestCase):
    """Readable without helm: the value is declared, and closed below `credentialProxy`."""

    def test_values_yaml_carries_an_empty_default(self):
        values = yaml.safe_load(_VALUES.read_text())
        self.assertEqual(values["platformAgent"]["deployment"]["credentialProxy"], {"resources": {}})

    def test_credential_proxy_is_closed_and_resources_is_an_open_object(self):
        node = _schema_node(("platformAgent", "deployment", "credentialProxy"))
        self.assertIs(node["additionalProperties"], False)
        self.assertEqual(node["properties"]["resources"], {"type": "object"})


@unittest.skipUnless(_HELM, "helm is not installed")
class CredentialProxyResourcesRenderTest(unittest.TestCase):
    def _render_cr(self, sets=(), expect_failure=False, values=None):
        args = [_HELM, "template", "r", str(_CHART), "-s", _CR_TEMPLATE, *_REQUIRED]
        for item in sets:
            args += ["--set", item]
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as fh:
            # A values file, unlike `--set`, hands the chart YAML floats and nulls.
            yaml.safe_dump(values or {}, fh)
            fh.flush()
            res = subprocess.run([*args, "-f", fh.name], capture_output=True, text=True)
        if expect_failure:
            return res
        self.assertEqual(res.returncode, 0, res.stderr)
        docs = [d for d in yaml.safe_load_all(res.stdout) if d and d.get("kind") == "PlatformAgent"]
        self.assertEqual(len(docs), 1, res.stdout)
        return docs[0]

    def test_default_render_writes_no_credential_proxy_block(self):
        cr = self._render_cr()
        self.assertNotIn("credentialProxy", cr["spec"]["deployment"])

    def test_a_null_resources_value_writes_no_block_either(self):
        # `--set ...resources=null` is the documented Helm way to drop a key; the chart
        # has to read it as "nothing set", not render `resources: null` onto the CR.
        cr = self._render_cr([f"{_VALUE_PATH}=null"])
        self.assertNotIn("credentialProxy", cr["spec"]["deployment"])

    def test_a_nulled_leaf_alone_writes_no_block(self):
        # A non-empty map whose only leaf is null holds nothing to override.
        for values in (_proxy_values({"limits": {"memory": None}}),
                       _proxy_values({"limits": {"memory": None}, "requests": {"cpu": None}})):
            cr = self._render_cr(values=values)
            self.assertNotIn("credentialProxy", cr["spec"]["deployment"], values)

    def test_a_nulled_leaf_beside_a_set_one_is_dropped(self):
        cr = self._render_cr(values=_proxy_values({"limits": {"memory": None, "cpu": "2"}}))
        self.assertEqual(_cr_resources(cr), {"limits": {"cpu": "2"}})

    def test_an_empty_string_leaf_alone_writes_no_block(self):
        # The chart reads "" as unset, as kube-agents.compactFields does; rendered,
        # `memory: ""` is refused by the CRD's quantity pattern.
        for values in (_proxy_values({"limits": {"memory": ""}}),
                       _proxy_values({"limits": {"memory": ""}, "requests": {"cpu": ""}})):
            cr = self._render_cr(values=values)
            self.assertNotIn("credentialProxy", cr["spec"]["deployment"], values)
        cr = self._render_cr([f"{_VALUE_PATH}.limits.memory="])
        self.assertNotIn("credentialProxy", cr["spec"]["deployment"])

    def test_an_empty_string_leaf_beside_a_set_one_is_dropped(self):
        cr = self._render_cr(values=_proxy_values({"limits": {"memory": "", "cpu": 1}}))
        self.assertEqual(_cr_resources(cr), {"limits": {"cpu": "1"}})

    def test_a_float_cpu_from_a_values_file_reaches_the_cr_as_a_string(self):
        # The CRD types a quantity as int-or-string; a bare 1.5 is refused by the API server.
        cr = self._render_cr(values=_proxy_values({"limits": {"cpu": 1.5, "memory": "2Gi"}}))
        self.assertEqual(_cr_resources(cr), {"limits": {"cpu": "1.5", "memory": "2Gi"}})

    def test_a_memory_limit_alone_reaches_the_cr_as_written(self):
        cr = self._render_cr([f"{_VALUE_PATH}.limits.memory=2Gi"])
        node = cr
        for key in _CR_PATH:
            node = node[key]
        # Only the key set: the operator merges it over its defaults, so the chart
        # must not pad the block with defaults of its own.
        self.assertEqual(node, {"limits": {"memory": "2Gi"}})

    def test_requests_and_limits_both_reach_the_cr(self):
        cr = self._render_cr([
            f"{_VALUE_PATH}.requests.memory=1Gi",
            f"{_VALUE_PATH}.limits.memory=4Gi",
            f"{_VALUE_PATH}.limits.cpu=2",
        ])
        # `--set ...cpu=2` is an integer; the CR carries it quoted, which the CRD accepts.
        self.assertEqual(_cr_resources(cr), {"requests": {"memory": "1Gi"}, "limits": {"memory": "4Gi", "cpu": "2"}})

    def test_the_schema_refuses_an_unknown_key_under_credential_proxy(self):
        res = self._render_cr(["platformAgent.deployment.credentialProxy.replicas=2"], expect_failure=True)
        self.assertNotEqual(res.returncode, 0)
        # Helm 3 prints `platformAgent.deployment.credentialProxy: Additional property
        # replicas is not allowed`; Helm 4 prints the path as a JSON pointer and the
        # phrase in lower case. Both name the parent and the key.
        self.assertIn("credentialProxy", res.stderr)
        self.assertIn("replicas", res.stderr)


class CrdGuardTest(unittest.TestCase):
    """`helm upgrade` does not apply crds/, so an override against a CRD from before
    the field would be pruned silently. `lookup` is empty under `helm template`, so
    the guard can only be pinned as text here; the render tests above show the block
    still renders when the lookup returns nothing. The failing branch is exercised
    only against a live install."""

    _LOOKUP = 'lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "platformagents.kubeagents.x-k8s.io"'

    def setUp(self):
        template = (_CHART / _CR_TEMPLATE).read_text()
        block = re.search(r"\{\{- \$proxyOther := .*?\n(.*?)\n\s*credentialProxy:\n", template, re.DOTALL)
        self.assertIsNotNone(block, "the credentialProxy block is missing from the CR template")
        self.guard = block.group(1)

    def test_a_set_override_looks_up_the_installed_crd(self):
        gate = re.search(r"\{\{- if or \$proxyQuantities \$proxyOther \}\}\n\s*\{\{- \$crd := " + re.escape(self._LOOKUP), self.guard)
        self.assertIsNotNone(gate, "the CRD lookup is not gated on a set override")

    def test_the_guard_reads_the_storage_versions_deployment_properties(self):
        self.assertIn("{{- if .storage }}", self.guard)
        self.assertIn('dig "schema" "openAPIV3Schema" "properties" "spec" "properties" "deployment" "properties" (dict) .', self.guard)

    def test_the_guard_fails_naming_the_field_and_the_remedy(self):
        fail = re.search(r'\{\{- if not \(hasKey \$deploymentProps "credentialProxy"\) \}\}\n\s*\{\{- fail "([^"]*)" \}\}', self.guard)
        self.assertIsNotNone(fail, "the CRD guard does not fail on a missing key")
        for want in ("predates spec.deployment.credentialProxy", "helm upgrade does not update CRDs",
                     "charts/kube-agents/crds/", "upgrade.sh", "prunes the value silently", "release record keeps it"):
            self.assertIn(want, fail.group(1))


if __name__ == "__main__":
    unittest.main()
