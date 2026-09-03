# An Opt-In Multi-Project Scope for the Platform Agent

> **STATUS — design of record; not implemented.** Nothing below ships today. The Platform Agent
> discovers clusters in one GCP project, its service account holds roles in one project, and the
> architecture documents define it as one agent per project. This document proposes replacing that
> single project with a declared scope, and gives the order the change has to land in. Each section
> says what is true on `main` now and what the design changes.

**Scope:** Which GCP projects a single kube-agents install manages, how that set is declared,
resolved, granted, and kept current as projects come and go, and what in the codebase assumes there
is only one.
**Owns:** the scope model on the `PlatformAgent` resource, the discovery path that resolves it to
clusters, the IAM that makes the discovery readable, the membership snapshot and its drift signal,
and the sequencing. What a credential may do once it reaches a cluster belongs to
[`../credential-isolation-design.md`](../credential-isolation-design.md); the per-cluster service
account pool it would eventually feed is `terraform/modules/kube-agents-iam/scoped_pool.tf`; how
per-cluster profiles are scheduled once they exist is
[`spec-subagent-profiles.md`](spec-subagent-profiles.md).

---

## 1. The problem

The cluster profile sync is single-project by construction. `cluster_agent_reconcile.py`, the hourly
job that gives every GKE cluster a Cluster Agent profile, resolves exactly one project and lists it
exactly once:

- `_project()` (`agents/platform/scripts/cluster_agent_reconcile.py:87-96`) returns one string:
  `RECONCILE_PROJECT` from the environment, else the GCE metadata server's `project/project-id`,
  else `gcloud config get-value project`. All three answer "the project the management cluster runs
  in".
- `_all_clusters(project)` (`:99-132`) runs one `gcloud container clusters list --project <P>` and
  tags every row with that project.
- `reconcile()` (`:261-262`) calls it once. When the project cannot be resolved, or that one list
  call fails, the CREATE direction is skipped for the run and the job exits 0 in prune-only mode.
  #566 is that path: the broker refuses the list call, and nothing reports it.
- The header (`:7-10`) states the policy: "every cluster in the project gets a Cluster Agent
  profile". The only opt-out is `RECONCILE_EXCLUDE`, a list of cluster names (`:63`).

IAM matches the code. `terraform/modules/kube-agents-iam/main.tf:57-68` binds `project_roles` to the
agent's service account with `google_project_iam_member` in `var.project_id` and nowhere else, and
`terraform/examples/full-install/main.tf:229-234` passes the install's one `project_id`. Widening the
list call without widening IAM would produce a 403 per extra project, which `_all_clusters` reports
and then treats as "skip create this run".

The documents agree with both. `docs/architecture/01-vision-scope.md:75` gives the Platform Agent a
cardinality of "1 per project"; `02-agent-personas.md:280` says it is "scoped to its one project" and
"cannot read or reach another project"; `03-security-model.md:114` lists "any other project" under
what it is forbidden to touch; `06-api-and-data-contracts.md:82` keys the `platform` tier on a single
`projectId`. Single-project is the documented end-state, so this is a scope change to the
architecture, not a gap in the implementation of it.

The cost today is that an organisation with clusters in several projects installs kube-agents
several times: one management cluster, one operator, one Pub/Sub topic, one chat front door per
project, with no view across them. A question like "which of our clusters run a version behind" has
no single agent that can answer it.

**Prior art.** PR #588 added `--monitored-projects` to `install.sh` and per-project IAM to the
bash provisioning scripts. The provisioning scripts were replaced by Terraform in #797 while that
branch was open; a force-push then dropped the IAM code without porting it, and the branch was
closed on 2026-08-28 with a comment that `main` handles multi-project IAM and reconciliation
natively through Terraform and Helm. It does not: the IAM module above takes one project, and no
reconciler reads more than one. Epic #618 still lists multi-project onboarding as its open phase 3.
#953 (the agent cannot route a request that does not name a cluster) and #1126 (the broker's read
allowlist withholds discovery reads a leaf read needs) are the same problem seen from the agent's
side: the fleet it can enumerate is narrower than the fleet it is asked about.

## 2. What already generalises

