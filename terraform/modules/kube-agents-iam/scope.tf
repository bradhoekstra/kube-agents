# Grants for the projects `spec.scope` declares beyond the host project.
#
# The scope is the set of GCP projects whose GKE clusters the Cluster Agent
# reconcile enumerates (docs/designs/multi-project-scope.md §3). The reconcile
# reads the declaration from the PlatformAgent CR; this file is the IAM half of
# the same value, so a project named in `scope.projects` is listable by the
# time the reconcile first tries it. `exclude` is carried in the variable
# because the composition renders the CR from the same object, but it binds
# nothing: an excluded project is a declaration the reconcile applies after
# resolution, and a glob cannot be evaluated here.
#
# What a scoped project gets is `local.scope_roles`, never `var.project_roles`.
# The allowlist below is the read subset of the default project role list,
# intersected with what the caller actually granted the host project, so a
# `custom` list that carries roles/container.admin for the host project does
# not carry container.clusters.impersonate into every other project, and a
# quota-consuming role does not consume quota where the agent only reads.
# Widening what the scope carries is an edit to this list, on purpose.

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

  scope_roles = [for role in local.scope_role_allowlist : role if contains(var.project_roles, role)]

  # The host project is always in scope and already carries project_roles;
  # naming it in scope.projects is harmless and binds nothing twice.
  scope_projects = toset([for project in var.scope.projects : project if project != var.project_id])

  scope_bindings = {
    for pair in setproduct(sort(tolist(local.scope_projects)), local.scope_roles) :
    "${pair[0]}/${pair[1]}" => { project = pair[0], role = pair[1] }
  }
}

resource "google_project_iam_member" "scope_roles" {
  #checkov:skip=CKV_GCP_41:The scope binds read roles only, filtered through local.scope_role_allowlist
  #checkov:skip=CKV_GCP_42:Service account is granted non-admin project roles
  #checkov:skip=CKV_GCP_49:The scope binds read roles only, filtered through local.scope_role_allowlist
  #checkov:skip=CKV_GCP_117:Standard GCP viewer roles granted for read-only cluster discovery in scoped projects
  for_each = local.scope_bindings

  project = each.value.project
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.agent.email}"
}
