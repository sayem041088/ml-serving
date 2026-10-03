# Non-secret environment settings. project_id is intentionally absent: the
# Makefile passes it as TF_VAR_project_id (default: `gcloud config get project`).

region       = "us-central1"
zone         = "us-central1-a"
cluster_name = "resnet-inference"

# Capacity: e2-standard-4 = 4 vCPU / 16 GB, ~3 serving pods per node
# (1 CPU request each). 2 nodes hold 6 pods; HPA max (10) needs 4.
machine_type       = "e2-standard-4"
initial_node_count = 2
min_node_count     = 2
max_node_count     = 5

autoscaling_profile = "BALANCED"
use_spot            = false

# owner/repo allowed to deploy via GitHub Actions (Workload Identity Federation).
github_repository = ""

# alert_email = "oncall@example.com"
