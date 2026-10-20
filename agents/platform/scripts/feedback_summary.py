#!/usr/bin/env python3
"""A copy-pasteable, identifier-free usage and feedback summary of one install (issue #3428).

Customers in restricted networks cannot send telemetry. What they can do is paste
a short block of text into an email or a chat message after reading it. This
module assembles that block from three sources and nothing else:

* the ``PlatformAgent`` resource, read with ``kubectl get`` (required): mode,
  image tag, readiness, install age and ``status.usage``;
* the gateway pod, through one ``kubectl exec`` each (optional): the session-kv
  task feed on its loopback port, rolled up to counts and durations, and the
  inference gateway's Prometheus text for token, spend and latency figures;
* ``kubectl version`` and the gateway's ConfigMap for the Kubernetes version and
  the model name (optional).

The block carries static enums, version strings, integer counters and durations.
No cluster name, project id, namespace, task title, summary, result or error text
reaches it: the task roll-up reads status and timestamps only, and ``main`` refuses
to print a block in which any identifier read from the resource appears. Every
optional source that fails prints as ``unavailable``; the block never exceeds
SUMMARY_MAX_LINES so a reader can check all of it before pasting.

Run from a workstation::

    python3 agents/platform/scripts/feedback_summary.py --namespace kubeagents-system

No model is in the loop: the output is a deterministic function of the sources.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, TextIO, Tuple

SUMMARY_MAX_LINES = 40
SUMMARY_TITLE = "kube-agents usage & feedback summary"
TASK_WINDOW_DAYS = 14
RECENT_TASKS_SHOWN = 5
TASK_FEED_LIMIT = 200
DEFAULT_NAMESPACE = "kubeagents-system"
DEFAULT_AGENT_NAME = "platform-agent"
GATEWAY_POD_LABEL = "app.kubernetes.io/component=gateway"
GATEWAY_POD_PREFIX = "-gateway-"
AGENT_CONTAINER = "platform-agent"
SESSION_KV_URL = "http://127.0.0.1:8699/v1/tasks"
SESSION_KV_KEY_ENV = "SESSION_KV_API_KEY"
INFERENCE_GATEWAY_METRICS_URL = "http://inference-gateway/metrics/"
#: The gateway's ConfigMap: this exact name under the chart, this name plus a
#: content hash under the kustomize base.
LITELLM_CONFIG_MAP = "litellm-config"
LITELLM_CONFIG_MAP_HASHED_PREFIX = LITELLM_CONFIG_MAP + "-"
LITELLM_CONFIG_KEY = "config.yaml"
KUBECTL = "kubectl"
KUBECTL_TIMEOUT_SECONDS = 30
CURL_TIMEOUT_SECONDS = 10
EXIT_OK = 0
EXIT_COLLECT = 1
EXIT_LEAK = 3
UNAVAILABLE = "unavailable"
NEVER = "never"
UNKNOWN = "unknown"
#: ``status.usage`` fields, in the order the block prints them; a missing one prints 0.
USAGE_COUNTERS: Tuple[str, ...] = (
    "clustersRegistered",
    "clustersMonitored",
    "chatMessagesInboundTotal",
    "sessionsTotal",
    "toolExecutionsTotal",
    "eventsIngestedTotal",
    "remediationsProposedTotal",
    "remediationsMergedTotal",
)
#: Kanban statuses folded into the roll-up's buckets; anything else is "other".
STATUS_BUCKETS: Mapping[str, str] = {
    "done": "done",
    "archived": "done",
    "blocked": "blocked",
    "gave_up": "gave_up",
    "running": "active",
    "ready": "active",
    "todo": "active",
    "triage": "active",
    "scheduled": "active",
    "review": "active",
}
BUCKET_ORDER: Tuple[str, ...] = ("done", "blocked", "gave_up", "active", "other")
#: Prometheus families the inference gateway may publish, by LiteLLM version.
TOKEN_FAMILIES: Mapping[str, Tuple[str, ...]] = {
    "in": ("litellm_input_tokens_metric_total", "litellm_input_tokens_metric", "litellm_input_tokens_total", "litellm_input_tokens"),
    "out": ("litellm_output_tokens_metric_total", "litellm_output_tokens_metric", "litellm_output_tokens_total", "litellm_output_tokens"),
}
SPEND_FAMILIES: Tuple[str, ...] = ("litellm_spend_metric_total", "litellm_spend_metric")
LATENCY_FAMILIES: Tuple[str, ...] = ("litellm_request_total_latency_metric", "litellm_llm_api_latency_metric")
LATENCY_QUANTILES: Tuple[float, float] = (0.5, 0.95)
#: Completion-time quantiles over the window's done tasks, nearest rank.
COMPLETION_QUANTILES: Tuple[float, float] = (0.5, 0.95)
PROMETHEUS_LINE = re.compile(r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)')
PROMETHEUS_LABEL = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:[^"\\]|\\.)*)"')
LITELLM_MODEL_LINE = re.compile(r"^\s*model:\s*['\"]?(?P<model>[A-Za-z0-9_./:-]+)", re.MULTILINE)
FEEDBACK_TEMPLATE: Tuple[str, ...] = (
    "feedback:",
    "  Rating: [ 👍 / 👎 / 😐 ]",
    "  What worked well:",
    "  What failed or was slow:",
)


class CollectError(RuntimeError):
    """The required source, the PlatformAgent resource, could not be read."""


@dataclasses.dataclass(frozen=True)
class Sample:
    name: str
    labels: Mapping[str, str]
    value: float


@dataclasses.dataclass(frozen=True)
class Options:
    namespace: str = DEFAULT_NAMESPACE
    name: str = DEFAULT_AGENT_NAME
    context: Optional[str] = None
    kubeconfig: Optional[str] = None
    in_pod: bool = True
    now: Optional[dt.datetime] = None


@dataclasses.dataclass(frozen=True)
class Snapshot:
    generated_at: dt.datetime
    cr: Mapping[str, Any]
    server_version: Optional[Mapping[str, Any]]
    litellm_config: Optional[str]
    tasks: Optional[Sequence[Mapping[str, Any]]]
    litellm_metrics: Optional[str]
    sources: Mapping[str, bool]

    @property
    def identifiers(self) -> List[str]:
        """Strings read from the resource that must never appear in the block."""
        harness = (self.cr.get("spec") or {}).get("harness") or {}
        metadata = self.cr.get("metadata") or {}
        values = [harness.get("clusterName"), harness.get("projectId"), metadata.get("namespace")]
        return [v for v in values if isinstance(v, str) and v]


def build_snapshot(**fields: Any) -> Snapshot:
    return Snapshot(**fields)


# --- parsing -------------------------------------------------------------------

def parse_prometheus(text: str) -> List[Sample]:
    samples: List[Sample] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = PROMETHEUS_LINE.match(line)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        labels = {m.group("key"): m.group("value") for m in PROMETHEUS_LABEL.finditer(match.group("labels") or "")}
        samples.append(Sample(match.group("name"), labels, value))
    return samples


def family_total(samples: Iterable[Sample], names: Sequence[str]) -> Optional[float]:
    """The sum over every series of the first family in ``names`` that has samples."""
    by_name: Dict[str, float] = {}
    for sample in samples:
        if sample.name in names:
            by_name[sample.name] = by_name.get(sample.name, 0.0) + sample.value
    for name in names:
        if name in by_name:
            return by_name[name]
    return None


def histogram_quantiles(samples: Iterable[Sample], family: str, quantiles: Sequence[float]) -> Optional[Tuple[float, ...]]:
    """Quantiles of a Prometheus histogram, interpolated inside the bucket, over all series."""
    buckets: Dict[float, float] = {}
    for sample in samples:
        if sample.name == f"{family}_bucket" and "le" in sample.labels:
            try:
                le = float(sample.labels["le"])
            except ValueError:
                continue
            buckets[le] = buckets.get(le, 0.0) + sample.value
    if not buckets:
        return None
    bounds = sorted(buckets)
    total = buckets[bounds[-1]]
    if total <= 0:
        return None
    results = []
    for quantile in quantiles:
        rank = quantile * total
        previous_bound, previous_count = 0.0, 0.0
        value = bounds[-1]
        for bound in bounds:
            count = buckets[bound]
            if count >= rank:
                if bound == float("inf"):
                    value = previous_bound
                else:
                    width = count - previous_count
                    value = previous_bound + (bound - previous_bound) * ((rank - previous_count) / width if width else 0.0)
                break
            previous_bound, previous_count = bound, count
        results.append(value)
    return tuple(results)


def model_from_litellm_config(config_yaml: Optional[str]) -> Optional[str]:
    """The first provider-qualified model the gateway routes to, from its rendered config."""
    if not config_yaml:
        return None
    for match in LITELLM_MODEL_LINE.finditer(config_yaml):
        model = match.group("model")
        if "/" in model:
            return model
    return None


# --- the roll-up ----------------------------------------------------------------

def _as_epoch(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def summarise_tasks(tasks: Sequence[Mapping[str, Any]], now: dt.datetime) -> Dict[str, Any]:
    """Counts by bucket, completion quantiles and the newest completed rows, without any text."""
    cutoff = now.timestamp() - TASK_WINDOW_DAYS * 86400
    buckets: Dict[str, int] = {bucket: 0 for bucket in BUCKET_ORDER}
    durations: List[float] = []
    recent: List[Tuple[float, str, float]] = []
    for task in tasks:
        created = _as_epoch(task.get("created_at"))
        completed = _as_epoch(task.get("completed_at"))
        latest = completed or _as_epoch(task.get("updated_at")) or created
        if latest is None or latest < cutoff:
            continue
        status = str(task.get("status") or "")
        bucket = STATUS_BUCKETS.get(status, "other")
        buckets[bucket] += 1
        if completed is not None and created is not None:
            duration = max(0.0, completed - created)
            if bucket == "done":
                durations.append(duration)
            recent.append((now.timestamp() - created, status, duration))
    recent.sort(key=lambda row: row[0])
    quantiles = None
    if durations:
        ordered = sorted(durations)
        quantiles = tuple(_nearest_rank(ordered, q) for q in COMPLETION_QUANTILES)
    return {"total": sum(buckets.values()), "buckets": buckets, "quantiles": quantiles, "recent": recent[:RECENT_TASKS_SHOWN]}


def _nearest_rank(ordered: Sequence[float], quantile: float) -> float:
    return ordered[min(len(ordered) - 1, int(round(quantile * (len(ordered) - 1))))]


def audit(text: str, identifiers: Iterable[str]) -> List[str]:
    """The identifiers that appear in ``text``; an empty list means the block is clean."""
    return [identifier for identifier in identifiers if identifier and identifier in text]


# --- rendering ------------------------------------------------------------------

def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s" if rest else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _install_age(cr: Mapping[str, Any], now: dt.datetime) -> str:
    created = _as_epoch((cr.get("metadata") or {}).get("creationTimestamp"))
    if created is None:
        return UNKNOWN
    return f"{int((now.timestamp() - created) // 86400)} days"


def _ready(cr: Mapping[str, Any]) -> str:
    for condition in (cr.get("status") or {}).get("conditions") or []:
        if condition.get("type") == "Ready":
            return str(condition.get("status", UNKNOWN)).lower()
    return UNKNOWN


def _server_version(server_version: Optional[Mapping[str, Any]]) -> str:
    if not server_version:
        return UNAVAILABLE
    return str((server_version.get("serverVersion") or {}).get("gitVersion") or UNAVAILABLE)


def render(snapshot: Snapshot) -> str:
    cr, now = snapshot.cr, snapshot.generated_at
    spec, status = cr.get("spec") or {}, cr.get("status") or {}
    usage = status.get("usage") or {}
    lines: List[str] = ["```", f"{SUMMARY_TITLE} ({now.strftime('%Y-%m-%d')})", "environment:"]
    lines.append(f"  version: {(spec.get('deployment') or {}).get('tag') or UNKNOWN}")
    lines.append(f"  mode: {spec.get('mode') or 'today'}")
    lines.append(f"  model: {model_from_litellm_config(snapshot.litellm_config) or UNAVAILABLE}")
    lines.append(f"  kubernetes: {_server_version(snapshot.server_version)}")
    lines.append(f"  installed: {_install_age(cr, now)}; ready: {_ready(cr)}")
    interfaces = usage.get("activeInterfaces") or []
    lines.append(f"  interfaces: {', '.join(sorted(str(i) for i in interfaces)) or 'none'}")
    lines.append("usage:")
    for counter in USAGE_COUNTERS:
        lines.append(f"  {counter}: {int(usage.get(counter) or 0)}")
    lines.append(f"  lastActive: {usage.get('lastActiveTime') or NEVER}")
    lines.append("performance:")
    if snapshot.litellm_metrics is not None:
        samples = parse_prometheus(snapshot.litellm_metrics)
        tokens_in = family_total(samples, TOKEN_FAMILIES["in"])
        tokens_out = family_total(samples, TOKEN_FAMILIES["out"])
        spend = family_total(samples, SPEND_FAMILIES)
        latency = next((q for q in (histogram_quantiles(samples, f, LATENCY_QUANTILES) for f in LATENCY_FAMILIES) if q), None)
    else:
        tokens_in = tokens_out = spend = latency = None
    tokens = f"in {int(tokens_in)} / out {int(tokens_out)}" if tokens_in is not None and tokens_out is not None else UNAVAILABLE
    lines.append(f"  tokens: {tokens}")
    lines.append(f"  spend: {spend:.2f} USD" if spend is not None else f"  spend: {UNAVAILABLE}")
    lines.append(f"  latency p50 {latency[0]:.1f}s / p95 {latency[1]:.1f}s" if latency else f"  latency: {UNAVAILABLE}")
    if snapshot.tasks is None:
        lines.append(f"tasks ({TASK_WINDOW_DAYS} days): {UNAVAILABLE}")
    else:
        rollup = summarise_tasks(snapshot.tasks, now)
        counts = ", ".join(f"{bucket} {rollup['buckets'][bucket]}" for bucket in BUCKET_ORDER if rollup["buckets"][bucket])
        lines.append(f"tasks ({TASK_WINDOW_DAYS} days): {rollup['total']}" + (f" ({counts})" if counts else ""))
        if rollup["quantiles"]:
            lines.append(f"  completed p50 {_duration(rollup['quantiles'][0])} / p95 {_duration(rollup['quantiles'][1])}")
        for age, status_name, duration in rollup["recent"]:
            lines.append(f"  - {_age(age):<8} {status_name:<8} {_duration(duration)}")
    lines.extend(FEEDBACK_TEMPLATE)
    lines.append("```")
    return "\n".join(lines)


# --- collection -----------------------------------------------------------------

def _kubectl(options: Options, *args: str, timeout: int = KUBECTL_TIMEOUT_SECONDS) -> str:
    command = [KUBECTL]
    if options.context:
        command += ["--context", options.context]
    if options.kubeconfig:
        command += ["--kubeconfig", options.kubeconfig]
    command += list(args)
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        raise CollectError(f"{' '.join(command)}: {result.stderr.strip() or 'exit ' + str(result.returncode)}")
    return result.stdout


def _optional(options: Options, *args: str, timeout: int = KUBECTL_TIMEOUT_SECONDS) -> Optional[str]:
    try:
        return _kubectl(options, *args, timeout=timeout)
    except (CollectError, OSError, subprocess.SubprocessError):
        return None


def _gateway_pod(options: Options) -> Optional[str]:
    raw = _optional(options, "get", "pods", "-n", options.namespace, "-o", "json")
    if raw is None:
        return None
    try:
        items = json.loads(raw).get("items") or []
    except ValueError:
        return None
    for item in items:
        name = (item.get("metadata") or {}).get("name") or ""
        if GATEWAY_POD_PREFIX in name and (item.get("status") or {}).get("phase") == "Running":
            return name
    return None


def _in_pod_curl(options: Options, pod: str, shell: str) -> Optional[str]:
    return _optional(
        options, "exec", "-n", options.namespace, pod, "-c", AGENT_CONTAINER, "--", "sh", "-c", shell,
        timeout=KUBECTL_TIMEOUT_SECONDS + CURL_TIMEOUT_SECONDS,
    )


def _litellm_config(options: Options) -> Optional[str]:
    """The gateway's rendered config, from the ConfigMap under either of its names."""
    raw = _optional(options, "get", "configmap", "-n", options.namespace, "-o", "json")
    if raw is None:
        return None
    try:
        items = json.loads(raw).get("items") or []
    except ValueError:
        return None
    for item in items:
        name = (item.get("metadata") or {}).get("name") or ""
        if name == LITELLM_CONFIG_MAP or name.startswith(LITELLM_CONFIG_MAP_HASHED_PREFIX):
            config = (item.get("data") or {}).get(LITELLM_CONFIG_KEY)
            if isinstance(config, str):
                return config
    return None


