# Keyless CI/CD: GitHub Actions exchanges its OIDC token for short-lived Google
# credentials via Workload Identity Federation. No service-account JSON keys.
# Only pushes to main and v* tags of var.github_repository can get a token, so
# pull requests (including from forks) can never deploy.

locals {
  enable_github = var.github_repository != ""
}

resource "google_iam_workload_identity_pool" "github" {
  count = local.enable_github ? 1 : 0

  workload_identity_pool_id = "github-actions"
  display_name              = "GitHub Actions"

  depends_on = [google_project_service.services]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = local.enable_github ? 1 : 0

  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  attribute_condition = <<-EOT
    assertion.repository == "${var.github_repository}" &&
    (assertion.ref == "refs/heads/main" || assertion.ref.startsWith("refs/tags/v"))
  EOT

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deployer" {
  count = local.enable_github ? 1 : 0

  account_id   = "github-deployer"
  display_name = "GitHub Actions deployer"
}

resource "google_service_account_iam_member" "deployer_wif" {
  count = local.enable_github ? 1 : 0

  service_account_id = google_service_account.deployer[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository/${var.github_repository}"
}

# Push images to this one repository only.
resource "google_artifact_registry_repository_iam_member" "deployer_push" {
  count = local.enable_github ? 1 : 0

  location   = google_artifact_registry_repository.models.location
  repository = google_artifact_registry_repository.models.name
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.deployer[0].member
}

# Apply Kubernetes manifests. container.developer cannot modify RBAC or the
# cluster itself; for tighter scoping, bind a namespace Role via Kubernetes RBAC
# and grant only roles/container.clusterViewer here.
resource "google_project_iam_member" "deployer_gke" {
  count = local.enable_github ? 1 : 0

  project = var.project_id
  role    = "roles/container.developer"
  member  = google_service_account.deployer[0].member
}
