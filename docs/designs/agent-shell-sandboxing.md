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

**Status:** the sandbox image ships ([`deploy/sandbox/`](../../deploy/sandbox/));
nothing reconciles it yet. Tracked as Parts A and B of
[#737](https://github.com/gke-labs/kube-agents/issues/737). Part C, the credential
proxy, is a separate document — [`credential-proxy-placement.md`](credential-proxy-placement.md) —
and lands first.

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
SSH-reachable pod at a stable name, a workspace volume, and an image that knows
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
- **the sandbox pod** — an `sshd`, a workspace volume, the agent's tools, the
  credential-proxy shims. No Kubernetes service-account token, no route to the
  metadata server, no real `kubectl`.

### The sandbox workload

Three objects per agent, all owned by the `PlatformAgent` CR so they are garbage
collected with it. Sketched in
[`shell_sandbox_manifests.go`](../../k8s-operator/internal/controller/shell_sandbox_manifests.go)
— builders and their tests, not yet called from `Reconcile`.

| Object                       | Named           | What it is for                                                                     |
| ---------------------------- | --------------- | ---------------------------------------------------------------------------------- |
| `StatefulSet`, `replicas: 1` | `<agent>-shell` | the pod, and the `workspace` volumeClaimTemplate behind it                         |
| `Service`, `clusterIP: None` | `<agent>-shell` | the StatefulSet's governing service, and the name Hermes dials                     |
| `NetworkPolicy`              | `<agent>-shell` | ingress on 2222 from the gateway only; egress to DNS and the credential proxy only |

Five fields carry an argument rather than a default:

- **`persistentVolumeClaimRetentionPolicy: Retain` / `Retain`.** The volume holds the
  sshd host keys. Reclaiming it means the next pod generates new ones, and
  `accept-new` turns a changed host key into every subsequent command failing — so a
  scale-down or a workload delete has to leave the claim, at the cost of a PVC that
  outlives its StatefulSet.
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
an RSA host key on the workspace volume the first time a pod starts on it, and leaves
them alone on every later start
([`entrypoint.sh`](../../deploy/sandbox/entrypoint.sh)). Hermes connects with
`StrictHostKeyChecking=accept-new`, so the first connection trusts the key and every
later one pins it. Because the keys live on the PVC rather than in a Secret, no
private key is written to etcd and no install surface has to know they exist — which
is also the reason for the `Retain` retention policy above. Agent Sandbox's own SSH
example regenerates an ephemeral host key on every start unless you mount one; this
avoids both that churn and the Secret it would otherwise need.

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

`grep` runs **in the sandbox pod**. `output.log` is read from the sandbox's workspace
volume, where it was written by whichever earlier command produced it — the sandbox's
disk is the only filesystem in the picture. Stdout comes back over the SSH channel;
Hermes strips the marker and returns the rest to the model. The agent pod's
filesystem is never involved.

If the previous command had been `cd /workspace/logs`, that would have been captured
by the marker and applied here, and `read_file("output.log")` would resolve against
the same directory — because the file tools share the environment object.

### `HERMES_WRITE_SAFE_ROOT` has to move with the shell

The Hermes base image sets `HERMES_WRITE_SAFE_ROOT=/opt/data`, and with the sandbox on
that value denies every write the agent attempts — including to the sandbox's own
workspace. The first live run found it immediately: `write_file` and `patch` returned
"Write denied" for every path.

The guardrail is a string-prefix test, and it runs in the wrong process to know about
any of this. `agent/file_safety.py` splits the variable on `os.pathsep`, `realpath`s
each entry, and requires the resolved target to equal a root or begin with `root + "/"`
— all of it in the agent process, before the write is dispatched to any backend. So it
is checking sandbox paths against a list containing only the agent's own home. Every
sandbox path fails the prefix test, and `/opt/data`, the one path that would pass, does
not exist in the sandbox. Unsetting it is not the answer either: the check is opt-in and
an empty value skips it entirely, which drops the guardrail rather than moving it.

The operator therefore repoints it, in `buildPodTemplateSpec` and only when the sandbox
is enabled, at the sandbox's two writable directories — `/workspace` and
`/home/agent`, the latter being `shellSandboxUser`'s home and the cwd every `ssh`
command starts in. That gives up no isolation. With `backend: ssh` the file tools
cannot reach the agent's filesystem at all, so the roots they are checked against
should describe the filesystem they actually write to.
`TestSandboxRepointsTheWriteSafeRoot` asserts the variable is absent with the sandbox
off, is exactly these two paths with it on, and never admits `/opt/data`.

Two things this does not cover. `/home/agent` is on the container filesystem rather
than the workspace volume, so writes there do not survive a sandbox restart — it is
writable because commands land there, not because anything should be kept there. And
the credential denylist that sits alongside this check (`~/.ssh`, `~/.aws`,
`~/.config/gcloud`, `~/.docker`) is still expressed against the agent's home; in the
sandbox those paths name nothing, which is harmless today and wrong if the sandbox ever
holds credentials of its own.

### What persists, and for how long

| Thing             | Mechanism                                 | Lifetime               |
| ----------------- | ----------------------------------------- | ---------------------- |
| Files             | the sandbox's attached volume             | the sandbox's lifetime |
| Working directory | in-band stdout marker, tracked in Hermes  | the task's environment |
| Environment vars  | `export -p` snapshot file in the sandbox  | the sandbox's lifetime |
| Shell processes   | nothing — every call is a fresh `bash -c` | one command            |
| Background jobs   | only if explicitly detached               | until the pod restarts |

Sandbox lifetime should be tied to the agent, not to the conversation. The agent is a
long-running operator, not a session; a per-conversation sandbox would throw away
working state between related tasks and make warm-pool startup the common case rather
than the rare one.

### What the sandbox needs, and where it comes from

The shell moved; the paths it was written against did not. `/opt/data` is the agent's
home on its PVC, it is named 59 files deep in `agents/`, and in the sandbox it does not
exist. The first live run found this immediately: an environment probe dispatched as a
Kanban card ran on `platform-agent-shell-0` as user `agent` and reported
`/opt/data exists: False`, so the card's own declared workspace under
`/opt/data/kanban/workspaces/` was not creatable and the worker fell back to its home
directory.

Enumerating what the sandbox legitimately needs — listed under
[What is still unproven](#what-is-still-unproven) as nobody having done it — sorts the
references into five classes with four different delivery mechanisms.

| What                                         | Where it comes from                    | Why                                           |
| -------------------------------------------- | -------------------------------------- | --------------------------------------------- |
| Persona (`SOUL.md`, `AGENTS.md`)             | stays in the agent pod                 | read into the prompt, never through the shell |
| Skills, including their `scripts/`           | Hermes' existing `~/.hermes` sync      | already works, verified live                  |
| Governance SOPs                              | baked into the sandbox image           | static, versioned with the repo               |
| The shell-invoked subset of `scripts/`       | baked into the sandbox image           | static, and the subset is small               |
| `SETTINGS.md`                                | ConfigMap mounted into the sandbox pod | per-install content, rendered by the operator |
| Outputs (`INVENTORY.md`, scratch workspaces) | written to `/workspace` at runtime     | data, not delivery                            |

**The persona stays behind, and that is a property rather than an omission.** Nothing
writes `SOUL.md` through the shell. The only writer is
[`profile_scaffold.py`](../../agents/platform/scripts/profile_scaffold.py), which
overlays the image-baked `/opt/platform-template` onto the profile home at pod startup;
everything else that names the file is a test fixture. So the agent cannot modify its
own persona, because the persona is not on a filesystem any of its tools can reach.
Before the sandbox that was true only by convention.

**Skills already arrive, and whole.** Hermes' SSH backend syncs `~/.hermes/skills` into
the sandbox, and it syncs trees rather than just the markdown: on the live pair,
`github-issue-resolver`, `submit-suggestion` and `fleet-audit` each have their
`scripts/*.py` present under `/home/agent/.hermes/skills/`. 62 skills, 16 MB. Nothing
to build.

**Governance and shared scripts are baked into the image rather than synced.** The
alternative was extending the sync to cover them, which keeps one delivery mechanism
instead of two. It was rejected because the sync is upstream Hermes behaviour scoped to
`~/.hermes`, so widening it means carrying a patch, and because a baked image is
auditable in a way a sync is not: what the sandbox contains is what the Dockerfile
says, reviewable in the diff. The cost is accepted and real — skills update when the
agent restarts, baked files only when the image is rebuilt, and the two can drift.

**Not all of `scripts/` goes.** The directory holds over a hundred files and most are
agent-side servers and their tests — `platform_mcp_server.py`, `session_kv_server.py`,
`router_server.py`, `profile_cron_tick.py`, `credential_proxy.py`. Shipping them
wholesale would put a file named `credential_proxy.py` inside the sandbox, which is the
wrong thing for a reviewer to find even though it is inert there. The set the agent
actually invokes from the shell is `cluster_agent_profile.py` (8 call sites in the
prose), `kanban_notify_propagate.py` (4) and `cluster_preflight.sh` (3). So the image
gets an explicit allowlist, enforced the way the image already fails the build if a
real `gcloud` appears. `kanban_notify_propagate.py` is disqualified below.

`cluster_agent_reconcile.py` looked like a fourth and is not, which is the trap the
allowlist has to be written against. It has one shell call site
(`cluster-agent-lifecycle/SKILL.md:100`, a `--dry-run`), but it is also the script
behind the `cluster-agent-reconcile` cron job — and cron scripts run in the agent pod,
for the reasons below. Baking it would put a second copy in the sandbox that can drift
from the one that actually runs. A script's shell call sites do not qualify it on their
own; being absent from every `jobs.json` is the other half of the test.

**`SETTINGS.md` is mounted, and not at `/opt/data`.** Its content is per-install: the
operator renders it from `spec.integration.github.gitRepo` into an `<agent>-settings`
ConfigMap (`buildSettingsConfigMap`) and mounts it as a subPath. The ConfigMap already
exists, so the operator mounts it a second time into the sandbox pod.

Mounting it at `/opt/data/SETTINGS.md` inside the sandbox was considered, because it
would need no code change at all — three parsers hardcode that path
(`resolver.py:18` with no override, `audit_report.py:4079` behind
`FLEET_AUDIT_SETTINGS`, `gitops_workspace.py:119` off `agent_home()`). It was rejected:
it makes `/opt/data` exist in the sandbox, and the absence of `/opt/data` is the
one-line check that tells anybody whether the isolation is real. The
`gke-stockout-investigator` plugin already reads
`${PLATFORM_AGENT_HOME:-/opt/data}/SETTINGS.md`, so the override precedent exists;
`PLATFORM_AGENT_HOME` becomes the standard, set in the sandbox environment, and the two
parsers that cannot honour it are changed.

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
It is gated on Part C: the sandbox image ships the four proxy symlinks at
`/opt/credential-proxy/bin/`, but that directory is not on its `PATH` and
`CREDENTIAL_PROXY_URL` is unset, so `gh` does not resolve there yet.

#### The agent pod gives up cluster tooling entirely

Sandboxing the shell does not, by itself, take `kubectl` away from the agent. The
`platform-agent` container has no native `kubectl`, `gcloud`, `gh` or `git` — all four
are symlinks to `credential-proxy-exec`, and the real binaries live only in the
`envoy-credential-proxy` sidecar — but the symlinks are a working credential path, and
the model can reach one without going near a shell.
[`platform_mcp_server.py`](../../agents/platform/scripts/platform_mcp_server.py) is the
proof: it is launched as a stdio MCP server from `agents/platform/config.yaml:30`, runs
in the agent pod, and shells out at eleven sites — `kubectl logs`, `kubectl describe`,
`kubectl get pods`, `gcloud logging read` among them. Those are model-facing tools. The
shell moving to the sandbox does nothing to them.

So the decision is that the agent pod holds no way to invoke cluster tooling in any
form: the four symlinks and `credential-proxy-exec` itself leave the agent image, and
the image gains the same build-time guard the sandbox image already has, failing the
build if any of the four resolves. The sandbox becomes the only place a credential-proxy
call can originate.

Agent-side callers reach the tooling the same way everything else in this section does —
by executing in the sandbox over SSH. `platform_mcp_server.py` (11 sites),
`cluster_agent_reconcile.py` (3), `cluster_agent_profile.py` (1) and `gke_endpoint.py`
(1, a capability probe) share `sandbox_exec.py`, which reads `terminal.ssh_*` from the
managed config at `/etc/hermes/config.yaml` rather than re-deriving the address. Nothing
else in `agents/` needs it: the remaining callers — `gitops_workspace.py`, `resolver.py`,
`cluster_preflight.sh` — are already invoked from the shell and so already run there.

Two calls in those files stay in the agent pod, and neither is an exception to the rule
above: `hermes send` and `hermes profile delete` are not cluster tooling. They need the
profiles on the data PVC and the gateway on loopback, and the sandbox image does not
carry the binary. The overlap in names is unfortunate — the SSH principal below is also
called `hermes` — and is the one thing to check when reading a diff against these files.
Which SSH identity that helper uses is the subject of the next section, and is not the
one configured today.

**This makes the sandbox required.** With the symlinks gone there is no fallback, so
`shellSandbox` disabled is a configuration in which the MCP tools fail. That is accepted
rather than worked around: keeping a local path alive for the disabled case would keep
the exact capability this removes, and an image that behaves differently depending on a
CR field is harder to reason about than one that does not carry the binaries at all.
Removing the toggle is the follow-through and is not yet done.

The proxy still answers on pod loopback and authenticates no caller, so removing the
symlinks does not make the agent pod unable to reach it — but reaching it now requires
arbitrary code execution in that pod, and with the shell, the file tools and
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
another node — which the `ReadWriteOnce` `/workspace` PVC guarantees is slow, since it
has to detach and re-attach — that budget is exhausted, and the tools disappear from the
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
  where the workspace mount is exactly the kind of thing that gets widened for
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
  has not been decided.
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
- **The SSH helper is built and not yet exercised through a real cluster call.**
  `agents/platform/scripts/sandbox_exec.py` routes all fifteen agent-side call sites,
  and the `hermes` account, its authorised key and the `.bashrc` isolation are covered
  by `make docker-smoke-sandbox`. What no test can reach is the far end: every one of
  those commands stops at `CREDENTIAL_PROXY_URL is not configured`, so the connection
  is proven and the command behind it is not. The helper had to land before the agent
  image can drop `credential-proxy-exec`, which makes it the gate on that change.
- **The MCP server's kubeconfig has moved and the credential proxy does not know.**
  `_thread_kubeconfig_path` writes into `/home/hermes/.kubeconfigs` when the sandbox is
  on, because a kubeconfig names an `exec` credential plugin that kubectl runs, and any
  path uid 1000 can write is code execution as the trusted principal. The proxy accepts
  a caller-supplied `KUBECONFIG` only inside its workspace root, so Part C has to give
  that directory standing or the tools fail one step later than they do now.
- **The cluster-agent kubeconfig has nowhere to go yet.** `cluster_agent_profile.py`
  runs its `get-credentials` in the sandbox but still names the profile home on the
  agent pod's PVC, which has no counterpart there. Onboarding a cluster fails on the
  missing directory until per-profile sandbox directories land. The alternative was to
  invent that layout inside a call site, which is how two layouts end up shipping.
- **Cron has not been exercised against a sandboxed agent.** The finding that
  `no_agent` scripts stay in the agent pod is read from the scheduler and is not in
  doubt, but no roster has run in this configuration, and the bootstrap handoff the
  section above specifies is designed and unimplemented. Onboarding is broken until it
  lands, and broken silently.
- **Delegated subagents.** Whether a subagent spawned mid-turn inherits the SSH
  backend, or falls back to a local shell in the agent pod, is unexercised. A fallback
  would be a hole rather than a degradation.

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
