# Agent Shell Sandboxing

## Summary

The Platform Agent's shell runs in the same container as the Platform Agent. Hermes
supports seven terminal backends; this repository configures none of them, so the
default applies and every `terminal` call is a `bash -c` on the agent's own pod, as
the agent's own user, with the agent's own filesystem. There is no container
boundary, no separate namespace, no seccomp profile, and no cgroup between "the
agent reasons" and "the agent runs a command."

The consequence showed up in an incident: the agent, asked to fix a session-routing
problem, reasoned its way to editing the session database with `sqlite3`, wrote its
own configuration, and restarted itself. Every step was a legitimate shell command.
Nothing was exploited. The design simply allows it.

This document covers both halves of the separation. The shell runs in a **separate
Kubernetes pod** — its own filesystem, its own identity, no credentials — reached over
Hermes' existing `ssh` terminal backend. The **credential proxy runs in that same pod**,
as a second container, because a shell with no credential path is not a shell anybody can
operate a fleet from, and because the proxy cannot hold its own confidentiality property
anywhere else.

That second half follows from the first. The proxy exists so that credentials are never
readable by the agent: the agent may **use** a credential — run `kubectl`, push a branch,
post to Chat — but never hold one. **Workload Identity is scoped to the pod, not the
container**, so a proxy sharing a pod with the agent hands the same GCP identity to
anything else in it. Verified on a live install: a shell in the agent container reads the
pod's service-account identity and mints a full OAuth access token in one `curl`, with no
shim involved. Moving the shell out of the agent pod does not fix that on its own — it
relocates the problem to whichever pod the shell lands in.

What fixes it is removing the identity rather than moving the proxy away from it. The
sandbox pod's ServiceAccount carries no `iam.gke.io/gcp-service-account` annotation, so
the metadata server answers both containers with an unbound `<project>.svc.id.goog`
principal that IAM grants nothing, and the proxy's real identity is an audience-scoped
projected service-account token mounted into the proxy container alone and exchanged
through Workload Identity Federation. **A volumeMount is the one boundary a pod has per
container; the pod IP is not.**

