# Kube-Agents Scope Resolver Module

Resolves the two `spec.scope` selectors that are not Resource Manager containers, a Shared VPC host
(`sharedVpcHosts`) and a Cloud Monitoring Metrics Scope (`metricsScopes`), to the projects they
reach, at plan time, so the [`kube-agents-iam`](../kube-agents-iam/README.md) module can bind the
read roles in each. Nothing is inherited through either, which is why the resolution has to happen
before the bindings are planned ([`docs/designs/multi-project-scope.md`](../../../docs/designs/multi-project-scope.md)
§6, §10 step 3).

## What it reads, and as whom

Three `data "http"` reads, the same the reconcile makes each run: the Compute API's
`getXpnResources` for a host's attached service projects (a project that is not a Shared VPC host
resolves to no members, as it does at runtime); the Monitoring API's `metricsScopes.get` for a
scope's monitored projects, which it names by project number; and Resource Manager v3 to name each
number. Every read carries the google provider's own access token (`data "google_client_config"`),
so it is answered for the identity that applies, impersonation included, and not for gcloud's
active account. A read that fails fails the plan, before anything is applied, with the selector,
the HTTP status and the API's message in the error: the identity needs `compute.projects.get` on a
host, to read the Metrics Scope in its scoping project (`roles/monitoring.metricsScopesViewer` is
the narrowest role) with `monitoring.googleapis.com` enabled there, and
`resourcemanager.projects.get` on each monitored project. A 200 whose body is not the document the
module reads is refused rather than read as an empty selector, since an empty selector on the next
apply is every member's bindings revoked. A monitored project the identity cannot name, or whose ID
the scope cannot carry (a legacy domain-scoped ID), is left out by naming its project number in
`exclude_projects`, the scope's `exclude.projects`; that is the only entry of that list this module
acts on. IDs and globs are the callers': `kube-agents-iam` withholds the grant of a member an entry
names by ID, the reconcile evaluates globs.

## Why a module of its own

The full-install composition calls `kube-agents-iam` with a module-level `depends_on` (the Workload
Identity pool has to exist before its binding). A module-level `depends_on` defers every data source
inside the module to apply time whenever a target has a planned change, which is every first
install and every upgrade that enables an API, and a `for_each` keyed on a deferred read fails the
plan as unknown. This module is called with no `depends_on` and no input a managed resource
produces, so its reads happen on every plan. `kube-agents-iam` refuses the plan when a declared
selector has no entry in its `scope_selector_members` input, so a caller that skips this module is
told so rather than getting the host bound and its members not.

## Inputs and output

`shared_vpc_hosts` and `metrics_scopes` are project IDs, with the CRD's pattern; `exclude_projects`
is the scope's exclude list. `members` maps each selector's snapshot name (`sharedVpcHosts/<host>`,
`metricsScopes/<scope>`) to the sorted project IDs it reaches, the shape `kube-agents-iam` takes and
the one the reconcile's `fleet_scope.json` `containers` array can be read beside.