def collect(options: Options) -> Snapshot:
    now = options.now or dt.datetime.now(dt.timezone.utc)
    try:
        cr = json.loads(_kubectl(options, "get", "platformagent", options.name, "-n", options.namespace, "-o", "json"))
    except (CollectError, OSError, subprocess.SubprocessError, ValueError) as error:
        raise CollectError(f"cannot read platformagent {options.name} in {options.namespace}: {error}") from error
    server_version = None
    raw_version = _optional(options, "version", "-o", "json")
    if raw_version:
        try:
            server_version = json.loads(raw_version)
        except ValueError:
            server_version = None
    litellm_config = _litellm_config(options)
    tasks: Optional[Sequence[Mapping[str, Any]]] = None
    metrics: Optional[str] = None
    if options.in_pod:
        pod = _gateway_pod(options)
        if pod:
            since = int(now.timestamp()) - TASK_WINDOW_DAYS * 86400
            feed = _in_pod_curl(
                options, pod,
                f'curl -sS -m {CURL_TIMEOUT_SECONDS} -H "X-API-Key: ${SESSION_KV_KEY_ENV}" '
                f'"{SESSION_KV_URL}?since={since}&limit={TASK_FEED_LIMIT}"',
            )
            if feed:
                try:
                    tasks = json.loads(feed).get("tasks")
                except ValueError:
                    tasks = None
            metrics = _in_pod_curl(options, pod, f"curl -sSL -m {CURL_TIMEOUT_SECONDS} {INFERENCE_GATEWAY_METRICS_URL}")
            if metrics is not None and not metrics.strip():
                metrics = None
    return Snapshot(
        generated_at=now, cr=cr, server_version=server_version, litellm_config=litellm_config,
        tasks=tasks, litellm_metrics=metrics,
        sources={"session-kv": tasks is not None, "litellm-metrics": metrics is not None},
    )


def main(argv: Optional[Sequence[str]] = None, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--namespace", "-n", default=DEFAULT_NAMESPACE)
    parser.add_argument("--name", default=DEFAULT_AGENT_NAME, help="the PlatformAgent resource")
    parser.add_argument("--context")
    parser.add_argument("--kubeconfig")
    parser.add_argument("--no-exec", action="store_true", help="read the resource only; skip the in-pod sources")
    args = parser.parse_args(argv)
    options = Options(namespace=args.namespace, name=args.name, context=args.context, kubeconfig=args.kubeconfig, in_pod=not args.no_exec)
    try:
        snapshot = collect(options)
    except CollectError as error:
        print(f"feedback-summary: {error}", file=stderr)
        return EXIT_COLLECT
    text = render(snapshot)
    leaked = audit(text, snapshot.identifiers)
    if leaked:
        print(f"feedback-summary: refused to print: the block names {', '.join(leaked)}", file=stderr)
        return EXIT_LEAK
    print(text, file=stdout)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
