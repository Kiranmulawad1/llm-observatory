variable "project_id" { type = string }
variable "cluster_name" { type = string }
variable "namespace" { type = string }
variable "secret_ids" { type = map(string) }
variable "artifact_registry_id" { type = string }

# Workload Identity: pods authenticate as themselves, with no key file.
#
# The chain is: Kubernetes ServiceAccount -> (binding) -> Google service
# account -> (IAM) -> the specific resource. Every link is least-privilege and
# every link is auditable. What does *not* exist anywhere in it is a
# long-lived JSON credential that can be copied out of a pod, a laptop or a
# CI log.

# The External Secrets Operator's identity. It is the only workload that reads
# Secret Manager — the application pods read a Kubernetes Secret and never talk
# to GCP at all, which keeps the blast radius of an application compromise
# inside the cluster.
resource "google_service_account" "external_secrets" {
  project      = var.project_id
  account_id   = "lo-external-secrets"
  display_name = "llm-observatory External Secrets Operator"
  description  = "Reads application credentials from Secret Manager into the cluster."
}

# Granted per secret, not project-wide.
#
# roles/secretmanager.secretAccessor at the project level would let this
# identity read every secret in the project, including ones belonging to
# unrelated applications. Binding on each secret resource means adding a new
# secret is a deliberate act.
resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each = var.secret_ids

  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.external_secrets.email}"
}

resource "google_service_account_iam_member" "external_secrets_wi" {
  service_account_id = google_service_account.external_secrets.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/lo-secrets]"
}

# --- Application identities -------------------------------------------------
#
# One per workload. They currently hold nothing beyond the ability to write
# telemetry, and that is the point: identity is the thing you cannot retrofit
# after an incident, and a shared account makes the audit log useless.

resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = "lo-api"
  display_name = "llm-observatory API"
}

resource "google_service_account" "worker" {
  project      = var.project_id
  account_id   = "lo-worker"
  display_name = "llm-observatory worker"
}

resource "google_service_account_iam_member" "api_wi" {
  service_account_id = google_service_account.api.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/lo-api]"
}

resource "google_service_account_iam_member" "worker_wi" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/lo-worker]"
}

resource "google_project_iam_member" "app_telemetry" {
  for_each = {
    api    = google_service_account.api.email
    worker = google_service_account.worker.email
  }

  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${each.value}"
}

# --- CI identity ------------------------------------------------------------
#
# GitHub Actions authenticates by Workload Identity Federation: GitHub mints an
# OIDC token, GCP exchanges it for a short-lived access token. No service
# account key is ever created, so there is no secret in the repository to leak
# and nothing to rotate.

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github-actions"
  display_name              = "GitHub Actions"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Without a condition, *any* GitHub repository in the world can exchange a
  # token for credentials in this project. This is the single most commonly
  # misconfigured line in all of GCP.
  attribute_condition = "assertion.repository == '${var.github_repository}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

variable "github_repository" {
  type        = string
  description = "owner/repo allowed to federate into this project. Scoped narrowly on purpose."
  default     = "kiranmulawad/llm-observatory"
}

resource "google_service_account" "deployer" {
  project      = var.project_id
  account_id   = "lo-deployer"
  display_name = "llm-observatory CI deployer"
}

resource "google_service_account_iam_member" "deployer_federation" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  # Narrowed to the repository, not the whole pool.
  member = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

resource "google_artifact_registry_repository_iam_member" "deployer_push" {
  project    = var.project_id
  location   = split("/", var.artifact_registry_id)[3]
  repository = split("/", var.artifact_registry_id)[5]
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}

# roles/container.developer, not roles/container.admin. CI deploys workloads;
# it does not get to delete the cluster.
resource "google_project_iam_member" "deployer_gke" {
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

output "deployer_service_account" { value = google_service_account.deployer.email }
output "workload_identity_provider" { value = google_iam_workload_identity_pool_provider.github.name }
