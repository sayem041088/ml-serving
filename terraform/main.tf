locals {
  services = [
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "sts.googleapis.com",
  ]
}

resource "google_project_service" "services" {
  for_each = toset(local.services)

  service            = each.value
  disable_on_destroy = false
}

# -----------------------------------------------------------------------------
# Network: custom VPC, VPC-native ranges, private nodes.

resource "google_compute_network" "vpc" {
  name                    = var.network_name
  auto_create_subnetworks = false

  depends_on = [google_project_service.services]
}

resource "google_compute_subnetwork" "nodes" {
  name          = "${var.cluster_name}-nodes"
  region        = var.region
  network       = google_compute_network.vpc.id
  ip_cidr_range = var.subnet_cidr

  # Private nodes reach Artifact Registry and Google APIs without public IPs.
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pods_cidr
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.services_cidr
  }
}

resource "google_compute_router" "router" {
  count = var.enable_cloud_nat ? 1 : 0

  name    = "${var.cluster_name}-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "nat" {
  count = var.enable_cloud_nat ? 1 : 0

  name                               = "${var.cluster_name}-nat"
  router                             = google_compute_router.router[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# -----------------------------------------------------------------------------
# Artifact Registry: immutable tags make "deploy :latest" impossible, and the
# cleanup policies bound storage cost.

resource "google_artifact_registry_repository" "models" {
  location      = var.region
  repository_id = var.artifact_repository_id
  description   = "Model-serving container images"
  format        = "DOCKER"

  docker_config {
    immutable_tags = true
  }

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-recent-versions"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }

  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "7776000s" # 90 days
    }
  }

  depends_on = [google_project_service.services]
}

# -----------------------------------------------------------------------------
# Node identity: by default a dedicated least-privilege service account instead
# of the Compute Engine default account (which has Editor on the project).
# Creating it requires permission to grant IAM roles; where that is not
# available, set node_service_account to an existing account.

locals {
  create_node_sa = var.node_service_account == ""
  node_sa_email  = local.create_node_sa ? google_service_account.nodes[0].email : var.node_service_account
}

resource "google_service_account" "nodes" {
  count = local.create_node_sa ? 1 : 0

  account_id   = "${var.cluster_name}-nodes"
  display_name = "GKE nodes for ${var.cluster_name}"

  depends_on = [google_project_service.services]
}

resource "google_project_iam_member" "nodes_default" {
  count = local.create_node_sa ? 1 : 0

  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount" # logging, monitoring, metadata
  member  = google_service_account.nodes[0].member
}

resource "google_artifact_registry_repository_iam_member" "nodes_pull" {
  count = local.create_node_sa ? 1 : 0

  location   = google_artifact_registry_repository.models.location
  repository = google_artifact_registry_repository.models.name
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.nodes[0].member
}

# -----------------------------------------------------------------------------
# GKE cluster

resource "google_container_cluster" "ml_cluster" {
  name     = var.cluster_name
  location = var.zone

  network         = google_compute_network.vpc.id
  subnetwork      = google_compute_subnetwork.nodes.id
  networking_mode = "VPC_NATIVE"

  # Node pools are managed separately so they can change without recreating
  # the cluster.
  remove_default_node_pool = true
  initial_node_count       = 1

  deletion_protection = var.deletion_protection

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  # Nodes get no public IPs. The control-plane endpoint stays public (optionally
  # restricted below) so GitHub Actions can deploy.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }

  dynamic "master_authorized_networks_config" {
    for_each = length(var.master_authorized_networks) > 0 ? [1] : []
    content {
      dynamic "cidr_blocks" {
        for_each = var.master_authorized_networks
        content {
          cidr_block   = cidr_blocks.value.cidr_block
          display_name = cidr_blocks.value.display_name
        }
      }
    }
  }

  # Dataplane V2 (eBPF): enforces NetworkPolicy without a separate add-on.
  datapath_provider = "ADVANCED_DATAPATH"

  release_channel {
    channel = var.release_channel
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  addons_config {
    horizontal_pod_autoscaling {
      disabled = false
    }
    http_load_balancing {
      disabled = false
    }
  }

  cluster_autoscaling {
    autoscaling_profile = var.autoscaling_profile
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  # POD/DEPLOYMENT/HPA enable GKE-managed kube-state-metrics (replica counts,
  # pending pods) used by the dashboard and alerts. Managed Prometheus scrapes
  # TF Serving via kubernetes/podmonitoring.yaml.
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS", "POD", "DEPLOYMENT", "HPA"]
    managed_prometheus {
      enabled = true
    }
  }

  cost_management_config {
    enabled = true
  }

  maintenance_policy {
    daily_maintenance_window {
      start_time = "03:00"
    }
  }

  # Only used for the default pool that is created and immediately removed;
  # without it GKE would use the Compute Engine default service account.
  node_config {
    service_account = local.node_sa_email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  lifecycle {
    ignore_changes = [node_config]
  }

  depends_on = [
    google_project_service.services,
    google_project_iam_member.nodes_default,
  ]
}

resource "google_container_node_pool" "ml_pool" {
  name     = "ml-pool"
  location = var.zone
  cluster  = google_container_cluster.ml_cluster.name

  # initial_node_count rather than node_count: the cluster autoscaler owns the
  # size after creation and Terraform must not reset it on every apply.
  initial_node_count = var.initial_node_count

  autoscaling {
    min_node_count  = var.min_node_count
    max_node_count  = var.max_node_count
    location_policy = "BALANCED"
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  # Surge upgrades: add a node before draining one, so capacity never drops.
  upgrade_settings {
    strategy        = "SURGE"
    max_surge       = 1
    max_unavailable = 0
  }

  node_config {
    machine_type = var.machine_type
    disk_size_gb = var.disk_size_gb
    disk_type    = "pd-balanced"
    image_type   = "COS_CONTAINERD"
    spot         = var.use_spot

    service_account = local.node_sa_email
    # Broad scope, narrow IAM: access is governed by the service account roles.
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    labels = {
      workload = "ml-serving"
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }

    # Required for Workload Identity: pods see the GKE metadata server, never
    # the node's credentials.
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    # Image streaming: new nodes start the ~1.4 GB serving image in seconds
    # instead of pulling it fully, which shortens cluster-autoscaler scale-up.
    gcfs_config {
      enabled = true
    }
  }

  lifecycle {
    ignore_changes = [initial_node_count]
  }

  depends_on = [google_artifact_registry_repository_iam_member.nodes_pull]
}

# Optional load-generator pool: fixed size (not autoscaled) and tainted so only
# Locust pods land here; serving pods and cluster-autoscaler decisions are
# unaffected.
resource "google_container_node_pool" "loadgen" {
  count = var.enable_loadgen_pool ? 1 : 0

  name       = "loadgen"
  location   = var.zone
  cluster    = google_container_cluster.ml_cluster.name
  node_count = 1

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.loadgen_machine_type
    disk_size_gb    = var.disk_size_gb
    disk_type       = "pd-balanced"
    image_type      = "COS_CONTAINERD"
    service_account = local.node_sa_email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    labels = {
      workload = "loadgen"
    }

    taint {
      key    = "dedicated"
      value  = "loadgen"
      effect = "NO_SCHEDULE"
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }
}
