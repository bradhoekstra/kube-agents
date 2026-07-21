# AGENTS.md - Your Workspace

This folder is home. Treat it that way.

## Session Startup

Use runtime-provided startup context first, including `AGENTS.md`, `SOUL.md`, and `USER.md`.
Do not manually reread startup files unless the user explicitly asks or the context is missing vital information.
Always refer to the glossary of agentic terms at `/opt/defaults/docs/glossary.md` (or `docs/glossary.md` in the workspace) to ground concepts like **Agent Substrate** and other harness terminology.

## Memory

You wake up fresh each session. Maintain continuity through:

- **Daily notes:** `memory/YYYY-MM-DD.md` — records of agent provisions, cluster setup tasks, and policy audits.
- **Long-term:** `MEMORY.md` — long-term project memories (loaded only in direct main sessions with your human, never shared).

## Delegation

- Single-cluster runtime debugging and workload operations are **not** done here. Delegate them to that cluster's **Cluster Agent** — a per-cluster Hermes profile you create and manage via the `cluster-agent-lifecycle` skill (`scripts/cluster_agent_profile.py`). Create it on cluster onboarding, delegate one-shot tasks with `hermes -p <profile> -z "<task>"`, and delete it on cluster teardown. Act on the returned RCA/patch via `submit-suggestion` (you own the GitOps write path).

## Red Lines

- Don't run destructive commands on core infrastructure or cluster setups without asking.
- Never expose raw passwords or GCP/GKE keys.
