# Grants for the projects, folders, organisations, Shared VPC hosts and Metrics
# Scopes `spec.scope` declares beyond the host project.
#
# The scope is the set of GCP projects whose GKE clusters the Cluster Agent
# reconcile enumerates (docs/designs/multi-project-scope.md §3). The reconcile
# reads the declaration from the PlatformAgent CR; this file is the IAM half of
# the same value, so a project named in `scope.projects`, a folder or
# organisation named in `scope.folders` or `scope.organizations`, and every
# project a `scope.shared_vpc_hosts` or `scope.metrics_scopes` entry resolves
# to, is bound before the CR that declares it is written (the composition
# orders the release after this module; ordering, not IAM propagation, which
# the reconcile's first tick may still run ahead of). `exclude` travels in the
# variable because the composition renders the CR from the same object, but it
# binds nothing and revokes nothing: an exclusion is applied by the reconcile
# after resolution, a glob cannot be evaluated here, and a project named in
# `projects` is bound even when an exclude entry removes it from the resolved
# set -- drop it from `projects` instead. The one exception is a project a
# selector resolved to, which has no list to be dropped from: an exclude entry
# that names it exactly, by ID or by the project number the Monitoring API
# returns, keeps it out of the plan-time set below, so the operator's only
# lever over a selector's members also withholds the grant.
#
# What a scoped project gets is `local.scope_roles`, never `var.project_roles`
# (design §6). The allowlist below is the read subset of the default project
# role list, intersected with what the caller granted the host project, so a
# `custom` list that carries roles/container.admin at home does not carry
# container.clusters.impersonate into every other project, and a
# quota-consuming role does not consume quota where the agent only reads.
# Widening what the scope carries is an edit to this list, on purpose.
#
# A container (folder or organisation) carries the same allowlist plus
# `roles/cloudasset.viewer`, because the reconcile resolves a container's
# members with one Cloud Asset Inventory search scoped to it (design §4), and
# a container-level binding is inherited by every project beneath, including
# one created tomorrow: that inheritance is what makes onboarding under a
# declared folder zero-touch, and it is also why an organisation is wide (§9).
#
# A Shared VPC host or a Metrics Scope is not a container: IAM cannot be
# granted on a VPC or on a Metrics Scope, so nothing is inherited through
# either and every project they reach needs its own binding (design §6). The
# bindings are Terraform's and Terraform cannot read the runtime snapshot, so
# this file resolves the two selectors itself, at plan time, with the same
# three reads the reconcile makes each tick (§10 step 3): the Compute API's
# getXpnResources for a host's service projects, the Monitoring API's
# metricsScopes.get for a scope's monitored projects, and Resource Manager to
# name each monitored project, which the Monitoring API returns by number.
# The reads are made with the google provider's own access token
# (data.google_client_config), so they are answered for the identity that
# applies and not for whatever gcloud's active account happens to be; the
# provider offers no data source for either listing, hence hashicorp/http. A
# read that fails fails the plan, before anything is applied, with the
# selector, the status and the API's message in the error; the postconditions
# below are the whole of that reporting, so a lookup this identity cannot make
# is a refused plan and never a silently smaller set, which would retire the
# members it missed on the reconcile's next two clean runs. What the selectors
# do not have is the zero-touch onboarding a container gets: a service project
# attached, or a project added to the scope, after the last apply reads
# `denied` in the snapshot until the next `upgrade.sh` binds it.