The profile model is project-qualified end to end, so multi-project discovery does not change how a
Cluster Agent is named, stored, or driven:

- Profile names are `cluster-{project}-{cluster}-{location}`, derived in `profile_name()`
  (`agents/platform/scripts/cluster_agent_profile.py:66`), and the profile's `config.yaml` carries a
  `cluster_identity: {project, cluster, location}` block (`:99`). `read_cluster_identity()` reads it
  back (`:103-124`).
- PRUNE works per stamped identity, not per resolved project: `_cluster_exists`
  (`cluster_agent_reconcile.py:135-163`) runs `describe --project=<identity.project>`, so a profile
  for a cluster in another project is verified against the right project today.
- `create_profile()` fetches credentials with `--project=<P>` (`cluster_agent_profile.py:235-241`).
- The credential broker passes `--project` through as a value-taking flag
  (`agents/platform/scripts/command_policy.py:378`), takes the project from the kubeconfig context
  name (`credential_proxy.py:1168-1190`), and re-issues `get-credentials` with the target's project
  (`:2496`). It does not pin a project. Only IAM stops a cross-project call.
- The scoped service account pool is already keyed on a per-row project. `scoped_clusters` in
  `terraform/modules/kube-agents-iam/variables.tf:71-96` is a list of
  `{project_id, location, cluster_name}` objects, with the comment that "a cluster in another
  project is a row in this list rather than a second module"; the CRD mirror is
  `spec.security.scopedServiceAccounts[]` (`k8s-operator/api/v1alpha1/common_types.go:498-540`),
  whose `projectId` "need not be the project the agent runs in".

What changes is therefore confined to four places: how the set of projects is declared, how it is
resolved to clusters, how the service account is granted into it, and which documents describe the
boundary.

## 3. The scope model

A new block on `PlatformAgent`, `spec.scope`, declares an opt-in set. The name is provisional; there
is no `spec.fleet` or similar today; the top-level spec has `harness`, `integration`, `mode`,
`deployment`, `security`, `telemetry`, and `networkPolicy`.

```yaml
spec:
  scope:
    projects: # explicit project IDs
      - payments-prod
      - payments-staging
    folders: # Resource Manager folders, resolved to every project beneath them
      - folders/123456789012
    organizations: # an entire organisation; see §9 before using this
      - organizations/987654321098
    exclude:
      projects: # never resolved, even if a folder above contains them
        - payments-sandbox
      clusters: # by name, in any project; replaces RECONCILE_EXCLUDE
        - scratch-cluster
```

Rules:

- **Empty scope means today's behaviour.** No `spec.scope`, or one with every list empty, resolves
  to the management project alone, found the way `_project()` finds it now.
- **The management project is always in scope.** It cannot be excluded, because the management
  cluster's own alerts need a profile to be delegated to (the reasoning in
  `cluster_agent_reconcile.py:17-27` still holds).
- **Selectors union; exclusions subtract afterwards.** A project reached through a folder and named
  explicitly appears once. An excluded project is dropped whether it was reached through a list or a
  container.
- **`exclude.clusters` subsumes `RECONCILE_EXCLUDE`.** Exclusions travel in the scope file (§5)
  with the rest of the declaration. The environment variable keeps working for one release as a
  fallback the script honours only when no scope file is present, and is then removed.
- **Resolution is deterministic.** The resolved project set is sorted before it is listed or written
  anywhere, so two runs against an unchanged fleet produce byte-identical snapshots (§5) and an
  unchanged roster.
- **A later selector, `sharedVpcHosts`.** Teams group projects by Shared VPC as often as by folder,
  and "every service project attached to host `H`" is answerable from the Compute API. It is
  deferred because a VPC is a network grouping, not a Resource Manager container: IAM cannot be
  granted on it, so §6's inheritance argument does not apply and every attached project would need
  its own binding. It fits the model as a fourth list once the first three work.

## 4. Resolution

Resolution turns the declared scope into a set of `(project, cluster, location)` tuples, plus a
per-project outcome. It runs inside the existing reconcile job under the agent's identity, through
the credential broker, because that is the only process in the install that talks to GCP on a
schedule and the operator deliberately holds no GCP credential.

