# Producing the PlatformAgent Usage Counters

**Status:** design; the poller is not implemented yet.

## Summary

`PlatformAgent.status.usage` declares cumulative counters, `sessionsTotal`, `eventsIngestedTotal`,
`toolExecutionsTotal`, `remediationsProposedTotal` and `remediationsAppliedTotal`, and a
`lastActiveTime`, and nothing writes them. The schema shipped that way on purpose: the agent's
ServiceAccount holds no write verb on the status, and the operator, which does, saw no session,
event or tool call. Two of the counters now have an in-cluster source. The credential broker
serves `kubeagents_tool_invocations_total` on its metrics-only listener, and the event watcher
serves `k8s_event_watcher_events_injected_total` on the gateway pod's `agent-api-auth` sidecar,
both for the managed-Prometheus collector.

This document settles how the operator turns those series into the status fields: a poller
that runs on the leader off the reconcile path, scrapes the two endpoints over a NetworkPolicy
rule that admits the operator's pods and nothing else new, accumulates per-pod deltas into
totals it keeps beside their baseline in a ConfigMap, so the counters stay monotonic across pod,
process and operator restarts, and patches the status at most once per interval, only when the
status is behind. `toolExecutionsTotal`, `eventsIngestedTotal` and `lastActiveTime` land this
way. `sessionsTotal`, `remediationsProposedTotal` and `remediationsAppliedTotal` stay unwritten
until a series exists for each, and the last section says what that series is.

## What was verified

Read from `main` and, where marked, observed read-only on a running install on 2026-10-01.
These are the facts the design rests on.

