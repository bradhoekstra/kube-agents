# GKE Service Agent identity for KMS access
resource "google_project_service_identity" "gke_service_agent" {
  count    = var.enable_database_encryption ? 1 : 0
  provider = google-beta
  project  = var.project_id
  service  = "container.googleapis.com"
}

# Cloud KMS Keyring and CryptoKey for GKE Database Encryption (etcd CMEK)
resource "google_kms_key_ring" "gke_keyring" {
  count    = var.enable_database_encryption ? 1 : 0
  name     = var.kms_keyring_name
  location = var.location
  project  = var.project_id
}

resource "google_kms_crypto_key" "gke_key" {
  count    = var.enable_database_encryption ? 1 : 0
  name     = var.kms_key_name
  key_ring = google_kms_key_ring.gke_keyring[0].id
  purpose  = "ENCRYPT_DECRYPT"
}

resource "google_kms_crypto_key_iam_member" "gke_kms_binding" {
  count         = var.enable_database_encryption ? 1 : 0
  crypto_key_id = google_kms_crypto_key.gke_key[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.gke_service_agent[0].email}"
}

resource "google_container_cluster" "autopilot" {
  name     = var.cluster_name
  location = var.location
  project  = var.project_id

  enable_autopilot    = true
  deletion_protection = var.deletion_protection

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  release_channel {
    channel = var.release_channel
  }

  # Publish the DNS-based control plane endpoint and let it serve traffic from
  # outside the VPC. The Platform Agent reaches fleet clusters from wherever it
  # runs, and a cluster with only an IP endpoint it cannot route to is
  # unreachable. allow_external_traffic is the field the agent's detection reads
  # before it passes `get-credentials --dns-endpoint`; see
  # k8s-operator/scripts/gke_dns_endpoint.sh.
  #
  # Kept as a variable rather than a literal true so that turning it off is a
  # change to the configuration. Set out-of-band it would be drift, and the next
  # apply would silently re-publish an endpoint the operator had closed -- the
  # DNS endpoint is governed by IAM alone, so neither the private endpoint nor
  # master-authorized-networks would have been holding it shut in the meantime.
  control_plane_endpoints_config {
    dns_endpoint_config {
      allow_external_traffic = var.allow_external_dns_traffic
    }
  }

  dynamic "database_encryption" {
    for_each = var.enable_database_encryption ? [1] : []
    content {
      state    = "ENCRYPTED"
      key_name = google_kms_crypto_key.gke_key[0].id
    }
  }

  depends_on = [
    google_kms_crypto_key_iam_member.gke_kms_binding
  ]
}
