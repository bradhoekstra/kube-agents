# Fleet Handover — Retirement & Restore Guide

**Status:** RETIRED on 2026-07-23. The file-based fleet handover channel (`/opt/data/fleet`,
the `write_handover` tool, the `fleet-status-refresh` cron, and the `publish-status` cluster
skill) has been removed. **Kanban delegation is unaffected** and remains the coordination path.

This document explains **why** it was retired and gives a **precise, self-contained restore
procedure** you can hand to a coding agent to bring it back when scale justifies it.

> Full design of record (preserved): [`agent-communication.md`](agent-communication.md) §2.
> Realignment notes: [`../agent-communication-divergence.md`](../agent-communication-divergence.md).

---

## 1. What it was (one paragraph)

A continuous **cluster → platform status cache**. Each per-cluster Cluster Agent published typed
status records (`health`, `utilization`, …) through a `write_handover` tool that wrote an atomic
JSON envelope to a fixed path on the shared pod PVC:

```
/opt/data/fleet/clusters/<cluster>/<location>/<type>.json
envelope: {schema_version, cluster, location, type, generated_at, expires_at, payload}
```

The Platform Agent read these files directly (plain `ls`/`cat`, honoring `expires_at`) as an
"always-available" signal for fleet-level reasoning, deep-diving a cluster live only when needed.
A platform cron (`fleet-status-refresh`) drove production by invoking each cluster agent to publish.

## 2. Why it was retired

At the current fleet scale (a handful of clusters) it was **not load-bearing** — it added moving
parts without being trusted or used:

1. **Value only appears at scale.** With direct `gke`/`kubectl` access and few clusters, querying
   live is cheap; a cache doesn't pay for itself. The platform agent rationally treated live
   cluster state as ground truth.
2. **The producer was unreliable → stale/partial → distrusted.** `fleet-status-refresh` invoked an
   LLM turn per cluster and hit timeouts / model contention (e.g. only 4/6 clusters covered, one
   record stale). A sometimes-stale, partial cache is worse than none — it invites "is this
   current?" doubt, so the agent re-queried live anyway.
3. **SOPs weren't cache-first.** Nothing enforced "read handover before going live," so it was
   dead weight.

Net: a stale cache nobody trusted. Removing it reduces complexity and eliminates the
stale-data footgun; the platform loses nothing it was actually using.

## 3. When to bring it back

Restore it when **one or more** of these hold — and only if you also commit to the fixes in §5:

- **Fleet grows** past roughly **10–15 clusters**, where enumerating live state across the whole
  fleet for a routine overview becomes slow, rate-limited, or costly.
- You want **cheap, always-on fleet-level reasoning / dashboards** (periodic fleet scans, capacity
  planning) without an LLM deep-dive per cluster each time.
- **Cluster agents become persistent** (own gateway + cron) instead of transient one-shot workers —
  then producers are reliable self-cron rather than a fragile platform fan-out.

If none of these hold, prefer live queries.

## 4. How to restore (procedure for a coding agent)

**Fastest path:** revert the retirement commit, then apply the §5 improvements.
```
git revert <RETIREMENT_COMMIT_SHA>    # the commit/PR that introduced this document
```
(Find it with `git log --oneline -- docs/designs/fleet-handover-retirement.md` — its introducing
commit is the retirement.) That restores every file below verbatim. Prefer this over hand-rebuilding.

**If reconstructing manually**, recreate/re-wire exactly these pieces:

| # | Path | What to restore |
|---|------|-----------------|
| 1 | `agents/platform/plugins/handover/` (`__init__.py`, `plugin.yaml`, `test_handover.py`) | The `write_handover` tool. Core helper `write_record(cluster, location, type, payload, ttl_seconds, fleet_root)` writes an **atomic** (temp + `fsync` + `os.replace`) JSON envelope `{schema_version:1, cluster, location, type, generated_at, expires_at, payload}` under `FLEET_DIR` (`os.environ["FLEET_DIR"]`, default `/opt/data/fleet`)`/clusters/<cluster>/<location>/<type>.json`. `VALID_TYPES = {health, utilization, upgrade_readiness, drift, inventory}`; `DEFAULT_TTL_SECONDS = 900`. `register(ctx)` reads `cluster_identity` (`project/cluster/location`) from the profile config via `cfg_get`, and **registers the tool only if identity is present**; identity is captured in a closure — **never** taken from tool args. Toolset name: `handover`. |
| 2 | `agents/platform/scripts/fleet_status_refresh.py` | `no_agent` cron producer. Iterates `$HERMES_HOME/profiles/*`, **skips `RESERVED_PROFILES = {default, platform}`**, requires a pinned `kubeconfig.yaml`, and runs `hermes -p <name> -z "<publish prompt>"` per cluster with `KUBECONFIG` set. Resilient (always exit 0). **See §5 for the required fixes.** |
| 3 | `agents/cluster/skills/publish-status/SKILL.md` | Cluster skill: gather `health` + `utilization` via read-only diagnostics, call `write_handover` once per record type. |
| 4 | **Cron wiring** → `agents/chat/defaults/cron/jobs.json` | Add the `fleet-status-refresh` job to the cron of the profile the gateway **actually ticks**. **CRITICAL past bug:** it was placed only in `agents/platform/cron/jobs.json`, but under the Chat-Agent-front-door architecture the gateway ticks the **`default` (chat)** profile, whose cron seeds from `agents/chat/defaults/cron/jobs.json`. Put it there (a `no_agent` job that runs a shared script is fine on the router profile). Job shape: `{"id":"fleet-status-refresh","name":"Fleet Status Refresh","schedule":{"kind":"interval","minutes":10},"prompt":"","no_agent":true,"script":"fleet_status_refresh.py","enabled":true,"deliver":"local"}`. |
| 5 | `agents/cluster/config.yaml` | Add `handover` to `platform_toolsets.cli`, `platform_toolsets.api_server`, and `plugins.enabled`. (The plugin loads from `/opt/hermes/plugins` when enabled — see Dockerfile note.) |
| 6 | `agents/platform/scripts/cluster_agent_profile.py` | **Already injects `cluster_identity`** into each scaffolded cluster profile's config (kept at retirement — `write_handover` reads it). Verify `_inject_cluster_identity` still runs in `cmd_create`. |
| 7 | Persona/SOP re-adds | `agents/cluster/SOUL.md` ("Publish Status via `write_handover` Only" truth + "Publishing Status" §); `agents/cluster/AGENTS.md` (write_handover-only status bullet); `agents/platform/SOUL.md` ("Consuming Fleet Status" §); `agents/platform/skills/cluster-agent-lifecycle/SKILL.md` ("Consuming continuous handover status" §); `agents/platform/skills/manage-cluster/SKILL.md` (optional immediate-publish step); `agents/platform/skills/workload-rebalancing/SKILL.md` (trigger reads `/opt/data/fleet/**/utilization.json`); `docs/glossary.md` (Handover entry). |
| 8 | Dockerfile | No change needed: `deploy/docker/Dockerfile` already does `COPY agents/platform/plugins/ /opt/hermes/plugins/`, so restoring the `handover/` dir is enough for it to ship. |

**No IAM / GCP changes are required** — handover is pure in-pod filesystem on the PVC.

## 5. Fix the reasons it failed (do this as part of any restore)

Restoring it verbatim will reproduce the staleness problem. Also do:

1. **Reliable, complete producer.** Publish **all** managed clusters every cycle, run them
   **concurrently** with a per-cluster timeout, and **verify** each write (fleet file exists with a
   fresh `generated_at`) — the old script reported success on exit-0-without-publish.
2. **Cheaper producer (recommended):** replace the per-cluster **LLM** invocation with a plain
   `no_agent` script that gathers `health`/`utilization` via **direct `kubectl`/`gke`** against each
   pinned kubeconfig and writes the envelope itself. No LLM cost, far more reliable.
3. **Freshness gating on the read side:** consumers must ignore records past `expires_at` and fall
   back to live. Don't act on stale data.
4. **SOP-first:** platform fleet-level SOPs consult handover **first** and deep-dive live only on
   stale/missing/concerning records — otherwise the cache is dead weight again.

## 6. Verify a restore
- `fleet-status-refresh` actually fires (it's on the **ticking** profile), every ~10m.
- `/opt/data/fleet/clusters/*/*/{health,utilization}.json` exist and are **fresh for every managed
  cluster** (not a subset), with `expires_at` in the future.
- The Platform Agent reads them for a fleet scan and only deep-dives on stale/missing.