| Fact                                                                                                                                                                                                                                                                                                                                                                                                                   | Where                                                                                                                                                                                                                           |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The watcher's listener is port 9095, named `event-metrics`, on `agent-api-auth`, which the gateway pod runs as a native sidecar: an entry in `initContainers` with `restartPolicy: Always`, not in `containers`. The broker's is port 8766, named `cred-metrics`, on the `envoy-credential-proxy` container. Both are container ports, not Services, and both bind every interface.                                    | `platformagent_manifests.go` (`eventWatcherMetricsPort`, `credentialProxyMetricsPort`), `credential_proxy_manifests.go` for the broker container; `start-services.sh`, `credential_proxy.py` for the bind; the golden manifests |
| Each pod's NetworkPolicy admits its metrics port from one peer: pods in the `gke-gmp-system` namespace. The gateway policy's first rule admits every pod in the agent's namespace, but on the API ports only.                                                                                                                                                                                                          | `buildNetworkPolicy`, `buildCredentialProxyNetworkPolicy`; live: the gateway policy's 9095 rule names that one namespace                                                                                                        |
| The operator's pod carries `app.kubernetes.io/name: kube-agents-operator` in both install paths: the chart's `operatorSelectorLabels` and the kustomize manager manifest.                                                                                                                                                                                                                                              | `_helpers.tpl`, `config/manager/manager.yaml`; live: the running operator pod                                                                                                                                                   |
| The operator already reads its own namespace from the ServiceAccount namespace file and its own Pod through the API reader, to discover its image.                                                                                                                                                                                                                                                                     | `cmd/main.go`                                                                                                                                                                                                                   |
| The gateway Deployment has one replica by default; `spec.deployment.availability.replicas` renders more, with a leader-election wrapper that points the Service at one pod, and the HA fixture renders three. Every gateway pod runs the sidecar and its watcher; nothing gates the watcher on leadership. The broker Deployment has one replica and rolls with `Recreate`.                                            | the golden manifests, `start-services.sh`; live: both at 1                                                                                                                                                                      |
| The entrypoint supervises the watcher in a retry loop and restarts it in place after any exit, in the same container and pod, so the watcher's counters reset more often than its pod does.                                                                                                                                                                                                                            | `deploy/shared/start-services.sh`                                                                                                                                                                                               |
| `k8s_event_watcher_events_seen_total` increments on every delivery the informer makes, before the reason filter: that includes the initial list, which replays every Event still inside the API server's retention, and the redeliveries of watch-connection rotations. `k8s_event_watcher_events_injected_total` increments once an event has passed the filter and the dedup cache and has been handed to the agent. | `cmd/k8s-event-watcher/main.go` (`Dispatch`), `watcher.go`, the watcher's README ("Replay Shielding", "On-Disk Snapshots")                                                                                                      |
| The dedup caches are snapshotted to the data volume under `/opt/data/event-watcher/` every thirty seconds and at shutdown, and restored at start, so a restart re-offers nothing already triaged; a lost or unreadable snapshot costs one replay of the retained Events.                                                                                                                                               | the watcher's README; `platformagent_manifests.go` (the sidecar writes its snapshots to the shared volume)                                                                                                                      |
| The watcher's `k8s_event_watcher_session_creates_total{…,outcome}` records `outcome` as `ok` or `error`. The broker's series is `kubeagents_tool_invocations_total{tool,subcommand,status}` with `status` one of `success`, `error`, `blocked`, `busy`, `abandoned`; `abandoned` is recorded both for a running command killed when its caller left and for a caller that left the queue before the command started.   | `cmd/k8s-event-watcher/main.go`, `credential_proxy.py`                                                                                                                                                                          |
| The operator's ClusterRole lists pods and manages ConfigMaps, cluster-wide grants the poller uses in the agent's namespace; it patches the status subresource of other kinds already (`Status().Patch` on `AgentPlugin`).                                                                                                                                                                                              | `config/rbac/role.yaml`, `platformagent_controller.go`                                                                                                                                                                          |
| The controller `Owns` ConfigMaps with no predicate: a write to any ConfigMap whose controller owner is the CR re-enqueues the CR. The `PlatformAgent` watch itself carries no predicate either, so every status write re-enqueues the CR.                                                                                                                                                                              | `platformagent_controller.go` (`SetupWithManager`)                                                                                                                                                                              |
| `github.com/prometheus/common`, which holds the text-format parser (`expfmt`), is already in the operator's module graph as an indirect dependency; `google.golang.org/api` is a direct one.                                                                                                                                                                                                                           | `k8s-operator/go.mod`                                                                                                                                                                                                           |
| The Ready writer gates its `Status().Update` on `status.usage.activeInterfaces` and keeps a per-CR record, `prunedUsageStatus`, of a served CRD that drops `status.usage`; it re-probes every `usageStatusReprobeInterval` (5 minutes).                                                                                                                                                                                | `platformagent_controller.go` (`noteUsageStatusEcho`, `usageStatusPruned`)                                                                                                                                                      |
| The RBAC self-check is a manager `Runnable` on its own ticker, added in `main.go`, with `NeedLeaderElection` false because it is about the pod's own permissions.                                                                                                                                                                                                                                                      | `rbac_selfcheck.go`                                                                                                                                                                                                             |

## The decision

A `UsageCounterPoller`, a manager `Runnable` beside the RBAC self-check, with
`NeedLeaderElection` returning true: the counters are per cluster, so exactly one operator
replica advances them. On start and then every `usageCountersPollInterval` (five minutes, the
interval the controller already uses for the RBAC re-probe and the pruned-status re-probe, on
the same reasoning: one status write per interval is a cost nobody notices) it lists the
`PlatformAgent`s from the cache and, for each:

1. lists the pods the two policies select, in the CR's namespace: `app: <name>-gateway` for the
   gateway, and for the broker the two labels `credentialProxySelector` returns,
   `app: <name>-credential-proxy` and `kubeagents.x-k8s.io/component: credential-proxy`;
2. for each running pod, finds the container port by name, `event-metrics` on the gateway pod
   and `cred-metrics` on the broker pod, looking through `initContainers` as well as
   `containers` because the sidecar is a native one, and reads `http://<podIP>:<port>/metrics`
   with a short deadline. Selecting the port by its name means a renumbering in the manifests
   moves the scrape with it;
3. parses the exposition with `expfmt`, sums the series it wants over every label set (the next
   section says which), and takes one sample per pod per counter;
4. folds the samples into the totals through the per-pod baseline described below. Totals and
   baseline live together in one ConfigMap, which is the accumulator's source of truth; the
   status is a projection of it;
5. writes the ConfigMap when a total or the baseline changed, then patches `status.usage` when
   the status is behind the ConfigMap's totals, with `lastActiveTime` set to the time of the
   poll that last moved a total. The order, ConfigMap first, is deliberate; the resets section
   says why.

