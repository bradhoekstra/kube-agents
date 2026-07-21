# Glossary of Agentic Terms

This glossary defines key terms and concepts related to the Kubernetes Agentic Harness (`kube-agents`) and the broader agentic ecosystem.

---

## Agent Platforms for Kubernetes

### Agent Substrate

- **Source:** [agent-substrate/substrate](https://github.com/agent-substrate/substrate)
- **Definition:** An open-source, Kubernetes-native platform specifically engineered to orchestrate, scale, and manage AI agent workloads. It introduces abstractions like Workers (managed compute pools in Kubernetes Pods) and Actors (individual agent instances running inside Pods) to facilitate high-efficiency multiplexing and stateful execution sandboxes.

### Agent Sandbox

- **Source:** [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
- **Definition:** An open-source Kubernetes SIG Apps project designed to manage isolated, stateful, singleton workloads. It provides low-latency warm pod pools, stable identity, persistence, and secure sandboxed execution environments (e.g., via gVisor or Kata Containers) suitable for running untrusted LLM-generated code.

---

## Agent Runtimes & Frameworks

### Agent Executor (AX)

- **Source:** [google/ax](https://github.com/google/ax)
- **Definition:** An open-source distributed agent runtime designed to manage the execution lifecycle of AI agents. It provides durable execution capabilities (including pausing, resuming, snapshotting, and replaying agent states) to ensure agent workloads remain operational and recover automatically from transient infrastructure failures.

### Kubernetes Agentic Harness (`kube-agents`)

- **Definition:** An agentic system designed to replace traditional Kubernetes/GKE interfaces (e.g., `kubectl`, `gcloud`, Google Cloud Console) with intelligent, intent-driven autonomous platform agents.

---

## Agents in `kube-agents`

### Chat Agent (`agents/chat/`, the `default` profile)

- **Role:** The single conversational front door to the harness, and the delegator/router.
- **Scope:** The `default` [Hermes Profile](#hermes-profile) — the only profile that receives chat ingress. It analyzes each message, discovers which specialist agents exist and what each is responsible for (via the `router` MCP tools `list_agents` / `ask_agent`), delegates the request to the right specialist, and relays the response for the best user experience. It holds **no** infrastructure tools of its own (no GKE, provisioning, or GitOps write path) — the front door can route, not mutate. Unlike the specialists, it is **exempt** from the pointer-only [Work Item](#work-item-shared-state) rule: it passes full context to specialists and relays their real responses.

### Platform Agent (`platform`)

- **Role:** Architectural custodian and fleet orchestrator; the privileged doer behind the Chat Agent.
- **Scope:** A named [Hermes Profile](#hermes-profile) (`platform`) scaffolded at pod startup from the `agents/platform/` template. Configured with an architectural persona (`SOUL.md`), it manages multi-tenancy boundaries, fleet-wide governance, and RBAC isolation, and owns the GitOps write path. It no longer receives chat directly — the Chat Agent routes work to it — and it delegates single-cluster runtime debugging to Cluster Agents (pointer-only). It runs in the operator-deployed gateway pod and shares that pod's identity.

### Cluster Agent (`agents/cluster/`)

- **Role:** Single-cluster SRE operator for read-only runtime operations and workload root-cause analysis.
- **Scope:** A per-cluster [Hermes Profile](#hermes-profile) that the Platform Agent creates dynamically inside its own pod (one per managed GKE cluster, persistent until the cluster is deleted). It is scoped to one cluster by persona, toolset, and a pinned `KUBECONFIG`, and shares the Platform Agent pod's identity. It is strictly read-only: it returns an RCA and any proposed manifest patch to the Platform Agent rather than mutating the cluster or opening Pull Requests. It is not represented by the operator or a CRD.

---

## Hermes Runtime Concepts

### Hermes Profile

- **Definition:** A native Hermes feature (`hermes profile` / `hermes -p <name>`) that provides multiple isolated Hermes instances, each with its own config, sessions, skills, and home directory. Multiple profiles run concurrently within a single gateway process/pod. In `kube-agents`, the `default` profile is the [Chat Agent](#chat-agent-agentschat-the-default-profile) (front door), the `platform` profile is the [Platform Agent](#platform-agent-platform) (scaffolded at startup from `agents/platform/`), and each [Cluster Agent](#cluster-agent-agentscluster) is a profile scaffolded at runtime from `agents/cluster/`. Executable scripts are shared across profiles at `$HERMES_HOME/scripts`; persona, config, and skills are per-profile.

---

## Coordination

### Work Item (Shared State)

- **Definition:** The unit of coordination between **specialist** personas (Platform ↔ Cluster). Specialists never pass task context or results directly to one another; they exchange a work item in a shared store (`agents/platform/scripts/worklog.py`) — a pluggable interface with a local-file backend (on the shared PVC) as the default and a documented GitHub-issue/PR backend seam. A requester writes the request to a work item and invokes the worker with only a pointer ("Please work on work item `<id>`"); the worker reads the request, does the work, and writes its findings back to the same work item. This keeps invocation messages to imperative pointers and makes the coordination auditable and backend-agnostic.
- **Exception — the Chat Agent:** The [Chat Agent](#chat-agent-agentschat-the-default-profile) is deliberately exempt from the pointer-only rule. As the conversational relay it passes full context to a specialist (via the `router` `ask_agent` tool) and relays the specialist's real response back to the user. The pointer-only rule still governs all specialist-to-specialist coordination.
