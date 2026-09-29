variable "shared_vpc_hosts" {
  description = <<-EOT
    Shared VPC host project IDs (`spec.scope.sharedVpcHosts`). Each is
    resolved to the service projects attached to it, by ID, through the
    Compute API's getXpnResources; a project that is not a Shared VPC host
    resolves to no members, as it does at runtime. The host itself is not a
    member.
  EOT
  type        = list(string)
  nullable    = false
  default     = []

  validation {
    condition     = alltrue([for entry in var.shared_vpc_hosts : can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", entry))])
    error_message = "Each shared_vpc_hosts entry is a GCP project ID (^[a-z][a-z0-9-]{4,28}[a-z0-9]$), the pattern the CRD accepts for the same field."
  }
}

variable "metrics_scopes" {
  description = <<-EOT
    Cloud Monitoring Metrics Scope scoping-project IDs
    (`spec.scope.metricsScopes`). Each is resolved to the projects it
    monitors, the scoping project included, through the Monitoring API's
    metricsScopes.get, which names them by project number; each number is
    then named through Resource Manager.
  EOT
  type        = list(string)
  nullable    = false
  default     = []

  validation {
    condition     = alltrue([for entry in var.metrics_scopes : can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", entry))])
    error_message = "Each metrics_scopes entry is a GCP project ID (^[a-z][a-z0-9-]{4,28}[a-z0-9]$), the pattern the CRD accepts for the same field."
  }
}

variable "exclude_projects" {
  description = <<-EOT
    The scope's `exclude.projects` entries. Only an entry that is a bare
    project number acts here: a monitored project the Monitoring API returned
    under that number is neither named nor listed, the runtime's own escape
    for a project the account cannot name. IDs and globs are the caller's to
    apply (kube-agents-iam withholds the grant of a member an entry names by
    ID; the reconcile evaluates globs).
  EOT
  type        = list(string)
  nullable    = false
  default     = []
}