**Explicit projects** use the call the script makes today, once per project:
`gcloud container clusters list --project <P> --format=value(name,location)`.

**Folders and organisations** use Cloud Asset Inventory rather than walking the tree:

```bash
gcloud asset search-all-resources \
  --scope=folders/123456789012 \
  --asset-types=container.googleapis.com/Cluster \
  --format='value(name,location)'
```

One call returns every cluster under the container, including in projects created since the last
run, and needs `roles/cloudasset.viewer` on the container plus the Cloud Asset API enabled in the
host project only. The project ID is parsed from the asset `name`
(`//container.googleapis.com/projects/<ID>/locations/<L>/clusters/<C>`), not read from the
`project` field: that field carries the project _number_ (`projects/757207957170`, measured against
`bhoekstra-gkedemos`), and a cluster keyed by number would get a second profile beside the one its
explicit project ID produces, and would never match an `exclude.projects` entry. Everything
downstream keys on the ID.

The fallback when the Asset API is not enabled is a Resource Manager walk: list the folders under
the container with `gcloud resource-manager folders list --folder=<F>` recursively, then
`gcloud projects list --filter='parent.type=folder AND parent.id=<F>'` for each folder found, then
`clusters list` per project. That is one call per folder plus one per project, needs
`resourcemanager.folders.list` and `resourcemanager.projects.list` at the container, and has to
recurse: `parent.id` matches the immediate parent only, so a walk that stops at the declared folder
misses every project in a sub-folder, silently. The snapshot records which path ran.

The discovery verbs are absent from the broker's read allowlist. `GCLOUD_READ_COMMANDS`
(`command_policy.py:277-363`) admits `container clusters list` and `projects list` but no `asset`
or `resource-manager` command, and `projects list` today serves no scope wider than one. This is
the class of gap #1126 describes: a discovery read the leaf reads depend on, refused fail-closed
with no signal. Adding `("asset", "search-all-resources")` and
`("resource-manager", "folders", "list")` is part of phase 1.

**Every project gets an outcome, and no outcome is silent.** For each resolved project the run
records one of:

| Outcome        | Meaning                                                     | Effect on profiles                          |
| -------------- | ----------------------------------------------------------- | ------------------------------------------- |
| `ok`           | Listed; zero or more clusters returned                      | CREATE runs for its clusters                |
| `denied`       | 403: the service account is not granted in this project     | Existing profiles kept; CREATE skipped      |
| `api-disabled` | `container.googleapis.com` is off in this project           | Treated as zero clusters; nothing to manage |
| `unreachable`  | Timeout, network, quota, or a `gcloud` error not classified | Existing profiles kept; CREATE skipped      |

**Every container gets the same outcome, and a container that is not `ok` freezes its members.**
A folder or organisation whose resolution call failed (`denied`, `api-disabled` on the Asset API
with no working fallback, `unreachable`) has produced no project list, and "no projects" and
"could not list projects" must not read the same. For a container that is not `ok` the run carries
its member projects forward from the previous snapshot, skips CREATE for them, and prunes nothing
under it. Without this rule one failed folder lookup would make every project beneath it "out of
scope" and §7's prune would delete every profile under the folder in a single tick, which is the
one thing `cluster_agent_reconcile.py:11-15` exists to never do.

`denied` and `unreachable`, for projects and containers alike, are counted in the report the job
already prints (`report` at `cluster_agent_reconcile.py:228-236`) and make the bootstrap gate's
roster read as partial rather than complete. This is the lesson of #566: a project the agent was
told to manage and cannot list is a finding, and folding it into an empty list turns a permission
gap into a clean fleet.

## 5. Where the resolved membership lives

The `PlatformAgent` resource carries the declaration only. The resolved set lives with the
profiles, on the data PVC, in a snapshot the reconcile run rewrites every hour:

```json
{
  "resolvedAt": "2026-09-03T14:11:07Z",
  "declared": { "projects": [...], "folders": [...], "organizations": [...], "exclude": {...} },
  "resolver": "asset-inventory",
  "containers": [
    { "id": "folders/123456789012", "outcome": "ok", "projects": 3 }
  ],
  "projects": [
    { "id": "payments-prod", "via": ["folders/123456789012"], "outcome": "ok", "clusters": 4 },
    { "id": "payments-staging", "via": ["explicit"], "outcome": "denied", "clusters": null }
  ]
}
```

