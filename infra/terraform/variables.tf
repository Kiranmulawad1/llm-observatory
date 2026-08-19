variable "project_id" {
  type        = string
  description = "GCP project id. Nothing here creates a project — that is an org-level action with billing attached, and Terraform should not be the thing that spends money by surprise."
}

variable "region" {
  type        = string
  description = "Primary region for the cluster, Redis and Artifact Registry."
  default     = "europe-west3"
}

variable "environment" {
  type        = string
  description = "Environment name, used in resource names and labels."
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "cluster_name" {
  type    = string
  default = "llm-observatory"
}

variable "gke_release_channel" {
  type        = string
  description = "GKE upgrade channel. REGULAR trades a few weeks of latency for versions that have been run by other people first."
  default     = "REGULAR"

  validation {
    condition     = contains(["RAPID", "REGULAR", "STABLE"], var.gke_release_channel)
    error_message = "gke_release_channel must be RAPID, REGULAR or STABLE."
  }
}

variable "redis_memory_gb" {
  type        = number
  description = "Memorystore capacity. The queue holds job payloads, not results, so this is sized for burst depth rather than data volume."
  default     = 1
}

variable "authorized_networks" {
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  description = "CIDRs allowed to reach the GKE control plane. Empty means the plane is reachable only from inside the VPC, which is the correct default."
  default     = []
}

variable "labels" {
  type        = map(string)
  description = "Applied to every resource that supports labels. This is what makes a billing export answer 'what does this project cost'."
  default = {
    application = "llm-observatory"
    managed-by  = "terraform"
  }
}
