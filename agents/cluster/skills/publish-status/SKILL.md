---
name: publish-status
description: Gather this cluster's current status (health, utilization) with read-only diagnostics and publish it to the fleet handover channel via the write_handover tool. Use whenever the Platform Agent asks you to publish or refresh cluster status.
---

# Publish Status Skill (Cluster → Platform Handover)

When the Platform Agent invokes you to **publish status**, gather the requested record
types for **your** cluster and emit each one through the `write_handover` tool. This is
the continuous status channel the Platform Agent reads to reason about the fleet — it is
separate from the on-demand work-item store.

## Rules

- Publish **only** via `write_handover`. Never `write_file` under `/opt/data/fleet/...`.
- Do **not** pass `cluster`/`location` — the tool stamps them from your profile identity.
- Use read-only diagnostics only (`kubectl get/describe/top`, events, logs; the `gke` MCP).
- One `write_handover` call per record type. Records are latest-wins (overwrite).
- Start with `health` and `utilization` unless asked for more.

## `health` — SRE health snapshot

Gather (see the `gke-workload-troubleshooting` skill): node readiness, pods in
CrashLoopBackOff / OOMKilled (exit 137) / Pending, recent warning events, apiserver
latency if available. Then:

```
write_handover(type="health", payload={
  "overall": "healthy | degraded | critical",
  "node_ready_ratio": "<ready>/<total>",
  "pods_crashlooping": <int>,
  "pods_oomkilled_1h": <int>,
  "pods_pending": <int>,
  "failing_workloads": [
    {"namespace": "...", "kind": "Deployment|StatefulSet|DaemonSet", "name": "...", "reason": "..."}
  ],
  "notes": "<short grounded summary>"
})
```

## `utilization` — capacity / rightsizing input

Gather (see the `gke-observability` skill): node/pod CPU & memory via `kubectl top` or
Managed Prometheus; allocatable vs requested vs used; headroom; top consumers. Then:

```
write_handover(type="utilization", payload={
  "window": "15m",
  "node_count": <int>,
  "cpu": {"allocatable_vcpu": <n>, "requested_vcpu": <n>, "used_vcpu": <n>, "utilization_pct": <n>},
  "memory": {"allocatable_gib": <n>, "requested_gib": <n>, "used_gib": <n>, "utilization_pct": <n>},
  "headroom": {"cpu_vcpu": <n>, "memory_gib": <n>},
  "pressure": <bool>,
  "top_consumers": [{"namespace": "...", "name": "...", "cpu_vcpu": <n>, "mem_gib": <n>}]
})
```

## After publishing

Reply with a brief acknowledgement only (e.g. "Published health + utilization."). The
records themselves are the channel — do not paste them into the chat reply.

Other record types (`upgrade_readiness`, `drift`, `inventory`) use the same pattern;
add them when the Platform Agent requests them.
