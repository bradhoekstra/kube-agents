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

### Platform Agent (`platform`)

- **Role:** Architectural custodian and agent orchestrator.
- **Scope:** Configured with an architectural persona (`SOUL.md`). It manages multi-tenancy boundaries, fleet-wide governance, and RBAC isolation. It is the operator-deployed gateway pod and owns the GitOps write path; it delegates single-cluster runtime debugging to Cluster Agents.

### Cluster Agent (`agents/cluster/`)

- **Role:** Single-cluster SRE operator for read-only runtime operations and workload root-cause analysis.
- **Scope:** A per-cluster [Hermes Profile](#hermes-profile) that the Platform Agent creates dynamically inside its own pod (one per managed GKE cluster, persistent until the cluster is deleted). It is scoped to one cluster by persona, toolset, and a pinned `KUBECONFIG`, and shares the Platform Agent pod's identity. It is strictly read-only: it returns an RCA and any proposed manifest patch to the Platform Agent rather than mutating the cluster or opening Pull Requests. It is not represented by the operator or a CRD.

---

## Hermes Runtime Concepts

### Hermes Profile

- **Definition:** A native Hermes feature (`hermes profile` / `hermes -p <name>`) that provides multiple isolated Hermes instances, each with its own config, sessions, skills, and home directory. Multiple profiles run concurrently within a single gateway process/pod. In `kube-agents`, each [Cluster Agent](#cluster-agent-agentscluster) is materialized as a Hermes profile scaffolded from the `agents/cluster/` template.

---

## Coordination

### Work Item (Shared State)

- **Definition:** The unit of coordination between personas. Personas never pass task context or results directly to one another; they exchange a work item in a shared store (`agents/platform/scripts/worklog.py`) — a pluggable interface with a local-file backend (on the shared PVC) as the default and a documented GitHub-issue/PR backend seam. A requester writes the request to a work item and invokes the worker with only a pointer ("Please work on work item `<id>`"); the worker reads the request, does the work, and writes its findings back to the same work item. This keeps invocation messages to imperative pointers and makes the coordination auditable and backend-agnostic.