Nothing in `Reconcile`'s behaviour changes: the reconcile loop keeps writing `activeInterfaces`
through the Ready writer as it does today, the poller never touches that field, and the Ready
writer never computes a counter. The one piece of code the two share is the echo check in the
served-CRD section, generalised so that both writers call it.

## Counter sources

| Status field          | Series                                                       | Aggregation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| --------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `toolExecutionsTotal` | `kubeagents_tool_invocations_total`, broker pod              | Sum over `tool` and `subcommand`, over `status` in `success` and `error`: the commands the broker ran to an exit. `blocked` and `busy` are refusals that never ran. `abandoned` is left out because the series cannot say whether the command had started: the broker records it for a command killed mid-run and for a caller that left the queue before the start. `error` also covers a rejected request and a broker fault, so the count is the broker's view of "ran", a little wide. |
| `eventsIngestedTotal` | `k8s_event_watcher_events_injected_total`, every gateway pod | Within a pod, sum over every label: cluster, project, location, reason and namespace. Across gateway pods, the largest per-pod delta in the poll rather than the sum: each replica's watcher works the same event stream, so the sum would count an event once per replica. With one pod the two are the same.                                                                                                                                                                             |
| `lastActiveTime`      | derived                                                      | The time of the last poll in which any total moved: a command ran, or an event was accepted for triage. That is the field's documented meaning, the most recent interaction or event triage.                                                                                                                                                                                                                                                                                               |

`eventsIngestedTotal` counts the events the watcher accepted for triage, past its reason filter
and its dedup window, not the events it observed. The observed series,
`k8s_event_watcher_events_seen_total`, is the wrong source for a cumulative status field for two
reasons. It counts the informer's initial list, which replays every Event the API server still
retains, and the redeliveries of watch rotations; the watcher restarts in place more often than
its pod, so every restart would add the fleet's retained Event volume to the status under the
"a new process starts from zero" rule below, and no monotonicity check would catch it, because
the counter only ever rises. And it moves with routine cluster churn, scheduling, pulls, probe
warnings, on every watched cluster, so `lastActiveTime` would never rest and would stop meaning
what the schema says. The injected series sits behind the dedup cache, whose snapshot persists
on the data volume, so a restart re-injects nothing already triaged; when a snapshot is lost the
replay is injected again, and the counter counts that, because it happened. The field's
description changes from "ingested and evaluated" to say "accepted for triage", and the observed
volume stays where it is, in Prometheus.

The largest delta is an estimate when replicas disagree: a replica that started mid-interval
has seen fewer events than one that was up throughout, and the largest is the closest to the
number of distinct events. During a rollout, when old and new pods overlap, the per-pod reset
handling below runs first, so a new pod's whole sample competes with an old pod's difference
and the larger wins, which is still the better estimate of the two. The broker runs one pod,
so its series needs no such rule.

## Reach: how the operator gets to the endpoints

The scrape is a direct read of the pod IP, and today the two policies admit the metrics ports
from the collector's namespace alone. The implementation adds one ingress rule to each policy,
the same shape as the collector's rule beside it: the operator's namespace by its
`kubernetes.io/metadata.name` label, and within it pods with
`app.kubernetes.io/name: kube-agents-operator`, on the metrics port only. The operator renders
these policies, so it knows what to write: its own namespace it already reads from the
ServiceAccount namespace file, and the label is the one both install paths put on its pod. A
deployment that relabels the operator pod breaks the scrape and nothing else; the failure
section says how that shows.

The rule is narrower than the obvious alternative of admitting the agent's namespace, which
would open both listeners to the shell sandbox and to anything else that lands in the
namespace. The listeners serve counters with closed label vocabularies, so that would leak
nothing secret, but the broker's metrics listener was admitted past the broker's own
reachable-off-pod refusal on the argument that it serves counters to a collector; a pod
selector keeps that argument true.

Two documents state the current peer set as a security property rather than as a description:
`docs/security-requirements.md` and `docs/credential-isolation-design.md` both say the broker
opens 8766 to the collector's namespace and to no other peer. The rule here adds one peer, and
the implementation rewrites those sentences to say so, with the pod selector as the reason the
property holds in substance: the listener reaches the collector and the operator, both readers
of counters, and nothing else.

