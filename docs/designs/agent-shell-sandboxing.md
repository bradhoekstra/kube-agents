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

This document proposes running the shell in a **separate Kubernetes pod** — its own
filesystem, its own identity, no credentials — reached over Hermes' existing `ssh`
terminal backend, and states what has to be true first.

**Status:** the sandbox image ships ([`deploy/sandbox/`](../../deploy/sandbox/)) and the
operator reconciles it behind `harness.experimental.shellSandbox`. Tracked as Parts A and
B of [#737](https://github.com/gke-labs/kube-agents/issues/737). Part C, the credential
proxy, is a separate document — [`credential-proxy-placement.md`](credential-proxy-placement.md) —
and until it lands the sandbox's `kubectl`, `gcloud`, `gh` and `git` report that they are
unconfigured.

**Revised 2026-08-18.** The pod was going to be a `Sandbox` custom resource from
[Agent Sandbox]. It is a StatefulSet reconciled by our own operator instead. The
evidence for the reversal is in
[Agent Sandbox, and why not yet](#agent-sandbox-and-why-not-yet); everything else in
this document is unchanged, because nothing else depended on which controller
created the pod.

| Layer                          | Where it lives                                                                                                                                                                                                                                                                        |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Terminal backend selection     | Hermes `terminal.backend` / `TERMINAL_ENV`. **Unset everywhere in this repo** → `local`                                                                                                                                                                                               |
| The agent's Hermes config      | [`agents/platform/config.yaml`](../../agents/platform/config.yaml)                                                                                                                                                                                                                    |
| The pod that would host it     | a `<agent>-shell` StatefulSet, one per agent, reconciled by the operator — [`shell_sandbox_manifests.go`](../../k8s-operator/internal/controller/shell_sandbox_manifests.go)                                                                                                          |
| The image it runs              | [`deploy/sandbox/`](../../deploy/sandbox/) — first-party, `sshd` plus the credential-proxy wrappers                                                                                                                                                                                   |
| The Session KV store           | [`agents/platform/scripts/session_kv_server.py`](../../agents/platform/scripts/session_kv_server.py), SQLite under `/var/lib/kube-agents/session/`                                                                                                                                    |
| Its in-process clients         | [`agents/chat/defaults/plugins/session_store/`](../../agents/chat/defaults/plugins/session_store/), [`session_otel_bridge/`](../../agents/chat/defaults/plugins/session_otel_bridge/), [`agents/platform/plugins/incident_context/`](../../agents/platform/plugins/incident_context/) |
| Existing session documentation | [`agents/platform/docs/session_management.md`](../../agents/platform/docs/session_management.md)                                                                                                                                                                                      |

## How to read this document

| Section                                       | What it gives you                                              |
| --------------------------------------------- | -------------------------------------------------------------- |
| [Background](#background)                     | the incident, and what Hermes actually offers                  |
| [The decision](#the-decision)                 | what runs the sandbox pod, and what was rejected               |
| [The design](#the-design)                     | a tool call traced end to end, and what persists between calls |
| [The Session KV store](#the-session-kv-store) | Part A, and why the shell move does not fully replace it       |
| [Prerequisites](#prerequisites)               | what has to land first, including one known blocker            |

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

Every defect found so far has the same shape: a host-side operation performed on a
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

### Agent Sandbox, and why not yet

This document originally chose **Agent Sandbox**
([`kubernetes-sigs/agent-sandbox`][Agent Sandbox]), a SIG Apps subproject available
as a GKE addon: its `Sandbox` CRD is a long-running stateful singleton pod with a
stable identity and an attached volume, `SandboxTemplate` and `SandboxClaim` give the
operator a per-agent provisioning path, `SandboxWarmPool` amortises startup, and
isolation strength becomes a `runtimeClassName` choice. The closing argument was that
"adding one more CR is the smallest new concept."

That was written from the project's documentation. Installing v0.5.5 on the reference
cluster and running the sandbox image under it produced four observations, and
together they invert the conclusion:

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

So the sentence to withdraw is the one about the smallest new concept. With only
`Sandbox` shipped, the CR is not the smaller concept: the operator already builds
StatefulSets, Services, PVCs and NetworkPolicies for this agent, and the sandbox is
one more of each. Agent Sandbox costs a dependency, three install-surface changes and
a controller whose reconciliation we would have to work around, in exchange for
fields we can already write.

**This is a deferral, not a rejection**, and the difference is load-bearing. The bet
the original decision made — a Kubernetes-native sandbox API, warm pools, isolation
as a one-line runtime choice — is still the right bet if the project delivers it. So
the interface below is drawn to make adopting it a swap rather than a rewrite: an
SSH-reachable pod at a stable name, an attached volume, and an image that knows
nothing about what scheduled it. On the day the other three CRDs exist, what changes
is one builder function in the operator. Nothing in `deploy/sandbox/`, nothing in
Hermes' configuration, and nothing in this document above this line.

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
choice turns on, and it is why the reversal above changes nothing here — a
StatefulSet is no more of a density layer than a `Sandbox` CR was.

### What sandboxing does and does not close

| Incident step                        | Closed by the sandbox?                                  |
| ------------------------------------ | ------------------------------------------------------- |
| 2 — `sqlite3` on the session DB      | Yes, if the DB volume is not mounted into the sandbox   |
| 3 — editing the harness working tree | Yes — the tree is not there                             |
| 4 — writing Hermes `config.yaml`     | Yes; `config.yaml` is not in the sync set (see below)   |
| 5 — restarting its own process       | Yes — the process is in another pod                     |
| Credential exfiltration              | **No.** That is [Part C](credential-proxy-placement.md) |

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

Any implementation must decide what to do about this. The options are to disable
`sync_back`, to restrict the sync set to non-executable paths, or to accept it and
say so explicitly. Silently inheriting the default is not one of them.

---

## The design

### Topology

Three pods per agent instead of one:

- **the agent pod** — Hermes, the gateway, the plugins. No cloud identity after
  Part C. No shell of consequence.
- **the credential proxy pod** — [Part C](credential-proxy-placement.md). Holds the
  credentials, runs the credentialed commands.
- **the sandbox pod** — an `sshd`, a durable `/opt/data`, the agent's tools, the
  credential-proxy shims. No Kubernetes service-account token, no route to the
  metadata server, no real `kubectl`.

### The sandbox workload

Three objects per agent, all owned by the `PlatformAgent` CR so they are garbage
collected with it, built in
[`shell_sandbox_manifests.go`](../../k8s-operator/internal/controller/shell_sandbox_manifests.go).

| Object                       | Named           | What it is for                                                                     |
| ---------------------------- | --------------- | ---------------------------------------------------------------------------------- |
| `StatefulSet`, `replicas: 1` | `<agent>-shell` | the pod, and the `data` and `sshd` volumeClaimTemplates behind it                  |
| `Service`, `clusterIP: None` | `<agent>-shell` | the StatefulSet's governing service, and the name Hermes dials                     |
| `NetworkPolicy`              | `<agent>-shell` | ingress on 2222 from the gateway only; egress to DNS and the credential proxy only |

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
  [`deploy/sandbox/entrypoint.sh`](../../deploy/sandbox/entrypoint.sh).

Notably absent: a ServiceAccount, a Role, and any Secret other than the public half
of the agent's SSH key. If a future change needs one of those in the sandbox, that is
the boundary moving, and it should be argued for here first.

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
same contract, and three of the four surfaces can express it with what they already
use:

| Surface                                   | How it generates a secret today                                | What it does for the keypair                                                                                                                      |
| ----------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `provision_07_gcp_k8s_secrets.sh`         | reads the live Secret back, `openssl rand` only if absent      | `ssh-keygen -t ed25519` in a private temp dir, read back from the live Secret first; `ssh-keygen` added to its `check_prereqs`                    |
| `upgrade.sh` (`backfill_sandbox_ssh_key`) | additive `kubectl patch`, an existing value is never rewritten | the same guard, for installs that predate the keypair                                                                                             |
| `terraform/examples/full-install`         | `random_password`                                              | `tls_private_key` with `ED25519`, whose `private_key_openssh` / `public_key_openssh` attributes give both halves without shelling out             |
| Helm, `platformAgent.credentials.create`  | `lookup` the live Secret, else `randAlphaNum 48`               | **cannot generate.** sprig's `genPrivateKey "ed25519"` emits PEM and sprig has no function that encodes the public half in `authorized_keys` form |

So the two paths that install production — the scripts and the Terraform composition
— get a keypair with nothing typed. Helm's `credentials.create` accepts a supplied
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
`buildShellSandboxClientKeyMount`. Like the rest of that file they are builders with
tests and no caller — the agent Deployment does not mount the key yet, because
nothing reads it until `terminal.backend` is switched to `ssh`.

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
the sandbox did not already have; the reverse is what [Part C](credential-proxy-placement.md)
rules out, and for the same reason. The artifact crossing back is model-authored text
that was already going to the user verbatim — it is read, never executed — and no model
sits in the delivery path, so the `no_agent` guarantee holds unchanged.

`github_scan_gate.py` is the clean case, because it already has the seam. Its agent-side
half files a kanban card through `run_slash`; its other half is `resolver.py poll`, which
needs only `gh` and the standard library and returns JSON on stdout. That half is a skill
script under `github-issue-resolver/scripts/`, which Hermes' sync already places in the
sandbox — so the split is one function, `run_resolver_poll`, becoming an `ssh` call.
That split is now the smaller half of what has already happened. `resolver.py` reaches
`gh` through `sandbox_exec.run`, so every `gh` call the poll makes executes in the
sandbox today; what is left to move is the script around them. It waits on Part C, for
the same reason the sweep is currently down — the next section states the cost.

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

`git` had nowhere to go. `credential_proxy.py::_execute` confines a git command's working
directory to `CREDENTIAL_PROXY_WORKSPACE_ROOT` and re-runs it in the sidecar against the
shared `/opt/data` volume, which only the agent pod and its sidecar mount — a
sandbox-side `git` cannot reach that tree. It was removed anyway rather than left as the
one credentialed binary in the container this section exists to disarm.

**What that costs until Part C.** The sandbox image ships the four proxy shims at
`/opt/credential-proxy/bin/` and puts that directory first on `PATH`, but
`CREDENTIAL_PROXY_URL` is unset there — `buildShellSandboxStatefulSet` omits the variable
while the URL is empty — so each of them resolves and then exits 1 with
`CREDENTIAL_PROXY_URL is not configured`. `kubectl` and `gcloud` were already in that
state. `gh` joining them takes the `*/10` `github-repo-watcher` sweep down: it reports a
fault every tick rather than the repository's open work. `git` takes the GitOps and
pull-request write paths with it. All of it returns when Part C gives the proxy an
address off the agent pod's loopback and a workspace both sides can reach.

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

The proxy still answers on pod loopback and authenticates no caller, so removing the
symlinks does not make the agent pod unable to reach it — a `curl` to `127.0.0.1:8765`
is a working path, and `GOOGLE_CHAT_RELAY_URL` still names that address in the agent
container's environment for the relay code that legitimately posts there. Reaching it
requires arbitrary code execution in that pod, and with the shell, the file tools and
`execute_code` all in the sandbox the model has no path to that. What is left in the
agent pod is trusted code: the MCP server, the cron scripts, the gateway. The point of
the proxy is that raw credentials never reach the agent, not that no process can invoke
a command, so this is the property that matters. [Part C](credential-proxy-placement.md)
moving the proxy into its own pod turns loopback into a network boundary as well.

One entanglement to carry across. `gcloud container clusters get-credentials` writes a
kubeconfig that the proxy validates with `_within_workspace`
(`credential_proxy.py:1044`), and the shared volume making that work is `/opt/data`,
mounted `rw` into both containers. A sandbox-side `kubectl` cannot use it, and neither
can a proxy in its own pod — the PVCs are `ReadWriteOnce`. Part C already replaces the
mechanism with `content_workspace.py` and a `/v1/workspace/*` API whose working tree
lives in the broker's own volume. The sandbox should land on that rather than grow a
second handoff.

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

#### Two problems deferred, and what has already been ruled out for them

Neither blocks the work above. Both are recorded here so the dead ends are not
re-walked.

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

Making `hermes` a fifth wrapped executable on the credential proxy was proposed and
rejected. The pattern fits — `credential_proxy.py` already forwards argv, runs the real
binary on the trusted side, and enforces a per-executable subcommand policy — but
[Part C](credential-proxy-placement.md) moves the proxy into its own pod, which holds
credentials and no profile state. The wrapper would run `hermes` somewhere it still
cannot work. Whatever replaces it has to execute in the agent pod.

`cronjob(action='run')` is the nearest existing tool and is not equivalent:
`AGENTS.md:37` records that in several runtimes it executes the job synchronously
inside the calling session, which is the behaviour `hermes cron run` was chosen to
avoid.

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
server, and the event-watcher injector — all run either in the Hermes process or in
the proxy pod. **None of them run in the shell.** So after Part B the shell has no
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

### gVisor breaks WAL SQLite — a real blocker

An earlier claim that `runtimeClassName: gvisor` is "nearly free" was wrong.
[#610](https://github.com/gke-labs/kube-agents/issues/610) records gVisor corrupting
WAL-mode SQLite on the gofer-backed mount, and `session_kv.db` is WAL-mode SQLite.

This does not block the design — the session DB should not be in the sandbox at all —
but it does mean the sandbox's own storage must be audited for SQLite before gVisor
is enabled, and that the isolation tier is a decision with a cost rather than a free
upgrade. Starting on the default runtime and moving to gVisor as a second step is
legitimate; most of the value here comes from the pod boundary, not the syscall
filter.

### Egress

Agent Sandbox ships a default GKE policy blocking egress to RFC1918, cluster DNS and
the metadata server. Not taking the CRD means not inheriting that default either, so
the equivalent is ours to write: deny by default, with holes punched only for cluster
DNS, the credential proxy Service, and the agent pod's SSH ingress on 2222. That is
the `NetworkPolicy` in the table above, and it is the one piece of the reversal that
is genuinely extra work rather than a rename.

Note that NetworkPolicy is **not enforced** on the reference install
(`addonsConfig.networkPolicyConfig.disabled: true`, no Dataplane V2), so on that
cluster the metadata-server block is aspirational. Enabling enforcement is a separate,
disruptive maintenance action and should be sequenced deliberately.

### Ordering

Part C first — it is independent, it closes the credential path without waiting on
any of this, and it is the only part with a proven live exploit. Part A next, because
it is cheap and unblocked. Part B last, because it is the largest change and the only
one that depends on another part: without Part C the sandbox has no credential path
at all, so `kubectl`, `gcloud`, `gh` and `git` report that they are unconfigured.
That is a usable state for testing file and code-execution tools, and not one to ship
the agent in.

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
  a caller-supplied `KUBECONFIG` only inside its workspace root, so Part C has to give
  that directory standing or the tools fail one step later than they do now.
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

## Related work

- [`credential-proxy-placement.md`](credential-proxy-placement.md) — Part C. Ships
  independently and first.
- [#720](https://github.com/gke-labs/kube-agents/pull/720) — **a prerequisite, not
  merely complementary.** It moves the credential broker into its own Deployment. A
  shell in a sandbox pod cannot reach a broker bound to the agent pod's loopback
  interface, so until the broker has a Service address of its own, moving the shell
  means giving up `kubectl`, `gcloud`, `gh`, and `git` entirely.
- [#674](https://github.com/gke-labs/kube-agents/pull/674) — read-only root
  filesystem. Complementary.
- [#610](https://github.com/gke-labs/kube-agents/issues/610) — the gVisor/WAL SQLite
  corruption. A gate on the isolation tier.
- [`gchat-session-metadata-data-flow.md`](gchat-session-metadata-data-flow.md) — what
  actually flows through the Session KV.

[Agent Sandbox]: https://github.com/kubernetes-sigs/agent-sandbox

_Drafted with the help of Claude._
