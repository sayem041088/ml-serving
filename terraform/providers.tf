terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }

  # Local state by default. For anything shared, keep state in GCS:
  #   gcloud storage buckets create gs://PROJECT_ID-tfstate --location=us-central1 --uniform-bucket-level-access
  #   terraform init -backend-config="bucket=PROJECT_ID-tfstate"
  # backend "gcs" {
  #   prefix = "gke-resnet-inference"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region

  default_labels = {
    project    = "gke-resnet-inference"
    managed-by = "terraform"
  }
}