The operator's own egress is not restricted by any policy this repository renders, so no rule is
needed on its side.

## Resets and the state that keeps the counters monotonic

A Prometheus counter is the life of one process. The broker's restarts with its pod and with
its container; the watcher's with the gateway pod, with its sidecar container, and with every
restart the entrypoint's supervisor performs in place. A status counter that copied the sample
would fall back to zero on every one of those. The poller therefore keeps, per CR, a baseline:
the last sample it took from each pod, keyed by pod UID, per counter. On a poll it adds, for
each pod it scraped:

- the difference from the pod's last sample, when the pod UID is known and the sample is not
  below the last one;
- the whole sample, when the pod UID is new (a new pod starts from zero, so everything it has
  counted is new) or the sample is below the last one (the process restarted inside the same
  pod);
- nothing, when the pod could not be scraped this poll; its baseline entry is kept, so the next
  successful poll adds the difference across the gap.

Entries for pods that no longer exist are dropped when the baseline is next written; their
counts are already in the totals.

The totals and the baseline live together in a ConfigMap, `<name>-usage-counters`, in the CR's
namespace, holding one JSON document: the running total per counter, and per pod UID the pod's
name for a reader and its last sample per counter. The ConfigMap is the source of truth the
accumulator reads at the start of every poll; the status is written from it, never the other
way round, so a cache that hands the poller a stale CR can never pull a total backwards. It
carries an owner reference to the CR, without the controller flag, so it is collected with the
CR but does not re-enqueue it: the controller `Owns` ConfigMaps with no predicate, and a
controller-owned one would cost a reconcile on every write. It is written only in a poll in
which a total or the baseline changed, so a quiet install writes nothing.

In memory alone the baseline would be lost with the operator, and the first poll after a
restart would see every pod as new and add its whole sample again, counting every command and
event since those pods started a second time; the totals, kept in memory, would start from zero
and the status would fall back. With the ConfigMap, an operator restart loses nothing.

When the ConfigMap is absent but the status already carries counters, something removed the
state after the counters had been written. The poll then seeds the totals from the status,
records every pod's current sample as its baseline, and adds nothing: it under-counts whatever
happened between the last written poll and this one, once, rather than over-counting everything
the pods have ever done. On a fresh install both are absent, and the first poll adds every pod's
whole sample, which is right: nothing before it was counted.

The ConfigMap is written before the status. A crash between the two leaves the status one poll
behind the totals, and the next poll repairs it, because the status patch is issued whenever the
status is behind the ConfigMap, not only when this poll moved a total. The other order would
leave the totals behind the status after a crash, and the next poll would add the interval's
deltas a second time. Under-counting until the next poll is the error this document prefers
everywhere; an over-count is permanent.

## Write cadence and the status writers

The status is written with `Status().Patch` and a merge patch from the CR as read, touching only
the counters and `lastActiveTime`, at most once per poll and only when the status is behind the
ConfigMap's totals. Every status write re-enqueues the CR through the unfiltered `PlatformAgent`
watch, which is why the write is bounded by the interval and never issued from `Reconcile`: a
busy install costs one reconcile per five minutes, and the ConfigMap's non-controller owner
reference keeps it at one rather than two; a quiet one costs none.

The Ready writer and the poller write the same subresource from different goroutines. The
Ready writer's `Update` carries the counters as it read them, so it never zeroes a field the
poller wrote; when the two cross, the API server's resource-version check rejects the later one
and controller-runtime retries that reconcile, as it does for any conflict. The poller's patch
carries no resource version and cannot be rejected that way; it can only land on top of a Ready
write, which is fine, because the fields are disjoint and the values come from the ConfigMap,
not from the CR the poller read.

## The served-CRD skew

