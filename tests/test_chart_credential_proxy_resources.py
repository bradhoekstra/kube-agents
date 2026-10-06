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
import shutil
import subprocess
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
    def _render_cr(self, sets=(), expect_failure=False):
        args = [_HELM, "template", "r", str(_CHART), "-s", _CR_TEMPLATE, *_REQUIRED]
        for item in sets:
            args += ["--set", item]
        res = subprocess.run(args, capture_output=True, text=True)
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
        node = cr
        for key in _CR_PATH:
            node = node[key]
        self.assertEqual(node, {"requests": {"memory": "1Gi"}, "limits": {"memory": "4Gi", "cpu": 2}})

    def test_the_schema_refuses_an_unknown_key_under_credential_proxy(self):
        res = self._render_cr(["platformAgent.deployment.credentialProxy.replicas=2"], expect_failure=True)
        self.assertNotEqual(res.returncode, 0)
        # Helm 3 prints `platformAgent.deployment.credentialProxy: Additional property
        # replicas is not allowed`; Helm 4 prints the path as a JSON pointer and the
        # phrase in lower case. Both name the parent and the key.
        self.assertIn("credentialProxy", res.stderr)
        self.assertIn("replicas", res.stderr)


if __name__ == "__main__":
    unittest.main()
