# Agent Communication — Divergence from the Design Doc

**Reference design:** [`docs/designs/agent-communication.md`](https://github.com/bradhoekstra/kube-agents/blob/feat/mvp/docs/designs/agent-communication.md) (branch `feat/mvp`).

This note records where the current implementation on this branch **intentionally differs** from that design. The divergence was accepted knowingly for the MVP; this document is the checklist for realigning later.

## Front-door split (Chat Agent)

The harness now has a dedicated **front door**. The `default` Hermes profile is a thin **Chat Agent** (`agents/chat/`) that receives all chat ingress, discovers the available specialist agents and their responsibilities (via the `router` MCP tools `list_agents` / `ask_agent`), delegates the request, and relays the response. Today's Platform Agent is demoted to a named `platform` profile scaffolded at pod startup (`deploy/shared/docker-entrypoint.sh` + `scripts/profile_scaffold.py`) from `agents/platform/`. The pod/Deployment/CR names are unchanged.

Coordination rule: the Chat Agent is **exempt** from the pointer-only rule — as the conversational relay it passes full context to specialists and relays their real responses. The pointer-only rule still governs all specialist-to-specialist (Platform ↔ Cluster) coordination. This front-door split is **not** part of the reference design doc; it is a deliberate addition for cleaner role separation and is tracked here.

## What already aligns

- **Persona split & topology:** a Chat Agent front door (`default` profile) routes to a Platform Agent (`platform` profile) and per-cluster Cluster Agents, all co-located in the same pod on one PVC, each scaffolded from its template (`agents/chat/`, `agents/platform/`, `agents/cluster/`).
- **No specialist-to-specialist prompting:** the Platform and Cluster specialists never call each other directly; they coordinate only through shared state (the Chat Agent front door is the sole, deliberate exception).
- **Read-only / declarative posture:** the Cluster Agent diagnoses and proposes; the Platform Agent owns the GitOps write path (`submit-suggestion`). No imperative cluster mutation.
- **Co-located MVP on one shared PVC.**

## Where it diverges

| Design doc                                                                                                                                                                                                                                              | This implementation                                                                                                                                             |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Primary channel:** cluster→platform **continuous structured handover** — typed records (`health`, `utilization`, `upgrade_readiness`, `drift`, `inventory`) at `/opt/data/fleet/clusters/<cluster>/<location>/<type>.json`, produced by cluster cron. | **Implemented** — `write_handover` tool (gated to cluster profiles) writes atomic, identity-scoped, TTL-stamped records; the platform reads them with plain file tools honoring `expires_at`. **Producer trigger is platform-cron fan-out** (`fleet-status-refresh` invokes each cluster to publish), not cluster self-cron — see note.                                                                                                  |
| **Delegation:** the native **Hermes kanban board** (`kanban_create`/`kanban_complete`, dispatcher auto-spawns `hermes -p <cluster> chat -q …`, DAG fan-in, chat transparency).                                                                          | **Implemented** — delegation runs on the native Hermes kanban board (`kanban_create`/`kanban_complete`, dispatcher auto-spawns the assigned cluster profile, DAG fan-in, chat transparency). `worklog.py` and the `cluster_agent_profile.py invoke` path were removed. |
| **Constrain writes, free reads:** schema-enforcing `write_handover` **tool** for writes; plain file reads for consumption.                                                                                                                              | Handover: satisfied (tool writes, plain reads). Delegation: kanban tools write cards; readers read them. `worklog.py` (which was used for both) is removed.      |
| **Ownership by identity:** `cluster`/`location` derived from the writer's **profile identity**, never caller arguments.                                                                                                                                 | Handover derives identity from profile config. Delegation `assignee` = the cluster profile name. (`worklog.py`'s arg-based identity is gone.)                    |
| **Atomic writes:** temp + `fsync` + `os.replace` on every write.                                                                                                                                                                                        | Handover uses temp+fsync+`os.replace`; kanban uses a WAL SQLite store. (`worklog.py`'s non-atomic `write_text` is gone.)                                         |
| **§2.8:** deliberately **no pluggable backend interface** while co-located; the single write helper is the only migration seam.                                                                                                                         | Resolved — `worklog.py` and its pluggable backend removed. Handover keeps a single write helper.                                                                |
| Cluster subagent keeps its **own `multiuser_memory`** and its own cron.                                                                                                                                                                                 | Cluster profile has memory **disabled** and no cron.                                                                                                            |

## Realignment checklist (for a follow-up)

1. ✅ **Done** — the **primary handover channel**: a `write_handover` tool (toolset gated to cluster profiles) writing atomic, identity-scoped, TTL-stamped records under `/opt/data/fleet/clusters/<cluster>/<location>/<type>.json`; the Platform Agent reads them with plain file tools honoring `expires_at`. (`agents/platform/plugins/handover/`.)
2. ✅ **Done (via platform-cron fan-out, not cluster self-cron)** — producers for `health` + `utilization`: the platform `fleet-status-refresh` cron job (`agents/platform/scripts/fleet_status_refresh.py`) invokes each transient cluster agent one-shot to publish via `write_handover` (the `publish-status` cluster skill). This adapts the design's "cluster cron producers" to Brad's transient-cluster-agent model; persistent per-cluster gateways with self-cron remain a later option.
3. ✅ **Done** — delegation moved to the **Hermes kanban board** (`kanban_create`/`kanban_complete` + dispatcher auto-spawn, fan-out/fan-in, chat transparency). `worklog.py` and the `cluster_agent_profile.py invoke` path removed; a `name` subcommand resolves the kanban `assignee`. Validation-then-declare CUJ in the `workload-rebalancing` skill. (`agents/platform/skills/{cluster-agent-lifecycle,workload-rebalancing}`.)
4. ✅ **Done** — the pluggable backend abstraction is gone (deleted with `worklog.py`); handover keeps a single write helper.
5. ✅ **N/A / Done** — `worklog.py`'s arg-based identity is removed; handover already derives `cluster`/`location` from profile identity, and kanban `assignee` is the profile name.
6. Enable **`multiuser_memory`** in the cluster profile. *(still open)*

## Status

The design is now substantially realigned: the **handover** channel (#1, #2) and **kanban delegation** (#3, #4, #5) are implemented. The only remaining open item is **#6** — enabling `multiuser_memory` on cluster profiles for per-cluster continuity across tasks. `worklog.py` has been removed; delegation runs on the shared kanban board and continuous status flows through the file-based handover channel.
