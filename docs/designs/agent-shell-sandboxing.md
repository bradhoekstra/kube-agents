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

This document proposes running the shell in a **[Agent Sandbox]** pod — a separate
Kubernetes workload with its own filesystem and identity — reached over Hermes'
existing `ssh` terminal backend, and states what has to be true first.

**Status:** proposed, not implemented. Tracked as Parts A and B of
[#737](https://github.com/gke-labs/kube-agents/issues/737). Part C, the credential
proxy, is a separate document — [`credential-proxy-placement.md`](credential-proxy-placement.md) —
and lands first.

| Layer                          | Where it lives                                                                                                                                                                                                                                                                        |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Terminal backend selection     | Hermes `terminal.backend` / `TERMINAL_ENV`. **Unset everywhere in this repo** → `local`                                                                                                                                                                                               |
| The agent's Hermes config      | [`agents/platform/config.yaml`](../../agents/platform/config.yaml)                                                                                                                                                                                                                    |
| The pod that would host it     | new `Sandbox` resource, one per agent, reconciled by the operator                                                                                                                                                                                                                     |
| The Session KV store           | [`agents/platform/scripts/session_kv_server.py`](../../agents/platform/scripts/session_kv_server.py), SQLite under `/var/lib/kube-agents/session/`                                                                                                                                    |
| Its in-process clients         | [`agents/chat/defaults/plugins/session_store/`](../../agents/chat/defaults/plugins/session_store/), [`session_otel_bridge/`](../../agents/chat/defaults/plugins/session_otel_bridge/), [`agents/platform/plugins/incident_context/`](../../agents/platform/plugins/incident_context/) |
| Existing session documentation | [`agents/platform/docs/session_management.md`](../../agents/platform/docs/session_management.md)                                                                                                                                                                                      |

## How to read this document

| Section                                       | What it gives you                                              |
| --------------------------------------------- | -------------------------------------------------------------- |
| [Background](#background)                     | the incident, and what Hermes actually offers                  |
| [The decision](#the-decision)                 | why Agent Sandbox, and what was rejected                       |
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

**Agent Sandbox** ([`kubernetes-sigs/agent-sandbox`][Agent Sandbox]), a SIG Apps
subproject available as a GKE addon. Its `Sandbox` CRD is a long-running stateful
singleton pod with a stable identity and an attached volume — which is exactly the
shape Hermes' persistence model assumes. `SandboxTemplate` and `SandboxClaim` give
the operator a per-agent provisioning path, `SandboxWarmPool` amortises startup, and
isolation strength is a `runtimeClassName` choice (gVisor or Kata) rather than a
rewrite.

It is also the only option on the list that is a Kubernetes API. The operator already
reconciles per-agent resources; adding one more CR is the smallest new concept.

### Agent Substrate, and why not

Agent Substrate was evaluated seriously and rejected. It is a **density and
scheduling** layer — roughly 250 sessions across 8 pods, with a minimal control plane
that deliberately bypasses the Kubernetes API and an Envoy-based router for session
addressing. Density is not our problem: one agent, one shell. Bypassing the
Kubernetes API costs us the operator integration that makes this cheap. And it
depends on Pod Certificates, which are default-off until Kubernetes 1.36.

The distinction worth keeping: Substrate optimises _many sessions per node_; Agent
Sandbox optimises _one durable, isolated session with an identity_. We want the
second.

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

Agent Sandbox's default GKE policy blocks egress to RFC1918, cluster DNS, and the
metadata server. That default is close to what we want and should be kept, with holes
punched only for the credential proxy Service and the agent pod's SSH ingress.

Note that NetworkPolicy is **not enforced** on the reference install
(`addonsConfig.networkPolicyConfig.disabled: true`, no Dataplane V2), so on that
cluster the metadata-server block is aspirational. Enabling enforcement is a separate,
disruptive maintenance action and should be sequenced deliberately.

### Ordering

Part C first — it is independent, it closes the credential path without waiting on
any of this, and it is the only part with a proven live exploit. Part A next, because
it is cheap and unblocked. Part B last, because it is the largest change and the one
with an external dependency.

---

## What is still unproven

- **Whether `sshd` in the sandbox is the right transport**, or whether an exec-based
  Hermes backend should be written instead. SSH is what exists today; a
  `kubectl exec`-shaped backend would avoid running a second authentication system,
  but it is upstream work.
- **Startup latency.** A cold sandbox in front of the first `terminal` call of a
  conversation is a user-visible delay. `SandboxWarmPool` exists for this and has not
  been measured here.
- **What the agent legitimately needs mounted.** The whole design's strength is a
  function of this list, and nobody has enumerated it.
- **Whether `sync_back` should be on at all.** Stated above as an open decision, not
  a resolved one.
- **Agent Sandbox's own maturity.** It is a young subproject. Depending on it is a
  bet, and the fallback if it stalls is a hand-rolled StatefulSet with the same shape.

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
