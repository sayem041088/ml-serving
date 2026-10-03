variable "project_id" {
  description = "Google Cloud project ID. Passed by the Makefile as TF_VAR_project_id so it is not committed."
  type        = string
}

variable "region" {
  description = "Region for the VPC subnet and Artifact Registry."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Zone for the (zonal) GKE cluster. Zonal keeps node counts exact (2 -> 5) and the control plane free-tier eligible; use a regional cluster for production HA."
  type        = string
  default     = "us-central1-a"
}

variable "cluster_name" {
  description = "GKE cluster name."
  type        = string
  default     = "resnet-inference"
}

variable "network_name" {
  description = "VPC network name."
  type        = string
  default     = "resnet-vpc"
}

variable "subnet_cidr" {
  description = "Primary range for nodes."
  type        = string
  default     = "10.10.0.0/20"
}

variable "pods_cidr" {
  description = "Secondary range for pods (a /24 per node at 110 pods/node)."
  type        = string
  default     = "10.20.0.0/16"
}

variable "services_cidr" {
  description = "Secondary range for Services."
  type        = string
  default     = "10.30.0.0/20"
}

variable "master_authorized_networks" {
  description = "CIDRs allowed to reach the public control-plane endpoint. Empty = unrestricted (GitHub-hosted runners have no fixed IPs)."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}

variable "enable_cloud_nat" {
  description = "Give private nodes internet egress. Not needed for images from Artifact Registry (Private Google Access)."
  type        = bool
  default     = false
}

variable "release_channel" {
  description = "GKE release channel."
  type        = string
  default     = "REGULAR"
}

variable "autoscaling_profile" {
  description = "Cluster autoscaler profile. OPTIMIZE_UTILIZATION removes idle nodes faster than BALANCED."
  type        = string
  default     = "BALANCED"

  validation {
    condition     = contains(["BALANCED", "OPTIMIZE_UTILIZATION"], var.autoscaling_profile)
    error_message = "autoscaling_profile must be BALANCED or OPTIMIZE_UTILIZATION."
  }
}

variable "machine_type" {
  description = "Node machine type."
  type        = string
  default     = "e2-standard-4"
}

variable "disk_size_gb" {
  description = "Node boot disk size."
  type        = number
  default     = 50
}

variable "initial_node_count" {
  description = "Nodes created with the pool. Afterwards the cluster autoscaler owns the count."
  type        = number
  default     = 2
}

variable "min_node_count" {
  description = "Cluster autoscaler lower bound."
  type        = number
  default     = 2
}

variable "max_node_count" {
  description = "Cluster autoscaler upper bound."
  type        = number
  default     = 5

  validation {
    condition     = var.max_node_count >= var.min_node_count
    error_message = "max_node_count must be >= min_node_count."
  }
}

variable "enable_loadgen_pool" {
  description = "Add a fixed 1-node pool, tainted dedicated=loadgen, for in-cluster Locust (scripts/load_test.sh RUNNER=cluster). Keeps the load generator off the serving nodes."
  type        = bool
  default     = false
}

variable "loadgen_machine_type" {
  description = "Machine type for the load-generator pool."
  type        = string
  default     = "e2-standard-4"
}

variable "use_spot" {
  description = "Use Spot VMs (60-91% cheaper, can be preempted). Fine for load-test environments, not for production."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Block `terraform destroy` of the cluster. Off so the test environment can be torn down."
  type        = bool
  default     = false
}

variable "node_service_account" {
  description = "Existing service account for nodes. Empty = create a dedicated least-privilege one (needs permission to grant IAM roles)."
  type        = string
  default     = ""
}

variable "enable_workload_alerts" {
  description = "Create alerts on TF Serving metrics. Cloud Monitoring rejects PromQL alerts on metrics it has never ingested, so enable this after the first deploy has served traffic."
  type        = bool
  default     = false
}

variable "artifact_repository_id" {
  description = "Artifact Registry Docker repository."
  type        = string
  default     = "ml-models"
}

variable "k8s_namespace" {
  description = "Namespace of the serving workload (kubernetes/kustomization.yaml). Used by dashboards and alerts."
  type        = string
  default     = "ml-serving"
}

variable "github_repository" {
  description = "GitHub repository (owner/name) allowed to deploy via Workload Identity Federation. Empty = no CI/CD identity."
  type        = string
  default     = ""
}

variable "enable_monitoring" {
  description = "Create the Cloud Monitoring dashboard and alert policies."
  type        = bool
  default     = true
}

variable "alert_email" {
  description = "Email for alert notifications. Empty = alerts only appear in the console."
  type        = string
  default     = ""
}
