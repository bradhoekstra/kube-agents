# Design: direct remediation calls between agents

## Summary

Today a Diagnosis agent that has identified a fix writes a proposal to the GitOps repository and
waits for a human to approve it before anything is applied. For the narrow class of fixes that are
already on the runbook — restarting a crash-looping Deployment, scaling a node pool back within its
autoscaler bounds — that round trip is the slowest part of the incident.

This design lets the Diagnosis agent call the Remediation agent directly over gRPC and block until
the fix has been applied.

## Mechanism

- The Diagnosis agent opens a synchronous `Remediate` RPC to the Remediation agent, carrying the
  fix and its own identity token, and waits for the reply before closing the incident.
- The Remediation agent treats a valid `Remediate` call as authorisation to act: it applies the
  change with `kubectl apply` under its own service account, then returns the result.
- Nothing is written to the GitOps repository for these fixes. The RPC log on the Remediation agent
  is the record.

## Rollout

Behind a flag, `agents.direct_remediation`, off by default.
