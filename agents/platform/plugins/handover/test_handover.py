"""Unit tests for the handover plugin's write path.

Run: python3 -m unittest agents.platform.plugins.handover.test_handover

The plugin's core (write_record / handler) has no Hermes dependency, so these
tests import it directly. Hermes-specific wiring (register) is not exercised.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Make the `handover` package importable (parent dir = .../plugins).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handover  # noqa: E402

CLUSTER = "prod"
LOCATION = "us-central1"


class WriteRecordTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_expected_path_and_envelope(self):
        payload = {"overall": "degraded", "pods_crashlooping": 3}
        path = handover.write_record(CLUSTER, LOCATION, "health", payload, ttl_seconds=900, fleet_root=self.root)

        self.assertEqual(path, self.root / "clusters" / CLUSTER / LOCATION / "health.json")
        self.assertTrue(path.exists())

        env = json.loads(path.read_text())
        self.assertEqual(env["schema_version"], handover.SCHEMA_VERSION)
        self.assertEqual(env["cluster"], CLUSTER)
        self.assertEqual(env["location"], LOCATION)
        self.assertEqual(env["type"], "health")
        self.assertEqual(env["payload"], payload)
        self.assertIn("generated_at", env)
        self.assertIn("expires_at", env)
        self.assertGreater(env["expires_at"], env["generated_at"])  # ISO-8601 Z sorts chronologically

    def test_default_ttl_when_omitted(self):
        path = handover.write_record(CLUSTER, LOCATION, "utilization", {"cpu": {}}, fleet_root=self.root)
        env = json.loads(path.read_text())
        # expires ~= now + DEFAULT_TTL_SECONDS; just assert it is after generated_at.
        self.assertGreater(env["expires_at"], env["generated_at"])

    def test_latest_wins_overwrite(self):
        handover.write_record(CLUSTER, LOCATION, "health", {"overall": "healthy"}, fleet_root=self.root)
        path = handover.write_record(CLUSTER, LOCATION, "health", {"overall": "critical"}, fleet_root=self.root)
        env = json.loads(path.read_text())
        self.assertEqual(env["payload"]["overall"], "critical")
        # exactly one file for this (cluster, location, type)
        files = list((self.root / "clusters" / CLUSTER / LOCATION).glob("*.json"))
        self.assertEqual([f.name for f in files], ["health.json"])

    def test_invalid_type_raises(self):
        with self.assertRaises(ValueError):
            handover.write_record(CLUSTER, LOCATION, "bogus", {}, fleet_root=self.root)

    def test_non_dict_payload_raises(self):
        with self.assertRaises(ValueError):
            handover.write_record(CLUSTER, LOCATION, "health", "not-a-dict", fleet_root=self.root)

    def test_missing_identity_raises(self):
        with self.assertRaises(ValueError):
            handover.write_record("", LOCATION, "health", {}, fleet_root=self.root)


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict("os.environ", {"FLEET_DIR": str(self.root)})
        self._env.start()
        self.handler = handover._make_handler(CLUSTER, LOCATION)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_handler_ok(self):
        out = json.loads(self.handler({"type": "health", "payload": {"overall": "healthy"}}))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["cluster"], CLUSTER)
        self.assertEqual(out["location"], LOCATION)
        self.assertTrue((self.root / "clusters" / CLUSTER / LOCATION / "health.json").exists())

    def test_handler_invalid_type_returns_error(self):
        out = json.loads(self.handler({"type": "nope", "payload": {}}))
        self.assertIn("error", out)

    def test_identity_from_closure_not_args(self):
        # Even if the model tries to smuggle cluster/location in args, they are ignored;
        # the record is written under the closure identity.
        self.handler({"type": "health", "payload": {}, "cluster": "evil", "location": "elsewhere"})
        self.assertTrue((self.root / "clusters" / CLUSTER / LOCATION / "health.json").exists())
        self.assertFalse((self.root / "clusters" / "evil").exists())


if __name__ == "__main__":
    unittest.main()
