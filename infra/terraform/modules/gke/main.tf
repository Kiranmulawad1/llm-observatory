variable "project_id" { type = string }
variable "region" { type = string }
variable "cluster_name" { type = string }
variable "release_channel" { type = string }
variable "network_id" { type = string }
variable "subnet_id" { type = string }
variable "pods_range_name" { type = string }
variable "services_range_name" { type = string }
variable "labels" { type = map(string) }
variable "authorized_networks" {
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
}

# GKE Autopilot rather than Standard.
#
# The trade: Autopilot bills per pod request instead of per node, refuses
# privileged workloads, and takes node upgrades and right-sizing away from you.
# What you give up is DaemonSets that need host access, custom kernel tuning,
# and cheap over-committed nodes.
#
# For this workload the trade is clearly right — nothing here needs host
# access, and "nobody has to remember to patch the nodes" removes the single
# most common way a small team's cluster becomes a liability. Autopilot also
# enables Workload Identity and Shielded Nodes by default, which is a lot of
# security posture you do not have to argue for in review.
#
# Where I would choose Standard instead: a workload needing GPUs with specific
# drivers, or a cost profile where bin-packing many small pods onto large
# spot nodes matters more than operational simplicity.
resource "google_container_cluster" "this" {
  provider = google-beta

  project  = var.project_id
  name     = var.cluster_name
  location = var.region

  enable_autopilot = true

  network    = var.network_id
  subnetwork = var.subnet_id

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_range_name
    services_secondary_range_name = var.services_range_name
  }

  # Nodes have no public IPs. The control plane keeps one, gated by the
  # authorized-networks list below, because a fully private endpoint needs a
  # bastion or a proxy to administer and that is a real operational cost.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "172.16.0.0/28"
  }

  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.authorized_networks
      content {
        cidr_block   = cidr_blocks.value.cidr_block
        display_name = cidr_blocks.value.display_name
      }
    }
  }

  release_channel {
    channel = var.release_channel
  }

  # Pods authenticate to Google APIs as a Google service account through a
  # projected, short-lived token. The alternative is a JSON key file mounted
  # into the pod — a credential with no expiry that leaks into logs, images and
  # laptops. Workload Identity is the single highest-value thing on this page.
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # Encrypt Secrets at rest with a key you control, so a compromised etcd
  # backup is not a compromised set of API keys.
  database_encryption {
    state    = "ENCRYPTED"
    key_name = google_kms_crypto_key.etcd.id
  }

  # Autopilot ships Managed Prometheus; naming it makes the dependency explicit
  # rather than a default someone later turns off without realising what breaks.
  monitoring_config {
    managed_prometheus {
      enabled = true
    }
  }

  # Refuse to be destroyed by an unreviewed `terraform destroy`. Removing this
  # line is a deliberate, visible act in a pull request.
  deletion_protection = true

  resource_labels = var.labels
}

resource "google_kms_key_ring" "this" {
  project  = var.project_id
  name     = "${var.cluster_name}-keyring"
  location = var.region
}

resource "google_kms_crypto_key" "etcd" {
  name     = "${var.cluster_name}-etcd"
  key_ring = google_kms_key_ring.this.id

  # 90 days. Rotation re-encrypts new writes only; existing data stays under
  # the old key version, which is why the old versions are never destroyed.
  rotation_period = "7776000s"

  lifecycle {
    prevent_destroy = true
  }
}

# GKE's own service agent must be able to use the key, or the cluster fails to
# create with a permissions error that does not mention KMS.
data "google_project" "this" {
  project_id = var.project_id
}

resource "google_kms_crypto_key_iam_member" "gke_etcd" {
  crypto_key_id = google_kms_crypto_key.etcd.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.this.number}@container-engine-robot.iam.gserviceaccount.com"
}

output "cluster_name" { value = google_container_cluster.this.name }
output "cluster_endpoint" {
  value     = google_container_cluster.this.endpoint
  sensitive = true
}
output "cluster_location" { value = google_container_cluster.this.location }
