---
title: Resource labels
description: The app.kubernetes.io labels stamped on every Kubernetes object kube-agents installs, and how to select on them.
sidebar:
  order: 8
---

Everything kube-agents installs into a cluster carries the
[Kubernetes recommended labels](https://kubernetes.io/docs/concepts/overview/working-with-objects/common-labels/),
so the project's whole footprint is selectable in one query:

```bash
kubectl get all,configmap,pvc,serviceaccount,secret -A \
  -l app.kubernetes.io/part-of=kube-agents
```

## The label contract

Four of the six recommended labels are set. `component` and `version` are deliberately absent:
there is no build-time version to report, and image references may carry a digest whose `@` and
`:` are not legal in a label value.

| Source                                                  | `name`                 | `instance`                    | `part-of`     | `managed-by`               |
| ------------------------------------------------------- | ---------------------- | ----------------------------- | ------------- | -------------------------- |
| PlatformAgent controller output (per CR)                | `platform-agent`       | `<namespace>-<agent name>`    | `kube-agents` | `platformagent-controller` |
| Operator install (`k8s-operator/config/`)               | `kube-agents-operator` | `kube-agents-operator`        | `kube-agents` | `kustomize`                |
| LiteLLM integration                                     | `litellm`              | `litellm`                     | `kube-agents` | `kustomize`                |
| GitHub token minter                                     | `github-token-minter`  | `github-token-minter`         | `kube-agents` | `kustomize`                |
| Inference replay                                        | `inference-replay`     | `inference-replay`            | `kube-agents` | `kustomize`                |
| Provisioned Secrets (`provision_07_gcp_k8s_secrets.sh`) | `platform-agent`       | `${NAMESPACE}-platform-agent` | `kube-agents` | `provisioner`              |

`instance` carries the namespace for controller-created objects because the controller also
writes cluster-scoped ClusterRoles and ClusterRoleBindings, where a bare CR name is ambiguous
between two agents of the same name in different namespaces. Nothing bounds a PlatformAgent name
to a length that fits a label value, so the joined value is truncated to 63 characters. The
singleton installs use `instance == name`; there is only one per cluster.

`managed-by` names the thing that writes the object, so you can tell an object the operator
reconciles from one a human applied with `kustomize` or the provisioner created.

## What is not labelled

Objects the Platform Agent authors at runtime through its skills are user workloads, not project
infrastructure, and do not get these labels. Those carry the
`kubeagents.x-k8s.io/requested-by` annotation instead — see
[User attribution](/kube-agents/reference/attribution/) for why per-requester identity belongs in
an annotation rather than a label.

## Upgrade notes

Two constraints shape how the labels are applied, and both matter when upgrading an existing
install:

- **Selectors are never touched.** Deployment and StatefulSet `spec.selector` is immutable, so
  adding a label there would make the API server reject the update on every existing install.
  The controller keeps `app: <name>-gateway` as the sole selector key, and every kustomization
  sets `includeSelectors: false`. Do not "tidy" this by adding the recommended labels to a
  selector.
- **Pod templates do get the labels**, which changes the pod template hash and therefore causes
  **one rollout** of the agent Deployment the first time you upgrade to a version that includes
  them. This is expected and happens once.

PersistentVolumeClaims are created once and never updated by the controller, so claims that
existed before the upgrade stay unlabelled until they are recreated. They will not appear in the
`-l app.kubernetes.io/part-of=kube-agents` query above.

## Query recipes

Everything one PlatformAgent owns, including its cluster-scoped RBAC:

```bash
AGENT_INSTANCE=kubeagents-system-platform-agent
kubectl get all,configmap,pvc,serviceaccount,secret -A -l "app.kubernetes.io/instance=$AGENT_INSTANCE"
kubectl get clusterrole,clusterrolebinding -l "app.kubernetes.io/instance=$AGENT_INSTANCE"
```

Only what the operator reconciles, excluding anything applied by hand:

```bash
kubectl get all -A -l app.kubernetes.io/managed-by=platformagent-controller
```

Just the integrations:

```bash
kubectl get all -A \
  -l 'app.kubernetes.io/part-of=kube-agents,app.kubernetes.io/name in (litellm,github-token-minter,inference-replay)'
```

Cluster-scoped RBAC the project owns — the objects most easily orphaned, since a namespaced
PlatformAgent cannot own a cluster-scoped resource through an owner reference:

```bash
kubectl get clusterrole,clusterrolebinding -l app.kubernetes.io/part-of=kube-agents
```

## Where to go next

- [User attribution](/kube-agents/reference/attribution/) — annotations that connect an object to
  the human who asked for it.
- [Security & IAM](/kube-agents/reference/security-and-iam/) — what the agent is and is not
  permitted to do.