A served CRD older than this release prunes `status.usage` on every write, and the Ready writer
already stops gating on `activeInterfaces` while its `prunedUsageStatus` record for the CR is
fresh, probing again after the interval. The poller consults the same record and skips the
status patch while it is fresh, so an operator running ahead of its CRD costs one probe per
interval in total, not one per writer. The ConfigMap is still written on every poll that moved
a total, so nothing is lost during the skew: when the CRD is applied, the next patch carries the
totals accumulated since the operator was upgraded, not since the last interval. After its own
patch the poller reads the echo the same way `noteUsageStatusEcho` does: counters it wrote that
come back absent mean the pruning, recorded in the shared map; counters that come back clear it.
The echo check moves from a function that knows about `activeInterfaces` to one that takes the
fields a writer expects to see, and both writers call it.

## Failure behaviour

The schema has no field for an error, by design: static enums and integer counts only. A
scrape that fails leaves the pod's baseline untouched and the totals where they were. The
operator log carries one line when an endpoint first fails and one when it recovers, naming
the pod and the error type, never the body. The visible symptom of a standing failure, a
NetworkPolicy regime that blocks the rule, a relabelled operator pod, a listener that moved, is
a `lastActiveTime` that stops advancing while the install is plainly busy. A `kubectl describe`
of the CR shows the operator's events; the implementation records one warning event per failure
streak so that the symptom has a cause beside it without a log search.

## What stays unwritten, and what lands it

