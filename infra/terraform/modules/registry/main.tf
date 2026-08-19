variable "project_id" { type = string }
variable "region" { type = string }
variable "repository_id" { type = string }
variable "labels" { type = map(string) }

resource "google_artifact_registry_repository" "this" {
  project       = var.project_id
  location      = var.region
  repository_id = var.repository_id
  format        = "DOCKER"
  description   = "Container images for llm-observatory."

  docker_config {
    # Tags cannot be moved once pushed.
    #
    # This is the control that makes "deploy by digest" meaningful in the other
    # direction: it also stops someone re-pushing :v1.4.2 with different bytes
    # after the review that approved it. Rollback becomes "point at the old
    # tag" rather than "hope the old tag still means what it meant".
    immutable_tags = true
  }

  # Untagged images pile up on every rebuild and are pure storage cost.
  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }

  # Keep the last 20 releases regardless of age. `keep_count` is evaluated
  # before delete rules, so this wins over any age-based policy added later —
  # which is exactly what you want the night you need to roll back six months.
  cleanup_policies {
    id     = "keep-recent-releases"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }

  labels = var.labels
}

output "repository_id" { value = google_artifact_registry_repository.this.id }
output "repository_url" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.this.repository_id}"
}
