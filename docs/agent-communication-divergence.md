# Agent Communication — Divergence from the Design Doc

**Reference design:** [`docs/designs/agent-communication.md`](https://github.com/bradhoekstra/kube-agents/blob/feat/mvp/docs/designs/agent-communication.md) (branch `feat/mvp`).

This note records where the current two-persona implementation on this branch **intentionally differs** from that design. The divergence was accepted knowingly for the MVP; this document is the checklist for realigning later.

## What already aligns

- **Persona split & topology:** Platform Agent = the `default` Hermes profile (user-facing); Cluster Agent = one Hermes profile **per managed cluster**, co-located in the same pod, scaffolded from `agents/cluster/`.
- **No agent-to-agent prompting:** personas never call each other directly; they coordinate only through shared state.
- **Read-only / declarative posture:** the Cluster Agent diagnoses and proposes; the Platform Agent owns the GitOps write path (`submit-suggestion`). No imperative cluster mutation.
- **Co-located MVP on one shared PVC.**

## Where it diverges

| Design doc                                                                                                                                                                                                                                              | This implementation                                                                                                                                             |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Primary channel:** cluster→platform **continuous structured handover** — typed records (`health`, `utilization`, `upgrade_readiness`, `drift`, `inventory`) at `/opt/data/fleet/clusters/<cluster>/<location>/<type>.json`, produced by cluster cron. | **Not implemented.** Only a request/response task flow exists.                                                                                                  |
| **Delegation:** the native **Hermes kanban board** (`kanban_create`/`kanban_complete`, dispatcher auto-spawns `hermes -p <cluster> chat -q …`, DAG fan-in, chat transparency).                                                                          | Bespoke shared store `agents/platform/scripts/worklog.py` + `cluster_agent_profile.py invoke` calling `hermes -p <profile> -z "Please work on work item <id>"`. |
| **Constrain writes, free reads:** schema-enforcing `write_handover` **tool** for writes; plain file reads for consumption.                                                                                                                              | `worklog.py` CLI is used for **both** read and write.                                                                                                           |
| **Ownership by identity:** `cluster`/`location` derived from the writer's **profile identity**, never caller arguments.                                                                                                                                 | `worklog.py` takes `project`/`cluster`/`location` as **arguments**.                                                                                             |
| **Atomic writes:** temp + `fsync` + `os.replace` on every write.                                                                                                                                                                                        | `worklog.py` local backend uses a plain `write_text` (not atomic).                                                                                              |
| **§2.8:** deliberately **no pluggable backend interface** while co-located; the single write helper is the only migration seam.                                                                                                                         | `worklog.py` ships a **pluggable backend** abstraction (local-file default + a guarded GitHub seam), which §2.8 explicitly advises against.                     |
| Cluster subagent keeps its **own `multiuser_memory`** and its own cron.                                                                                                                                                                                 | Cluster profile has memory **disabled** and no cron.                                                                                                            |

## Realignment checklist (for a follow-up)

1. Add the **primary handover channel**: a `write_handover` tool (toolset gated to cluster profiles) writing atomic, identity-scoped, TTL-stamped records under `/opt/data/fleet/clusters/<cluster>/<location>/<type>.json`; the Platform Agent reads them with plain file tools honoring `expires_at`.
2. Add **cluster cron producers** for the record types, starting with `health` and `utilization`.
3. Move **delegation to the Hermes kanban board** (`kanban_create`/`kanban_complete` + dispatcher), replacing `worklog.py` and the `cluster_agent_profile.py invoke` path; keep fan-out/fan-in and chat transparency.
4. Remove the pluggable backend abstraction from the write path in favor of a **single write helper** (the documented migration seam).
5. Derive `cluster`/`location` from **profile identity**, not arguments.
6. Enable **`multiuser_memory`** in the cluster profile.

## Rationale for keeping the current approach for now

The current `worklog.py` request/response store satisfies the core guardrails the design cares about most — no direct agent-to-agent context passing, shared-state coordination, and a read-only/declarative cluster posture — and is fully self-contained and testable in-pod today. The transport (bespoke store vs. kanban) and the missing continuous-handover channel are deferred, tracked by the checklist above.
