# A dedicated VPC, not the default one.
#
# The default VPC ships with auto-created subnets in every region and a
# permissive firewall (allow-internal across 10.128.0.0/9). Building on it
# means inheriting a security posture chosen by Google for convenience.

variable "project_id" { type = string }
variable "region" { type = string }
variable "name_prefix" { type = string }
variable "labels" { type = map(string) }

resource "google_compute_network" "vpc" {
  project                 = var.project_id
  name                    = "${var.name_prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "subnet" {
  project       = var.project_id
  name          = "${var.name_prefix}-subnet"
  region        = var.region
  network       = google_compute_network.vpc.id
  ip_cidr_range = "10.10.0.0/20"

  # Nodes get addresses from the primary range; pods and services get their own
  # secondary ranges. This is VPC-native (alias IP) networking: pod IPs are
  # real routable VPC addresses, so a Google load balancer can target a pod
  # directly instead of hopping through kube-proxy on a node.
  #
  # Sized generously and deliberately: the pod range cannot be resized after
  # the cluster is created, and running out means building a new cluster.
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.20.0.0/14"
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.24.0.0/20"
  }

  # Without this, VPC flow logs do not exist, and neither does any way to
  # answer "what did that pod talk to" after an incident.
  log_config {
    aggregation_interval = "INTERVAL_10_MIN"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }

  private_ip_google_access = true
}

# Nodes have no external IPs, so egress to LLM provider APIs goes through NAT.
# The side benefit is a stable set of source addresses, which is what a
# provider needs if you ever want them to allowlist you.
resource "google_compute_router" "router" {
  project = var.project_id
  name    = "${var.name_prefix}-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "nat" {
  project = var.project_id
  name    = "${var.name_prefix}-nat"
  region  = var.region
  router  = google_compute_router.router.name

  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# Memorystore is created inside Google's own producer network and reached over
# a peering. This reserves the address block that peering uses.
resource "google_compute_global_address" "private_service_range" {
  project       = var.project_id
  name          = "${var.name_prefix}-psa"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "psa" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_service_range.name]
}

# There is no allow-all-internal rule here, on purpose. The only traffic that
# needs to cross the VPC is pod-to-pod, which the cluster CNI handles inside
# the pod range, and NetworkPolicy governs that (infra/k8s/base/networkpolicy.yaml).

output "network_id" { value = google_compute_network.vpc.id }
output "subnet_id" { value = google_compute_subnetwork.subnet.id }
output "pods_range_name" { value = "pods" }
output "services_range_name" { value = "services" }
