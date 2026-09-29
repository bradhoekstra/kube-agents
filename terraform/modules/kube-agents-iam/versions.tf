terraform {
  required_version = "~> 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30, < 8.0"
    }
    # The plan-time resolution of scope.shared_vpc_hosts and
    # scope.metrics_scopes (scope.tf): the google provider has no data source
    # that lists a host's service projects or a Metrics Scope's monitored
    # projects, so the two REST reads are made directly, with the provider's
    # own token.
    http = {
      source  = "hashicorp/http"
      version = ">= 3.4, < 4.0"
    }
  }
}
