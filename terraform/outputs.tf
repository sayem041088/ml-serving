output "cluster_name" {
  value = google_container_cluster.ml_cluster.name
}

output "cluster_location" {
  value = google_container_cluster.ml_cluster.location
}

output "get_credentials_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.ml_cluster.name} --zone ${google_container_cluster.ml_cluster.location} --project ${var.project_id}"
}

output "image_repository" {
  description = "Push serving images here (tag with an immutable version, e.g. sha-<git sha>)."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.models.repository_id}/resnet101"
}

output "node_service_account" {
  value = local.node_sa_email
}

output "github_workload_identity_provider" {
  description = "Set as the GCP_WIF_PROVIDER repository variable in GitHub."
  value       = local.enable_github ? google_iam_workload_identity_pool_provider.github[0].name : null
}

output "github_deployer_service_account" {
  description = "Set as the GCP_DEPLOY_SA repository variable in GitHub."
  value       = local.enable_github ? google_service_account.deployer[0].email : null
}

output "dashboard_url" {
  value = var.enable_monitoring ? "https://console.cloud.google.com/monitoring/dashboards/builder/${element(split("/", google_monitoring_dashboard.serving[0].id), 3)}?project=${var.project_id}" : null
}