**Status:** the sandbox image ships ([`deploy/sandbox/`](../../deploy/sandbox/)) and the
operator reconciles it behind `harness.experimental.shellSandbox`. Co-locating the proxy
is gated on `spec.security.workloadIdentityFederation` naming a pool as well; without
both, the proxy falls back to a Deployment of its own — reachable, but unable to run
`git`, and unauthenticated on a ClusterIP. Tracked as
[#737](https://github.com/gke-labs/kube-agents/issues/737), whose Part A — putting the
Session KV behind its own interface — is the one piece still unbuilt.

| Layer                          | Where it lives                                                                                                                                                                                                                                                                        |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Terminal backend selection     | Hermes `terminal.backend` / `TERMINAL_ENV`. **Unset everywhere in this repo** → `local`                                                                                                                                                                                               |
| The agent's Hermes config      | [`agents/platform/config.yaml`](../../agents/platform/config.yaml)                                                                                                                                                                                                                    |
| The pod that hosts the shell   | a `<agent>-shell` StatefulSet, one per agent, reconciled by the operator — [`shell_sandbox_manifests.go`](../../k8s-operator/internal/controller/shell_sandbox_manifests.go)                                                                                                          |
| The image it runs              | [`deploy/sandbox/`](../../deploy/sandbox/) — first-party, `sshd` plus the credential-proxy wrappers                                                                                                                                                                                   |
| The proxy itself               | [`agents/platform/scripts/credential_proxy.py`](../../agents/platform/scripts/credential_proxy.py)                                                                                                                                                                                    |
| Its loopback front door        | [`deploy/shared/envoy-credential-proxy.yaml`](../../deploy/shared/envoy-credential-proxy.yaml)                                                                                                                                                                                        |
| The client the shims run       | [`agents/platform/scripts/credential_proxy_client.py`](../../agents/platform/scripts/credential_proxy_client.py)                                                                                                                                                                      |
| The shims themselves           | [`deploy/docker/Dockerfile`](../../deploy/docker/Dockerfile) — symlinks under `/opt/credential-proxy/bin`                                                                                                                                                                             |
| Its command policy             | `/etc/credential-proxy/policy.json`, from `CREDENTIAL_PROXY_POLICY`                                                                                                                                                                                                                   |
| Proxy placement and federation | [`credential_proxy_manifests.go`](../../k8s-operator/internal/controller/credential_proxy_manifests.go), `buildCredentialProxyEnv` in [`platformagent_manifests.go`](../../k8s-operator/internal/controller/platformagent_manifests.go)                                               |
| The STS credential file        | [`agents/platform/scripts/wif_credentials.py`](../../agents/platform/scripts/wif_credentials.py)                                                                                                                                                                                      |
| The isolation contract         | [`docs/credential-isolation-design.md`](../credential-isolation-design.md)                                                                                                                                                                                                            |
| The Session KV store           | [`agents/platform/scripts/session_kv_server.py`](../../agents/platform/scripts/session_kv_server.py), SQLite under `/var/lib/kube-agents/session/`                                                                                                                                    |
| Its in-process clients         | [`agents/chat/defaults/plugins/session_store/`](../../agents/chat/defaults/plugins/session_store/), [`session_otel_bridge/`](../../agents/chat/defaults/plugins/session_otel_bridge/), [`agents/platform/plugins/incident_context/`](../../agents/platform/plugins/incident_context/) |
| Existing session documentation | [`agents/platform/docs/session_management.md`](../../agents/platform/docs/session_management.md)                                                                                                                                                                                      |

## How to read this document

| Section                                             | What it gives you                                                         |
| --------------------------------------------------- | ------------------------------------------------------------------------- |
| [Background](#background)                           | the incident, what Hermes offers, and why the credentials cannot stay put |
| [The decision](#the-decision)                       | what runs the sandbox pod, where the proxy lands, and what was rejected   |
| [The design](#the-design)                           | a tool call traced end to end, and everything that breaks                 |
| [Setting up the pool](#setting-up-the-pool)         | the GCP commands an install has to run once                               |
| [The standalone fallback](#the-standalone-fallback) | what an install without federation gets instead                           |
| [The Session KV store](#the-session-kv-store)       | Part A, and why the shell move does not fully replace it                  |
| [Prerequisites](#prerequisites)                     | what has to land first, including one known blocker                       |

---

## Background

### The incident, as a design statement

Five steps, none of which required a bug:

1. The agent diagnosed a session-routing problem.
2. It opened `session_kv.db` with `sqlite3` and edited rows directly.
3. It read and modified files under the harness working tree.
4. It wrote its own Hermes configuration.
5. It restarted its own process to pick the change up.

Steps 2 and 3 are the shell reaching things the shell has no business reaching. Step
4 is the shell reaching the agent's own definition. The unifying property is that the
shell and the agent share a filesystem and a process namespace, so "what the agent
can run" and "what the agent is made of" are the same set of files.

Sandboxing the shell separates them. It does not make the agent safer at what it is
_meant_ to do — it makes the blast radius of a bad idea stop at the sandbox.

### What Hermes already offers

Verified against the `hermes-agent` tree at `413ed6b9d` — these are source
observations, not documentation claims.

| Backend           | Isolation                        | Fit here                                                          |
| ----------------- | -------------------------------- | ----------------------------------------------------------------- |
| `local` (default) | none                             | what runs today                                                   |
| `docker`          | container on the same host       | needs a Docker daemon in-pod; docker-in-Kubernetes is a step back |
| `ssh`             | whatever the far end provides    | **the one we want** — the far end becomes a Kubernetes pod        |
| `singularity`     | HPC container runtime            | wrong ecosystem                                                   |
| `modal`           | third-party cloud sandbox        | code leaves the cluster                                           |
| `daytona`         | third-party dev-environment SaaS | code leaves the cluster                                           |
| `vercel_sandbox`  | third-party cloud sandbox        | code leaves the cluster                                           |

The three SaaS backends are all disqualified by the same clause: this agent operates
production Kubernetes and its shell handles cluster state. Shipping that to a
third-party execution service is a data-residency decision, not a sandboxing one.

`ssh` is the useful one precisely because it delegates. Hermes does not care what is
on the other end, so the isolation properties become a Kubernetes question we can
answer with Kubernetes tools.

### Three mechanics that had to be verified

The `ssh` backend is only viable if the _rest_ of the tool surface follows it. If
`terminal` went remote while `read_file` stayed local, the agent would face a
split-brain filesystem and the design would collapse. All three were checked in
source:

**File tools follow the backend.** `read_file`, `write_file`, `patch`, and
`search_files` are not Python filesystem calls — they are shell commands.
`file_tools.py` builds a `ShellFileOperations` over the terminal environment, whose
`_exec` calls `env.execute(...)`; `_get_file_ops()` reads the same
`_active_environments` registry as the terminal tool, keyed by the same `task_id`,
and creates environments honouring `TERMINAL_ENV`. There is no local fast path. They
also share live cwd, so a `cd` in `terminal` moves `read_file`'s relative paths.

**`execute_code` follows too.** `code_execution_tool.py` branches on
`env_type != "local"` and takes a remote path that ships the script plus a generated
`hermes_tools.py` stub into the sandbox and proxies tool callbacks over file-based
RPC. The callback surface is an explicit allowlist — web search, web extract, the
four file tools, and `terminal` — all of which route back _into_ the sandbox. No
escalation out.

**Continuity is reconstructed, not held open.** Every command is a fresh `bash -c`.
Working directory survives via an in-band stdout marker that the environment parses
and strips; environment variables survive via an `export -p` snapshot file, replaced
atomically and re-sourced before the next command. Files survive for the mundane
reason that the sandbox's disk is still there.

That last point is what makes a _long-running_ sandbox necessary rather than a
per-call container. Hermes' persistence model assumes the far end outlives the call.

### The `ssh` backend is unfinished, and this design carries the workarounds

Picking `ssh` means taking on the least finished of Hermes' backends. It is 435 lines
against `docker.py`'s 2050 and `local.py`'s 1687, and the difference is capability
rather than padding. `local.py` opts into environment passthrough with
`_profile_scoped_passthrough = True` and `docker.py` resolves the same values into
`-e KEY=VALUE` arguments; `ssh.py` sets neither and implements no environment handling
of its own, so the only occurrence of the word `env` in the file is in a docstring. It
also defines no `_wrap_command`, which leaves it inheriting a command preamble
`base.py` wrote for a filesystem the caller shares with the shell.

Most defects found so far have the same shape: a host-side operation performed on a
guest path, or a feature the local and Docker backends implement that the SSH backend
does not. Below the environment layer Hermes has two filesystems; above it, everything
still assumes one. Three instances are confirmed, each found by running real work
through the sandbox rather than by reading:

- **The working directory is never created on the far side.** `base.py` emits
  `builtin cd -- <cwd> || exit 126`, and `ssh.py`'s `_ensure_remote_dirs` creates only
  `~/.hermes` and three children. Any other cwd has to already exist there, and the
  kanban dispatcher's per-card workspace is created on the agent pod's PVC. Upstream
  has this as [#86413](https://github.com/NousResearch/hermes-agent/issues/86413) —
  "`terminal.cwd` carries no filesystem namespace", which counts five independent cwd
  resolvers — and [#62169](https://github.com/NousResearch/hermes-agent/issues/62169),
  with no fix in `main`.
- **The dispatcher's environment does not cross.** `terminal.env_passthrough` exists
  for exactly this and is read by `code_execution_tool.py` and the local and Docker
  backends only, so every `HERMES_KANBAN_*` variable arrives empty on this backend.
- **`kanban_complete(artifacts=[...])` stats a guest path on the host.**
  `kanban_db.py` resolves each declared artifact with `pathlib` and calls `is_file()`
  in the gateway process, so a file that exists in the sandbox is reported as
  unavailable.

A fourth is a different shape, and worse, because it fails work that was
otherwise succeeding — see [One connection under every
environment](#one-connection-under-every-environment) below.

Three workarounds carry the design past them. The sandbox image's
[`ForceCommand`](#the-working-directory-has-to-exist-on-the-far-side-and-hermes-does-not-create-it)
creates the working directory and recovers the two `HERMES_KANBAN_*` variables that a
path can yield; the [skills tree is baked](#what-the-sandbox-needs-and-where-it-comes-from)
into the image rather than left to the backend's profile-unaware sync; and workers use
`kanban_attach` in place of declared artifacts. None of them are in Hermes source — see
[Two problems deferred](#two-problems-deferred-and-what-has-already-been-ruled-out-for-them)
for why the repository takes the parsing risk instead of a patch.

The cost is functionality, not isolation. Every one of these failures is a command that
exits 126, a variable that reads empty, or a tool call that refuses; none of them widen
what the sandboxed account can reach, and none of them are a way back into the agent
pod. The nearest thing to a security consequence is a worker's unquoted
`cd $HERMES_KANBAN_WORKSPACE` landing in the shared `/home/agent` instead of its own
workspace, which is cards colliding with each other inside the sandbox rather than
anything crossing its boundary. The `ForceCommand`'s derived variables are likewise
influenced by a cwd the model chooses, and add nothing: it is the model's own shell,
where `export HERMES_KANBAN_TASK=anything` was already available.

Calling the backend unfinished rather than buggy is worth the distinction because it
predicts where the next one is — anywhere Hermes touches a path or an environment
variable that did not come from the environment layer. Delegated subagents, cron
handoff and the MCP server's kubeconfig are all unexercised and all sit on that line.
It is not a reason to reverse the decision, since the alternatives lose more (below),
but it does mean a Hermes version bump is a re-test of this surface rather than a
dependency update.

#### One connection under every environment

The fourth defect is not a path or an environment variable. Hermes gives every
task its own `SSHEnvironment`, but derives the `ssh` `ControlPath` from
`sha256(user@host:port)` — and this design fixes all three of those, since the
operator publishes one host, one user and one port for the whole agent. Every
concurrent task therefore multiplexes over a single master connection.

Teardown is per environment and not per connection. `cleanup()` runs
`ssh -O exit` against that shared path, which drops the master and kills every
session riding it. A sibling task that was mid-command loses it: exit 255, empty
stderr, no indication that another task's teardown is what ended it. Three things
call `cleanup()` — the idle reaper, `close_environment` when the agent closes,
and the environment's `__del__` — and with `delegation.max_concurrent_children`
at 3, the reaper reaches that state whenever one child idles while another works.

The operator's managed terminal block sets `lifetime_seconds` to 30 days, which
takes the reaper out of the picture. It is a number and not an off switch
because Hermes takes an int here and has no sentinel for never; at that size the
only environments the reaper can still collect are ones whose process has
outlived a month of rollouts, which nothing here does. Nothing is reclaimed by
reaping in this topology anyway — the far side is a StatefulSet pod that stays up
either way — so the timeout was buying nothing and costing the race.

Two ways of breaking the sharing itself were tried and rejected. Pointing
`ControlPath` at somewhere unbindable fails hard rather than falling back to a
direct connection: `ssh` exits 255 with `unix_listener: cannot bind`. Planting a
root-owned file at the socket path so the socket can never be created fails
because `cleanup()` unlinks that path, and the sandbox entrypoint runs as an
unprivileged uid that cannot write a file the shell user could not then remove.

What remains is the other two triggers. A process that exits still tears down
its environment and still drops the shared master, so a delegated worker
finishing while a sibling works can still cut the sibling's command — kanban
workers are subprocesses that share the same socket directory, so they are the
realistic case. Closing that needs a per-environment `ControlPath` in Hermes,
which is an upstream change and not one this repository will carry as a patch.

### What the credential proxy is for

`kubectl`, `gcloud`, `gh`, and `git` are not real binaries in the agent image. They are
symlinks to `credential-proxy-exec`, a client that forwards the argv to the proxy and
returns its output. The agent composes a command; something else runs it, holding the
credential.

That gives three properties: the credential material never enters the agent's address
space or filesystem, every credentialed command passes one chokepoint that can log and
refuse it, and the refusal logic lives outside anything the agent can edit.

Two listeners, and only one of them authenticates. The **credential exec path** — the one
the shims use — is fronted by Envoy bound to `127.0.0.1:8765`, forwarding over a Unix
socket at `/var/run/credential-proxy/backend.sock` to the Python handler, with **no caller
authentication at all**. Loopback plus the socket is the entire access control, which is
sound as long as "in the pod" and "authorised" are the same set. The **PlatformAgent API
proxy** on `0.0.0.0:8643` requires `API_SERVER_EXTERNAL_KEY` and refuses to start without
it. The Python listener's own `--host` defaults to `0.0.0.0`, so the loopback confinement
comes from Envoy rather than from the application.

Two things already work in the design's favour. The agent pod sets
`automountServiceAccountToken: false`, so the Kubernetes service account token is not a
bypass; and the proxy's `policy.json` refuses commands that would author a kubeconfig,
which is what stops the agent redirecting `kubectl` at an endpoint of its choosing.

### Workload Identity is scoped to the pod

The proxy gets its GCP credentials from `google.auth.default()`, which on GKE resolves to
the node metadata server at `169.254.169.254`. Every container in a pod shares the pod's
network namespace, and Workload Identity binds a **Kubernetes service account to a Google
service account** — a pod-level relationship. The metadata server has no way to tell one
container in the pod from another, and no Kubernetes mechanism exists to give it one.

So the agent's shell asks the metadata server the same question the proxy asks and gets
the same answer. The operator-managed NetworkPolicy explicitly permits egress to
`169.254.169.254` on ports 80 and 8080, so the path is allowed rather than merely
unblocked.

No amount of container hardening reaches it. Distinct UIDs, a separate PID namespace, a
read-only root filesystem, dropped capabilities and seccomp all constrain what one
container can read from another's **processes and files**. The metadata server is neither:
it is a network endpoint that both containers can route to, and it hands the same identity
to whoever asks. The only way out is for the pod not to have an identity the metadata
server will serve.

### Why possession is worse than use

The credentials are scoped to what the agent is permitted to do, so a natural response is
that a lifted token grants nothing new. Three things separate the two.

**It leaves the chokepoint.** Every property the proxy provides — the command policy, the
audit trail, the workspace check — is a property of _going through the proxy_. A token
used directly has none of them, and an action taken with it appears in cloud audit logs as
the service account with no agent-side record of who composed it.

**It carries the whole scope.** The proxy permits a subset of what the service account can
do; `policy.json` is a filter over the credential, not a description of it. A lifted token
carries the service account's full IAM grant.

**It leaves the cluster.** A bearer token works from anywhere until it expires, and the
ability to mint means the ability to keep minting. Every other network control in this
design assumes the credential stays inside the boundary.

### Which credentials a sidecar can still hold

The two kinds of credential the proxy holds do not have the same answer.

Slack tokens and `API_SERVER_EXTERNAL_KEY` arrive as environment variables on the proxy
container, so separating the UID and PID namespaces is enough to hold them — the
`/proc/<pid>/environ` read is the whole exposure, and
[#720](https://github.com/gke-labs/kube-agents/pull/720) closes it.

Anything obtained through ADC needs the pod to stop having a cloud identity of its own,
because the exposure is a network endpoint rather than a process or a file. Nothing about
the pod's network narrows it either: `runtimeClassName`, NetworkPolicy and metadata
concealment are all pod-scoped and the containers share one IP.

What is left is the filesystem. Containers in a pod do not share a mount namespace, so a
`volumeMount` is the one thing a pod can give to one container and not the other — and a
credential the proxy reads from a **file** is a credential the shell beside it cannot
reach.

---

## The decision

**A StatefulSet, one per agent, reconciled by the operator that already reconciles
everything else the agent needs.** One replica, a `volumeClaimTemplate` for the
workspace, a headless Service in front of it, and an image this repository builds.
Hermes reaches it over `ssh` at a stable DNS name.

The shape is dictated by Hermes rather than by taste. Its persistence model
reconstructs continuity from state left behind on the far end — the cwd marker, the
`export -p` snapshot, the files themselves — so the far end has to be a durable
singleton with a stable name and an attached volume. A Deployment gives none of the
three; a Job or a per-call container gives less. `StatefulSet` with `replicas: 1` is
the Kubernetes noun for exactly this.

One item on that volume is why a Deployment plus a PVC is not an equivalent
spelling: sshd's **host keys**. Hermes connects with
`StrictHostKeyChecking=accept-new`, which accepts a key it has never seen and
refuses one that changed. A sandbox that regenerates its host key on restart does
not prompt anybody — it fails every command from then on until `known_hosts` is
edited by hand. Stable identity is a correctness requirement here, not a nicety.

### The credential proxy is a container of that StatefulSet, and the pod is unbound

Three parts:

1. The sandbox pod's ServiceAccount carries no `iam.gke.io/gcp-service-account`
   annotation. The metadata server then hands both containers the unbound
   `<project>.svc.id.goog` principal, which IAM grants nothing.
2. An audience-scoped projected service-account token is mounted into the proxy container
   only, at `/var/run/secrets/kubeagents/wif/token`.
3. The proxy exchanges that token for a GCP access token through Workload Identity
   Federation, whose credential source is a **file path** rather than the metadata server,
   then impersonates the existing GSA.

`shareProcessNamespace` stays `false`, pinned in `buildShellSandboxStatefulSet` and
asserted by a test. It is load-bearing: with it true, `/proc/<pid>/root` and
`/proc/<pid>/environ` reach into the proxy container's mount namespace and the file
boundary this rests on is gone. The gateway pod sets it true, which is
[#720](https://github.com/gke-labs/kube-agents/pull/720)'s finding rather than this
document's.

The success criterion is that **the pod running the agent's shell has no cloud identity
worth stealing**. Removing the identity is what meets it; moving the proxy to a pod of its
own does not, because the pod the shell lands in would keep one.

#### Why the same pod rather than a pod of its own

A separate Deployment satisfies the identity property just as well. It fails on `git`.

`credential_proxy_client.py` sends `cwd` and `kubeconfig` only when
`shares_filesystem_with_proxy()` says the endpoint is loopback. A cross-pod caller sends
neither, and the proxy falls back to a workspace directory in its own emptyDir.
`kubectl get` does not care. `git` does — its state _is_ the working tree — so proxied
`git` from a separate pod is refused by the lease check, and so is every `kubectl -f FILE`
write path, because the file is in the caller's filesystem. The sandbox's data volume is a
ReadWriteOnce claim that only its own pod can mount, so there is no arrangement in which a
second pod sees that tree.

Co-location gives back both: the proxy is on loopback, and it mounts the same data volume
the shell does. That is the property a sidecar in the agent pod has, without the shared
cloud identity that comes with it.

#### Denying the route versus removing the identity

[#720](https://github.com/gke-labs/kube-agents/pull/720) closes the metadata path by **not
listing** `169.254.169.254` in a default-deny egress policy. This design closes it by
**leaving the Kubernetes service account unbound**. The distinction is enforcement:

| Property                                         | Egress allowlist (#720)          | Unbound service account |
| ------------------------------------------------ | -------------------------------- | ----------------------- |
| Depends on the CNI enforcing NetworkPolicy       | Yes                              | No                      |
| Effective on GKE Standard without network policy | No                               | Yes                     |
| Effective in the default install                 | No — the split is off by default | Yes, once defaulted     |

The first row is not hypothetical, and #720 says so in its own API documentation: _"The
policy does nothing at all on a cluster whose CNI does not enforce NetworkPolicy (GKE
Standard without network policy enabled)."_ The reference install this project is
developed against is exactly that cluster, so on it the allowlist is inert and the
metadata-server path stays open.

An unbound service account needs no CNI feature, no admission check, and no operator guard
against a misconfiguration, because there is nothing left to reach. The two are
complementary — keep the allowlist as defence in depth — but only one of them holds on a
cluster that does not enforce policy.

### Agent Sandbox, and why not yet

**Agent Sandbox** ([`kubernetes-sigs/agent-sandbox`][Agent Sandbox]) is the purpose-built
answer to this: a SIG Apps subproject available as a GKE addon, whose `Sandbox` CRD is a
long-running stateful singleton pod with a stable identity and an attached volume, with
`SandboxTemplate` and `SandboxClaim` giving the operator a per-agent provisioning path,
`SandboxWarmPool` amortising startup, and isolation strength reduced to a
`runtimeClassName` choice. On its documentation it is the smaller concept — one more CR
against four Kubernetes objects the operator has to build and keep in step.

Installing v0.5.5 on the reference cluster and running the sandbox image under it produces
four observations that reverse that:

- **Three of the four CRDs do not exist.** The install creates
  `sandboxes.agents.x-k8s.io` and nothing else; `SandboxTemplate`, `SandboxClaim` and
  `SandboxWarmPool` each come back as "the server doesn't have a resource type". The
  per-agent provisioning path and the warm pool were the two things the API was
  supposed to give us that a StatefulSet does not, and neither has shipped.
- **What did ship maps one-to-one onto a StatefulSet.** `podTemplate` we write either
  way. `service: true` is a headless Service, six lines of it.
  `volumeClaimTemplates` is the same field under the same name.
  `shutdownPolicy: Retain` is `persistentVolumeClaimRetentionPolicy`.
  `operatingMode: Running` is `replicas: 1`. `runtimeClassName` is a pod field and
  belongs to neither.
- **It propagates spec changes worse than a StatefulSet does.** A patch to
  `spec.podTemplate` on a running `Sandbox` never reached the pod, while the
  resource's conditions stayed `Ready` and `DependenciesReady`. Only
  `kubectl delete pod` applied it. A StatefulSet would have rolled it; had it not,
  `.status` would have said which revision the pod was on.
- **Nothing in this repository installs it.** No chart, no Terraform module and no
  provisioning script mentions `agents.x-k8s.io`. Adopting it means a third-party CRD
  and controller added to all three install surfaces under the IaC-parity contract,
  plus `registry.k8s.io/agent-sandbox/agent-sandbox-controller` mirrored into
  [`images.json`](../../images.json) and kept pinned.

So the smallest-new-concept argument does not survive contact with what shipped. With only
`Sandbox` in the cluster, the CR is not the smaller concept: the operator already builds
StatefulSets, Services, PVCs and NetworkPolicies for this agent, and the sandbox is one
more of each. Agent Sandbox costs a dependency, three install-surface changes and a
controller whose reconciliation we would have to work around, in exchange for fields we
can already write.

**This is a deferral, not a rejection**, and the difference is load-bearing. The bet
behind that API — Kubernetes-native sandboxing, warm pools, isolation as a one-line
runtime choice — is still the right bet if the project delivers it. So the interface below
is drawn to make adopting it a swap rather than a rewrite: an SSH-reachable pod at a
stable name, an attached volume, and an image that knows nothing about what scheduled it.
On the day the other three CRDs exist, what changes is one builder function in the
operator, and nothing in `deploy/sandbox/` or in Hermes' configuration.

What we give up meanwhile is the warm pool, and it costs less than it sounds: sandbox
lifetime is tied to the agent rather than the conversation (see
[What persists](#what-persists-and-for-how-long)), so a cold start is a pod restart,
not a per-conversation tax.

### Agent Substrate, and why not

Agent Substrate was evaluated seriously and rejected. It is a **density and
scheduling** layer — roughly 250 sessions across 8 pods, with a minimal control plane
that deliberately bypasses the Kubernetes API and an Envoy-based router for session
addressing. Density is not our problem: one agent, one shell. Bypassing the
Kubernetes API costs us the operator integration that makes this cheap. And it
depends on Pod Certificates, which are default-off until Kubernetes 1.36.

The distinction worth keeping: Substrate optimises _many sessions per node_; this
design wants _one durable, isolated session with an identity_. That is the axis the
choice turns on, and it is why the section above changes nothing here — a StatefulSet is
no more of a density layer than a `Sandbox` CR.

### What sandboxing does and does not close

| Incident step                        | Closed by the sandbox?                                                    |
| ------------------------------------ | ------------------------------------------------------------------------- |
| 2 — `sqlite3` on the session DB      | Yes, if the DB volume is not mounted into the sandbox                     |
| 3 — editing the harness working tree | Yes — the tree is not there                                               |
| 4 — writing Hermes `config.yaml`     | Yes; `config.yaml` is not in the sync set (see below)                     |
| 5 — restarting its own process       | Yes — the process is in another pod                                       |
| Credential exfiltration              | Only with the proxy co-located and the pod unbound, per the section above |

### The residual channel: `sync_back`

The SSH backend's `cleanup()` tars the _entire_ remote `~/.hermes` and copies changed
files back onto the host. Conflicts are last-write-wins in the remote's favour, with
a warning. New files created in the sandbox, with no push mapping, get a host path
inferred by prefix substitution.

`config.yaml` is not in the sync set, so incident step 4 stays closed. **Skills are**,
and skills are executable content the gateway loads. A sandbox that writes
`~/.hermes/skills/x.md` gets that file onto the host, where the agent picks it up —
a write channel from the untrusted side into the trusted side, by design and by
default.

The choice here is to disable the channel, and to disable it at the sandbox end
rather than the gateway end.

The sandbox image creates `/home/agent/.hermes` ahead of time as
`0555 root:root`. The shell user is `agent`, uid 1000, so the push that opens the
channel cannot land: `FileSyncManager._ensure_remote_dirs` fails to `mkdir -p`
underneath it, and the initial `sync(force=True)` the SSH backend runs at
environment setup writes nothing. `sync_back()` then early-returns on its own
guard — it does nothing when there is no prior push state to reverse — so the
writeback is a no-op rather than a failure. Nothing in the sandbox reads the
pushed copy of the agent's Hermes home, so denying it costs no working
behaviour.

Both halves of that path degrade quietly by design in Hermes:
`_ensure_remote_dirs` runs its `mkdir` without checking the exit status, `sync`
catches and rolls back, and `sync_back` returns early. A directory the sync
cannot write is therefore the one lever that closes the channel without a code
change on either side, and without a failure mode that takes a task down with
it. The cost is that the closure is silent in both directions: an operator
reading Hermes' logs sees no refusal, only an absence, which is why the
`Dockerfile` carries the explanation next to the `install -d` line rather than
leaving a bare mode to be read as tidiness.

The alternative considered and rejected was making the gateway's own
`skills/` tree read-only, which would deny the write at the destination instead.
It cannot distinguish the sandbox's writeback from a legitimate write by the
agent's own `skill_manage` tool, which runs in-process in the gateway and
targets the same directory. Closing the channel there would take the agent's
ability to author a skill with it. Denying at the source is the narrower cut:
it is scoped to code the model ran in the sandbox, which is the thing the
sandbox exists to distrust.

Two things this does not close. Skills are not the only executable content under
`~/.hermes`, so the guarantee rests on the directory being unwritable rather
than on an enumeration of paths — a future Hermes that creates the parent
itself, or syncs to a different root, would reopen it and nothing here would
notice. And an install that runs a different sandbox image gets the default
behaviour back; the closure is a property of the image, not of the operator's
reconciliation.

### Rejected alternatives for the credential path

**Sidecar in the agent pod with hardening (UID and PID split alone).** Fails for ADC
credentials, per [Which credentials a sidecar can still
hold](#which-credentials-a-sidecar-can-still-hold) — which is why #720 pairs its namespace
hardening with a Pod split rather than relying on it. This design keeps the hardening and
removes the ADC identity instead.

**gVisor as the credential boundary.** gVisor's boundary is the host kernel, not the
network. Its sentry implements the syscall surface, but a socket to `169.254.169.254` is a
socket, and GKE's metadata server serves it for the pod's IP the same as under runc.
`runtimeClassName` is pod-scoped besides, so it cannot be applied to one container and not
the other — it would put the proxy inside the sentry alongside the shell it is supposed to
be separated from. It buys a different thing entirely, which is worth having and which the
sandbox now ships as an opt-in: see [Running the sandbox under
gVisor](#running-the-sandbox-under-gvisor).

**A NetworkPolicy denying the shell egress to the metadata server.** Pod-scoped, like
everything else in the network namespace, so it denies the proxy at the same time. And on
the reference install it is inert.

**GKE metadata concealment.** Node-scoped, and deprecated in favour of Workload Identity.
It would also break the proxy in the standalone placement, which still reads the metadata
server.

**`automountServiceAccountToken: false` alone.** Already set, and orthogonal: it withholds
the Kubernetes API token, not the cloud one. GKE's Workload Identity path does not read
the projected token file — the metadata server answers on pod IP — which is exactly why
federation, which _does_ read a file, is what changes the outcome.

**Re-audiencing the pod's existing service-account token instead of projecting a second
one.** The `automountServiceAccountToken` projection is pod-wide and has the API server's
audience. Federation needs the provider's full resource name as `aud`, and
`github-token-minter` separately needs `kubeagents-credential-proxy`. One token cannot
satisfy all three, and the projection has to be per-container anyway, so it is a new
volume regardless.

**`fsGroup` for the shared data volume.** The sandbox entrypoint already chowns `$DATA` to
uid 1000 and the proxy container runs as that uid, which is enough. `fsGroup` would
relabel the whole volume on every mount and apply to volumes that should not be
group-readable. Sharing a uid between the two containers is only safe because
`shareProcessNamespace` is false and the data volume is the sole shared mount; both are
asserted by tests.

**A separate namespace for the proxy.** Better RBAC isolation for the Secret, but
cross-namespace `ownerReferences` are invalid, so the operator loses garbage collection
and has to manage lifecycle by hand. Not worth it at this step; revisit if the proxy is
ever shared across agents.

**A node-level DaemonSet.** Makes scheduling a security property and widens the blast
radius to every agent on the node.

**One proxy for the whole fleet.** A single credential set behind every agent. The
operator already reconciles per-agent resources and the Secret is already per-agent, so
per-agent is the natural grain.

### What co-location costs

**The shell container inherits the proxy's egress.** A NetworkPolicy applies to the pod,
so the rules that let the proxy reach STS, IAM, the Kubernetes API and
`github-token-minter` also let the shell reach them. There is no way to narrow that while
the two share a network namespace. What makes it acceptable is that reach is not
possession: the shell arrives at those endpoints with the unbound `<project>.svc.id.goog`
principal and no bearer token, and every one of them rejects it.
`buildShellSandboxNetworkPolicy` excludes RFC1918 space and `169.254.169.254/32` from the
`0.0.0.0/0:443` rule so the widened egress does not become in-cluster reach.

**Both containers run as uid 1000.** Required for the shared working tree, and the reason
`shareProcessNamespace` has to stay false rather than merely defaulting to it.

**Git hooks execute next to the credentials.** A shared writable tree means the shell can
write `.git/hooks/pre-commit` and the proxy will run it during a proxied `git commit`,
inside the container that holds the token. The proxy pins `core.hooksPath=/dev/null` and
`protocol.ext.allow=never` through
`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`, sets `GIT_CONFIG_NOSYSTEM=1`,
and refuses any argv that tries to override either key with `-c` or `--config-env` — the
command line outranks the environment form, so the argv guard is not redundant.
`GIT_CONFIG_GLOBAL` is deliberately left alone: the proxy's own author identity lives
there. Those pins close the routes they name and cannot close the class, because the class is
"anything `.git/config` can make git execute" and it does not enumerate. What closes it is the
agent not having a `.git` at all — see
[Content-passing removes the shared tree](#content-passing-removes-the-shared-tree), which is
partly delivered: the two write skills have migrated, the volume is still mounted for the
skills that have not.

**Every install needs a Workload Identity pool.** Three `gcloud` commands, below, that no
install surface runs.

### Where the install surfaces stand

The Helm chart expresses both halves: `platformAgent.harness.experimental.shellSandbox`
and `platformAgent.security.workloadIdentityFederation`. It refuses a half-filled
federation block and refuses federation without the sandbox, rather than letting the
operator's fail-safe silently keep the proxy on the metadata server — an install that
asked for federation and did not get it has no symptom except a placement nobody looks at.
The Terraform composition reaches the same values through `extra_helm_values`.

What no surface does is **create the Workload Identity pool**: a knob the config layer owns
and the infrastructure layer does not. The chart README lists it under the knobs that need
context beyond the chart, and a `kube-agents-iam` addition that creates the pool, the
provider and the `roles/iam.workloadIdentityUser` grant is the obvious next step — it needs
the cluster's OIDC issuer, which that module does not have today.

The gVisor node pool is the opposite case — it has a full surface, pointed at the wrong
pod. `install.sh --gvisor` sets `enable_gvisor_node_pool` on Standard, or on Autopilot
takes the built-in RuntimeClass and no pool, and the composition renders the result into
`deployment.availability.runtimeClassName`. That is the _agent_ pod, the one holding the
WAL-mode SQLite that gVisor corrupts — see
[Running the sandbox under gVisor](#running-the-sandbox-under-gvisor). The sandbox pod,
which holds no SQLite and is the one running code the model wrote, is left on the default
runtime. Turning the sandbox on through Terraform should point
`harness.experimental.shellSandbox.runtimeClassName` at the pool as well, and that rewiring
is not in this change: the flag predates the sandbox and repointing it is a behaviour change
for installs that already pass `--gvisor`.

---

## The design

### Topology

Two pods per agent instead of one:

- **the agent pod** — Hermes, the gateway, the plugins, the cron scripts, the MCP server.
  No shell of consequence, and no cloud identity that anything in it uses.
- **the sandbox pod** — two containers sharing a data volume and a loopback interface, and
  nothing else. One runs `sshd`, a durable `/opt/data`, the agent's tools and the
  credential-proxy shims; the other is the credential proxy, holding the credentials and
  running the credentialed commands. The shell container has no Kubernetes
  service-account token, no real `kubectl`, and no identity the metadata server will
  answer for.

An install without federation has a third pod, the standalone proxy Deployment — see
[The standalone fallback](#the-standalone-fallback).

### The sandbox workload

Four objects per agent, all owned by the `PlatformAgent` CR so they are garbage
collected with it, built in
[`shell_sandbox_manifests.go`](../../k8s-operator/internal/controller/shell_sandbox_manifests.go).

| Object                       | Named           | What it is for                                                               |
| ---------------------------- | --------------- | ---------------------------------------------------------------------------- |
| `StatefulSet`, `replicas: 1` | `<agent>-shell` | the pod, and the `data` and `sshd` volumeClaimTemplates behind it            |
| `Service`, `clusterIP: None` | `<agent>-shell` | the StatefulSet's governing service, and the name Hermes dials               |
| `ServiceAccount`             | `<agent>-shell` | no annotations at all — the unbound identity the whole pod runs as           |
| `NetworkPolicy`              | `<agent>-shell` | ingress on 2222 from the gateway only; egress to DNS, and to 443 off-cluster |

Five fields carry an argument rather than a default:

- **`persistentVolumeClaimRetentionPolicy: Retain` / `Retain`.** One claim holds the
  sshd host keys and the other holds the model's work, and neither survives being
  reclaimed on a scale-down. New host keys turn `accept-new` into every subsequent
  command failing; a fresh data volume loses whatever the agent had written. The cost
  is two PVCs that outlive their StatefulSet.
- **`automountServiceAccountToken: false`.** The entire point. Without it the sandbox
  has a Kubernetes credential and the boundary is decorative.
- **`enableServiceLinks: false`.** Kubelet otherwise injects a docker-link-style env
  var for the cluster IP and port of every Service in the namespace. None of them are
  secrets, and the first live pod came up with the address of an unrelated workload's
  Service in its environment for no reason: the sandbox reaches the credential proxy
  by an explicit URL and needs no service discovery.
- **No `runAsNonRoot`.** sshd's privilege separation forks as root and drops to the
  `agent` user for the session, so the container starts as uid 0 and nothing the
  agent runs does. This one reads like a gap in a security review and is not; the
  comment in the builder says so at the field.
- **`CREDENTIAL_PROXY_URL`, and nothing else, from the pod environment.** The image's
  entrypoint forwards an allowlist into the SSH session, because sshd does not pass
  its own environment to sessions. See
  [`deploy/sandbox/entrypoint.sh`](../../deploy/sandbox/entrypoint.sh). Co-located, the
  value is `127.0.0.1:8765` again, from `credentialProxySandboxURL`.

The ServiceAccount is the pod's identity and not a credential: it carries no annotation,
so the metadata server answers every container in the pod with a principal IAM grants
nothing, and `automountServiceAccountToken` is false on both the ServiceAccount and the
pod. Notably absent from the shell container is any Role, any projected token, and any
Secret other than the public half of the agent's SSH key. The proxy container's federation
token is a volume of its own, mounted there and nowhere else. If a future change needs one
of those in the shell container, that is the boundary moving, and it should be argued for
here first.

### Running the sandbox under gVisor

`harness.experimental.shellSandbox.runtimeClassName` puts the sandbox pod on a sandboxed
container runtime, `gvisor` being the one GKE offers. It is unset by default, and an
install that does not name it renders exactly the object it rendered before the field
existed.

This is a second boundary and not the one the rest of this design is built on. Unbinding
the ServiceAccount is what takes the cloud credential away; running the shell as a
different pod is what takes the agent's filesystem away. Neither has anything to say about
the node. gVisor's sentry does: it puts a user-space kernel between the code the model runs
and the host's syscall surface, so a kernel bug the model finds gets the sentry rather than
the machine every other pod on that node is sharing. That is worth having precisely because
the shell container is the one place in this system where arbitrary code is expected to run.

The field is the sandbox's own rather than a reuse of
`deployment.availability.runtimeClassName`, because the two pods do not want the same
answer. The agent pod holds `session_kv.db`, WAL-mode SQLite, which gVisor corrupts on the
gofer-backed mount ([#610](https://github.com/gke-labs/kube-agents/issues/610)); the sandbox
pod holds no SQLite at all. One field would force the WAL hazard and the node protection to
be taken together or not at all, and the install that wants them is the one that wants the
untrusted pod sandboxed and the trusted one left alone. Splitting them is what makes that
expressible.

The pod-scoped nature of `runtimeClassName` — the reason it is no use as the credential
boundary — costs nothing here. Both containers of the sandbox pod go inside the sentry,
including the credential proxy, and the proxy's separation from the shell was never the
sentry's job: it is the mount namespace, `shareProcessNamespace: false`, and the fact that
the token file is mounted in one container and not the other.

On GKE Standard this needs a node pool created with `--sandbox type=gvisor`; the
`gke-cluster` module's `enable_gvisor_node_pool` creates one, and GKE adds the pool's
`sandbox.gke.io/runtime=gvisor:NoSchedule` toleration to pods naming the RuntimeClass
itself, so no toleration plumbing is needed here. Autopilot ships the RuntimeClass natively.
A name the cluster does not have leaves the pod Pending with nothing in the CR that explains
why, so the operator checks every RuntimeClass the CR asks for — the agent's and the
sandbox's, deduplicated — before applying anything, and reports `Degraded` naming the one it
could not resolve.

### Key management

Two keypairs are in play and only one of them is a problem.

**Host keys are already automatic.** The image's entrypoint generates an ed25519 and
an RSA host key under `/var/lib/sandbox-sshd` the first time a pod starts on that
volume, and leaves them alone on every later start
([`entrypoint.sh`](../../deploy/sandbox/entrypoint.sh)). Hermes connects with
`StrictHostKeyChecking=accept-new`, so the first connection trusts the key and every
later one pins it. Because the keys live on a PVC rather than in a Secret, no
private key is written to etcd and no install surface has to know they exist — which
is also the reason for the `Retain` retention policy above. Agent Sandbox's own SSH
example regenerates an ephemeral host key on every start unless you mount one; this
avoids both that churn and the Secret it would otherwise need.

The second volume is the correction to a first version that kept the keys on the one
the model writes and `chown`ed them to uid 1000. Both clients pin the host key, and
the account being constrained by the pin could read the private half of it —
demonstrated on the live install with `su agent -c 'cat …/ssh_host_ed25519_key'`. Mode
bits would not have fixed it: uid 1000 owns that volume's mount point, so it can move
any directory inside it aside and have the entrypoint populate a replacement it
controls on the next start. Only a volume it cannot write settles the question, and
sshd reads these as root, so uid 1000 needs no access to them at all. The entrypoint
refuses to start if `/var/lib/sandbox-sshd` is not root-owned, and
`make docker-smoke-sandbox` checks both the refusal and the read. Exploiting the
original would still have needed a way to redirect the agent pod's connection, which
the sandbox has no route to — so this was a control that was not doing its job rather
than a live compromise.

**The client keypair is generated at install time.** `SSHEnvironment` passes
`-i <key_path>`, so the private half has to arrive as a **file** in the agent pod,
not an environment variable — which turns out to be the hard part, and is dealt
with under [Getting the key into the agent pod](#getting-the-key-into-the-agent-pod)
below. Nothing rotates it; see the sharp edges.

#### It follows an existing pattern

`SESSION_KV_API_KEY` and `SESSION_KV_SALT` are the model: generated by every install
surface, never prompted for, and never rewritten once present. The keypair takes the
same contract, and two of the three surfaces can express it with what they already
use:

| Surface                                   | How it generates a secret today                                | What it does for the keypair                                                                                                                      |
| ----------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `terraform/examples/full-install`         | `random_password`                                              | `tls_private_key` with `ED25519`, whose `private_key_openssh` / `public_key_openssh` attributes give both halves without shelling out             |
| `upgrade.sh` (`backfill_sandbox_ssh_key`) | additive `kubectl patch`, an existing value is never rewritten | `ssh-keygen -t ed25519` in a private temp dir, under a guard that treats a half-written pair as absent, for installs that predate the keypair     |
| Helm, `platformAgent.credentials.create`  | `lookup` the live Secret, else `randAlphaNum 48`               | **cannot generate.** sprig's `genPrivateKey "ed25519"` emits PEM and sprig has no function that encodes the public half in `authorized_keys` form |

So the path that installs production — the Terraform composition the installer
drives — gets a keypair with nothing typed. Helm's `credentials.create` accepts a supplied
pair and renders the sandbox's Secret from it, but generates nothing; absent a key it
renders no Secret and the sandbox stays unusable. Adding a post-install hook `Job` to
close that gap would mean a ServiceAccount with write access to the credential
Secret, which is a worse trade than the gap. It is also consistent with what
[`values.yaml`](../../charts/kube-agents/values.yaml) already says about the flag:
convenience for dev installs, and a pre-created Secret in production.

#### Two Secrets, not one

| Secret                                | Holds                                                  | Mounted into  |
| ------------------------------------- | ------------------------------------------------------ | ------------- |
| `platform-agent-secrets` (existing)   | `SANDBOX_SSH_PRIVATE_KEY` and `SANDBOX_SSH_PUBLIC_KEY` | the agent pod |
| `<agent>-shell-authorized-keys` (new) | `authorized_keys`, the public half, alone              | the sandbox   |

One Secret with `items:` selecting a different key for each pod would also work —
kubelet projects only the listed items. It is rejected because it puts the object
holding every model API key into the sandbox's volume list, one edit away from being
readable there in full. "The sandbox mounts no credential Secret" is a claim worth
being able to make without qualification, and duplicating a **public** key across two
Secrets is the cheapest possible way to buy it.

Both halves live in `platform-agent-secrets` so that any surface re-running against
an existing install can recover the pair from one place, and so the chart can render
the sandbox's Secret without being handed the key again. The private half goes there
rather than into a dedicated `kubernetes.io/ssh-auth` Secret — the typed one is the
better convention and Agent Sandbox uses it, but it would mean teaching four install
surfaces to create a fourth object, where an extra key in a Secret they all already
create costs them a line each.

#### Getting the key into the agent pod

Mounting the Secret and pointing `ssh -i` at it does not work, and the way it fails
is worth stating so nobody re-derives it. The agent pod runs `runAsNonRoot` as uid
10000; a Secret volume's files are owned by root; and `ssh` refuses any private key
with a group or other permission bit set. So `0400` is unreadable by the agent and
`0440` is refused by `ssh` — there is no mode that satisfies both, and every
combination fails at connection time with a message about permissions that reads like
a bad key and sends the reader to the wrong pod.

The way through is a copy. The Secret is mounted `0444` — world-readable _within a
pod that is the key's legitimate holder_, which concedes nothing — and a small init
container running as the pod's own uid `install -m 0600`s it into an `emptyDir` the
agent container mounts read-only. The copy is owned by the account that reads it, so
`ssh` is satisfied. A missing key logs and exits 0 rather than failing the pod,
because the sandbox is opt-in and an install without a keypair is not broken.

Built in
[`shell_sandbox_manifests.go`](../../k8s-operator/internal/controller/shell_sandbox_manifests.go)
as `buildShellSandboxClientKeyVolumes`, `buildShellSandboxClientKeyInitContainer` and
`buildShellSandboxClientKeyMount`, and mounted into the agent pod whenever the
sandbox is on — which is the same condition under which `terminal.backend` becomes
`ssh`.

#### What the operator publishes as `terminal`

The managed scope carries the whole block or none of it. With the sandbox off the
key is absent, Hermes' own default (`local`) applies, and nothing about an existing
install changes. With it on, six keys are Hermes' and one is this design's:

| Key                                | Value                                                                                           |
| ---------------------------------- | ----------------------------------------------------------------------------------------------- |
| `backend`                          | `ssh`                                                                                           |
| `ssh_host`, `ssh_user`, `ssh_port` | the sandbox's Service, `agent`, 2222                                                            |
| `ssh_key`                          | where the init container stages the private half                                                |
| `lifetime_seconds`                 | 30 days — see [One connection under every environment](#one-connection-under-every-environment) |
| `workspace_root`                   | `/opt/data`, the sandbox's data path                                                            |

`workspace_root` is not a Hermes key. It is published so that the agent-side
helpers which shell into the sandbox read the sandbox's layout from one place
instead of each hard-coding it, which is the same reason `sandbox_exec.py` reads
`ssh_host` from the managed config rather than re-deriving the Service name.

#### Two sharp edges left

- **Rotation is ordered.** Write the Secret, restart the sandbox, then restart the
  gateway. In the other order the agent holds a key the sandbox has not authorised
  yet. The restart is needed because the entrypoint copies `authorized_keys` into
  place once at startup; symlinking the mounted file instead would let kubelet's
  Secret propagation make rotation live, and is worth doing when rotation is. Every
  install surface therefore preserves an existing pair rather than regenerating it —
  a re-run that quietly minted a new key would lock the agent out of its own shell.
- **Nothing rotates on a schedule.** The key's lifetime is the install's. Acceptable
  for a key that never leaves the cluster and authenticates one pod to one pod, but
  it is a decision rather than an oversight.

Agent Sandbox contributes nothing to lift here: its controller generates no SSH keys
at all — the only key generation in it is an ECDSA CA for its own webhook TLS — and
its example scripts `ssh-keygen` and `kubectl create secret` by hand. The one idea in
that example worth taking is not about keys: it runs dropbear rather than OpenSSH
specifically so the pod can be fully non-root with all capabilities dropped, which
bears on the `runAsNonRoot` bullet above and is tracked as open below.

### A tool call, traced

The agent calls `terminal("grep error output.log")`.

Hermes resolves the environment for the task from `_active_environments`, creating an
`SSHEnvironment` on first use. That environment opens a multiplexed SSH connection to
the sandbox (`ControlMaster=auto`, so subsequent calls reuse the socket) and runs a
wrapper script: re-source the env snapshot, `cd` to the tracked working directory,
run `bash -c 'grep error output.log'`, then emit the cwd marker and rewrite the
snapshot.

`grep` runs **in the sandbox pod**. `output.log` is read from the sandbox's data
volume, where it was written by whichever earlier command produced it — the sandbox's
disk is the only filesystem in the picture. Stdout comes back over the SSH channel;
Hermes strips the marker and returns the rest to the model. The agent pod's
filesystem is never involved.

If the previous command had been `cd /opt/data/logs`, that would have been captured
by the marker and applied here, and `read_file("output.log")` would resolve against
the same directory — because the file tools share the environment object.

### `HERMES_WRITE_SAFE_ROOT` has to move with the shell

The Hermes base image sets `HERMES_WRITE_SAFE_ROOT=/opt/data`, and on the install that
first ran the sandbox that value denied every write the agent attempted. `write_file`
and `patch` returned "Write denied" for every path.

The guardrail is a string-prefix test, and it runs in the wrong process to know about
any of this. `agent/file_safety.py` splits the variable on `os.pathsep`, `realpath`s
each entry, and requires the resolved target to equal a root or begin with `root + "/"`
— all of it in the agent process, before the write is dispatched to any backend. So it
was checking sandbox paths against a list containing only the agent pod's own home, and
at the time no `/opt/data` existed in the sandbox for any of them to match. Unsetting it
is not the answer either: the check is opt-in and an empty value skips it entirely,
which drops the guardrail rather than moving it.

The operator therefore writes it out, in `buildPodTemplateSpec` and only when the
sandbox is enabled, naming the sandbox's two writable directories: `/opt/data`, and
`/home/agent` for the commands that land in the home. Since the sandbox's data volume
now carries the `/opt/data` path itself, the interesting half of that is the home — but
the value is written rather than left to the image default so the policy is visible in
the pod spec rather than inherited from a base image two repositories away. It gives up
no isolation. With `backend: ssh` the file tools cannot reach the agent pod's
filesystem at all, so the roots they are checked against should describe the filesystem
they actually write to. `TestSandboxRepointsTheWriteSafeRoot` asserts the variable is
absent with the sandbox off, is exactly these two paths with it on, and names nothing
that does not resolve in the sandbox.

One thing this does not cover: the credential denylist that sits alongside the check
(`~/.ssh`, `~/.aws`, `~/.config/gcloud`, `~/.docker`) is still expressed against the
agent pod's home. In the sandbox those paths name nothing, which is harmless today and
wrong if the sandbox ever holds credentials of its own.

### Where the model's files go

The sandbox has three directories that matter and only one of them keeps anything.

| Path                    | Backing                        | Owner    | What it is                   |
| ----------------------- | ------------------------------ | -------- | ---------------------------- |
| `/opt/data`             | `data` PVC                     | uid 1000 | the model's work             |
| `/home/agent`           | the container's ephemeral disk | uid 1000 | the login's home             |
| `/home/hermes`          | the container's ephemeral disk | uid 1001 | the trusted principal's home |
| `/var/lib/sandbox-sshd` | `sshd` PVC                     | root     | the host keys                |

**The homes are ephemeral on purpose.** `agent` owns `/home/agent/.bashrc`, bash sources
it for a non-interactive `ssh host cmd`, and the model can delete Debian's
non-interactive guard — so a shim planted there is executed for anything that logs in as
`agent`. Putting that file on a volume would make the hijack outlive a pod recycle. It
does not, and that is the arrangement working.

**A durable home was the other option and it loses more than it gains.** The interesting
files under a home are the dotfiles, and those are exactly what the previous paragraph
wants thrown away. The model's actual output has somewhere better to be.

**Which leaves `TERMINAL_CWD`.** Hermes' `ssh` backend defaults its working directory to
`~` (`tools/terminal_tool.py`), so with an ephemeral home and nothing pointing elsewhere,
every relative path the model wrote landed on the container overlay while the volume
beside it stayed empty. That is what the live install did for five days: 5Gi attached,
44K used, `lost+found` and the host keys the only things on it. The operator now sets
`TERMINAL_CWD=/opt/data` on the agent container when the sandbox is on. It is an
environment variable rather than a `terminal.cwd` in the managed config scope because
the config bridge treats an explicit config key as an override of the environment
(`hermes_cli/config.py`), which leaves this a pod-wide default a profile can narrow —
the per-profile directories are their own issue, and a managed-scope value could not be
narrowed by anything.

**The path is `/opt/data` on both sides deliberately.** It is the agent pod's Hermes home
as well, named in 59 files across `agents/`, and the alternative was a sandbox path that
no existing SOP, skill or model-written script would resolve. The cost is one path naming
two different directories on two different volumes, and one rule that follows from it:
**no handoff may assume write-here-read-there.** Nothing is copied between them and
nothing can read across, so a script that writes `/opt/data/x` in the agent pod and reads
`/opt/data/x` through the shell gets a missing file — and, unlike before, gets it without
the path itself looking wrong. The entrypoint writes a `.sandbox` marker into the
sandbox's copy, which is how a script or a person tells which side they are on. The
bootstrap inventory handoff is the known case; it has its own issue.

The kubeconfig the platform MCP server writes stays out of `/opt/data` for the reason
under [The SSH principal cannot be the shell user](#the-ssh-principal-cannot-be-the-shell-user):
a kubeconfig names an `exec` credential plugin and `kubectl` runs it, so one the model
can author is arbitrary code execution as `hermes`. `/opt/data` is now durable as well
as model-writable, which makes it a worse place for that file rather than a better one.

`volumeClaimTemplates` is immutable, so an install that already ran the single-volume
layout does not roll into this one. The StatefulSet has to be deleted with
`--cascade=orphan` and left to the operator to recreate, and the old claim is orphaned
rather than reclaimed — which is the retention policy behaving as intended, and leaves
whatever was on it available to copy across by hand. The pod's new host keys will not
match what the agent pod pinned, so its `known_hosts` entry needs clearing in the same
maintenance window.

### Per-profile directories, and moving what is already there

Giving the sandbox's volume the same `/opt/data` makes an absolute path resolve on both
sides. Two things it does not settle: whether each profile needs its own directory over
there, and what happens to the files the model wrote before any of this existed.

**The per-profile question is answered by what the agent pod already does, which is
nothing per profile.** `/proc/1/environ` on the gateway holds `HERMES_HOME=/opt/data`,
`PLATFORM_AGENT_HOME=/opt/data` and `TERMINAL_CWD=/opt/data` — process-global, one set
for the whole gateway. A shell command dispatched by the `platform` profile and one
dispatched by a cluster profile both land in `/opt/data`, and always have.
`_get_env_config` in `tools/terminal_tool.py` reads `os.environ` and nothing else; the
per-task overrides that could change it (`register_task_env_overrides`) are called by the
ACP adapter and the TUI gateway, not by the chat gateway that serves these profiles. So
the sandbox setting the same three variables to its own `/opt/data` is parity, not a
regression, and a per-profile `TERMINAL_CWD` would be a new behaviour rather than a
restored one. The per-profile homes under `/opt/data/profiles/<name>` are real, but they
are Hermes' Python-side config and state homes, reached through `get_hermes_home()` and
never through the shell.

What the profiles do need is for their paths to _exist_ over there. A skill that writes
`/opt/data/profiles/platform/plans/x.md` gets `No such file or directory` in a sandbox
where only the machine home was created, and the model's recovery from that is to write
somewhere else. So the layout is mirrored: every home the agent pod has — the machine
home and one per profile — gets the same skeleton of working directories in the sandbox
(`artifacts`, `gitops`, `plans`, `scratch`, `tmp`, `workspace`).

**[`sandbox_mirror.py`](../../deploy/shared/sandbox_mirror.py) does the mirroring, and it
runs on the agent pod.** That side has the three things the job needs and the sandbox has
none of them: the profile list, the SSH key, and the files themselves. It runs from the
agent entrypoint (step 5.7, backgrounded and non-fatal, gated on the bootstrap primary so
two replicas do not both push) and again from `cluster_agent_profile.py` when a profile is
scaffolded later, so a cluster onboarded at 3am does not wait for a pod restart to get its
directories. Nothing about it is ordered against the sandbox starting: the Deployment and
the StatefulSet come up independently, so the script waits for sshd and, failing that,
leaves the agent pod's files untouched and lets the next start retry.

**The migration is the same mechanism run once.** An install that upgrades into the
sandbox has files on the agent pod's PVC that the model will look for and not find —
`scratch`, `gitops`, and on the install this was written against four directories the
model invented for itself (`infra`, `infra-repo`, `infra_repo`, `work-d0452361`). Those
cross on the first run, a `.sandbox-migrated` marker records what moved, and later starts
skip the copy.

The selection is a denylist. An allowlist of the directories the personas name would have
carried `scratch` and `gitops` and silently dropped all four of the invented ones — which
is precisely the "user upgraded and lost their files" outcome the migration exists to
prevent. A denylist fails the other way: something unneeded gets copied, and it is visible
in the log line that names it. What it excludes is Hermes' own runtime state, credentials,
the trees the sandbox image delivers, databases and their write-ahead logs, and
`$HERMES_HOME/home` — the process `$HOME`, which despite the name is where pip and gcloud
caches accumulate (831 MiB of them here) rather than anywhere the model works.

**Exclusion has to happen at two levels, and finding that out cost a leaked token.** The
first live run applied the rules to each home's top-level entries only, decided `tmp` was
the model's and copied it whole — carrying `tmp/gke_gcloud_auth_plugin_cache`, a cached
GKE access token, into the pod whose entire purpose is to hold no credentials. The lesson
generalises past that one file: a directory the model owns is a directory the model has
been running `gcloud` and `kubectl` inside, so credential files land wherever `$HOME` or
`$KUBECONFIG` pointed at the time, at whatever depth. The top-level rules now decide only
which entries are named to `tar`, and a second set goes to `tar` as `--exclude` patterns,
which GNU tar matches unanchored against every member — a bare `.kube` drops `tmp/.kube`
as readily as `.kube`. `tests/test_sandbox_mirror.py` covers both levels against the real
`tar`, including the nested case that got through.

Two smaller properties. The copy extracts with `--skip-old-files`, because with no
ordering between the two pods a migration can arrive mid-turn and must not replace a file
the model wrote thirty seconds ago with the agent pod's older copy. And it is bounded —
2 GiB by default, and never past leaving 512 MiB free on the sandbox's smaller volume —
spending the budget smallest-first so one large clone cannot evict everything else.

### What persists, and for how long

| Thing             | Mechanism                                 | Lifetime               |
| ----------------- | ----------------------------------------- | ---------------------- |
| Files             | the sandbox's `data` volume, `/opt/data`  | the sandbox's lifetime |
| Working directory | in-band stdout marker, tracked in Hermes  | the task's environment |
| Environment vars  | `export -p` snapshot file in the sandbox  | the sandbox's lifetime |
| Shell processes   | nothing — every call is a fresh `bash -c` | one command            |
| Background jobs   | only if explicitly detached               | until the pod restarts |

Sandbox lifetime should be tied to the agent, not to the conversation. The agent is a
long-running operator, not a session; a per-conversation sandbox would throw away
working state between related tasks and make warm-pool startup the common case rather
than the rare one.

### The working directory has to exist on the far side, and Hermes does not create it

Every terminal command Hermes sends opens with the same line, built by
`_wrap_command` in `tools/environments/base.py`:

```
builtin cd -- <cwd> || exit 126
```

Under the local and Docker backends the directory named there is on the same filesystem
as the process that created it, so the `cd` always succeeds. Under the SSH backend it is
not, and nothing bridges the gap: `tools/environments/ssh.py` defines no `_wrap_command`
of its own, and its `_ensure_remote_dirs` creates `~/.hermes` and three children and
stops. Any other working directory has to already exist on the sandbox.

The Kanban dispatcher is where that bites. `hermes_cli/kanban_db.py` allocates a
per-card scratch workspace under `workspaces_root(board)/<task id>` and `mkdir`s it — on
the agent pod's PVC — then pins the path as the worker's `TERMINAL_CWD` and launches the
worker process with the same path as its own `cwd`. The worker's terminal resolves that
as its working directory and the `cd` runs on the sandbox, which has a different
ReadWriteOnce volume. Every command a delegated card runs exits 126 with no output and
no message the model can act on. There is no shared-filesystem answer available: both
volumes are RWO and nothing here has Filestore.

Upstream calls this a defect and has not fixed it.
[NousResearch/hermes-agent#86413](https://github.com/NousResearch/hermes-agent/issues/86413)
is the general statement — `terminal.cwd` carries no filesystem namespace, five
independent resolvers disagree, guest paths are validated with a host `stat()` — and it
names the `TERMINAL_CWD` pin in `kanban_db.py` as an unexercised surface.
[#62169](https://github.com/NousResearch/hermes-agent/issues/62169) reports the hard
`|| exit 126` directly. Two patches have been proposed,
[#62189](https://github.com/NousResearch/hermes-agent/pull/62189) and the closed
duplicate [#62405](https://github.com/NousResearch/hermes-agent/pull/62405); both make a
missing directory fall back to `$HOME` rather than creating it, and a maintainer
confirmed on the latter that main still has the hard exit. So the fix has to be ours,
and it has to hold if #62189 ever lands — under that change a card would stop failing and
start silently running in `/home/agent`, which is worse. Creating the directory is right
against both.

It lives in the sandbox image rather than in a Hermes source patch. `deploy/sandbox/`
already owns `sshd_config` and the entrypoint, so there is a lever here that costs the
repository no new anchor into upstream source — every patch pair under
`deploy/docker/patches/` is another way a base-image bump breaks the build.
`sshd_config` sets `ForceCommand /usr/local/bin/sandbox-session-command` inside a
`Match User agent` block; the script reads `$SSH_ORIGINAL_COMMAND`, recovers the wrapped
script from the `bash -c '<script>'` that `ssh.py` sends, takes the target out of the
first `builtin cd --` line, `mkdir -p`s it, and then execs exactly what `sshd` would have
run. Scoped to `agent` because `hermes` — the account trusted agent-pod code connects as
for cluster commands — does not go through Hermes' terminal wrapper and has nothing to
gain. Placed below the `Include`, because a `Match` block ends the global section and
would otherwise strand the entrypoint's generated `SetEnv` in a per-user scope.

The cost of fixing it on this side is that the script parses a string `base.py` owns, and
the failure mode of a reshape is silence. That is what the drift alarm is for: a wrapper
that carries the `__hermes_ec` marker and no `builtin cd --` line makes the script write
to stderr, which surfaces in the tool result the model reads. A directory that cannot be
created is not made fatal — the `cd` fails and the command exits 126, exactly as before —
because a wrapper for a missing-directory bug must never turn a working command into a
broken one. Section 4c of `deploy/sandbox/smoke-test.sh` covers all of it over a real SSH
connection: the missing workspace, the uncreatable one, the `~` and `$HOME/'a b'` forms
`_quote_cwd_for_cd` emits, `tar` and plain commands passing through untouched, an
interactive session still getting a shell, and the drift alarm firing.

What this does not settle is where the card's output ends up. The workspace the worker
writes now lives on the sandbox's volume, the gateway never collects it, and
`kanban_db.py` deletes its own copy on completion. Workers report through
`kanban_complete` rather than by leaving files behind, so nothing known depends on it —
but a card written to hand back a file will not work, and that is a separate decision.

#### The same crossing drops `HERMES_KANBAN_TASK` and `HERMES_KANBAN_WORKSPACE`

Three probe cards run in parallel against the fixed image found the second half of the
same gap. The dispatcher sets both variables in the worker's process environment, and
nothing carries them to the far side: `ssh.py` has no environment handling at all, and
`terminal.env_passthrough` — the config key that exists for exactly this — is read only
by `code_execution_tool.py` and the local and Docker backends. Both arrive empty.

That is not harmless, because of how the worker protocol tells workers to use them. Two
of the three probes wrote `cd "$HERMES_KANBAN_WORKSPACE"`, quoted, where an empty value
is a no-op, and stayed put. The third wrote it unquoted, which is `cd` with no argument
at all — and a bare `cd` goes to `$HOME`. It wrote its output into `/home/agent`, which
every concurrent card on the pod shares, with exit 0 and nothing in the output to say
so. The workspaces themselves are isolated and the shell already lands in the right one;
it is the documented `cd` that moves a worker back out of it.

The wrapper recovers both from the cd target, which is the one place that information
survives the crossing. The derivation is deliberately narrow. It reads the
`<...>/workspaces/<task id>` prefix rather than the whole path, so a command run from a
subdirectory still reports the workspace; it accepts both layouts `workspaces_root()`
produces, the default board's `<home>/kanban/workspaces/<id>` and every other board's
`<home>/kanban/boards/<slug>/workspaces/<id>`; and it requires the component to look
like a task id under a kanban `workspaces/` directory, leaving both variables unset for
anything else rather than guessing. Absent beats wrong here — a script that builds an
absolute path from a workspace that is not its own writes outside it, which is the
failure the derivation exists to prevent. Section 4d of the smoke test covers the two
layouts, the subdirectory case, the three shapes that must set nothing, and the unquoted
idiom itself.

Only these two. The dispatcher also injects `HERMES_KANBAN_BOARD`, `_DB`,
`_WORKSPACES_ROOT`, `_RUN_ID` and others, and none of them are recoverable from a path.
They remain unset in the sandbox.

#### `kanban_complete(artifacts=[...])` checks the file on the wrong pod

A second run of three parallel probe cards, against the image carrying both fixes,
resolved the workspace and both variables correctly and then hit the third instance of
the same root cause. `kanban_complete` takes a list of scratch artifacts, and
`kanban_db.py` validates each one by expanding it with `pathlib`, resolving it, checking
it is under the workspace root, and calling `is_file()`. All four run in the gateway
process. The file is on the sandbox, so the call fails with `declared scratch artifact
is unavailable or not a regular file` for a file the worker can `cat` in the same turn.

Nothing on this side can fix it. The validation is not a path the sandbox participates
in — no command is sent, so there is nothing for the `ForceCommand` to repair — and the
gateway genuinely cannot see the file, because the two pods hold separate
ReadWriteOnce volumes. All three probes reached the same workaround unprompted:
`kanban_attach` with the content inline, which travels through the tool call rather
than through the filesystem. That is what a worker should use here, and the persona
text has not been updated to say so.

This is the concrete version of the open question above about whether anything needs to
read a card's workspace after it finishes. One thing does, and it is a documented
parameter of the completion tool.

### What the sandbox needs, and where it comes from

The shell moved; the paths it was written against did not. `/opt/data` is the agent's
home on its PVC and it is named 59 files deep in `agents/`. On the first live run it did
not exist in the sandbox at all: an environment probe dispatched as a Kanban card ran on
`platform-agent-shell-0` as user `agent` and reported `/opt/data exists: False`, so the
card's own declared workspace under `/opt/data/kanban/workspaces/` was not creatable and
the worker fell back to its home directory. Giving the sandbox's data volume the same
path (above) is necessary and was not sufficient: the paths resolve on both sides now,
against different volumes, but a directory the agent pod created still does not exist
here, which is the subject of the section above. What neither closes is content: a script
that expects to _read_ something the agent pod put at `/opt/data` still finds nothing.

Enumerating what the sandbox legitimately needs sorts the references into six classes
with four delivery mechanisms.

| What                                         | Where it comes from                                | Why                                           |
| -------------------------------------------- | -------------------------------------------------- | --------------------------------------------- |
| Persona (`SOUL.md`, `AGENTS.md`)             | stays in the agent pod                             | read into the prompt, never through the shell |
| Skills, including their `scripts/`           | baked at `/opt/defaults`, synced by the entrypoint | the existing sync delivers the wrong tree     |
| Governance SOPs                              | the same bake and sync                             | static, versioned with the repo               |
| The shell-invoked subset of `scripts/`       | the same bake and sync, as an allowlist            | static, and the subset is small               |
| `SETTINGS.md`                                | ConfigMap mounted into the sandbox pod             | per-install content, rendered by the operator |
| Outputs (`INVENTORY.md`, scratch workspaces) | written to `/opt/data` at runtime                  | data, not delivery                            |

**The persona stays behind, and that is a property rather than an omission.** Nothing
writes `SOUL.md` through the shell. The only writer is
[`profile_scaffold.py`](../../agents/platform/scripts/profile_scaffold.py), which
overlays the image-baked `/opt/platform-template` onto the profile home at pod startup;
everything else that names the file is a test fixture. So the agent cannot modify its
own persona, because the persona is not on a filesystem any of its tools can reach.
Before the sandbox that was true only by convention.

**The skills that arrive by sync are the wrong ones.** An earlier read of this said the
sync already handled it, on the strength of `github-issue-resolver`,
`submit-suggestion` and `fleet-audit` being present under
`/home/agent/.hermes/skills/`. Those three are in the intersection of two different
skill sets, which is why the spot-check passed. Diffing the sets shows what it missed:
the sandbox's synced tree holds the 40 skills of the machine-level home, and 19 of the
platform agent's own — `fleet-audit`, `pr-conversation`, and every `gke-*`
troubleshooting skill — are not among them. 22 stock Hermes skills (`apple`,
`creative`, `smart-home`) are there in their place.

The copies that do arrive are stale as well. `resolver.py` is 28091 bytes in the repo
and in the platform profile, md5 `627c7fb6`; the sandbox has a 14492-byte copy, md5
`45e687e0`, dated five days earlier and without the `sandbox_exec` routing the shell
move added to it.

Neither is a bug in the sync so much as the sync answering a different question.
`iter_skills_files` reads `_resolve_hermes_home()/skills`, which resolves through
`HERMES_HOME` to the agent's data root — the default (chat) profile's skills — rather
than to the active profile at `/opt/data/profiles/platform`. It is structurally
profile-unaware, there is no configuration that changes it, and its source directory is
one the startup skill sync marks user-modified and skips. So the sync is not the
delivery mechanism; it is a 15 MB tree in the sandbox that nothing reads. Skill
discovery happens in the agent pod, which reads `SKILL.md` from the platform profile
and puts it in the prompt, and every path a `SKILL.md` then names resolves through
`HERMES_HOME` or `TERMINAL_CWD` — both `/opt/data` in the sandbox, and both the baked
tree.

**Skills, governance and the shared scripts are baked, and synced onto the volume by
the entrypoint.** Baking them at `/opt/data` directly does not work: the StatefulSet
mounts a PVC over that path and the image's copy disappears under it. This is the
problem the agent image already solved, and the sandbox uses the same shape — the
image stages at `/opt/defaults`, and `deploy/sandbox/entrypoint.sh` copies it onto the
volume on every start, before sshd is exec'd.

The sync replaces rather than merges. Copying over the top leaves a skill deleted from
the image, or a script renamed in it, sitting on the volume for as long as the PVC
lives and looking current. That makes the trees image-owned: the model can edit a
script it is debugging and the edit is gone at the next restart, which is the same
contract the agent pod's force-sync gives. Model-written files belong in
`/opt/data/scratch` and `/opt/data/gitops`, which the sync does not touch.

Extending Hermes' sync to cover governance and scripts was the alternative, and it
keeps one mechanism instead of two. It was rejected before the measurement above and
the measurement only strengthens it: the sync is upstream behaviour scoped to
`~/.hermes`, widening it means carrying a patch, and a baked image is auditable in a
way a sync is not — what the sandbox contains is what the Dockerfile says.

**Not all of `scripts/` goes.** The directory holds over a hundred files and most are
agent-side servers and their tests — `platform_mcp_server.py`, `session_kv_server.py`,
`router_server.py`, `profile_cron_tick.py`, `credential_proxy.py`. Shipping them
wholesale would put a file named `credential_proxy.py` inside the sandbox, which is the
wrong thing for a reviewer to find even though it is inert there. So the image gets an
explicit allowlist: `sandbox_exec.py`, `forge.py`, `pr_triggers.py`,
`github_token_refresh.py`, `gitops_workspace.py`, `gke_endpoint.py` and
`cluster_preflight.sh` — the entry points an agent is told to run, plus the transitive
closure of what they import.

**The test for whether a script qualifies is what it needs, not how it is called.** An
earlier version of this proposed "shell call sites, and absent from every `jobs.json`",
and that test admits `cluster_agent_profile.py` — the script with the most shell call
sites of any, and one that cannot run in the sandbox at all. It shells out to `hermes
profile create` and writes `/opt/data/profiles` on the agent pod's PVC, as its own line
325 says: _"Stays in the agent pod: `hermes` needs the profiles on the data PVC"_. The
qualifying question is the one the cron section below already asks — does it need
agent-pod-only resources: the `hermes` binary, the profiles tree, the session or kanban
databases, Hermes' own Python namespace.

Three scripts an agent is told to run fail it: `cluster_agent_profile.py`,
`cluster_agent_reconcile.py` and `kanban_notify_propagate.py`. Each gets a stub at its
path in the sandbox that prints why it cannot run there and exits non-zero. Leaving the
path empty was the other option and reads worse — the model gets `No such file or
directory`, concludes the image is broken, and spends a turn proving it. The fuller
answer for the profile scripts is an MCP tool, since the MCP server runs in the agent
pod; `platform_mcp_server.py` exposes no profile tool today.

None of this is held together by review.
[`test_sandbox_delivery.py`](../../agents/platform/scripts/test_sandbox_delivery.py)
reads the allowlist out of the Dockerfile and checks it against the agents' own
instructions: every shared script named by a runtime path is baked or stubbed, the
allowlist is closed under import, and nothing on it names an interpreter the sandbox
does not have. Adding a skill that calls a new shared script fails that test rather
than failing in a pod.

**`SETTINGS.md` is mounted, at `/opt/data/SETTINGS.md`.** Its content is per-install:
the operator renders it from `spec.integration.github.gitRepo` into an
`<agent>-settings` ConfigMap (`buildSettingsConfigMap`) and mounts it as a subPath. The
ConfigMap already exists, so the operator mounts it a second time into the sandbox pod
— over the data volume, the way the agent container already mounts it over its PVC.

An earlier version of this rejected that path on the grounds that it makes `/opt/data`
exist in the sandbox, and that the absence of `/opt/data` is the one-line check for
whether the isolation is real. That reasoning is obsolete: giving the sandbox's volume
the same path was a deliberate later decision, and the `.sandbox` marker the entrypoint
writes is what replaces absence as the tell. Mounting at the path the parsers already
hardcode (`resolver.py:18` with no override, `audit_report.py:4079` behind
`FLEET_AUDIT_SETTINGS`, `gitops_workspace.py:119` off `agent_home()`) therefore needs
no parser change. The mount is optional, unlike the agent container's: the ConfigMap
and the StatefulSet are separate objects, and a sandbox that will not start because one
is briefly missing takes the agent's whole shell down, while a skill reading an absent
`SETTINGS.md` fails on its own terms.

A subPath mount is resolved once at pod start and is never refreshed, so the sandbox
pod template carries the same `kubeagents.x-k8s.io/settings-config-hash` annotation the
agent's Deployment does. Without it, editing the CR's scope rolls the agent pod onto the
new file and leaves the sandbox holding the old one — and the sandbox is where the shell
reads it, so the six skills that read `SETTINGS.md` by path would be the ones served the
stale answer, indefinitely and with nothing in either pod's logs to say so.

**`HERMES_HOME` and `PLATFORM_AGENT_HOME` are set in the sandbox**, both to its own
`/opt/data`. sshd starts sessions with neither, and the delivery is only half done
without them: a `SKILL.md` writes `"$HERMES_HOME"/scripts/…` about as often as the
literal path, `cluster_preflight.sh` defaults `HERMES_HOME` to `/opt/data` and would
check the wrong tree if that default moved, and `gitops_workspace.agent_home()` reads
`PLATFORM_AGENT_HOME` to decide where a leased clone goes. They are set from the
sandbox's own data path rather than forwarded from the agent container, because the two
roots are different volumes that happen to share a path — forwarding the agent's value
would point every skill here at a directory this pod does not have the moment an
install moves `spec.harness.hermes.agentHome`.

**Four scripts named an interpreter the sandbox does not have.**
`github_token_refresh.py`, `gitops_workspace.py`, `audit_report.py` and
`submit_suggestion.py` began `#!/opt/hermes/.venv/bin/python3`, a path that exists in
the agent image and not in this one, so `./audit_report.py` here died with `No such
file or directory` naming an interpreter rather than the script. They now use
`/usr/bin/env python3`, which is what the other 18 shared scripts already used. Nothing
in the four imports a third-party module, so neither image cares which Python answers;
`python:3.11-slim` has no `/usr/bin/python3` at all, so there was nothing to fall
through to.

#### Cron scripts stay in the agent pod and reach into the sandbox from there

A `no_agent` cron job — one carrying a `script` rather than a `prompt` — is unaffected
by the terminal backend. The scheduler's `_run_job_script` resolves the script against
`HERMES_HOME/scripts`, rejects anything resolving outside it, picks `/bin/bash` or
`sys.executable` by extension, and calls `subprocess.run` with an environment from
`build_subprocess_env` — imported from `tools.environments.local`, hardcoded. There is
no backend lookup anywhere in the scheduler. The script is a subprocess of the gateway
process, in the agent pod. A cron job with a `prompt` behaves the opposite way: it runs
a real turn, and that turn's tools go to the sandbox.

This is the outcome to want. It keeps the no-LLM guarantee that the `no_agent` mode
exists for, and it is what lets all five of these scripts keep their PVC, their
`kanban.db`, and their `hermes` binary while the shell moves away.

It also closes a path that was open before. `HERMES_HOME/scripts` holds trusted code
executed agent-side with full credentials, and until now the model could `write_file`
into that directory and then register a cron job pointing at it. With file tools routed
to the sandbox it cannot, which raises the stakes on the `sync_back` channel above:
whatever that syncs back must not be able to land in a scripts directory.

What it does break is bootstrap onboarding, and quietly. The inventory pipeline
straddles the boundary in the wrong direction: `INVENTORY.raw.md` is written by the
`platform` kanban worker and `INVENTORY.md` by the prioritization worker — both agent
turns, so both writes now go to the sandbox, where `/opt/data` neither exists nor is
writable. The readers did not move. `bootstrap_delivery.py` and `bootstrap_scan_gate.py`
are `no_agent` scripts hardcoding `/opt/data/INVENTORY*.md` on the PVC, so the delivery
job ticks every minute against a file that will never appear. A silent run is its normal
no-op, so onboarding simply never delivers and nothing logs an error.

**Moving the scripts into the sandbox was the obvious fix and does not work.** The
mechanism is sound — `subprocess.run(capture_output=True)` returns stdout verbatim and
`ssh` forwards both remote stdout and the remote exit code, so verbatim delivery
survives the hop. There is just nothing left to wrap. Every one of the five is bound to
the agent pod: `profile_cron_tick.py` and `cluster_agent_reconcile.py` drive `hermes`
against profile state on the PVC; `bootstrap_scan_gate.py` shells
`/opt/hermes/.venv/bin/hermes profile list`; and `bootstrap_delivery.py` and
`github_scan_gate.py` import Hermes' own Python namespace — `from cron.jobs import
remove_job` and `from hermes_cli.kanban import run_slash`, which no amount of packaging
reproduces in the sandbox. Moving them would make both of the deferred problems below
blocking instead.

Two lesser obstacles point the same way. The scheduler passes `job.get("script")`
straight to path resolution with no `shlex` and no arguments field, so a generic
`run_in_sandbox.sh <script>` entry cannot be expressed without patching Hermes. And a
volume shared between the two pods is unavailable regardless: `platform-agent-data` and
`workspace-platform-agent-shell-0` are both `ReadWriteOnce` on `standard-rwo`, so two
pods cannot mount one, and `ReadWriteMany` would mean Filestore.

**So the script stays in the agent pod and reaches into the sandbox for the part that
belongs there.** The agent pod already has `/usr/bin/ssh`, the client key at
`/etc/sandbox-ssh`, and `known_hosts` on the PVC, so this needs no new infrastructure
and no new trust: the agent pod is the privileged side and already holds the key.
Direction is the whole argument. The agent pod reaching into the sandbox grants nothing
the sandbox did not already have; the reverse — the sandbox reaching into the agent pod —
is what the isolation contract rules out. The artifact crossing back is model-authored text
that was already going to the user verbatim — it is read, never executed — and no model
sits in the delivery path, so the `no_agent` guarantee holds unchanged.

`github_scan_gate.py` is the clean case, because it already has the seam. Its agent-side
half files a kanban card through `run_slash`; its other half is `resolver.py poll`, which
needs only `gh` and the standard library and returns JSON on stdout. That half is a skill
script under `github-issue-resolver/scripts/`, which Hermes' sync already places in the
sandbox — so the split is one function, `run_resolver_poll`, becoming an `ssh` call.
That split is now the smaller half of what has already happened. `resolver.py` reaches
`gh` through `sandbox_exec.run`, so every `gh` call the poll makes executes in the
sandbox today; what is left to move is the script around them. It waits on a working
`gh` in the sandbox, which is what a co-located proxy provides — the next section states
the cost of the interval before it.

#### Nothing collects the sandbox's finished workspaces, so a cron job does

Hermes removes a card's scratch workspace from one place: `_cleanup_workspace`, called
by `complete_task` after the transaction commits, best-effort with the exception
swallowed. There is no periodic sweep anywhere in `kanban_db.py`, and that single call
site misses more than it catches. On the month-old install this was measured against,
`/opt/data/kanban/workspaces` held 33 scratch directories — 20 of them `done`, 2
`cancelled`, and only 11 belonging to live cards. All but one of the `done` ones had no
children at all, so the deliberate active-children deferral does not explain them: a
card that reaches a terminal state by any route other than `kanban_complete` never
reaches the call site, and `cancelled` and `failed` never reach it by any route.

The sandbox turns that into a second leak with no cleanup path at all, because
`_cleanup_workspace` calls `shutil.rmtree` in the gateway process on the gateway's path.
Under `terminal.backend: ssh` the directory the worker actually wrote to is on the
sandbox's own ReadWriteOnce volume, which that call cannot see — the same
host-operates-on-a-guest-path shape as the rest of
[the backend's defects](#the-ssh-backend-is-unfinished-and-this-design-carries-the-workarounds).

`kanban_workspace_gc.py` reconciles both sides, daily, as a `no_agent` job on the
platform roster. It is the third shape of cron job on that roster and it belongs in the
agent pod for a reason neither of the others has: its authority is the board DB, and the
board DB is here. `kanban_home()` resolves through `get_default_hermes_root()`, which
strips the `/profiles/<name>` suffix, so a job running under the platform profile
reaches the one shared board rather than forking a per-profile view of it.

A sweep rather than the `kanban_task_completed` plugin hook, which is the other
mechanism Hermes offers. The hook fires on precisely the path that already works, while
the leak is in the paths that have none; a reconciler is also self-healing after a
missed event, where a hook is one more thing that can miss one. It fires in the worker
process too, so it would need the same SSH call regardless.

Two details carry the safety of it. The removable set is derived from task rows alone —
terminal status, `workspace_kind='scratch'`, no non-terminal children, and a path that
is a direct child of that board's `workspaces_root()` with the name the dispatcher mints
— which reproduces Hermes' own `_is_managed_scratch_path` containment guard and keeps
the sweep away from the task-shaped directories other code paths leave elsewhere. A live
sandbox has `/opt/data/tmp/t_384aaaba` and `/opt/data/gitops/t_dc3f1647`, and a
`find -name 't_*'` would have taken both. The sandbox's own directory listing is then
used only to narrow that set, never to add to it, so the account the model owns can at
worst hide a directory from the sweep.

It connects as `agent` rather than `hermes`, which is the one place in the repository
that does, and the reason is permissions: the workspaces are `agent:agent 755` to the
leaves, so uid 1001 cannot unlink inside them. The alternative was a shared group, a
setgid workspaces root and a `umask 002` for every session, which grants the trusted
account standing write access to the model's tree in order to delete from it — a wider
change than the narrower login. What makes the narrower login safe here does not
generalise, and `sandbox_exec.TERMINAL_PRINCIPAL` says so: this caller reads no output
as a fact about the cluster, and a `.bashrc` that hijacked its `rm` would be doing to
uid 1000's own files what uid 1000 can already do. The commands are `/bin/ls` and
`/bin/rm` by absolute path, which that file cannot shadow — a bash function name cannot
contain a slash, and a non-interactive shell does not expand aliases.

#### The agent pod gives up cluster tooling entirely

Sandboxing the shell does not, by itself, take `kubectl` away from the agent. The
`platform-agent` container never held a native `kubectl`, `gcloud`, `gh` or `git` — the
four were symlinks to `credential-proxy-exec`, and the real binaries live only in the
credential-proxy image — but a symlink is a working credential path, and the
model can reach one without going near a shell.
[`platform_mcp_server.py`](../../agents/platform/scripts/platform_mcp_server.py) is the
proof: it is launched as a stdio MCP server from `agents/platform/config.yaml:30`, runs
in the agent pod, and shells out at eleven sites — `kubectl logs`, `kubectl describe`,
`kubectl get pods`, `gcloud logging read` among them. Those are model-facing tools. The
shell moving to the sandbox does nothing to them.

So the decision is that the agent pod holds no way to invoke cluster tooling in any
form: the symlinks and eventually `credential-proxy-exec` itself leave the agent image,
and the image gains the same build-time guard the sandbox image already has. The sandbox
becomes the only place a credential-proxy call can originate.

All four names left in one change, along with `helm`, `k9s` and `yq` — the utility CLIs
that were in the agent image only because the shell was. `/opt/credential-proxy` went
with them, so there is no `credential-proxy-exec` left for a symlink to point at. Two
guards keep it that way. The `agent-base` stage checks the seven names where `git` is
purged, which catches a real binary; the guard at the end of the `platform` stage runs
last and so sees everything both stages wrote, and it fails the build if
`/opt/credential-proxy` exists or any of the seven resolves. The second is what catches
a reinstated shim, which `command -v` in `agent-base` cannot see, because that directory
is not on the build PATH.

`gh` needed one step the others did not.
[`github_scan_gate.py`](../../agents/platform/scripts/github_scan_gate.py) runs
`resolver.py poll` as a `no_agent` cron script in the pod, and the resolver shells out to
`gh` at every call site. Both modules funnel those invocations through one function —
`forge.run_gh` and `resolver._run_gh_once` — so routing that pair through
`sandbox_exec.run` carried the whole sweep across without moving the script. Both files
also run on the far side of the boundary when the model invokes them from its shell, and
one call site serves both: `sandbox_enabled()` reads an agent-pod file, so in the sandbox
it is false and `run()` executes locally.

`git` is the one that turns on placement. `credential_proxy.py::_execute` confines a git
command's working directory to `CREDENTIAL_PROXY_WORKSPACE_ROOT` and re-runs it on the
proxy's own filesystem, so a `git` in the sandbox image would need the proxy to see the
same tree. It was removed rather than left as the one credentialed binary in the container
this section exists to disarm, and the shim replaces it once the proxy is a container of
the same pod.

**What that costs on an install without federation.** The sandbox image ships the four
proxy shims at `/opt/credential-proxy/bin/` and puts that directory first on `PATH`, but
`CREDENTIAL_PROXY_URL` is set only when the proxy is co-located —
`buildShellSandboxStatefulSet` omits the variable while the URL is empty — so on a
standalone-proxy install each of them resolves and then exits 1 with
`CREDENTIAL_PROXY_URL is not configured`. `gh` in that state takes the `*/10`
`github-repo-watcher` sweep down: it reports a fault every tick rather than the
repository's open work, and `git` takes the GitOps and pull-request write paths with it.
Pointing the variable at the standalone Service would not recover them, because that proxy
sees neither the caller's `cwd` nor its files; co-location is what returns them.

Agent-side callers reach the tooling the same way everything else in this section does —
by executing in the sandbox over SSH. `platform_mcp_server.py` (11 sites),
`cluster_agent_reconcile.py` (3), `cluster_agent_profile.py` (1) and `gke_endpoint.py`
(1, a capability probe) share `sandbox_exec.py`, which reads `terminal.ssh_*` from the
managed config at `/etc/hermes/config.yaml` rather than re-deriving the address. Nothing
else in `agents/` needs it for a cluster command: the remaining callers —
`gitops_workspace.py`, `github_token_refresh.py`, `cluster_preflight.sh` — are invoked
from the shell and so already run in the sandbox, and none of them touches a cluster.
`resolver.py` is the exception the previous paragraph names: the model invokes it from
the shell, but `github_scan_gate.py` also invokes it from the pod, so it runs on both
sides.

Two calls in those files stay in the agent pod, and neither is an exception to the rule
above: `hermes send` and `hermes profile delete` are not cluster tooling. They need the
profiles on the data PVC and the gateway on loopback, and the sandbox image does not
carry the binary. The overlap in names is unfortunate — the SSH principal below is also
called `hermes` — and is the one thing to check when reading a diff against these files.
Which SSH identity that helper uses is the subject of the next section, and is not the
one configured today.

**This makes the sandbox required.** With nothing left in the image to fall back to,
`shellSandbox` disabled is a configuration in which the MCP tools fail — `sandbox_exec`'s
local branch runs `subprocess.run(["kubectl", …])` against an image that has no
`kubectl`, and reports the `FileNotFoundError` honestly. That is accepted rather than
worked around: keeping a local path alive for the disabled case would keep the exact
capability this removes, and an image that behaves differently depending on a CR field is
harder to reason about than one that does not carry the binaries at all. Removing the
toggle is the follow-through and is not yet done.

Removing the symlinks does not by itself make the agent pod unable to reach a proxy that
is still in it: a `curl` to `127.0.0.1:8765` is a working path, and the relay code
legitimately posts there. Reaching it requires arbitrary code execution in that pod, and
with the shell, the file tools and `execute_code` all in the sandbox the model has no path
to that. What is left in the agent pod is trusted code: the MCP server, the cron scripts,
the gateway. The point of the proxy is that raw credentials never reach the agent, not
that no process can invoke a command, so this is the property that matters. Co-locating
the proxy with the sandbox settles the rest of it: the exec listener is then on the
sandbox pod's loopback, and the agent pod has no route to it at all.

One entanglement moves with the proxy. `gcloud container clusters get-credentials` writes
a kubeconfig that the proxy validates with `_within_workspace`
(`credential_proxy.py:1044`), which needs proxy and caller to see the same file. `/opt/data`
mounted `rw` into the gateway container and a sidecar is what used to make that true, and
a `ReadWriteOnce` PVC cannot be stretched across two pods to keep it. Co-location restores
it on the sandbox's own data volume, mounted into the shell container and the proxy
container alike — which is the same fact that makes proxied `git` work, stated for
`kubectl`.

#### The SSH principal cannot be the shell user

The sandbox restricts what the model's code can reach, not what the agent pod may do.
Trusted agent-side code moving into the sandbox is relocation, not demotion: it runs
whatever it needs to, on a different machine. The only thing that machine changes is that
the model's code is now on it too, which means trusted execution there needs a principal
the model cannot influence. It does not have one today.

`terminal.ssh_user` is `agent`, uid 1000 — the user whose shell the model drives, whose
home directory it writes to, and who owns `/home/agent/.bashrc`. Bash sources `.bashrc`
for a non-interactive `ssh host cmd`, so an agent-side caller that reaches into the
sandbox as `agent` executes the model's startup file before its own command. The stock
Debian guard at the top of that file returns early for non-interactive shells and hides
this, but the model owns the file and can delete the guard; moving an `export` above it
is enough to prove the mechanism, and the environment does cross.

The consequence is not that the model gains a capability. It already has a shell in the
sandbox and can run `kubectl` there whenever it likes. The consequence is that it can
choose what the agent-side caller _sees_ — shadow a binary earlier on `PATH`, define a
function, and hand back output that arrives as a trusted tool result. Forging the answer
to a diagnostic is a better attack than running the diagnostic.

So the SSH helper does not authenticate as `agent`. The sandbox image adds a second
principal with its own uid, its own home and its own `authorized_keys`, and the agent
pod's key authorises that principal only. `sshd` is already running and the key
distribution pattern already exists, so this is a second key pair rather than a second
authentication system. Two details it has to get right: the `SetEnv` that carries
`CREDENTIAL_PROXY_URL` and the credential-proxy `PATH` is written once by
`entrypoint.sh`, and `sshd` keeps the first `SetEnv` directive it parses and silently
discards the rest — so covering both principals means one directive that applies to both,
or a `Match User` block, not a second global line. And the helper must not build its
subprocess environment with `_run_env()`, which is `{**os.environ, "HOME": "/tmp"}` and
would hand the whole agent-pod environment to the `ssh` client. Nothing crosses today —
`sshd_config` sets `PermitUserEnvironment no` and `AcceptEnv LANG LC_*` — but that is the
remote end declining to accept what the local end should not have offered.

This settles the transport question for `platform_mcp_server.py`, which was the one
caller large enough to argue about. Running it in the sandbox and reaching it over HTTP
was the alternative: it would put the tools next to the binaries and make the
`_run_env()` leak harmless, since the sandbox environment holds nothing worth taking. It
was rejected on cost. It needs a bearer token in a mounted Secret, a Service, a readiness
probe and a supervised server process — a second mechanism running parallel to an SSH
helper the three scripts need anyway — and it needs the file split, because
`send_notification` reads `SESSION_KV_API_KEY` and the module is also the parent process
of the Session KV server, so moving it wholesale would put the incident's exact target
inside the sandbox. The dedicated principal, meanwhile, is not a cost the HTTP design
avoids: the three scripts need it either way. Once it exists, the MCP server using the
same helper is nearly free.

It also degrades better. Hermes recovers a dropped MCP transport with five retries at
one, two, four, eight and sixteen seconds and then parks the server, deregistering its
tools and self-probing every five minutes. Against an eviction that reschedules to
another node — which the sandbox's `ReadWriteOnce` PVCs guarantee is slow, since they
have to detach and re-attach — that budget is exhausted, and the tools disappear from the
model's toolset for up to five minutes. Per-call SSH has no such state: the sandbox being
down is an error on the call the model made, which it can see and react to. The cost is a
handshake per call, and OpenSSH 10.0 in the agent image supports `ControlMaster` with
`ControlPersist`, so the calls multiplex over one connection.

One correctness requirement, distinct from the security one above. Building a remote
command string means the sandbox's shell parses it, so every model-supplied argument — a
namespace, a pod name, a label selector, the `audit_log_searcher` filter — needs
`shlex.quote`. This is not a boundary crossing, since the model already has that shell.
It is that a pod name with a quote in it must not silently produce the wrong command.

Whether the six tools earn their place at all is a separate question, deferred to its own
issue. The proxy policy is a denylist of credential-disclosure patterns rather than an
allowlist of subcommands, so these tools wrap commands the model can already run from the
shell, and they may be worth less than the surface they add.

#### Three problems deferred, and what has already been ruled out for them

None of them blocks the work above. All three are recorded here so the dead ends
are not re-walked.

**Reaching `kanban.db`.** Two scripts touch the board.
[`kanban_board_health.py`](../../agents/chat/scripts/kanban_board_health.py) never
opens the file — it shells out to `hermes kanban diagnostics --json`, and says at line
29 that opening `/opt/data/kanban.db` from an agent shell is what the persona forbids.
[`kanban_notify_propagate.py`](../../agents/platform/scripts/kanban_notify_propagate.py)
does open it, `sqlite3.connect` at line 63 — and `SOUL.md:61` tells the agent to run it
from the shell. That is coherent today, where `SOUL.md:66`'s ban on touching the board
is a ban on ad-hoc edits and the script is a sanctioned writer, but it does not survive
the move.

Mounting `kanban.db` into the sandbox is ruled out. It would hand the shell exactly the
write path that the rule exists to close, after a worker used that path on 2026-08-07
to mark three cards `done` with an invented result. Under the split,
`kanban_board_health.py` stays agent-side and stops being a problem;
`kanban_notify_propagate.py` needs to become something the agent calls rather than
something it runs.

**Executing `hermes`.** Exactly one capability is invoked from sandbox-side prose:
`hermes cron run <job-id>`, at `agents/platform/AGENTS.md:32` and
`agents/platform/skills/fleet-audit/SKILL.md:53`. Both spell it
`/opt/hermes/.venv/bin/hermes`, which is absent from the sandbox twice over. The other
matches across `agents/` are either prose mentioning the binary or agent-side processes
that stay put; `agentplugins/gke-stockout-investigator/scenarios/lib/common.sh:617`
runs `hermes kanban ls --json` and has not been classified.

Installing `hermes` in the sandbox image is ruled out. The command needs
`HERMES_HOME=/opt/data/profiles/platform` — live profile state on the agent's PVC — so
a `hermes` in the sandbox would have nothing to act on, and giving it something means
mounting the profile tree there.

Making `hermes` a fifth wrapped executable on the credential proxy is the other
possibility, and where the proxy runs rules it out. The pattern fits —
`credential_proxy.py` already forwards argv, runs the real binary on the trusted side, and
enforces a per-executable subcommand policy — but `HERMES_HOME` is on the gateway pod's
data PVC, which is ReadWriteOnce and mounted by the gateway alone, so a wrapped `hermes`
executing in the proxy container could reach no profile state. The proxy is the sandbox's
path to _credentials_; it is not a general path back into the gateway pod, and anything
needing gateway-side state has to execute there.

`cronjob(action='run')` is the nearest existing tool and is not equivalent:
`AGENTS.md:37` records that in several runtimes it executes the job synchronously
inside the calling session, which is the behaviour `hermes cron run` was chosen to
avoid.

**A file written in the shell is not a file the agent can read.** The shell runs
in the sandbox pod, which has its own `/opt/data`; the gateway has a different
`/opt/data` on its PVC. A task that writes a scratch artifact — an audit CSV, a
rendered manifest, a diff staged for a PR — writes it on the sandbox's volume,
and every agent-side reader looks on the gateway's. Nothing errors. The file is
simply not there, and the failure reads as the task having produced nothing.

This one has no workaround in this design and is not a defect in it: it is the
split doing what the split does. Naming it as deferred rather than solving it
here is deliberate, because the shape of the fix is a decision about what is
allowed to cross the boundary, and that is worth its own design rather than a
mechanism chosen inside an isolation change. The options:

- **A ReadWriteMany volume mounted at the same path in both pods.** Everything
  works unchanged. It also re-establishes a filesystem shared between the
  trusted and untrusted sides, which is most of what the split just removed —
  narrower than the whole PVC, but the same class of channel, and it costs a
  Filestore instance or a GCS Fuse mount on every install.
- **Fetch on demand.** Agent-side readers fall back to pulling the file over the
  existing SSH connection when it is absent locally. No new mount and no new
  trust, but it needs a hook in every reader, and the count of independent path
  resolvers above is the reason to expect that list to be wrong.
- **Declared writeback (preferred).** The task names what should cross, and
  those files — and only those — are copied back over the connection that is
  already there. This is the shape `kanban_attach` already has and already
  works with, generalised beyond kanban. Nothing crosses implicitly, the
  crossing is auditable, and the content can be treated as untrusted at the
  point it lands because there is a point where it lands.
- **Accept it.** Document that artifacts do not survive the shell and require
  content to come back through tool return values. Free, and wrong for anything
  larger than a return value comfortably carries.

Re-enabling Hermes' own `sync_back` is not on the list: it is the channel
[closed above](#the-residual-channel-sync_back), and reopening it to move a CSV
would also reopen the write path into the agent's skills tree.

Declared writeback is preferred, but a design is still owed before anything is
built: who triggers the copy and at what point in a task's life, where the files
land on the agent side and who cleans them up, what the size and count limits
are, and whether the landing directory is treated as untrusted input by whatever
reads it next.

### Three of the proxy's five roles move

"The credential proxy" is five roles fused into one container, and they do not all want
the same placement:

| Role                                 | Credential held                      | Placement constraint                                  |
| ------------------------------------ | ------------------------------------ | ----------------------------------------------------- |
| Credential exec broker (Envoy → UDS) | GCP via ADC, kubeconfig              | beside the shell, sharing its working tree            |
| PlatformAgent API proxy (`:8643`)    | `API_SERVER_EXTERNAL_KEY`            | already authenticated and network-exposed             |
| k8s-event-watcher                    | `KUBECONFIG`, `SESSION_KV_API_KEY`   | posts to the Session KV over **pod loopback**         |
| Slack relay                          | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | `SocketModeClient` WebSocket — **stateful singleton** |
| Google Chat relay                    | GCP via ADC (Pub/Sub)                | inbound pump; must deliver into the gateway           |

The relays move with the broker rather than staying behind. The Google Chat relay
authenticates to Pub/Sub through ADC, so leaving it in the gateway pod would mean that pod
still needs Workload Identity and the exfiltration path survives intact. **Everything
using ADC has to leave together or none of it does.**

The other two cannot leave, because both have a peer on the gateway pod's loopback. The
**PlatformAgent API proxy** forwards to `127.0.0.1:8642`, which is the Hermes gateway; it
is not an internal detail, since `buildPlatformService` publishes port 8642 with
`targetPort: 8643`, so this listener _is_ the agent's external API front door. The
**k8s-event-watcher** posts to `127.0.0.1:8699` and reads `--profiles-dir` off the
ReadWriteOnce data PVC that only the gateway pod mounts.

So `CREDENTIAL_PROXY_ROLE` selects which services a container starts:

| Role          | Starts                                               | Runs in                              |
| ------------- | ---------------------------------------------------- | ------------------------------------ |
| `credentials` | credential exec broker, Google Chat and Slack relays | the sandbox pod, beside the shell    |
| `agent-api`   | API authenticator, k8s-event-watcher                 | the gateway pod, as `agent-api-auth` |
| `full`        | all of them                                          | nothing, now — the default           |

`full` is the default, so an image paired with an operator that does not set the variable
starts every service. The `agent-api` container loads no policy and builds no
`CommandExecutor`: it compares one key and forwards, holding no credential path. It is
called `agent-api-auth`, and `envoy-credential-proxy` names the `credentials` container —
the one that does proxy credentials.

### How the proxy gets a token

`google.auth.default()` picks up an `external_account` credential file rather than the
metadata server, and the operator points it there with `GOOGLE_APPLICATION_CREDENTIALS`
and `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` — `gcloud` needs the second or it falls
through to the metadata server on its own.

That file is written at container start by
[`wif_credentials.py`](../../agents/platform/scripts/wif_credentials.py), which
`start-services.sh` runs before anything else, from the four `CREDENTIAL_PROXY_WIF_*`
variables the operator sets. It names the projected token as a JWT credential source, the
pool provider as the audience, and the GSA as an impersonation target; STS returns a
federated token, IAM Credentials exchanges that for the GSA's access token. Nothing is
cached to disk beyond the document itself, which is written 0600 into the memory-backed
`credential-proxy-runtime` emptyDir and regenerated every start.

Two failure modes are deliberately loud. A half-filled `workloadIdentityFederation` block
never reaches the container — `credentialProxyFederation` treats it as absent, so the
proxy stays in its own pod on the metadata server — and a complete block with no token
file mounted exits non-zero under `set -euo pipefail`, crash-looping the container rather
than starting a proxy that fails every command.

### The one thing federation does not cover

Federation returns access tokens. GitHub is reached through a different kind of
credential: `github_token_refresh.py` presents a Google **ID** token to the token broker,
whose scope rule matches on `assertion.email`, and the broker answers with a
repository-scoped installation token. Under the metadata server `gcloud auth
print-identity-token` mints that ID token. Under an `external_account` credential the same
command refuses outright — _Invalid account type for `--audiences`. Requires valid service
account._ — so the co-located proxy could reach GCP and not GitHub, which surfaces as
`could not read Username for 'https://github.com'` from a `git` that never had a
credential helper configured.

`wif_credentials.fetch_identity_token` is what closes it, in the two calls gcloud will not
make: exchange the projected token at STS for a federated access token, then call
`generateIdToken` on the service account. The federated token authorises that second call
rather than the impersonated one, because `iam.workloadIdentityUser` — the binding every
install surface already creates — carries `getOpenIdToken`, whereas reaching
`generateIdToken` with the service account's own access token is self-impersonation and
would need a `tokenCreator` binding on itself that nothing grants.

The token it returns is issued by `accounts.google.com` and carries the service account's
email, which is what
[`configmap.yaml.template`](../../k8s-operator/config/integrations/github/configmap.yaml.template)
already matches on — so no broker-side configuration changes when an install moves the
proxy into the sandbox pod. The function returns `None` under any other credential type,
which leaves every other placement on the gcloud path unchanged.

`wif_credentials.py` is on the sandbox image's allowlist COPY as well as the proxy's,
because `github_token_refresh.py` imports it at module scope and that script is baked into
both. The sandbox copy never reaches the federated branch — `CREDENTIAL_PROXY_URL` is
always set there, so it delegates to the proxy and returns — but an import that is not
satisfied fails on line 1 regardless of which branch would have run.
`test_sandbox_delivery.py` holds the two lists together.

### What the gateway pod is left holding

With the proxy gone, the shell in the sandbox, and `automountServiceAccountToken: false`
already in place, the gateway pod's credential surface is `SESSION_KV_API_KEY` and
`SESSION_KV_SALT` — and Part A removes those too by putting the Session KV server behind
its own interface. It keeps the `iam.gke.io/gcp-service-account` annotation on
`kubeagents-platform-agent`, which is the remaining gap: no process in that pod uses it,
but it is still mintable from `169.254.169.254` by anything that gets execution there.

### Cron jobs that authenticate without a model turn

Emptying the gateway pod of credentials breaks the one class of work that still needs them
there. A roster entry marked `no_agent` runs as a Python subprocess on the gateway rather
than as a model turn, so it never touches the terminal backend and never reaches the
sandbox. `refresh_git_credentials` in `agents/platform/scripts/github_token_refresh.py`
prefers `CREDENTIAL_PROXY_URL` and falls back to `gcloud auth print-identity-token`; with
the variable gone from the gateway and no `gcloud` in the agent image, both branches are
now dead and the job fails with `No such file or directory: 'gcloud'`. On the reference
install this surfaced as the shipped GitHub repo watcher failing on every ten-minute tick
from the moment these images went live. Model-driven crons are unaffected: their shell work
runs on the sandbox terminal backend, where the proxy is co-located and answers on loopback.

Restoring `CREDENTIAL_PROXY_URL` on the gateway would fix it and undo the change — it puts a
credential path back in the pod this design exists to empty. The fix is to give the
subprocess jobs the same route the model's shell already takes and mint through the sandbox,
which also settles what `no_agent` should mean once the gateway holds nothing: either the
entry declares that it needs credentials and is scheduled into the sandbox, or it is
restricted to work that needs none. That is not implemented here.

### Caller authentication

Co-located, the exec path is back on `127.0.0.1:8765` and needs no bearer token: the one
caller that can reach it is the container it exists to serve.

The proxy Service keeps its name and port across both placements, with only its selector
swinging to the sandbox pod, because the gateway's relay clients dial it by name and
should not have to know where the relays run.

In the standalone fallback the exec path is on `0.0.0.0` behind a ClusterIP with nothing in
front of it. A bearer token mirroring `API_SERVER_EXTERNAL_KEY` is the fix and is not
implemented; the NetworkPolicy that ships instead is inert on the reference install
(`addonsConfig.networkPolicyConfig.disabled: true`, no Dataplane V2). Be precise about
what a bearer token would buy in any case: it is a **multi-tenancy control** that stops
another workload borrowing the agent's credentials, and not an agent-containment control,
because the agent's shell legitimately holds it.

### The relay path

`GOOGLE_CHAT_RELAY_URL` points at the proxy Service rather than `127.0.0.1`;
`SLACK_RELAY_URL` stays on loopback, and the gateway needs no new ingress rule for either.
Both Google Chat directions are gateway-initiated pulls against `/v1/chat/events` on the
relay, so moving the relay out of the gateway pod does not reverse the direction of any
connection.

### The workspace check

`credential_proxy_client.py` posts `"cwd": os.getcwd()` on every request, and the proxy
refuses any cwd outside `CREDENTIAL_PROXY_WORKSPACE_ROOT`. That assumes proxy and caller
see the same filesystem, and co-location is what makes it true again — the two containers
mount the same data volume, and `shares_filesystem_with_proxy()` sends the `cwd` and
`kubeconfig` because the endpoint is loopback.

The `cwd` is **self-reported by the caller**. It is a guardrail against the agent wandering
out of its workspace by accident, not a control against one that intends to. Anything
relying on it as a security boundary should stop.

### Content-passing removes the shared tree

The shared writable tree is what
[What co-location costs](#what-co-location-costs) is mostly about. The agent writes
`.git/hooks/pre-commit`, the broker runs `git commit` in that tree, and the hook executes in
the container holding the token. The proxy answers by pinning `core.hooksPath=/dev/null` and
`protocol.ext.allow=never` and refusing an argv that overrides either — which closes the two
routes it names, and leaves open the question of how many there are.

That question does not terminate. Git's exec surface is a feature surface: aliases, the
editor and the pager, credential helpers, clean and smudge filters, `ext::` transports,
`core.fsmonitor`, submodule commands. An overnight enumeration pass found sixteen distinct
paths; a second pass over already-reviewed code found nine more. Every one of them is
configured, and configuration for a repository lives in `.git/config` — which is why the
class closes when the agent stops having a `.git`, and not when the list of pinned keys gets
longer. A `.gitattributes` naming `filter=evil` is a file the agent can still write; without
`filter.evil.clean` in `.git/config` it names a driver that does not exist, and git leaves the
content alone.

So the exchange stops being a directory and starts being content. The agent hands the broker
`{path, contentBase64}` pairs and a commit message; the broker owns the only checkout, on a
volume the agent does not mount, and commits and pushes there. `content_workspace.py` holds
the seven verbs — `open`, `read`, `list`, `grep`, `commit`, `push`, `close` — reached over the
same loopback endpoint as `/v1/exec`, and `credential_proxy_client.Workspace` is the client. The
broker refuses a path that is absolute, contains `..`, or sits behind a symlink, and it
answers `list` from `git ls-files` rather than from a filesystem walk, so its own `.git` is
not nameable. `commit` continues `origin/<branch>` when the remote already has it rather than
recutting from the base, because recutting is how a second round of review feedback silently
deletes the reviewed commits; `push` uses `--force-with-lease` and does not fetch immediately
before it, since fetching first moves the very ref the lease compares against.

`assert_disjoint_roots` runs at construction and refuses to start the broker if its tree root
sits inside the agent-shared workspace, and `validateExtraVolumeMounts` in the operator
refuses a CR that mounts a broker-owned volume into the agent container. Neither is an
escape the agent could attempt — both are configuration mistakes that would apply cleanly and
produce no symptom.

**Where git is issued from today.** Auditing this before building it mattered, because a
broker that ran git of its own would have had the same problem one container over. Every git
in the product is agent-issued: the skills call `gitops_workspace.run_git`, which posts to
`/v1/exec`, which reaches `CommandExecutor.execute`. The broker's own non-agent-selectable
path is `execute_internal`, and its only caller is `/v1/github/refresh`, which runs
`git config --get remote.origin.url` and nothing else. The content workspaces run their git
through `execute_workspace_git`, which shares `_execute` with the agent-facing path — so it
inherits the same hardened environment — but is not reachable from `/v1/exec` and takes no
agent-supplied argv: the subcommands are literals in `content_workspace.py`, and the only
caller-supplied strings in them are a validated branch name and validated repository-relative
paths. That separation is what lets the agent-facing git surface go to zero once the skills
migrate, rather than going to zero by accident.

**What runs which way.** Both mechanisms are live at once, because a fleet does not upgrade
atomically. `harness.experimental.shellSandbox.contentWorkspaces` arms the broker, by way of
`CREDENTIAL_PROXY_CONTENT_WORKSPACES` on the co-located container; a skill asks it once per
process whether the routes exist and takes the same fork for the whole run. An unreachable
broker answers "no" and the run publishes through the leased clone, which is the one question
in a run where falling back beats failing — every other call still fails loudly. The
migrated skills are `submit-suggestion` and `fleet-audit`, and `fleet-audit` needed two new
commands to replace what the clone used to answer: `list` names the repository's tracked
files and `fetch` copies named files into the workspace, which is how a remediation path stays
something discovered rather than invented when there is nothing local to grep. The field is
only meaningful beside the sandbox, since the routes are served by the co-located proxy, so
the chart refuses it with `shellSandbox.enabled` false rather than rendering a CR that changes
no manifest.

**Reading is the half that nearly went missing.** Writing was the visible use of the shared
tree, so the write skills were migrated first and the protocol was sized for them: a commit
carries a handful of manifests, and a listing capped at 256 entries covers a GitOps directory.
Reading an unfamiliar repository is a different shape of request, and under the write-shaped
protocol the capability regressed — directory mode let a skill `git clone` anything through
the shim and grep it locally, and content mode answered `list` with a silently truncated page,
had no search at all, and cloned full-depth into an `emptyDir` sized for manifests. The
acceptance test that surfaced it is the one the change is now held to: clone a public
repository and analyse the code in it, using no file access to a repository.

What that took is four things, none of them a new trust boundary. `list` pages: it reports
`total` and `truncated`, and takes an `after` cursor so a caller can walk a tree larger than
the ceiling instead of guessing at names beyond it. `grep` runs `git grep` over the tracked
files, fixed-string unless the caller asks for a regular expression, with the pattern carried
as the argument of `-e` so one beginning with `-` is a pattern rather than an option; because
git greps tracked files, no pattern reaches `.git`. `read` takes a list of paths as well as a
single one, since materialising a tree one round trip per file is a reader that reads less
than it should, and it answers with what it skipped and why rather than dropping files
quietly. And `open` takes a `depth`, which makes a shallow single-branch clone — refused
together with `branch`, since a single-branch clone cannot see whether the working branch
exists on the remote and would answer from the base instead, and read-only, since `commit`
refuses on a shallow workspace rather than failing later at a push the remote rejects. A
clone that does not fit `CREDENTIAL_PROXY_MAX_CLONE_BYTES` is removed and refused; the ceiling
bounds what stays on the volume, not what a clone transiently writes.

The agent-facing surface for all of it is the `inspect-repository` skill, which is the thing a
list of protocol verbs does not give you: without a skill naming the capability, the agent has
no way to know that reading somebody else's code is still possible. Its `clone` subcommand
pages the listing, batches the reads and writes the files into a scratch directory; what lands
is source with no repository around it, which is the same property the write path has for the
same reason. Its `open`/`grep`/`fetch`/`close` sequence is for repositories too large to copy,
and the handle rather than a directory is what travels between the agent's turns. On a broker
that has not been armed, `clone` falls back to the leased checkout and says `"mode":
"directory"`; the handle subcommands say they cannot serve the request instead of pretending.

What content-passing does not remove is the volume itself. The clone path still needs it, so
the mount stays until the last skill has migrated and directory mode is deleted; the
restriction of the agent-facing git allowlist to the verbs nothing needs any more is the step
after that. Nor does it change the exec path for `kubectl`, `gcloud` or `gh`, which never
needed a shared filesystem — except that `gh` did, for `--body-file`, and now takes its
documents on stdin instead.

**The deny policy does not see the workspace verbs.** The proxy's policy matches on argv, and
the workspace protocol has none: `commit` and `push` are JSON bodies on their own routes, not
commands submitted to `/v1/exec`. So the rules that stop the agent merging its own pull
request — no `gh pr merge`, no `gh pr review --approve`, no mutating `gh api`, no repository
administration (#725) — do not apply to them. That is intended. `push` is the sanctioned
write, the thing the write skills exist to do, and what it can do is bounded by the verb
rather than by a pattern: it updates a branch, and it cannot merge, cannot approve or dismiss
a review, and cannot change repository settings or branch protection, because there is no
argv through which a caller could ask it to. It does trigger whatever CI the repository runs
on a push, the same as any other push and bounded by the installation token's scope. The line
worth writing down is that `commit` and `push` are a GitHub write path those `gh` rules do not
cover, so reading the deny policy as the complete set of writes the agent can perform gives
the wrong answer.

### One replica

The Slack relay holds a socket-mode WebSocket. Two replicas means two connections and
duplicate event delivery. The sandbox StatefulSet is one replica per agent, so co-location
satisfies this by construction; the standalone Deployment pins it explicitly. Either way
the credential broker cannot be made highly available while it is fused with an inbound
event pump, and splitting them is the obvious follow-up.

### The proxy image is not the agent image

Building `credential-proxy` on the `platform` image would ship the model's personas,
skills, plugins and cluster templates into the one container whose purpose is to be
where credentials live. It builds on `agent-base` instead, through a `proxy-tools`
stage that adds the credential-aware CLIs, the Envoy binary and the entrypoint, and a
final stage that copies in the scripts directory. What it inherits from `agent-base`
is the Hermes venv, the `k8s-event-watcher` binary and `/opt/defaults` — none of the
agent's own content.

Two consequences. `/opt/defaults/scripts` is now filled by two separate lists that
have to agree rather than by one image being the other, which
`scripts/test_check_prompt_assets.py` asserts. And `platform`, not
`credential-proxy`, is the deepest chain shipped, so `scripts/check_image_layers.py`
measures that one.

The federated credential path is written against the standard library alone for this
reason: it runs in a container whose package set is inherited rather than declared for
it, and a dependency added upstream to serve the agent is not a dependency the proxy
should come to rely on.

### Names and selectors

The standalone Deployment and the Service are called `<agent>-credential-proxy`, which an
earlier standalone proxy also used, so an upgrade meets objects that already exist under
those names. A Deployment's `spec.selector` is immutable, so `credentialProxySelector`
reproduces the labels those objects carry rather than a fresh set, and the legacy-cleanup
list must not name either object — deleting them each pass tears down the pod the same
reconcile applied.

The same immutability is why the sandbox StatefulSet's `spec.selector` does not gain a
`has-credential-proxy` label. That label goes on the pod template alone, as a superset of
the selector, so turning federation on does not require the StatefulSet to be deleted
first.

### Switching placements

`credentialProxyColocated` is read fresh each reconcile, so a CR edit moves the proxy.
Turning federation on swings the Service selector to the sandbox pod, adds the container
and its two volumes to the StatefulSet, and deletes the standalone Deployment and its
NetworkPolicy; turning it off does the reverse. The sandbox is reconciled before the proxy
so the new container exists before the old Deployment is removed, and
`deleteStandaloneCredentialProxy` skips objects the operator does not own.

---

## Setting up the pool

Three commands, run once per install, before the chart values that turn federation on.
Nothing in the Helm chart, the Terraform modules, or `k8s-operator/scripts/` runs them —
see [Where the install surfaces stand](#where-the-install-surfaces-stand).

```bash
PROJECT_ID=<project>
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
CLUSTER=<cluster>            # the cluster the agent runs in
NAMESPACE=kubeagents-system
AGENT=<platformagent name>   # the sandbox KSA is <name>-shell
GSA=kubeagents-platform-gsa@"$PROJECT_ID".iam.gserviceaccount.com

# 1. A pool, and a provider trusting the cluster's OIDC issuer. --allowed-audiences
#    is omitted deliberately: the default audience is the provider's own resource
#    name, which is what the projected token below is scoped to.
gcloud iam workload-identity-pools create kubeagents \
  --project="$PROJECT_ID" --location=global \
  --display-name='kube-agents credential proxy'

# The issuer comes from the cluster itself; kubectl must already be pointed at it.
ISSUER=$(kubectl get --raw /.well-known/openid-configuration | jq -r .issuer)

gcloud iam workload-identity-pools providers create-oidc "$CLUSTER" \
  --project="$PROJECT_ID" --location=global --workload-identity-pool=kubeagents \
  --issuer-uri="$ISSUER" \
  --attribute-mapping='google.subject=assertion.sub,attribute.namespace=assertion["kubernetes.io"]["namespace"]' \
  --attribute-condition="assertion['kubernetes.io']['namespace'] == '${NAMESPACE}'"

# 2. Let the federated principal impersonate the GSA that already holds the agent's
#    roles. The subject is the projected token's `sub`, which for a service-account
#    token is system:serviceaccount:<namespace>:<ksa>.
gcloud iam service-accounts add-iam-policy-binding "$GSA" \
  --project="$PROJECT_ID" --role=roles/iam.workloadIdentityUser \
  --member="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/kubeagents/subject/system:serviceaccount:${NAMESPACE}:${AGENT}-shell"
```

Then the chart values:

```yaml
platformAgent:
  harness:
    experimental:
      shellSandbox:
        enabled: true
  security:
    workloadIdentityFederation:
      audience: //iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/kubeagents/providers/<cluster>
      serviceAccountEmail: kubeagents-platform-gsa@<project>.iam.gserviceaccount.com
```

which render as `spec.harness.experimental.shellSandbox` and
`spec.security.workloadIdentityFederation` on the CR. A Kustomize or hand-written CR sets
the same two blocks directly.

The `audience` pattern is validated by the CRD, but only for shape. A pool that does not
exist, or an `attribute-condition` the token does not satisfy, is rejected by STS at
exchange time — the container starts and every credentialed command fails with
`invalid_target` or `unauthorized_client`.

The cluster must be on a public OIDC issuer for the provider to verify tokens, which GKE
clusters are by default (`kubectl get --raw /.well-known/openid-configuration`). A private
issuer needs the JWKS uploaded to the provider instead, which this document does not
cover.

---

## The standalone fallback

Without both `shellSandbox.enabled` and a complete `workloadIdentityFederation` block, the
proxy runs in a Deployment of its own on `kubeagents-platform-agent`, and reads the
metadata server. That is what a default install gets. Three things it does not give you:

**Every pod under that service account keeps a cloud identity.** The gateway pod and the
proxy pod both run as `kubeagents-platform-agent`, which carries the
`iam.gke.io/gcp-service-account` annotation, so anything with execution in either can mint
the GSA's token from `169.254.169.254`. The property this document is about is not
delivered — the separate pod buys reachability, not isolation.

**The exec path authenticates no caller.** Envoy binds
`CREDENTIAL_PROXY_LISTEN_ADDRESS=0.0.0.0` and nothing replaced loopback, so any pod that
can route to the Service can run a credentialed command. See
[Caller authentication](#caller-authentication).

**`git` does not work, and neither does `kubectl -f FILE`.** The caller sends no `cwd`, the
proxy falls back to an emptyDir workspace it alone can see, and the lease check refuses the
command. This is the constraint co-location exists to remove, and while it holds the
sandbox's `kubectl`, `gcloud`, `gh` and `git` are either unconfigured or unable to touch a
working tree.

---

## The Session KV store

Part A of #737: the Session KV lives in a SQLite file at
`/var/lib/kube-agents/session/session_kv.db`, in WAL mode, served over
`127.0.0.1:8699` with bearer authentication — **and** read and written directly as a
file by several in-process clients. The direct-file access is what the incident used.

**Does Part B make Part A unnecessary?** Mostly, but not entirely, and the exceptions
are the interesting part.

If the shell is in another pod and the session volume is not mounted there, the
`sqlite3` path is gone. The legitimate clients — the `session_store` and
`session_otel_bridge` plugins, `incident_context`, `session_manager.py`, the MCP
server, and the event-watcher injector — all run in the gateway pod, in the Hermes
process or beside it. **None of them run in the shell.** So after Part B the shell has no
reason to reach the KV at all, and the correct answer is that it simply is not
mounted or routable.

Three things keep Part A worth doing:

- **It decouples the outcome from mount hygiene.** "Safe because we did not mount the
  volume" is a property of a manifest that someone will eventually edit, in a repo
  where a volume mount is exactly the kind of thing that gets widened for
  convenience. An interface is a property of the code.
- **`sync_back` re-opens the door.** The shell can write a skill into the sandbox's
  `~/.hermes`; that file lands on the host and the gateway loads it — in the pod where
  the DB file is. The shell does not need filesystem access to the DB if it can
  arrange for in-pod code to have it.
- **Concurrent writers to a WAL SQLite file across a pod boundary do not work.** If
  any in-sandbox path ever does need session state, the network interface is the only
  way to give it one. Part A is then a prerequisite, not an alternative.

So: **not load-bearing for the incident once B lands, still load-bearing for the
design.** It is also the cheapest of the three parts and depends on nothing else,
which argues for doing it early regardless of where it sits in the threat model.

---

## Prerequisites

### gVisor breaks WAL SQLite, which is why the runtime is per-pod

An earlier claim that `runtimeClassName: gvisor` is "nearly free" was wrong.
[#610](https://github.com/gke-labs/kube-agents/issues/610) records gVisor corrupting
WAL-mode SQLite on the gofer-backed mount, and `session_kv.db` is WAL-mode SQLite.

That is a fact about the agent pod, which is where the session DB lives. The sandbox
pod holds no SQLite, so it can run under the sentry while the agent pod does not —
which is the argument for the separate field, and the reason this is a prerequisite
for the runtime rather than for the design. What it does still mean is that the
sandbox's storage has to be re-audited for SQLite whenever something new is written
to `/opt/data`: the safety of the setting is a property of what the image puts there,
not of the setting.

Most of the value in this design comes from the pod boundary, not the syscall filter,
so an install starting on the default runtime and turning gVisor on later loses
nothing in the meantime. See [Running the sandbox under
gVisor](#running-the-sandbox-under-gvisor) for what the setting is and is not.

### Egress

Agent Sandbox ships a default GKE policy blocking egress to RFC1918, cluster DNS and
the metadata server. Not taking the CRD means not inheriting that default either, so
the equivalent is ours to write: deny by default, with holes punched only for cluster
DNS, 443 off-cluster, and the agent pod's SSH ingress on 2222. That is the
`NetworkPolicy` in the table above, and it is the one piece of the reversal that is
genuinely extra work rather than a rename. The proxy needs no hole of its own, because
co-located it is reached over the pod's loopback; the 443 rule is what its own calls to
STS, IAM and the Google APIs need, and the shell inherits it.

Note that NetworkPolicy is **not enforced** on the reference install
(`addonsConfig.networkPolicyConfig.disabled: true`, no Dataplane V2), so on that
cluster the metadata-server block is aspirational. Enabling enforcement is a separate,
disruptive maintenance action and should be sequenced deliberately.

### Ordering

Part B is the pod, and the credential move rides on it: the proxy's new home _is_ the
sandbox pod, so it cannot land first. The two are one change for that reason, and the
interval between them — a sandbox with the shims installed and no proxy to talk to — is
the state the section above prices, where `kubectl`, `gcloud`, `gh` and `git` report that
they are unconfigured. That is a usable state for testing the file and code-execution
tools, and not one to ship an agent in.

Part A is independent of both, cheap, and unblocked, so it can go at either end. It is not
load-bearing for the incident once the shell is out of the pod — the argument for doing it
anyway is in [The Session KV store](#the-session-kv-store).

---

## What is still unproven

- **Whether `sshd` in the sandbox is the right transport**, or whether an exec-based
  Hermes backend should be written instead. SSH is what exists today; a
  `kubectl exec`-shaped backend would avoid running a second authentication system,
  but it is upstream work.
- **Startup latency.** A cold sandbox in front of the first `terminal` call is a
  user-visible delay, and has not been measured. Tying sandbox lifetime to the agent
  makes it rare rather than absent; the warm-pool answer is no longer available to
  us (see [Agent Sandbox, and why not yet](#agent-sandbox-and-why-not-yet)), so if
  the number turns out to matter, the fix is a pod that is already running before
  the agent asks — which is a decision, not a field.
- **How the sandbox image and the agent image stay in step.** `shellSandbox.image` is
  settable independently, so baked scripts and the persona that invokes them can drift
  apart silently. Defaulting the sandbox tag to the agent's is the obvious answer and
  has not been decided. Baking the skills tree raises the stakes: the `SKILL.md` in the
  prompt comes from the agent image and the `scripts/` it names come from the sandbox
  image, so a mismatched pair is now two halves of one skill at different versions.
- **The sync leaves a 15 MB tree in the sandbox that nothing reads.** Hermes' SSH
  backend uploads `~/.hermes/skills` on connect, and as measured above that is the chat
  profile's tree rather than the platform agent's. There is no configuration that turns
  it off, so it sits at `/home/agent/.hermes/skills` alongside the baked tree at
  `/opt/data/skills` — dead weight, and a wrong answer for anyone debugging by hand.
  Suppressing it means patching `iter_skills_files`, which has not been decided. The
  same channel creates empty `credentials` and `cache` directories: both are empty
  today because the agent pod's `~/.hermes/credentials` is, but anything that ever
  writes there would be pushed into the sandbox. That is the forward-direction mirror
  of the `sync_back` question above.
- **Whether `sync_back` should be on at all.** Stated above as an open decision, not
  a resolved one.
- **Whether the operator should own the sandbox at all**, or whether it belongs to a
  second controller with its own lifecycle. Reconciling it alongside the gateway is
  the smaller change and the one sketched; it also means a bad sandbox spec is a
  failed `PlatformAgent` reconcile.
- **Whether dropbear should replace OpenSSH in the image.** Agent Sandbox's example
  uses it so the pod can run `runAsNonRoot` with all capabilities dropped, and
  `fsGroup` then removes the entrypoint's `chown` — together the two reasons the
  container currently starts as uid 0. The risk is that dropbear has no `SetEnv`, and
  `SetEnv` is what carries `CREDENTIAL_PROXY_URL` into a non-login session. Worth a
  spike against `make docker-smoke-sandbox`; not worth assuming.
- **The SSH helper reaches the sandbox; nothing behind it runs yet.**
  `agents/platform/scripts/sandbox_exec.py` routes all fifteen agent-side call sites,
  and the `hermes` account, its authorised key and the `.bashrc` isolation are covered
  by `make docker-smoke-sandbox`. Run from the agent pod against a live install it
  connects as uid 1001 on the sandbox host, and a routed `gcloud` or `kubectl` stops at
  `CREDENTIAL_PROXY_URL is not configured` — a message the agent pod cannot produce,
  since the variable is set there. So the connection is proven and the command behind
  it is not. The helper had to land before the agent image can drop
  `credential-proxy-exec`, which makes it the gate on that change.
- **The MCP server's kubeconfig has moved and the credential proxy does not know.**
  `_thread_kubeconfig_path` writes into `/home/hermes/.kubeconfigs` when the sandbox is
  on, because a kubeconfig names an `exec` credential plugin that kubectl runs, and any
  path uid 1000 can write is code execution as the trusted principal. The proxy accepts
  a caller-supplied `KUBECONFIG` only inside its workspace root, so that directory needs
  standing there or the tools fail one step later than they do now.
- **The cluster-agent kubeconfig has nowhere to go yet, and onboarding now fails
  earlier than that.** `cluster_agent_profile.py` writes a profile home on the agent
  pod's PVC and shells out to `hermes`, so it is one of the three scripts the sandbox
  stubs rather than bakes. The four skills that tell the model to run it by its runtime
  path therefore stop at the stub's message instead of reaching the kubeconfig problem
  at all. Both want the same fix — per-profile directories on the sandbox side, and an
  MCP tool that lets the model ask the agent pod to create a profile rather than
  running a script that has to live there. Inventing that layout inside a call site was
  the alternative, and it is how two layouts end up shipping.
- **Cron has not been exercised against a sandboxed agent.** The finding that
  `no_agent` scripts stay in the agent pod is read from the scheduler and is not in
  doubt, but no roster has run in this configuration, and the bootstrap handoff the
  section above specifies is designed and unimplemented. Onboarding is broken until it
  lands, and broken silently.
- **Delegated subagents.** Whether a subagent spawned mid-turn inherits the SSH
  backend, or falls back to a local shell in the agent pod, is unexercised. A fallback
  would be a hole rather than a degradation.
- **The rest of the dispatcher's `HERMES_KANBAN_*` environment still does not cross.**
  `TASK` and `WORKSPACE` are derived from the cd target by the `ForceCommand` above;
  `BOARD`, `DB`, `WORKSPACES_ROOT`, `RUN_ID` and the others are not recoverable from a
  path and arrive empty. Nothing in the repository reads them from a shell today. The
  general fix is `terminal.env_passthrough` support in `environments/ssh.py`, which is
  upstream work.
- **A card's scratch workspace is unreadable from the gateway, and one tool needs it.**
  The `ForceCommand` above makes a delegated card run, and it runs in the sandbox's copy
  of the workspace; the gateway's copy stays empty. `kanban_complete(artifacts=[...])`
  is the known casualty, above — `kanban_attach` is the workaround, and the worker
  protocol does not yet tell anyone that. Whether anything else depends on those files
  is unenforced, and the failure mode is a card that reports success and leaves its
  output on the wrong volume. Reclaiming the space is settled (`kanban-workspace-gc`,
  above); getting the contents back before it runs is not.
- **The GSA's actual scope.** The argument that a lifted token is worse than mediated use
  is strongest when the service account is broadly granted. Nobody has enumerated what
  `kubeagents-platform-gsa` can do, and that enumeration bounds how urgent this is.
- **The gateway pod's annotation.** Federation removes the cloud identity from the sandbox
  pod. `kubeagents-platform-agent` keeps it, and no process there needs it any more.
  Removing it is a separate change with its own blast radius.
- **The lease check under a shared tree.** `git` works again because both containers see
  the same working tree, which also means the shell can move `.git` out from under a
  command the proxy is running. The hooks path is closed; races on the index are not
  analysed.
- **Whether `github-token-minter` and this should be one workload.** They are the same
  pattern serving different credentials. Merging them is attractive and out of scope.

## Related work

- [#720](https://github.com/gke-labs/kube-agents/pull/720) — overlapping and open.
  Unshares the proxy's UID and PID namespaces, adds `buildCredentialBrokerDeployment` and
  `splitCredentialBrokerPod`, and denies the metadata server through an `egressPolicy:
Allowlist`. It reaches the pod-scoped-identity finding independently and answers it with
  a Pod split plus a policy the reference install does not enforce; this document answers
  it by removing the binding. The namespace hardening is complementary and its
  `shareProcessNamespace` finding on the gateway pod is unaddressed here.
- [#723](https://github.com/gke-labs/kube-agents/pull/723),
  [#724](https://github.com/gke-labs/kube-agents/pull/724),
  [#725](https://github.com/gke-labs/kube-agents/pull/725) — proxy hardening: allowlists,
  native sidecar ordering, the GitHub write path. Complementary.
- [#674](https://github.com/gke-labs/kube-agents/pull/674) — read-only root
  filesystem. Complementary.
- [#610](https://github.com/gke-labs/kube-agents/issues/610) — the gVisor/WAL SQLite
  corruption. Why the sandbox's runtime is a field of its own.
- [`gchat-session-metadata-data-flow.md`](gchat-session-metadata-data-flow.md) — what
  actually flows through the Session KV.

[Agent Sandbox]: https://github.com/kubernetes-sigs/agent-sandbox

_Drafted with the help of Claude._
