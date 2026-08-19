variable "project_id" { type = string }
variable "region" { type = string }
variable "name" { type = string }
variable "memory_gb" { type = number }
variable "network_id" { type = string }
variable "labels" { type = map(string) }

# Memorystore, not Redis-on-Kubernetes.
#
# Unlike the database, there is no extension forcing our hand here — managed
# Redis is a straight win: failover, patching and backups become someone
# else's pager. STANDARD_HA gives a replica in a second zone with automatic
# failover, which matters because the queue holds accepted-but-unfinished eval
# jobs. Losing it loses work the API already returned 202 for.
resource "google_redis_instance" "this" {
  project        = var.project_id
  name           = var.name
  region         = var.region
  tier           = "STANDARD_HA"
  memory_size_gb = var.memory_gb

  # Private services access, so the instance has no public address at all.
  connect_mode            = "PRIVATE_SERVICE_ACCESS"
  authorized_network      = var.network_id
  transit_encryption_mode = "SERVER_AUTHENTICATION"
  auth_enabled            = true

  redis_version = "REDIS_7_2"

  redis_configs = {
    # noeviction, emphatically not allkeys-lru.
    #
    # This instance is a work queue, not a cache. Under memory pressure an
    # eviction policy silently deletes queued jobs — the API already returned
    # 202, so the user believes their eval is running and it simply never
    # completes. Refusing writes surfaces the problem as an error someone can
    # act on. The rate limiter's keys are collateral, and they expire anyway.
    maxmemory-policy = "noeviction"
  }

  # Sunday 03:00 local. Pick the window explicitly; the alternative is Google
  # picking one, and it will not be a Sunday.
  maintenance_policy {
    weekly_maintenance_window {
      day = "SUNDAY"
      start_time {
        hours   = 3
        minutes = 0
      }
    }
  }

  labels = var.labels
}

output "host" { value = google_redis_instance.this.host }
output "port" { value = google_redis_instance.this.port }
output "auth_string" {
  value     = google_redis_instance.this.auth_string
  sensitive = true
}
