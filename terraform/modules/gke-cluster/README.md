# GKE Autopilot Cluster Module

Reusable Terraform module for provisioning a GKE Autopilot cluster configured for Kube-Agents workloads. Autopilot clusters are regional: `location` must be a region (a zone is rejected at plan time).

By default (`enable_database_encryption = true`), the module provisions a Cloud KMS Keyring and CryptoKey, binds `roles/cloudkms.cryptoKeyEncrypterDecrypter` to the GKE Service Agent, and enables etcd database encryption (CMEK).

The cluster's DNS-based control plane endpoint is published and allowed to serve traffic from outside the VPC (`control_plane_endpoints_config.dns_endpoint_config.allow_external_traffic`). The Platform Agent reaches fleet clusters from wherever it happens to run, and `allow_external_traffic` is the field its endpoint detection reads before it passes `get-credentials --dns-endpoint`; see [`k8s-operator/scripts/gke_dns_endpoint.sh`](../../../k8s-operator/scripts/gke_dns_endpoint.sh). This is why the module requires provider `>= 6.11` — the block does not exist in 5.x. Set `allow_external_dns_traffic = false` if a cluster should only be reachable from inside the VPC; the detection then falls back to the IP endpoint on its own. Change it here rather than with `gcloud container clusters update --no-enable-dns-access`: the module manages the field, so an out-of-band change is drift that the next apply reverts, and this endpoint is governed by IAM alone — no private-endpoint or master-authorized-networks setting is holding it shut in the meantime.

> **KMS resources cannot be deleted.** Cloud KMS key rings and keys are never actually
> destroyed — `terraform destroy` only removes them from state, and a subsequent apply
> with the same names fails with a 409 (the provisioning scripts sidestep this by
> check-then-create). Recover by importing the existing resources back into state
> (`terraform import module.<name>.google_kms_key_ring.gke_keyring ...`) or by choosing new
> `kms_keyring_name`/`kms_key_name` values.

## Usage

```hcl
module "gke_cluster" {
  source       = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=vX.Y.Z"
  project_id   = "my-gcp-project"
  cluster_name = "production-host-01"
  location     = "us-central1"
}
```

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
