output "members" {
  description = <<-EOT
    What each selector resolved to at plan time, by project ID, under the
    name the reconcile's snapshot gives the selector (sharedVpcHosts/<host>,
    metricsScopes/<scope>), so this output and the `containers` array of
    fleet_scope.json can be read side by side. A monitored project an
    exclude_projects entry names by number is neither named nor listed here;
    a member an entry names by ID is listed, and it is the kube-agents-iam
    module that withholds its grant.
  EOT
  value       = local.scope_selector_members
}

output "uncarriable_members" {
  description = <<-EOT
    Projects a selector named by an ID the scope cannot carry (a legacy
    domain-scoped ID): a Shared VPC host's service projects, and a monitored
    project should the Monitoring API ever name one by such an ID rather than
    by number; keyed by the selector's snapshot name. Left out of `members`,
    so bound nowhere, and reported by the module's check block as a warning on
    every plan they appear in.
  EOT
  value       = local.scope_selector_uncarriable
}