Today the roster is the set of profile directories under `$HERMES_HOME/profiles/`, read by the
bootstrap gate (`agents/chat/scripts/bootstrap_scan_gate.py`) through one `hermes profile list`
call (`_roster_command()`, `:148`). The gate keeps reading that; the snapshot sits beside it as
`$HERMES_HOME/fleet_scope.json` and the gate's instructions to the sweep worker name any project
whose outcome is not `ok`, so a partial roster is reported as partial rather than audited as
complete.

The operator renders `spec.scope` to the pod the way it renders other agent configuration, as a
mounted file rather than an environment variable: the lists are unbounded and the CRD already
carries `spec.deployment.env` (`common_types.go:368-372`) only as a generic passthrough. The
rendered file's hash joins the ConfigMap hash that rolls the Deployment, so editing the scope
takes effect at the next pod start and the next reconcile tick, whichever is later.

Whether the snapshot should also be lifted into `.status` is open (§11). It would make `kubectl get
platformagent -o yaml` answer "which projects does this install manage" without a pod exec, but the
pod has no channel to the operator today and building one for this alone is out of proportion.

## 6. IAM

`kube-agents-iam` gains a `scope` input mirroring `spec.scope`, and the full-install composition
generates it from the same `terraform.tfvars` the installer front doors already write. Grants
follow the selector type:

- **Explicit project.** `google_project_iam_member` for each role in `project_roles`, in that
  project. This is the existing resource with a second `for_each` dimension.
- **Folder.** `google_folder_iam_member` for each role in `container_roles`, plus
  `roles/cloudasset.viewer`, on the folder.
- **Organisation.** `google_organization_iam_member`, same roles, on the organisation.

`container_roles` is `project_roles` minus the two entries that are not viewer roles.
`roles/iam.serviceAccountUser` is `iam.serviceAccounts.actAs`, held so the agent can run jobs as
service accounts in its own project; inherited across a folder it would let the one agent identity
act as every service account in every project beneath, including ones created tomorrow.
`roles/mcp.toolUser` lets the agent call the GKE MCP server; whether that check runs in the host
project or in the project a call targets is not measured here, so it stays host-only until it is
(§11). Both stay bound in the host project only. The remaining six
(`container.clusterViewer`, `container.viewer`, `compute.viewer`, `monitoring.viewer`,
`logging.viewer`, `iam.securityReviewer`) are read roles, and they are what a container binding
carries.

Inheritance is the point of offering containers at all. A folder-level binding reaches every
project beneath it, including one created tomorrow, so onboarding a new project under a declared
folder is zero-touch: it appears at the next hourly reconcile with no change to the CR, the tfvars,
or the IAM. That is the answer to "maintaining the list over time": the list is a container, and
GCP maintains it.

The same inheritance widens the blast radius of the one service account that holds these roles,
which §9 takes up.

Prerequisites the design has to state and the installer has to preflight:

- The identity running Terraform needs `resourcemanager.folders.setIamPolicy` on each folder, or
  `resourcemanager.organizations.setIamPolicy` for an organisation. Today it needs only
  project-level IAM admin. The installer's preflight reports which containers it cannot bind rather
  than failing on the first.
- A project in scope with `container.googleapis.com` disabled resolves to zero clusters (§4's
  `api-disabled`); Terraform must not enable the API in other people's projects.
- `project_roles` stays the list bound in the host project and in explicit projects, and the
  mirror between it and `read_only_roles` that `tests/test_scoped_sa_pool_iam.py` checks is
  unchanged. `container_roles` is derived from it by subtraction in one place, with a test that the
  two excluded roles are the only difference, so a role added to `project_roles` later reaches
  containers unless someone decides otherwise in that file.

Uninstall revokes what install granted: `terraform destroy` removes the bindings because Terraform
owns them, which is the property #588 lost when its revocation lived in a bash function.

## 7. The onboarding lifecycle

