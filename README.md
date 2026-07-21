# kube-agents: The Kubernetes Agentic Harness

The k8s agentic harness will fundamentally redefine the DevOps presentation layer by replacing traditional interfaces like kubectl, gcloud, and the Google Cloud console with intelligent, autonomous agents. By replacing the static, imperative nature of the traditional Kubernetes presentation layer with an autonomous agentic harness, we transition from reactive manual management to proactive, intent-driven operations.

## Key Components

### 1. Platform Agent (`platform`)

The master custodian and agent architect configured with an architectural persona (`SOUL.md`). It manages multi-tenancy governance, RBAC boundaries, and GKE infrastructure lifecycle. It is the operator-deployed gateway pod and the single chat entrypoint into the harness.

### 2. Cluster Agent (`agents/cluster/`)

A focused, single-cluster SRE persona for read-only runtime operations and deep workload debugging. It is **not** deployed by the operator or represented by a CRD. Instead, it is a **Hermes profile** — an isolated agent instance with its own persona, scoped toolset, and home directory — that the Platform Agent **creates dynamically inside its own pod**, one per managed GKE cluster, persisting on the data PVC until that cluster is deleted.

`agents/cluster/` is the profile _template_ baked into the image; the Platform Agent scaffolds it into a live, per-cluster profile (with a pinned `KUBECONFIG`) and delegates debugging to it. Cluster Agents are strictly read-only. Separation between the two personas is enforced by persona, toolset, and scoped kubeconfig — they share the pod's identity.

**Coordination is through shared state, never direct context.** The two personas never pass task details or results to each other in a message. Instead they exchange a **work item** in a shared store (`scripts/worklog.py` — a pluggable interface with a local-file backend on the shared PVC by default, and a documented GitHub-issue/PR backend seam). The Platform Agent writes the request to a work item and invokes the Cluster Agent with only a pointer — _"Please work on work item `<id>`"_; the Cluster Agent reads the request, investigates, writes its RCA and proposed patch back to the work item, and replies with only an acknowledgement. The Platform Agent then reads the work item and owns any GitOps write (`submit-suggestion`).

---

## Harness Integration & Setup

This workspace contains agent configurations, personas, and skills that can be imported into various pattern gateways or multi-agent platforms (such as CrewAI, Microsoft AutoGen, or LangGraph).

Multi-agent platforms and orchestrators can use the [INSTALL.md](INSTALL.md) guide to set up the Platform Agent. To delegate this task to your platform, clone this repository to the workspace of the default agent of multi-agent platform and ask it:

> "Using `kube-agents/INSTALL.md` provision k8s agentic harness and create platform agent"

### 1. Declarative Registration (YAML/JSON)

For platforms or gateways that load agents declaratively, add the Platform Agent workspace path to your profile or orchestrator configuration:

```yaml
agents:
  - id: platform
    workspace: ./agents/platform
```

### 2. Imperative CLI Registration

For hosts supporting CLI-driven imports, register the Platform Agent directory from the repository root. For example (using a generic gateway CLI or reference host):

```bash
# Register platform agent
gateway-cli agents add platform --workspace ./agents/platform --non-interactive
```

For more details on routing policies, proof gates, and showcasing scenarios, see the [Kubernetes Multi-Agent Integration Guide](docs/m1-demos.md).

## Disclaimer

This is not an officially supported Google product.

This project is not eligible for the Google Open Source Software Vulnerability Rewards Program.
