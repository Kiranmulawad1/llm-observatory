# llm-observatory on GCP.
#
# ---------------------------------------------------------------------------
# THIS IS NEVER APPLIED IN THIS PROJECT.
#
# It is validated with `terraform init`, `validate` and `fmt` — which parse the
# configuration, resolve the provider schemas and type-check every resource
# argument against them — and stops there. There is no GCP account and no
# billing attached. `make tf-validate` runs exactly those three, and there is
# deliberately no `make tf-apply`.
#
# The Kubernetes half is not hypothetical in the same way: it runs for real on
# a local kind cluster (`make kind-up`), which exercises the same manifests
# this configuration is designed to host.
# ---------------------------------------------------------------------------
#
# Managed where managed works; self-hosted only where a hard constraint forces
# it. Redis is Memorystore. The database is *not* Cloud SQL, because Cloud SQL
# cannot load the timescaledb extension and telemetry.spans is a hypertable —
# see ADR 0011 for the alternatives and why this one won.

locals {
  labels = merge(var.labels, {
    environment = var.environment
  })

  name_prefix = "${var.cluster_name}-${var.environment}"
}

# Enable the APIs this configuration needs, explicitly.
#
# `disable_on_destroy = false` matters: destroying this stack should not turn
# off container.googleapis.com for the whole project, which would break
# anything else living there.
resource "google_project_service" "required" {
  for_each = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "redis.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "servicenetworking.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

module "network" {
  source = "./modules/network"

  project_id  = var.project_id
  region      = var.region
  name_prefix = local.name_prefix
  labels      = local.labels

  depends_on = [google_project_service.required]
}

module "registry" {
  source = "./modules/registry"

  project_id    = var.project_id
  region        = var.region
  repository_id = var.cluster_name
  labels        = local.labels

  depends_on = [google_project_service.required]
}

module "gke" {
  source = "./modules/gke"

  project_id          = var.project_id
  region              = var.region
  cluster_name        = var.cluster_name
  release_channel     = var.gke_release_channel
  network_id          = module.network.network_id
  subnet_id           = module.network.subnet_id
  pods_range_name     = module.network.pods_range_name
  services_range_name = module.network.services_range_name
  authorized_networks = var.authorized_networks
  labels              = local.labels

  depends_on = [google_project_service.required]
}

module "redis" {
  source = "./modules/redis"

  project_id = var.project_id
  region     = var.region
  name       = "${local.name_prefix}-redis"
  memory_gb  = var.redis_memory_gb
  network_id = module.network.network_id
  labels     = local.labels

  depends_on = [google_project_service.required]
}

module "secrets" {
  source = "./modules/secrets"

  project_id = var.project_id
  region     = var.region
  labels     = local.labels

  depends_on = [google_project_service.required]
}

module "iam" {
  source = "./modules/iam"

  project_id           = var.project_id
  cluster_name         = var.cluster_name
  namespace            = "llm-observatory"
  secret_ids           = module.secrets.secret_ids
  artifact_registry_id = module.registry.repository_id

  depends_on = [google_project_service.required]
}