locals {
  # The read roles a scoped project may carry. tests/test_scope_iam.py holds
  # every entry to the module's default project_roles, so the allowlist cannot
  # name a role the agent does not hold at home.
  scope_role_allowlist = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/iam.securityReviewer",
  ]

  # The allowlist entries that carry both container.clusters.list and
  # container.clusters.get, which is what the reconcile needs: list to
  # discover, get for each cluster's credentials and liveness probe.
  # roles/iam.securityReviewer lists but cannot get, so a project bound with it
  # alone reads `ok` and then fails every profile create; the precondition on
  # google_service_account.agent (main.tf) refuses a plan whose intersection
  # carries neither of these two.
  scope_managing_roles = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
  ]

  scope_roles = [for role in local.scope_role_allowlist : role if contains(var.project_roles, role)]

  scope_can_manage = anytrue([for role in local.scope_roles : contains(local.scope_managing_roles, role)])

  # The host project is always in scope and already carries project_roles;
  # naming it in scope.projects is harmless and binds nothing twice.
  scope_projects = toset([for project in var.scope.projects : project if project != var.project_id])

  # Every project bound with scope_roles: the explicit ones and the ones the
  # two selectors resolved to (below), once each, so a project both an entry
  # and a selector name is one binding with one state address.
  scope_bound_projects = setunion(local.scope_projects, local.scope_selector_projects)

  scope_bindings = {
    for pair in setproduct(sort(tolist(local.scope_bound_projects)), local.scope_roles) :
    "${pair[0]}/${pair[1]}" => { project = pair[0], role = pair[1] }
  }

  # The one role a container carries beyond the allowlist: the reconcile's
  # `asset search-all-resources --scope=<container>` needs it on the container
  # it searches, and nowhere else. Not intersected with project_roles, because
  # the host project never holds it (the host is listed with `clusters list`).
  scope_container_asset_role = "roles/cloudasset.viewer"

  scope_container_roles = concat(local.scope_roles, [local.scope_container_asset_role])

  scope_folders       = toset(var.scope.folders)
  scope_organizations = toset(var.scope.organizations)

  scope_folder_bindings = {
    for pair in setproduct(sort(tolist(local.scope_folders)), local.scope_container_roles) :
    "${pair[0]}/${pair[1]}" => { folder = pair[0], role = pair[1] }
  }

  scope_organization_bindings = {
    for pair in setproduct(sort(tolist(local.scope_organizations)), local.scope_container_roles) :
    "${pair[0]}/${pair[1]}" => { organization = pair[0], role = pair[1] }
  }

  # What the manageability precondition in main.tf counts: any declaration
  # that binds outside the host project.
  scope_declares_anything = length(local.scope_projects) + length(local.scope_folders) + length(local.scope_organizations) + length(local.scope_shared_vpc_hosts) + length(local.scope_metrics_scopes) > 0
}

# ─── The two selectors, resolved at plan time ─────────────────────────────────

locals {
  scope_shared_vpc_hosts   = toset(var.scope.shared_vpc_hosts)
  scope_metrics_scopes     = toset(var.scope.metrics_scopes)
  scope_resolves_selectors = length(local.scope_shared_vpc_hosts) + length(local.scope_metrics_scopes) > 0

  # The three reads, as the reconcile makes them (agents/platform/scripts/
  # cluster_agent_reconcile.py): the Compute API names a host's service
  # projects by ID with a type; the Monitoring API names a scope's monitored
  # projects by project number, under locations/global/metricsScopes/<scope>/
  # projects/<number>; Resource Manager v3 names a project behind a number.
  scope_compute_api_url          = "https://compute.googleapis.com/compute/v1"
  scope_monitoring_api_url       = "https://monitoring.googleapis.com/v1"
  scope_resource_manager_api_url = "https://cloudresourcemanager.googleapis.com/v3"
  # getXpnResources is paged; one page of the API's maximum holds five times
  # the cap the CRD puts on any scope list, so a second page is refused rather
  # than followed, which HCL cannot do.
  scope_xpn_page_size             = 500
  scope_xpn_resource_type_project = "PROJECT"
  # What the Compute API answers, with HTTP 400, for a project that is not a
  # Shared VPC host. It has no service projects, which is a fact about the
  # estate and not a failed lookup; the reconcile reads it the same way, so a
  # misdeclared host neither fails the plan nor holds the scope prune.
  scope_not_xpn_host_marker            = "is not a shared VPC host project"
  scope_monitored_project_name_pattern = "^locations/global/metricsScopes/[^/]+/projects/(?P<project>[^/]+)$"
  scope_project_number_pattern         = "^[0-9]+$"
  # The CRD's project ID pattern: a monitored project whose ID does not match
  # it (a legacy domain-scoped `example.com:name`) cannot be declared, excluded
  # by ID or given a profile, so it is refused by number rather than bound.
  scope_project_id_pattern    = "^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
  scope_lookup_timeout_ms     = 20000
  scope_lookup_retry_attempts = 2
  # How much of an API's error body an error message carries.
  scope_lookup_error_excerpt_chars = 300

  # The role whose compute.projects.get the reconcile's host lookup needs in
  # the host project; the precondition in main.tf refuses a host without it.
  scope_shared_vpc_lookup_role = "roles/compute.viewer"
}

# The identity the google provider plans and applies with, so every read
# below is answered for it: a lookup that passes here passes for the apply,
# and one that fails names the principal an administrator has to grant.
data "google_client_config" "scope_resolver" {
  count = local.scope_resolves_selectors ? 1 : 0
}

locals {
  scope_resolver_headers = local.scope_resolves_selectors ? {
    Authorization = "Bearer ${data.google_client_config.scope_resolver[0].access_token}"
  } : {}

  scope_resolver_identity = "the identity the google provider plans with (its configured credentials, impersonation included)"
}

