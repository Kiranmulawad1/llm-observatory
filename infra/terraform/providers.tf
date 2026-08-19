provider "google" {
  project = var.project_id
  region  = var.region
}

# GKE Autopilot, Workload Identity Federation and a few Secret Manager
# arguments only exist in the beta provider. Declared explicitly rather than
# left implicit so it is obvious which resources are on the beta surface.
provider "google-beta" {
  project = var.project_id
  region  = var.region
}
