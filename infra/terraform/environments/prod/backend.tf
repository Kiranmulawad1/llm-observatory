# Remote state in GCS.
#
# Local state means the file lives on one laptop, is not locked, and is not
# backed up — two people applying at once corrupts it, and losing it means
# Terraform no longer knows what it created. GCS gives durable storage and
# object-level locking, so a concurrent apply blocks instead of racing.
#
# The bucket has to exist before `terraform init`, which is the standard
# chicken-and-egg. Create it once by hand, with versioning on so a corrupted
# state file can be rolled back:
#
#   gcloud storage buckets create gs://<project>-tfstate \
#       --location=europe-west3 --uniform-bucket-level-access
#   gcloud storage buckets update gs://<project>-tfstate --versioning
#
# State contains every attribute of every resource, including ones Terraform
# marks sensitive. Treat this bucket as a credential store: no public access,
# audit logging on, and access limited to the people who can already apply.
terraform {
  backend "gcs" {
    bucket = "REPLACE-ME-tfstate"
    prefix = "llm-observatory/prod"
  }
}
