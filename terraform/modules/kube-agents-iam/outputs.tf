output "service_account_email" {
  description = "Email of the created IAM service account"
  value       = google_service_account.agent.email
}

output "agent_project_roles" {
  description = <<-EOT
    Project-level roles actually granted to the agent's own service account.
    Surfaced because the residual ceiling is a security property worth being
    able to assert on rather than infer from which variables were set. It does
    not yet vary with scoped_clusters -- see the suspended coupling in main.tf.
  EOT
  value       = local.agent_project_roles
}

output "scoped_service_accounts" {
  description = <<-EOT
    Map from GKE resource name to the email of the service account for it. The
    key is the same string the credential broker looks up, so this output is
    directly comparable with the broker's mapping. The accounts hold no IAM
    grant; see scoped_pool.tf.
  EOT
  value       = { for key in keys(local.scoped_pool) : key => google_service_account.scoped[key].email }
}

output "scope_projects" {
  description = <<-EOT
    The projects beyond project_id that scope.projects named, each bound with
    scope_roles. The host project is omitted even when scope.projects names it,
    because it carries project_roles already.
  EOT
  value       = sort(tolist(local.scope_projects))
}

output "scope_roles" {
  description = <<-EOT
    The roles bound in every scope project: the module's read allowlist
    (local.scope_role_allowlist in scope.tf) intersected with project_roles.
    Surfaced so the ceiling a scoped project grants can be asserted on rather
    than inferred from the allowlist and the role list separately.
  EOT
  value       = local.scope_roles
}

output "scope_folders" {
  description = <<-EOT
    The folders scope.folders named, each bound on the folder itself with
    scope_container_roles, so every project beneath inherits the grant.
  EOT
  value       = sort(tolist(local.scope_folders))
}

output "scope_organizations" {
  description = <<-EOT
    The organisations scope.organizations named, each bound on the
    organisation itself with scope_container_roles.
  EOT
  value       = sort(tolist(local.scope_organizations))
}

output "scope_container_roles" {
  description = <<-EOT
    The roles bound on every folder and organisation in scope: scope_roles
    plus roles/cloudasset.viewer, which the reconcile's container search needs
    on the container it searches.
  EOT
  value       = local.scope_container_roles
}

output "scope_shared_vpc_hosts" {
  description = <<-EOT
    The Shared VPC host projects scope.shared_vpc_hosts named. Each is bound
    with scope_roles itself, so the reconcile's lookup can read it, beside
    the service projects scope_selector_members resolved it to.
  EOT
  value       = sort(tolist(local.scope_shared_vpc_hosts))
}

output "scope_metrics_scopes" {
  description = <<-EOT
    The Metrics Scope scoping projects scope.metrics_scopes named. Each is
    bound with scope_roles itself, so the reconcile's lookup can read the
    scope there, beside the monitored projects scope_selector_members
    resolved it to.
  EOT
  value       = sort(tolist(local.scope_metrics_scopes))
}

output "scope_bound_projects" {
  description = <<-EOT
    Every project beyond project_id bound with scope_roles: the explicit
    scope.projects, the selectors' members less an exact exclude entry, and
    each Shared VPC host and Metrics Scope scoping project, once each.
  EOT
  value       = sort(tolist(local.scope_bound_projects))
}