**Adding a project.** Under a declared folder or organisation: nothing to do; it is discovered at
the next tick. As an explicit project: add it to `spec.scope.projects` and to the tfvars, run
`upgrade.sh` so the IAM binding exists before the reconcile tries the list, and the project's
outcome goes from `denied` to `ok` at the following tick. The order matters and the snapshot shows
it: a project added to the CR before Terraform has run reads `denied`, which is correct and visible,
not an error to suppress.

**Removing a project from scope.** Its clusters' profiles are pruned the way `RECONCILE_EXCLUDE`
prunes a cluster today, on the strength of the declaration rather than of a cloud error. The rule
has two conditions, both required: the project is absent from the resolved set, _and_ every
declared container resolved `ok` this run, so that the absence is the declaration speaking and not
a failed lookup (§4). A project that became `denied` because a binding was revoked without editing
the scope is not pruned; the profiles stay, the outcome is reported, and an operator resolves it
one way or the other.

**A project that disappears.** Deleted, or moved out from under a declared folder: its clusters
stop appearing in the resolved set, and PRUNE's per-profile `describe --project=<P>` now returns a
403, because the folder binding no longer covers it, which `_cluster_exists` classifies as unknown
and keeps (`cluster_agent_reconcile.py:135-163`). It is the out-of-scope rule above, not NotFound,
that retires those profiles, and only once the containers have resolved `ok`. Moved to a different
declared folder: no change, because resolution is by project and the `via` field merely records
the new path.

**Never on ambiguity.** The rule at `cluster_agent_reconcile.py:11-15` holds: auth, network, quota,
and unclassified errors leave profiles untouched.

## 8. Everything else that assumes one project

Discovery and IAM are the mechanism; these are the places that will read wrong once the mechanism
works. Each is listed with whether it blocks the first phase or follows it.

| Where                                                              | What it assumes                                                                                   | Phase |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- | ----- |
| `agents/platform/scripts/session_kv_server.py:1155`                | `GCP_PROJECT_ID` is the project for every event's console links                                   | 1     |
| `agents/platform/scripts/platform_mcp_server.py:275-300`           | `get_project_id()` reads one `project:` line from `USER.md`                                       | 1     |
| `agents/platform/skills/cluster-agent-lifecycle/SKILL.md`          | Delegation needs `--project` from the requester; #953 already asks for enumeration first          | 1     |
| `terraform/modules/drift-pubsub`                                   | One log sink in `var.project_id`; other projects' audit logs need a sink each into the host topic | 2     |
| Fleet-audit SOPs and the cost, recommender, and compliance skills  | Query "the project" for quotas, recommendations, and IAM; need to iterate the snapshot            | 2     |
| `docs/site/src/content/docs/concepts/cluster-agents.md:24`         | "sweeps the project"                                                                              | docs  |
| `docs/site/src/content/docs/reference/security-and-iam.md:84`      | "an IAM role grants privileges across all clusters in the project" becomes "in the scope"         | docs  |
| `docs/site/src/content/docs/reference/credential-isolation.md:218` | Describes `RECONCILE_PROJECT` as the way the script finds its project                             | docs  |

Event delivery from other projects is the largest of these. The event watcher watches through each
profile's kubeconfig and already labels every metric with `project` and `location`, so Kubernetes
events fan in as soon as profiles exist. Reaching a private cluster in another VPC needs the DNS
endpoint, which `create_profile()` already selects per cluster; that is why cross-project reach
works, not an assumption that breaks. Cloud audit-log drift,
which `drift-pubsub` exports through a log sink, is per project by construction; a Shared VPC or a
folder-level aggregated sink can replace N per-project sinks, and that is its own design.

## 9. The boundary changes, and what does not

The four architecture documents move from "its one project" to "its declared scope":

- `01-vision-scope.md:75` and `:121`: cardinality becomes "1 per scope (one or more projects)".
- `02-agent-personas.md:16`, `:31`, `:262`, `:280-282`, `:477`: the persona is scoped to the projects
  in `spec.scope`, and the containment sentence becomes "it cannot read or reach a project outside
  its declared scope".
- `03-security-model.md:114`, `:123`, and `:381`: the forbidden column and the containment sentence
  read "any project outside its scope".
