variable "project_id" { type = string }
variable "region" { type = string }
variable "labels" { type = map(string) }

# Secret *containers*, never secret values.
#
# This is the whole point. A `google_secret_manager_secret_version` with a
# `secret_data` argument puts the plaintext into Terraform state — and state
# is a JSON file in a bucket that the entire infra team, and CI, can read.
# Marking the variable `sensitive` hides it from plan output and changes
# nothing about what is written to state.
#
# So Terraform creates the empty container and the IAM binding, and the value
# is written out of band exactly once:
#
#   openssl rand -base64 48 | gcloud secrets versions add lo-api-key-pepper \
#       --project=<project> --data-file=-
#
# Rotation is `versions add` plus a rollout; the External Secrets Operator
# re-reads on its refresh interval. Terraform is never in that loop, which
# means rotating a credential does not require a plan and apply.
locals {
  secrets = {
    lo-database-url      = "PostgreSQL DSN, including the password."
    lo-redis-url         = "Redis DSN including the Memorystore AUTH string."
    lo-api-key-pepper    = "Server-side pepper for hashing project API keys. Rotating this invalidates every issued key."
    lo-admin-token       = "Platform operator token. Creates projects and issues project keys."
    lo-postgres-password = "Superuser password for the in-cluster TimescaleDB StatefulSet."
    lo-anthropic-api-key = "Anthropic API key used by the judge and generation providers."
    lo-openai-api-key    = "OpenAI API key used by the embedding provider."
  }
}

resource "google_secret_manager_secret" "this" {
  for_each = local.secrets

  project   = var.project_id
  secret_id = each.key

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  labels = merge(var.labels, {
    # Labels are visible to anyone with list access on the project, so this is
    # a description of the slot, never a hint about the value.
    purpose = "application-credential"
  })

  lifecycle {
    # Versions are added out of band. Without this, a `terraform apply` that
    # sees no version in configuration is one refactor away from proposing to
    # delete the live credential.
    ignore_changes = [labels]
  }
}

output "secret_ids" {
  value = { for k, v in google_secret_manager_secret.this : k => v.id }
}