| Status field                | Why it stays absent                                                                                                                                                                                                                                                          | The series that lands it                                                                                                                                                                                                                                             |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sessionsTotal`             | The watcher's `k8s_event_watcher_session_creates_total{outcome="ok"}` counts the sessions it opens for triage and nothing else; chat sessions are opened by the gateway, which exports no series for them. A counter named for all sessions that counted half would mislead. | A gateway-side `kubeagents_sessions_total{origin}` beside the watcher's, summed with it. The `session_store` plugin sees every chat session start and is the natural emitter; it needs a listener, which the gateway pod does not have for Hermes-side series today. |
| `remediationsProposedTotal` | No series counts proposals. The proposal path is the broker's version-control route (`proposal-create`), which logs but does not count.                                                                                                                                      | A broker series over the version-control route's verbs and outcomes, counted at the same sites the exec route counts tool invocations; `proposal-create` with a success outcome is a proposal.                                                                       |
| `remediationsAppliedTotal`  | An applied remediation is an approved one; approvals are recorded by the `tool_call_audit` plugin's `approval_*` records in the log, not as a metric.                                                                                                                        | Either a Hermes-side series from the same plugin, which has the listener problem above, or a count of the broker's apply-side requests once those are distinguishable from reads. This is the least settled of the three and the last to land.                       |

Each lands on the poller's existing path: a new row in the source table, a new series summed,
and the CRD description changed from "nothing writes it yet". None needs a second mechanism.

## Security and privacy

Zero external egress: the operator reads two in-cluster listeners over the pod network and
writes two objects in the cluster. Zero PII or secret exposure: the status receives integer
totals and one timestamp; label values are summed away and never written anywhere, and the
baseline ConfigMap holds pod UIDs, pod names and integers. The new NetworkPolicy rules admit
the operator's pods on the two metrics ports and nothing else; the collector's rule is
unchanged. No new RBAC: pods are listed and ConfigMaps managed with verbs the ClusterRole
already grants, and the status subresource is already the operator's to write. No credential
of any kind is involved; both listeners are unauthenticated by design, as they are for the
collector.

## Alternatives not taken

**Counting observed events rather than accepted ones.** `k8s_event_watcher_events_seen_total`
is the closer match to the words "ingested and evaluated", and the sources section says why it
cannot be a cumulative status field: it replays the API server's retained Events on every
watcher start, and it never rests.

**Reading the series from Managed Prometheus through the Monitoring API.** The collector
already scrapes both endpoints and the Prometheus query endpoint would hand back `increase()`
over any window, which makes resets someone else's problem and needs no NetworkPolicy change.
It was not taken because the operator's ServiceAccount carries no Google identity, so it would
need a Workload Identity binding and `roles/monitoring.viewer` in the IAM module and the chart,
it would tie a core status field to GKE and to a collector an install may switch off, and it
would add the collector's ingestion lag to a counter that is otherwise seconds behind. The
poller's source is an interface with the pod scraper as its one implementation, so a
deployment that cannot admit operator-to-pod traffic can gain this source later without
changing the accumulation or the writer.

**Reaching the pods through the API server's pod proxy.** `GET .../pods/<pod>:<port>/proxy/metrics`
needs no new pod-network rule, but it needs `pods/proxy` on the operator's ClusterRole, which
is the power to reach any port of any pod, and on a cluster that applies policy to control-plane
traffic the proxy's own source would need admitting. Broader than the problem.

**Letting the agent write its own counters.** The security reference lists the agent
ServiceAccount's write grants, leader-election leases and, at more than one replica, its own
pod's labels, and the status is not among them. The operator is the only writer, and a workload
with cluster privileges describing its own activity is not a grant to add.

**Keeping the baseline, or the totals, in memory.** Simpler, and wrong on every operator
restart, as the resets section says.

**Polling from `Reconcile`.** The loop is event-driven and can be quiet for hours; a counter
that advanced only when something else changed the CR would read as broken on exactly the
installs where it is most useful. The reconcile loop's steady-state requeue is also capped by
the probes it schedules, and a poller there would either shorten every requeue or move with
them.

## Testing

Unit tests, beside the poller: the accumulator across the four cases the resets section lists
(a known pod moving, a new pod UID, a sample below the last one, a pod missing this poll), the
baseline-absent-with-counters-present case, the largest-delta rule across two gateway pods and
its agreement with the sum for one, the series selection (the `status` values summed and the
three excluded; the injected series and not the observed one), and the port-by-name lookup when
the port sits on a native sidecar among several containers. The accumulator takes samples and
the ConfigMap's document and returns the next document, so none of these needs a socket.

An envtest, beside the existing `usage_status_envtest_test.go`: a `PlatformAgent` served by
this release's CRD receives one patch per poll in which a stub source moves and none in which
it does not; a ConfigMap written with the non-controller owner reference enqueues no reconcile;
a status left behind the ConfigMap (the crash between the two writes, staged by hand) is
repaired by the next poll without the totals moving; under the CRD without `status.usage`, the
poller writes the status once per `usageStatusReprobeInterval`, shares the pruning record with
the Ready writer, and keeps the ConfigMap current throughout.

A live check, which is the acceptance criterion: on an install built from the branch,
`toolExecutionsTotal` rises after commands run from the sandbox and `eventsIngestedTotal` after
events the watcher accepts arrive; a broker pod restart, a gateway pod restart, a restart of the
watcher process alone (the supervisor's), and an operator restart each leave both counters where
they were and they keep rising afterwards, the watcher's restart in particular adding nothing for
the replay of the retained Events; a quarter of an hour with no command and no accepted event
produces no status write; and the two policies show the new rule with the operator pod as the
only peer added.

## Documents the implementation changes

- The CRD reference's `status.usage` rows for the two counters and `lastActiveTime`, from
  "declared; nothing writes it yet" to what they count and how often they move, including that
  `eventsIngestedTotal` counts events accepted for triage, and the rows for the counters that
  stay unwritten pointing at this document's last table.
- The security reference's NetworkPolicy summary, which lists what the gateway and broker
  policies admit on ingress, gains the operator's pods on the two metrics ports.
- The observability page's metrics section gains one sentence: the operator reads these two
  series into `status.usage`, which is where an operator without a collector sees the counts.
- The `AgentUsageStatus` field comments, which say nothing writes the counters, and the
  `EventsIngestedTotal` comment's meaning.
- Every page that states the metrics ports' peer set as closed: the security reference's two
  NetworkPolicy bullets (the gateway's and the broker's, each ending in "nothing else"), the
  credential-isolation reference's "and nothing else" on the broker's ports, the telemetry
  page's "admit only `gke-gmp-system`", the kustomize page's list of gateway ingress peers, and
  the chart README's note that the operator's policies admit the collector's namespace on
  those ports. Each gains the operator's pods on the metrics port.
- `docs/security-requirements.md` and `docs/credential-isolation-design.md`, which state 8766 as
  open to the collector's namespace and to no other peer; the Reach section says what replaces
  that sentence.
- `docs/designs/cron-report-relay.md`, which records that the operator reads nothing the agent's
  pods write and binds no metrics endpoint of its own. The poller is the exception, and that
  record points here.
- The map's identifier-sources table, which gains a row for the poll interval, the ConfigMap
  name and the operator-peer rule, with the event-watcher row as the model.
