"""Pass-through trajectory logger callback for LiteLLM inside kube-agents.

Records every raw LLM call (model alias, upstream modelVersion, request messages,
tool schemas, assistant response content, reasoning_content, tool_calls, token usage,
and latency_ms) as one JSONL line in LITELLM_TRACE_LOG_PATH (default:
/tmp/litellm_traces.jsonl).

Designed to be registered in `litellm_settings.callbacks` alongside
`litellm_thinking_replay_callback.proxy_handler_instance`. Never mutates the
request or response payload.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover
    class CustomLogger:  # type: ignore[no-redef]
        pass

TRACE_LOG_PATH = os.environ.get("LITELLM_TRACE_LOG_PATH", "/tmp/litellm_traces.jsonl")


def _safe_serialize(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_serialize(x) for x in obj]
    if hasattr(obj, "model_dump"):
        try:
            return _safe_serialize(obj.model_dump())
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return _safe_serialize(obj.dict())
        except Exception:
            pass
    return str(obj)


class PassThroughTrajectoryLogger(CustomLogger):
    async def async_log_success_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        self._write_record("success", kwargs, response_obj, start_time, end_time, error=None)

    async def async_log_failure_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        err = kwargs.get("exception") or kwargs.get("traceback_exception")
        self._write_record("failure", kwargs, response_obj, start_time, end_time, error=str(err) if err else "unknown")

    def _write_record(
        self,
        status: str,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
        error: str | None,
    ) -> None:
        try:
            litellm_params = kwargs.get("litellm_params") or {}
            optional_params = kwargs.get("optional_params") or {}
            latency_ms = None
            if start_time and end_time:
                try:
                    latency_ms = round((end_time - start_time).total_seconds() * 1000.0, 2)
                except Exception:
                    latency_ms = None

            resp_dict = _safe_serialize(response_obj) if response_obj is not None else {}
            record = {
                "timestamp": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "status": status,
                "model_alias": kwargs.get("model"),
                "custom_llm_provider": litellm_params.get("custom_llm_provider"),
                "api_base": litellm_params.get("api_base"),
                "reasoning_effort": optional_params.get("reasoning_effort"),
                "latency_ms": latency_ms,
                "messages": _safe_serialize(kwargs.get("messages")),
                "tools": _safe_serialize(optional_params.get("tools")),
                "response": resp_dict,
                "error": error,
            }
            log_path = os.environ.get("LITELLM_TRACE_LOG_PATH", TRACE_LOG_PATH)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            # Never break inference if trace logging fails
            pass


proxy_handler_instance = PassThroughTrajectoryLogger()