data "http" "scope_shared_vpc_host" {
  for_each = local.scope_shared_vpc_hosts

  url                = "${local.scope_compute_api_url}/projects/${each.key}/getXpnResources?maxResults=${local.scope_xpn_page_size}"
  request_headers    = local.scope_resolver_headers
  request_timeout_ms = local.scope_lookup_timeout_ms

  retry {
    attempts = local.scope_lookup_retry_attempts
  }

  lifecycle {
    postcondition {
      condition     = self.status_code == 200 || (self.status_code == 400 && strcontains(self.response_body, local.scope_not_xpn_host_marker))
      error_message = "scope.shared_vpc_hosts: the service projects of ${each.key} could not be listed by ${local.scope_resolver_identity}; the Compute API answered HTTP ${self.status_code}: ${substr(try(jsondecode(self.response_body).error.message, self.response_body), 0, local.scope_lookup_error_excerpt_chars)}. That identity needs compute.projects.get on the host project (roles/compute.viewer carries it), and the Compute API enabled there; or drop the host from the scope. Nothing was applied."
    }
    postcondition {
      condition     = self.status_code != 200 || can([for resource in try(jsondecode(self.response_body).resources, []) : "${resource.id}/${resource.type}"])
      error_message = "scope.shared_vpc_hosts: the Compute API's answer for ${each.key} is not the getXpnResources document this module reads (each resource carries an id and a type); refusing to resolve the host from it rather than bind a set that may be short. Nothing was applied."
    }
    postcondition {
      condition     = !can(jsondecode(self.response_body).nextPageToken)
      error_message = "scope.shared_vpc_hosts: ${each.key} has more than ${local.scope_xpn_page_size} attached service projects, more than one page of the Compute API's answer holds and far past the scope cap; declare the service projects wanted in scope.projects, or a folder that holds them, instead. Nothing was applied."
    }
  }
}

data "http" "scope_metrics_scope" {
  for_each = local.scope_metrics_scopes

  url                = "${local.scope_monitoring_api_url}/locations/global/metricsScopes/${each.key}"
  request_headers    = local.scope_resolver_headers
  request_timeout_ms = local.scope_lookup_timeout_ms

  retry {
    attempts = local.scope_lookup_retry_attempts
  }

  lifecycle {
    postcondition {
      condition     = self.status_code == 200
      error_message = "scope.metrics_scopes: the Metrics Scope of ${each.key} could not be read by ${local.scope_resolver_identity}; the Monitoring API answered HTTP ${self.status_code}: ${substr(try(jsondecode(self.response_body).error.message, self.response_body), 0, local.scope_lookup_error_excerpt_chars)}. That identity needs to read the scope in its scoping project (roles/monitoring.metricsScopesViewer is the narrowest role), and monitoring.googleapis.com enabled there; or drop the scope from scope.metrics_scopes. Nothing was applied."
    }
    postcondition {
      condition     = self.status_code != 200 || can([for row in try(jsondecode(self.response_body).monitoredProjects, []) : regex(local.scope_monitored_project_name_pattern, row.name)])
      error_message = "scope.metrics_scopes: the Monitoring API's answer for ${each.key} is not the metricsScopes.get document this module reads (each monitored project named locations/global/metricsScopes/<scope>/projects/<number>); refusing to resolve the scope from it rather than bind a set that may be short. Nothing was applied."
    }
  }
}

locals {
  scope_shared_vpc_members = {
    for host, response in data.http.scope_shared_vpc_host :
    host => response.status_code == 200 ? sort(distinct(compact([
      for resource in try(jsondecode(response.response_body).resources, []) :
      try(resource.type, "") == local.scope_xpn_resource_type_project ? try(resource.id, "") : ""
    ]))) : []
  }

  # A scope's monitored projects as the API named them: by number, or, should
  # the API ever name one by ID, as it came.
  scope_monitored_projects = {
    for scope, response in data.http.scope_metrics_scope :
    scope => distinct(compact([
      for row in try(jsondecode(response.response_body).monitoredProjects, []) :
      try(regex(local.scope_monitored_project_name_pattern, row.name)["project"], "")
    ]))
  }

  # The numbers to name: every monitored project the Monitoring API returned by
  # number, less the ones an exclude entry names by that number (the runtime's
  # own escape for a project the account cannot name, design §10 step 3), so
  # an excluded number is neither read nor bound.
  scope_monitored_numbers = toset([
    for member in flatten(values(local.scope_monitored_projects)) : member
    if can(regex(local.scope_project_number_pattern, member)) && !contains(var.scope.exclude.projects, member)
  ])
}

