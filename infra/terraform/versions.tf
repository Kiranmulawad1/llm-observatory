terraform {
  # Pinned minor, floating patch. An unpinned Terraform version means a
  # provider upgrade lands in a plan nobody asked for, on the day of an
  # unrelated deploy.
  required_version = "~> 1.15"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.20"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.20"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
