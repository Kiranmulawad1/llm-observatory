output "cluster_name" {
  value       = module.gke.cluster_name
  description = "Feed to: gcloud container clusters get-credentials <name> --region <region>"
}

output "cluster_location" {
  value = module.gke.cluster_location
}

output "registry_url" {
  value       = module.registry.repository_url
  description = "Docker registry prefix. CI tags images as <registry_url>/api:<sha>."
}

output "redis_host" {
  value       = module.redis.host
  description = "Private address of the Memorystore instance, reachable only from inside the VPC."
}

output "deployer_service_account" {
  value       = module.iam.deployer_service_account
  description = "Set as GCP_SERVICE_ACCOUNT in the GitHub Actions deploy workflow."
}

output "workload_identity_provider" {
  value       = module.iam.workload_identity_provider
  description = "Set as GCP_WORKLOAD_IDENTITY_PROVIDER in the GitHub Actions deploy workflow."
}

# Deliberately not an output: any credential.
#
# `terraform output` values live in state in plaintext, and `sensitive = true`
# only redacts the CLI display. Secret values are written to and read from
# Secret Manager out of band; Terraform never sees one.
