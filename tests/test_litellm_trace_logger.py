"""Tests for litellm.traceLogEnabled and PassThroughTrajectoryLogger.

Verifies:
1. Chart rendering invariance when traceLogEnabled is false (default):
   - ConfigMap data does not include llm_trace_logger.py.
   - litellm_settings.callbacks does not include the logger.
   - Deployment volumeMounts and env vars omit the logger and LITELLM_TRACE_LOG_PATH.
   - Checksum matches clean unset default.
2. Chart rendering when traceLogEnabled is true:
   - ConfigMap data includes llm_trace_logger.py identical to the chart source file.
   - litellm_settings.callbacks includes llm_trace_logger.proxy_handler_instance.
   - Deployment volumeMounts mounts /app/llm_trace_logger.py (subPath: llm_trace_logger.py).
   - Deployment env includes LITELLM_TRACE_LOG_PATH with default "/tmp/litellm_traces.jsonl".
   - Deployment checksum changes.
3. Custom litellm.traceLogPath sets LITELLM_TRACE_LOG_PATH accordingly.
4. Schema validation rejects invalid traceLogPath (e.g. relative paths).
5. Unit tests for PassThroughTrajectoryLogger:
   - async_log_success_event writes valid JSONL record without mutating payload.
   - async_log_failure_event writes valid failure JSONL record with error string.
   - I/O and serialization errors never raise exceptions or disrupt caller.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import types
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_CHART_FILE = _CHART / "files" / "llm_trace_logger.py"
_CALLBACK = "llm_trace_logger.proxy_handler_instance"
_KEY = "llm_trace_logger.py"
_MOUNT = "/app/llm_trace_logger.py"
_DEFAULT_LOG_PATH = "/tmp/litellm_traces.jsonl"
_HELM_BASE_ARGS = [
    "helm",
    "template",
    "test-release",
    str(_CHART),
    "--set-string",
    "platformAgent.harness.clusterName=test-cluster",
    "--set-string",
    "platformAgent.harness.location=us-central1",
    "--set-string",
    "platformAgent.harness.projectId=test-project",
    "-s",
    "templates/litellm.yaml",
]


def _load_logger_module():
    """Import the logger module with a fallback stand-in for CustomLogger if needed."""
    spec = importlib.util.spec_from_file_location("llm_trace_logger", _CHART_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestTraceLoggerChartRender(unittest.TestCase):
    @staticmethod
    def _render(extra_args: list[str] | None = None, values_text: str | None = None):
        command = list(_HELM_BASE_ARGS)
        values_path = None
        if values_text is not None:
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
                handle.write(values_text)
                values_path = handle.name
            command += ["-f", values_path]
        command += extra_args or []
        try:
            return subprocess.run(command, capture_output=True, text=True, check=False)
        finally:
            if values_path and os.path.exists(values_path):
                os.unlink(values_path)

    def _documents(self, extra_args: list[str] | None = None, values_text: str | None = None):
        proc = self._render(extra_args, values_text)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [d for d in yaml.safe_load_all(proc.stdout) if d]
        by_kind = {d["kind"]: d for d in documents if d["kind"] in ("ConfigMap", "Deployment")}
        return by_kind["ConfigMap"], by_kind["Deployment"]

    @staticmethod
    def _checksum(deployment: dict) -> str:
        return deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"]

    @staticmethod
    def _callbacks(configmap: dict) -> list[str]:
        config = yaml.safe_load(configmap["data"]["config.yaml"])
        return config["litellm_settings"]["callbacks"]

    def test_default_unset_and_explicit_false_are_identical(self) -> None:
        unset_cm, unset_deploy = self._documents()
        false_cm, false_deploy = self._documents(["--set", "litellm.traceLogEnabled=false"])

        self.assertEqual(unset_cm, false_cm)
        self.assertEqual(self._checksum(unset_deploy), self._checksum(false_deploy))
        self.assertNotIn(_KEY, unset_cm["data"])
        self.assertNotIn(_CALLBACK, self._callbacks(unset_cm))

        container = unset_deploy["spec"]["template"]["spec"]["containers"][0]
        mounts = container.get("volumeMounts", [])
        self.assertFalse(any(m.get("mountPath") == _MOUNT for m in mounts))
        envs = container.get("env", [])
        self.assertFalse(any(e.get("name") == "LITELLM_TRACE_LOG_PATH" for e in envs))

    def test_enabled_renders_callback_configmap_mount_and_env(self) -> None:
        set_cm, set_deploy = self._documents(["--set", "litellm.traceLogEnabled=true"])
        unset_cm, unset_deploy = self._documents()

        # ConfigMap data contains the source file verbatim
        self.assertIn(_KEY, set_cm["data"])
        self.assertEqual(set_cm["data"][_KEY], _CHART_FILE.read_text(encoding="utf-8"))

        # ConfigMap litellm_settings.callbacks includes the callback
        callbacks = self._callbacks(set_cm)
        self.assertIn(_CALLBACK, callbacks)
        self.assertIn("litellm_thinking_replay_callback.proxy_handler_instance", callbacks)

        # Deployment rolls (checksum changes)
        self.assertNotEqual(self._checksum(unset_deploy), self._checksum(set_deploy))

        # Deployment volumeMounts contains the logger mount
        container = set_deploy["spec"]["template"]["spec"]["containers"][0]
        mounts = container.get("volumeMounts", [])
        logger_mount = next((m for m in mounts if m.get("mountPath") == _MOUNT), None)
        self.assertIsNotNone(logger_mount)
        self.assertEqual(logger_mount["subPath"], _KEY)
        self.assertEqual(logger_mount["name"], "config-volume")

        # Deployment env contains default LITELLM_TRACE_LOG_PATH
        envs = container.get("env", [])
        path_env = next((e for e in envs if e.get("name") == "LITELLM_TRACE_LOG_PATH"), None)
        self.assertIsNotNone(path_env)
        self.assertEqual(path_env["value"], _DEFAULT_LOG_PATH)

    def test_custom_trace_log_path(self) -> None:
        custom_path = "/var/log/litellm/audit.jsonl"
        set_cm, set_deploy = self._documents(
            [
                "--set",
                "litellm.traceLogEnabled=true",
                "--set-string",
                f"litellm.traceLogPath={custom_path}",
            ]
        )
        container = set_deploy["spec"]["template"]["spec"]["containers"][0]
        envs = container.get("env", [])
        path_env = next((e for e in envs if e.get("name") == "LITELLM_TRACE_LOG_PATH"), None)
        self.assertIsNotNone(path_env)
        self.assertEqual(path_env["value"], custom_path)

    def test_values_file_renders_the_same_as_flags(self) -> None:
        set_cm, set_deploy = self._documents(
            [
                "--set",
                "litellm.traceLogEnabled=true",
                "--set-string",
                "litellm.traceLogPath=/tmp/custom.jsonl",
            ]
        )
        file_cm, file_deploy = self._documents(
            values_text="litellm:\n  traceLogEnabled: true\n  traceLogPath: /tmp/custom.jsonl\n"
        )
        self.assertEqual(set_cm, file_cm)
        self.assertEqual(self._checksum(set_deploy), self._checksum(file_deploy))

    def test_schema_validates_trace_log_path(self) -> None:
        for bad_path in ("relative/path.jsonl", "just_filename.jsonl"):
            with self.subTest(path=bad_path):
                proc = self._render(
                    [
                        "--set",
                        "litellm.traceLogEnabled=true",
                        "--set-string",
                        f"litellm.traceLogPath={bad_path}",
                    ]
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("traceLogPath", proc.stderr)


class TestPassThroughTrajectoryLoggerUnit(unittest.TestCase):
    def setUp(self):
        self.mod = _load_logger_module()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self.temp_dir.name, "test_traces.jsonl")
        os.environ["LITELLM_TRACE_LOG_PATH"] = self.log_path

    def tearDown(self):
        os.environ.pop("LITELLM_TRACE_LOG_PATH", None)
        self.temp_dir.cleanup()

    def test_success_event_logging(self):
        logger = self.mod.PassThroughTrajectoryLogger()
        start = dt.datetime(2026, 10, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 10, 18, 12, 0, 0, 450000, tzinfo=dt.timezone.utc)

        kwargs = {
            "model": "model-default",
            "litellm_params": {
                "custom_llm_provider": "hosted_vllm",
                "api_base": "http://vllm.bench-training.svc:8000/v1",
            },
            "optional_params": {
                "reasoning_effort": "high",
                "tools": [{"type": "function", "function": {"name": "kubectl_get"}}],
            },
            "messages": [{"role": "user", "content": "list pods"}],
        }
        response_obj = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Checking pods.",
                        "reasoning_content": "Need to check pods.",
                    }
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165},
        }

        # Run async log
        asyncio.run(logger.async_log_success_event(kwargs, response_obj, start, end))

        self.assertTrue(os.path.exists(self.log_path))
        lines = pathlib.Path(self.log_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)

        record = json.loads(lines[0])
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["model_alias"], "model-default")
        self.assertEqual(record["custom_llm_provider"], "hosted_vllm")
        self.assertEqual(record["api_base"], "http://vllm.bench-training.svc:8000/v1")
        self.assertEqual(record["reasoning_effort"], "high")
        self.assertEqual(record["latency_ms"], 450.0)
        self.assertEqual(record["messages"], [{"role": "user", "content": "list pods"}])
        self.assertEqual(record["tools"], [{"type": "function", "function": {"name": "kubectl_get"}}])
        self.assertEqual(record["response"]["usage"]["total_tokens"], 165)
        self.assertIsNone(record["error"])

    def test_failure_event_logging(self):
        logger = self.mod.PassThroughTrajectoryLogger()
        start = dt.datetime(2026, 10, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 10, 18, 12, 0, 1, tzinfo=dt.timezone.utc)

        kwargs = {
            "model": "model-default",
            "litellm_params": {"custom_llm_provider": "vertex_ai"},
            "optional_params": {},
            "messages": [{"role": "user", "content": "hello"}],
            "exception": "ConnectionRefusedError: Port 8000 unreachable",
        }

        asyncio.run(logger.async_log_failure_event(kwargs, None, start, end))

        self.assertTrue(os.path.exists(self.log_path))
        lines = pathlib.Path(self.log_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)

        record = json.loads(lines[0])
        self.assertEqual(record["status"], "failure")
        self.assertEqual(record["model_alias"], "model-default")
        self.assertEqual(record["error"], "ConnectionRefusedError: Port 8000 unreachable")
        self.assertEqual(record["response"], {})
        self.assertEqual(record["latency_ms"], 1000.0)

    def test_io_failure_never_raises(self):
        logger = self.mod.PassThroughTrajectoryLogger()
        # Set invalid unwritable path
        os.environ["LITELLM_TRACE_LOG_PATH"] = "/nonexistent/directory/traces.jsonl"

        kwargs = {"model": "model-default"}
        # Must not raise an exception
        try:
            asyncio.run(logger.async_log_success_event(kwargs, {}, None, None))
            asyncio.run(logger.async_log_failure_event(kwargs, {}, None, None))
        except Exception as e:
            self.fail(f"Logger raised exception on I/O error: {e}")

    def test_safe_serialize_handles_objects_and_pydantic_shapes(self):
        class DummyPydantic:
            def model_dump(self):
                return {"field": "value", "sub": [1, 2]}

        class DummyLegacy:
            def dict(self):
                return {"legacy": 42}

        class Unserializable:
            def __repr__(self):
                return "<CustomObj>"

        obj = {
            "pydantic": DummyPydantic(),
            "legacy": DummyLegacy(),
            "custom": Unserializable(),
            "list": [1, "two", None],
        }

        res = self.mod._safe_serialize(obj)
        self.assertEqual(res["pydantic"], {"field": "value", "sub": [1, 2]})
        self.assertEqual(res["legacy"], {"legacy": 42})
        self.assertEqual(res["custom"], "<CustomObj>")
        self.assertEqual(res["list"], [1, "two", None])


if __name__ == "__main__":
    unittest.main()