data "http" "scope_monitored_project" {
  for_each = local.scope_monitored_numbers

  url                = "${local.scope_resource_manager_api_url}/projects/${each.key}"
  request_headers    = local.scope_resolver_headers
  request_timeout_ms = local.scope_lookup_timeout_ms

  retry {
    attempts = local.scope_lookup_retry_attempts
  }

  lifecycle {
    postcondition {
      condition     = self.status_code == 200
      error_message = "scope.metrics_scopes: monitored project ${each.key} could not be named by ${local.scope_resolver_identity}; Resource Manager answered HTTP ${self.status_code}: ${substr(try(jsondecode(self.response_body).error.message, self.response_body), 0, local.scope_lookup_error_excerpt_chars)}. A project this identity cannot name it cannot bind, and the agent would read it denied. Ask for resourcemanager.projects.get on projects/${each.key} for that identity, or name the number in scope.exclude.projects to leave it out. Nothing was applied."
    }
    postcondition {
      condition     = self.status_code != 200 || can(regex(local.scope_project_id_pattern, jsondecode(self.response_body).projectId))
      error_message = "scope.metrics_scopes: monitored project ${each.key} is named ${try(jsondecode(self.response_body).projectId, "<unreadable>")}, a project ID the scope cannot carry (the CRD accepts ${local.scope_project_id_pattern}; a legacy domain-scoped ID does not match); the reconcile reports it denied by number. Name the number in scope.exclude.projects to leave it out. Nothing was applied."
    }
  }
}

locals {
  scope_project_id_by_number = {
    for number, response in data.http.scope_monitored_project :
    number => try(jsondecode(response.response_body).projectId, "")
  }

  scope_metrics_scope_members = {
    for scope, members in local.scope_monitored_projects :
    scope => sort(distinct(compact([
      for member in members :
      can(regex(local.scope_project_number_pattern, member)) ? lookup(local.scope_project_id_by_number, member, "") : member
    ])))
  }

  # Each selector's members under the name the snapshot's `containers` array
  # gives it, so the output is comparable with fleet_scope.json line by line.
  scope_selector_members = merge(
    { for host, members in local.scope_shared_vpc_members : "sharedVpcHosts/${host}" => members },
    { for scope, members in local.scope_metrics_scope_members : "metricsScopes/${scope}" => members },
  )

  # What the selectors add to the bound set: their members, less an exact
  # exclude entry and the host project (which carries project_roles already),
  # plus each Shared VPC host itself, which the reconcile's lookup has to read
  # (compute.projects.get) whether or not its own clusters are wanted -- the
  # host is not among its service projects, and is named in scope.projects
  # when they are. A host is bound even when an exclude entry names it: the
  # exclusion drops its clusters from the set, not the lookup that finds its
  # service projects.
  scope_selector_projects = toset(concat(
    [
      for project in flatten(values(local.scope_selector_members)) : project
      if project != var.project_id && !contains(var.scope.exclude.projects, project)
    ],
    [for host in local.scope_shared_vpc_hosts : host if host != var.project_id],
  ))
}

resource "google_project_iam_member" "scope_roles" {
  #checkov:skip=CKV_GCP_41:The scope binds read roles only, filtered through local.scope_role_allowlist
  #checkov:skip=CKV_GCP_42:Service account is granted non-admin project roles
  #checkov:skip=CKV_GCP_46:Dedicated custom service account used for agent workload identity
  #checkov:skip=CKV_GCP_49:The scope binds read roles only, filtered through local.scope_role_allowlist
  #checkov:skip=CKV_GCP_117:Standard GCP viewer roles granted for read-only cluster discovery in scoped projects
  for_each = local.scope_bindings

  project = each.value.project
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_folder_iam_member" "scope_roles" {
  #checkov:skip=CKV_GCP_41:The scope binds read roles only, filtered through local.scope_role_allowlist, plus roles/cloudasset.viewer
  #checkov:skip=CKV_GCP_42:Service account is granted non-admin folder roles
  #checkov:skip=CKV_GCP_46:Dedicated custom service account used for agent workload identity
  #checkov:skip=CKV_GCP_49:The scope binds read roles only, filtered through local.scope_role_allowlist, plus roles/cloudasset.viewer
  #checkov:skip=CKV_GCP_117:Standard GCP viewer roles granted for read-only cluster discovery beneath a declared folder
  for_each = local.scope_folder_bindings

  folder = "folders/${each.value.folder}"
  role   = each.value.role
  member = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_organization_iam_member" "scope_roles" {
  #checkov:skip=CKV_GCP_41:The scope binds read roles only, filtered through local.scope_role_allowlist, plus roles/cloudasset.viewer
  #checkov:skip=CKV_GCP_42:Service account is granted non-admin organisation roles
  #checkov:skip=CKV_GCP_46:Dedicated custom service account used for agent workload identity
  #checkov:skip=CKV_GCP_49:The scope binds read roles only, filtered through local.scope_role_allowlist, plus roles/cloudasset.viewer
  #checkov:skip=CKV_GCP_117:Standard GCP viewer roles granted for read-only cluster discovery across a declared organisation
  for_each = local.scope_organization_bindings

  org_id = each.value.organization
  role   = each.value.role
  member = "serviceAccount:${google_service_account.agent.email}"
}
