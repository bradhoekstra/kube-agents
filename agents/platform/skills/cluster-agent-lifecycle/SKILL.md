---
name: cluster-agent-lifecycle
description: Create, delegate to, and tear down per-cluster Cluster Agent Hermes profiles. Use whenever a GKE cluster is onboarded or deleted, or whenever a single-cluster runtime debugging/operations task should be delegated to that cluster's Cluster Agent.
---

# Cluster Agent Lifecycle Skill

As the Platform Agent you own the lifecycle of **Cluster Agents**. A Cluster Agent is a Hermes _profile_ — an isolated agent instance with its own persona (`SOUL.md`), scoped toolset, and home directory — that you create dynamically **inside your own pod**, one per managed GKE cluster. It handles read-only runtime operations and deep workload diagnostics on that single cluster, and returns its findings to you.

You never debug tenant workloads directly. You delegate that to the cluster's Cluster Agent and act on what it returns.

The engine for all of this is the helper script `scripts/cluster_agent_profile.py` (resolved at `/opt/data/scripts/cluster_agent_profile.py` at runtime).

## When to create a profile

Create the Cluster Agent profile as part of **cluster onboarding** — immediately after a cluster is successfully provisioned (see `gke-cluster-creator`) or when an existing cluster is first brought under management (see `gke-app-onboarding`).

```bash
python3 /opt/data/scripts/cluster_agent_profile.py create \
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

This scaffolds the profile home on the persistent data PVC, pins a kubeconfig scoped to that cluster, writes the cluster identity into the profile's `USER.md`, and registers the profile. It is **idempotent** — safe to re-run. It prints the profile name.

## How to delegate a debugging / runtime-ops task (shared state only)

For any request that concerns runtime behavior of workloads on a **single, specific** cluster (crash loops, OOMs, scheduling failures, mount errors, connectivity, autoscaling, storage, observability gaps), delegate to that cluster's Cluster Agent instead of investigating yourself.

**Personas never pass context directly.** You coordinate through a shared work item and invoke with a pointer only.

1. **Write the request to shared state** — create a work item assigned to the cluster:

   ```bash
   python3 /opt/data/scripts/worklog.py create \
     --requester platform --assignee cluster \
     --title "<short title>" \
     --request "<full request: namespace/workload, symptom, time window>" \
     --project "<project>" --cluster "<cluster>" --location "<location>"
   ```

   This prints a work item `<id>`. (For a long request, use `--request-file <path>` or `--request-file -` for stdin.)

2. **Invoke with a pointer only** — the Cluster Agent receives nothing but _"Please work on work item `<id>`."_:

   ```bash
   python3 /opt/data/scripts/cluster_agent_profile.py invoke \
     --project "<project>" --cluster "<cluster>" --location "<location>" \
     --work-item "<id>"
   ```

3. **Read results from shared state** — when the call returns, read the work item back; the Cluster Agent has written its RCA and any proposed patch there (its chat reply is only an ack):

   ```bash
   python3 /opt/data/scripts/worklog.py show "<id>"
   ```

## Acting on the result

The Cluster Agent is **read-only** and does not open Pull Requests. After reading the work item:

1. Review the RCA and proposed manifest patch recorded in the work item.
2. If a change is warranted, **you** open (or update) the Pull Request via the `submit-suggestion` skill — you own the GitOps write path. Reconcile against any existing branch/PR for the same workload before creating a new one.
3. Report the outcome to the user as a clean SRE status update.

## When to delete a profile

Delete the Cluster Agent profile as part of **cluster teardown** (see `gke-cluster-lifecycle`), after the cluster itself is removed:

```bash
python3 /opt/data/scripts/cluster_agent_profile.py delete \
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

This deregisters the profile and removes its home directory. Do not delete a profile while its cluster still exists.

## Listing profiles

```bash
python3 /opt/data/scripts/cluster_agent_profile.py list
```

Lists the currently provisioned Cluster Agent profiles (one per managed cluster).