- `06-api-and-data-contracts.md:82`: the `platform` tier's scope field becomes the resolved project
  set, with `projectId` kept as the management project.

What does not change: read-only stays read-only. The roles a container binding carries are the
viewer subset of §6, the two non-viewer roles in `project_roles` (`iam.serviceAccountUser`,
`mcp.toolUser`) stay in the host project, and nothing here grants a write anywhere. What does
change is how much one credential can read. The
agent's service account carries `roles/container.viewer`, which "lets an identity read Kubernetes
objects in every cluster in the project" (`kube-agents-iam/main.tf:30-31`); bound on a folder it
reads every cluster in every project beneath. That is the argument for landing the scoped service
account pool's authority (`scoped_pool.tf`, currently granting nothing) before offering
`organizations` in a release: a per-cluster credential bounds what a compromised sandbox reads to
one cluster regardless of how wide discovery is. Until then the design recommends `projects` and
`folders` for a fleet an operator would be comfortable reading with one account, and documents
`organizations` as available but wide.

## 10. Implementation order

Each step is shippable alone and live-testable on a shared install by granting its service account
into a second project the tester controls.

1. **Explicit projects.** `spec.scope.projects` and `spec.scope.exclude` on the CRD; the operator
   renders the scope file; `cluster_agent_reconcile.py` iterates the list and writes
   `fleet_scope.json` with per-project outcomes; `kube-agents-iam` binds `project_roles` per
   explicit project; the bootstrap gate names non-`ok` projects; `session_kv_server.py` and
   `platform_mcp_server.py` read the project from the event or the profile identity rather than one
   environment value. This is the smallest change that manages two projects from one install.
2. **Folders and organisations.** Asset Inventory resolution, the Resource Manager fallback, and
   their allowlist entries; container outcomes and the freeze rule; folder- and organisation-level
   bindings of `container_roles` plus `roles/cloudasset.viewer`; the installer preflight for
   container IAM permissions; `via` and `containers` in the snapshot.
3. **Downstream consumers.** The phase-2 rows of §8: audit-log sinks per project or an aggregated
   sink, and the fleet-audit SOPs and cost skills iterating the snapshot.
4. **Documents.** The architecture edits in §9 and the three site pages in §8, in one PR once
   phase 1 has merged, so the documents describe what runs.
5. **Shared VPC selector.** After the first three selectors have been used by someone other than
   the author.

## 11. Open questions

- **Snapshot in `.status`?** §5 keeps the resolved membership on the PVC because the pod has no
  channel to the operator. If one arrives for another reason, the snapshot should ride it.
- **Cardinality at organisation scale.** One Platform Agent for an organisation of hundreds of
  projects means one chat front door, one reconcile job, and one hourly sweep for all of them. The
  reconcile's per-profile `describe` in PRUNE is already O(clusters); at what fleet size does an
  install want two Platform Agents with disjoint scopes, and does anything need to prevent overlap?
- **A ceiling on projects per install.** The CRD caps `scopedServiceAccounts` at 100 entries; the
  scope lists should carry a cap for the same reason, and the number is a guess until phase 2 runs
  against a real folder.
- **Deriving `scopedServiceAccounts` from scope.** Once the pool grants authority, hand-listing
  every cluster in `spec.security.scopedServiceAccounts` duplicates what resolution already found.
  Terraform cannot read the snapshot, so either the pool moves to per-project accounts or the
  snapshot becomes a Terraform input through a data source; neither is settled.
- **`mcp.toolUser` across projects.** If the GKE MCP server checks `roles/mcp.toolUser` in the
  project a call targets rather than in the caller's project, MCP-backed reads of a scoped project
  fail while `gcloud` reads succeed, and the role has to join `container_roles`. One call against a
  second project settles it.
- **Who may widen the scope.** Editing `spec.scope` is a Kubernetes RBAC question on the
  management cluster; granting into a folder is a GCP IAM question. They are enforced by different
  systems and can disagree. The design assumes the tfvars is the source of both and the CR is
  rendered from it on the installer path, which holds for `install.sh` and not for a hand-applied
  CR.
