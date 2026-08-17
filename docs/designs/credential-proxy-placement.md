# Credential Proxy Placement

## Summary

The credential proxy exists so that API keys and tokens are never readable by the
agent. The agent may **use** a credential — run `kubectl`, push a branch, post to
Chat — but it may not **hold** one. The permissions behind those credentials are
scoped to what the agent is allowed to do anyway, so mediated use is not the thing
being defended; possession is.

Today the proxy runs as a sidecar in the agent's own pod, and for GCP credentials
that arrangement cannot deliver the property. **Workload Identity is scoped to the
pod, not the container.** The proxy obtains its GCP credentials from the node
metadata server, and so can any other process in the pod — including the agent's
shell. Verified on a live install: a shell in the agent container reads the pod's
service-account identity and mints a full OAuth access token in one `curl`, with no
shim involved.

This document proposes moving the proxy to a **dedicated Deployment per
PlatformAgent**, following the shape `github-token-minter` already uses in this
repository, and removing the Workload Identity binding from the agent pod entirely.

**Status:** proposed, not implemented. Tracked as Part C of
[#737](https://github.com/gke-labs/kube-agents/issues/737). Part C is independent of
the shell-sandboxing work in
[`agent-shell-sandboxing.md`](agent-shell-sandboxing.md) and can land first.

| Layer                     | Where it lives                                                                                                                                                                                         |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| The proxy itself          | [`agents/platform/scripts/credential_proxy.py`](../../agents/platform/scripts/credential_proxy.py)                                                                                                     |
| Its loopback front door   | [`deploy/shared/envoy-credential-proxy.yaml`](../../deploy/shared/envoy-credential-proxy.yaml)                                                                                                         |
| The client the shims run  | [`agents/platform/scripts/credential_proxy_client.py`](../../agents/platform/scripts/credential_proxy_client.py)                                                                                       |
| The shims themselves      | [`deploy/docker/Dockerfile`](../../deploy/docker/Dockerfile) — symlinks under `/opt/credential-proxy/bin`                                                                                              |
| Its command policy        | `/etc/credential-proxy/policy.json`, from `CREDENTIAL_PROXY_POLICY`                                                                                                                                    |
| How the operator wires it | `buildCredentialProxyEnv` in [`platformagent_manifests.go`](../../k8s-operator/internal/controller/platformagent_manifests.go)                                                                         |
| The pattern to copy       | [`k8s-operator/config/integrations/github/`](../../k8s-operator/config/integrations/github/kustomization.yaml), documented at [`deploy/token-minter`](../site/src/content/docs/deploy/token-minter.md) |
| The isolation contract    | [`docs/credential-isolation-design.md`](../credential-isolation-design.md)                                                                                                                             |

## How to read this document

| Section                       | What it gives you                                                  |
| ----------------------------- | ------------------------------------------------------------------ |
| [Background](#background)     | what the proxy protects and how it is wired today                  |
| [The problem](#the-problem)   | why a sidecar cannot hold the line, and why it matters             |
| [The decision](#the-decision) | where the proxy should run, and what was rejected                  |
| [The design](#the-design)     | the workload, the five roles inside it, and everything that breaks |

---

## Background

### What the proxy is for

`kubectl`, `gcloud`, `gh`, and `git` are not real binaries in the agent image. They
are symlinks to `credential-proxy-exec`, a client that forwards the argv to the proxy
and returns its output. The agent composes a command; something else runs it, holding
the credential.

That gives three properties: the credential material never enters the agent's
address space or filesystem, every credentialed command passes one chokepoint that
can log and refuse it, and the refusal logic lives outside anything the agent can
edit.

### How it is wired today

Two listeners, and only one of them authenticates.

The **credential exec path** — the one the shims use — is fronted by Envoy bound to
`127.0.0.1:8765`, forwarding over a Unix socket at
`/var/run/credential-proxy/backend.sock` to the Python handler. It has **no caller
authentication at all**. Loopback plus the socket is the entire access control, which
is sound as long as "in the pod" and "authorised" are the same set.

The **PlatformAgent API proxy** on `0.0.0.0:8643` is authenticated: it requires
`API_SERVER_EXTERNAL_KEY` and the process refuses to start without it. Note also that
the Python listener's own `--host` already defaults to `0.0.0.0` — the loopback
confinement comes from Envoy, not from the application.

Two things already work in the design's favour and should not be relitigated. The
agent pod sets `automountServiceAccountToken: false`, so the Kubernetes service
account token is not a bypass; and the proxy's `policy.json` refuses commands that
would author a kubeconfig, which is what stops the agent redirecting `kubectl` at an
endpoint of its choosing.

---

## The problem

### Workload Identity is scoped to the pod

The proxy gets its GCP credentials from `google.auth.default()`, which on GKE
resolves to the node metadata server at `169.254.169.254`. Every container in the pod
shares the pod's network namespace, and Workload Identity binds a **Kubernetes
service account to a Google service account** — a pod-level relationship. The
metadata server has no way to tell one container in the pod from another, and no
Kubernetes mechanism exists to give it one.

So the agent's shell asks the metadata server the same question the proxy asks, and
gets the same answer. Verified on a live install by reading the identity endpoint and
minting a token from the agent container. The operator-managed NetworkPolicy
explicitly permits egress to `169.254.169.254` on ports 80 and 8080, so the path is
allowed rather than merely unblocked.

This is not fixable inside the pod. Distinct UIDs, a separate PID namespace, a
read-only root filesystem, dropped capabilities, and seccomp all constrain what one
container can read from another's **processes and files**. The metadata server is
neither: it is a network endpoint that both containers can route to, and it hands the
same identity to whoever asks.

### Why possession is worse than use

The credentials are scoped to what the agent is permitted to do, so a natural
response is that a lifted token grants nothing new. Three things separate the two.

**It leaves the chokepoint.** Every property the proxy provides — the command policy,
the audit trail, the workspace check — is a property of _going through the proxy_. A
token used directly has none of them, and an action taken with it appears in cloud
audit logs as the service account with no agent-side record of who composed it.

**It carries the whole scope.** The proxy permits a subset of what the service
account can do; `policy.json` is a filter over the credential, not a description of
it. A lifted token carries the service account's full IAM grant.

**It leaves the cluster.** A bearer token works from anywhere until it expires, and
the ability to mint means the ability to keep minting. Every other network control in
this design assumes the credential stays inside the boundary.

### What this means for the sidecar

For Slack tokens and the `API_SERVER_EXTERNAL_KEY`, which arrive as environment
variables on the proxy container, a sidecar is adequate once the UID and PID
namespaces are separated — that is what
[#720](https://github.com/gke-labs/kube-agents/pull/720) does, and it closes the
`/proc/<pid>/environ` read.

For anything obtained through ADC, no sidecar arrangement works. Since GCP
credentials are the ones the agent uses most, the sidecar is the wrong shape for the
component's stated purpose.

---

## The decision

**A dedicated Deployment per PlatformAgent**, with its own ServiceAccount, reached
over a ClusterIP Service rather than loopback. The `iam.gke.io/gcp-service-account`
annotation moves to the new ServiceAccount, and the agent pod loses it.

That last clause is the whole point. Moving the proxy while the agent pod keeps its
Workload Identity binding changes nothing — the shell still mints the same token. The
success criterion is not "the proxy is elsewhere" but **"the agent pod has no cloud
identity worth stealing."**

### The precedent already in the tree

`github-token-minter` is this exact pattern, in production: a separate Deployment,
its own ServiceAccount, a ClusterIP Service the agent reaches at `TOKEN_BROKER_URL`,
and a NetworkPolicy restricting ingress to the agent pods. The credential proxy
should be the second instance of that shape rather than a new one, and the two should
converge over time.

### Rejected alternatives

**Sidecar with hardening (status quo plus #720).** Fails for ADC credentials, per
above. #720 remains worth landing on its own merits — it closes the environment-
variable read, which this proposal also closes but later.

**Bind to `0.0.0.0` and add a NetworkPolicy, keeping the sidecar.** Does not address
Workload Identity at all. It also inverts the threat model: a policy admitting the
agent grants precisely the caller in question, where loopback at least excluded
everything else. As a swap for loopback with no other change it is a downgrade.

**A separate namespace.** Better RBAC isolation for the Secret, but cross-namespace
`ownerReferences` are invalid, so the operator loses garbage collection and has to
manage lifecycle by hand. Not worth it at this step; revisit if the proxy is ever
shared across agents.

**A node-level DaemonSet.** Makes scheduling a security property and widens the blast
radius to every agent on the node.

**One proxy for the whole fleet.** A single credential set behind every agent. The
operator already reconciles per-agent resources and the Secret is already per-agent,
so per-agent is the natural grain.

---

## The design

### The unit that moves is the whole container

"The credential proxy" is five roles fused into one container, and they do not all
want the same placement:

| Role                                 | Credential held                      | Placement constraint                                  |
| ------------------------------------ | ------------------------------------ | ----------------------------------------------------- |
| Credential exec broker (Envoy → UDS) | GCP via ADC, kubeconfig              | away from the agent; stateless                        |
| PlatformAgent API proxy (`:8643`)    | `API_SERVER_EXTERNAL_KEY`            | already authenticated and network-exposed             |
| k8s-event-watcher                    | `KUBECONFIG`, `SESSION_KV_API_KEY`   | posts to the Session KV over **pod loopback**         |
| Slack relay                          | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | `SocketModeClient` WebSocket — **stateful singleton** |
| Google Chat relay                    | GCP via ADC (Pub/Sub)                | inbound pump; must deliver into the gateway           |

The relays are the reason the whole container moves rather than just the broker. The
Google Chat relay authenticates to Pub/Sub through ADC. Leave it in the agent pod and
that pod still needs Workload Identity, and the exfiltration path survives intact.
**Everything using ADC has to leave together or none of it does.**

### What the agent pod is left holding

After the move, and with `automountServiceAccountToken: false` already in place, the
agent pod's credential surface is `SESSION_KV_API_KEY` and `SESSION_KV_SALT` — and
Part A of #737 removes those too by putting the Session KV server behind its own
interface.

### Caller authentication

The exec path on 8765 becomes network-reachable, so it needs a bearer token mirroring
`API_SERVER_EXTERNAL_KEY`.

Be precise about what that buys. It is a **multi-tenancy control**: it stops another
workload in the cluster borrowing the agent's credentials. It is **not** an
agent-containment control, because the agent's shell legitimately holds the token —
the shell is the caller the proxy exists to serve. Nothing in this design should be
described as protecting against the agent by means of the bearer token.

The NetworkPolicy is the same kind of control, with the same caveat, plus one more:
NetworkPolicy is **not enforced** on the reference install
(`addonsConfig.networkPolicyConfig.disabled: true`, no Dataplane V2). Ship it as
defence in depth; do not let any argument here rest on it.

### The relay path reverses

`GOOGLE_CHAT_RELAY_URL` and `SLACK_RELAY_URL` currently point at `127.0.0.1` because
the relays and the gateway share a pod. Afterwards they point from the proxy pod _to_
the gateway's Service — the traffic direction reverses, and the gateway needs an
ingress rule it did not need before. `CREDENTIAL_PROXY_URL` moves the other way, from
loopback to the proxy Service.

### The workspace check has to be re-based

`credential_proxy_client.py` posts `"cwd": os.getcwd()` on every request, and the
proxy refuses any cwd outside `CREDENTIAL_PROXY_WORKSPACE_ROOT`. That assumes proxy
and caller see the same filesystem, which stops being true the moment they are in
different pods.

Worth stating plainly while re-basing it: the `cwd` is **self-reported by the
caller**. It is a guardrail against the agent wandering out of its workspace by
accident, not a control against one that intends to. Anything relying on it as a
security boundary should stop.

### One replica

The Slack relay holds a socket-mode WebSocket. Two replicas means two connections and
duplicate event delivery, so the new Deployment runs one replica until the relays are
split out from the broker. That is not a regression — the sidecar is one replica per
agent today — but it does mean the credential broker cannot be made highly available
while it is fused with an inbound event pump. Splitting them is the obvious follow-up
and is deliberately out of scope here.

---

## What is still unproven

- **The GSA's actual scope.** The argument that a lifted token is worse than mediated
  use is strongest when the service account is broadly granted. Nobody has enumerated
  what `kubeagents-platform-gsa` can do, and that enumeration should accompany the
  implementation — it also bounds how urgent this is.
- **Whether the event-watcher can reach the Session KV across pods.** It posts over
  loopback today. Part A of #737 puts the KV server behind a Service, which resolves
  this, but the two changes are coupled and the ordering has not been decided.
- **Latency.** Every credentialed command grows a network hop. Expected to be
  irrelevant against process spawn and API round-trip, but unmeasured.
- **Whether `github-token-minter` and this should be one workload.** They are the same
  pattern serving different credentials. Merging them is attractive and out of scope.

## Related work

- [#720](https://github.com/gke-labs/kube-agents/pull/720) — unshares the sandbox and
  proxy UID and PID namespace, and adds `splitCredentialBrokerPod`. Moves in this
  direction and closes the environment-variable read; does not close the
  metadata-server path.
- [#723](https://github.com/gke-labs/kube-agents/pull/723),
  [#724](https://github.com/gke-labs/kube-agents/pull/724),
  [#725](https://github.com/gke-labs/kube-agents/pull/725) — proxy hardening:
  allowlists, native sidecar ordering, the GitHub write path. Complementary.
- [#674](https://github.com/gke-labs/kube-agents/pull/674) — read-only root filesystem
  on the agent containers. Complementary.
- [`agent-shell-sandboxing.md`](agent-shell-sandboxing.md) — the other half of #737.
  Independent of this document, and slower to land.

_Drafted with the help of Claude._
