# Resolves the two `spec.scope` selectors that are not Resource Manager
# containers, a Shared VPC host and a Cloud Monitoring Metrics Scope, to the
# projects they reach, at plan time.
#
# Nothing is inherited through either (docs/designs/multi-project-scope.md
# §6), so every project they reach needs its own binding; the bindings are
# Terraform's (the kube-agents-iam module's `scope_selector_members` input)
# and Terraform cannot read the runtime snapshot, so this module makes the
# same three reads the reconcile makes each tick (§10 step 3): the Compute
# API's getXpnResources for a host's service projects, the Monitoring API's
# metricsScopes.get for a scope's monitored projects, and Resource Manager to
# name each monitored project, which the Monitoring API returns by number.
# The reads are made with the google provider's own access token
# (data.google_client_config), so they are answered for the identity that
# applies and not for whatever gcloud's active account happens to be; the
# provider offers no data source for either listing, hence hashicorp/http.
#
# A separate module, and not part of kube-agents-iam, because the composition
# calls that module with a module-level depends_on (the Workload Identity
# pool has to exist before its binding), and a module-level depends_on defers
# every data source inside the module to apply time whenever a target has a
# planned change -- a first install, or an upgrade that enables an API -- at
# which point a for_each keyed on the read fails the plan as unknown. This
# module is called with no depends_on and no input a managed resource
# produces, so its reads always happen at plan time.
#
# A read that fails fails the plan, before anything is applied, with the
# selector, the status and the API's message in the error; the postconditions
# below are the whole of that reporting, so a lookup this identity cannot make
# is a refused plan and never a silently smaller set, which would retire the
# members it missed on the reconcile's next two clean runs.


locals {
  scope_shared_vpc_hosts   = toset(var.shared_vpc_hosts)
  scope_metrics_scopes     = toset(var.metrics_scopes)
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
      error_message = "shared_vpc_hosts: the service projects of ${each.key} could not be listed by ${local.scope_resolver_identity}; the Compute API answered HTTP ${self.status_code}: ${substr(try(jsondecode(self.response_body).error.message, self.response_body), 0, local.scope_lookup_error_excerpt_chars)}. That identity needs compute.projects.get on the host project (roles/compute.viewer carries it), and the Compute API enabled there; or drop the host from shared_vpc_hosts. Nothing was applied."
    }
    postcondition {
      # A 200 whose body does not decode, or whose resources lack an id or a
      # type, is refused rather than read as a host with no service projects,
      # which the next apply would turn into revoked bindings. An absent
      # `resources` key stays legal: a host with nothing attached answers so.
      condition     = self.status_code != 200 || (can(jsondecode(self.response_body)) && can([for resource in try(jsondecode(self.response_body).resources, []) : "${resource.id}/${resource.type}"]))
      error_message = "shared_vpc_hosts: the Compute API's answer for ${each.key} is not the getXpnResources document this module reads (a JSON object whose resources each carry an id and a type); refusing to resolve the host from it rather than bind a set that may be short. Nothing was applied."
    }
    postcondition {
      condition     = !can(jsondecode(self.response_body).nextPageToken)
      error_message = "shared_vpc_hosts: ${each.key} has more than ${local.scope_xpn_page_size} attached service projects, more than one page of the Compute API's answer holds and far past the scope cap; declare the service projects wanted in the scope's projects, or a folder that holds them, instead. Nothing was applied."
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
      error_message = "metrics_scopes: the Metrics Scope of ${each.key} could not be read by ${local.scope_resolver_identity}; the Monitoring API answered HTTP ${self.status_code}: ${substr(try(jsondecode(self.response_body).error.message, self.response_body), 0, local.scope_lookup_error_excerpt_chars)}. That identity needs to read the scope in its scoping project (roles/monitoring.metricsScopesViewer is the narrowest role), and monitoring.googleapis.com enabled there; or drop it from metrics_scopes. Nothing was applied."
    }
    postcondition {
      # A scope always monitors its own scoping project, so a 200 that
      # decodes to no monitored project is not a document this module reads
      # either, and is refused rather than resolved to nothing.
      condition     = self.status_code != 200 || (can(jsondecode(self.response_body)) && length(try(jsondecode(self.response_body).monitoredProjects, [])) > 0 && can([for row in try(jsondecode(self.response_body).monitoredProjects, []) : regex(local.scope_monitored_project_name_pattern, row.name)]))
      error_message = "metrics_scopes: the Monitoring API's answer for ${each.key} is not the metricsScopes.get document this module reads (a JSON object with at least one monitored project, each named locations/global/metricsScopes/<scope>/projects/<number>); refusing to resolve the scope from it rather than bind a set that may be short. Nothing was applied."
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
    if can(regex(local.scope_project_number_pattern, member)) && !contains(var.exclude_projects, member)
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
      error_message = "metrics_scopes: monitored project ${each.key} could not be named by ${local.scope_resolver_identity}; Resource Manager answered HTTP ${self.status_code}: ${substr(try(jsondecode(self.response_body).error.message, self.response_body), 0, local.scope_lookup_error_excerpt_chars)}. A project this identity cannot name it cannot bind, and the agent would read it denied. Ask for resourcemanager.projects.get on projects/${each.key} for that identity, or name the number in exclude_projects (the scope's exclude.projects) to leave it out. Nothing was applied."
    }
    postcondition {
      condition     = self.status_code != 200 || can(regex(local.scope_project_id_pattern, jsondecode(self.response_body).projectId))
      error_message = "metrics_scopes: monitored project ${each.key} is named ${try(jsondecode(self.response_body).projectId, "<unreadable>")}, a project ID the scope cannot carry (the CRD accepts ${local.scope_project_id_pattern}; a legacy domain-scoped ID does not match); the reconcile reports it denied by number. Name the number in exclude_projects (the scope's exclude.projects) to leave it out. Nothing was applied."
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
}
