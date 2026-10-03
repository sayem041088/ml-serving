# Deployment guide

How to take this repository from a fresh clone to a running, autoscaling, monitored ResNet101
service on GKE, and how to tear it down again.

Values in `<ANGLE_BRACKETS>` are placeholders. Replace them with your own:

| Placeholder | Meaning | Example |
|---|---|---|
| `<PROJECT_ID>` | Google Cloud project ID | `my-ml-project` |
| `<PROJECT_NUMBER>` | project number (`gcloud projects describe <PROJECT_ID> --format='value(projectNumber)'`) | `123456789012` |
| `<REGION>` / `<ZONE>` | region for the registry and subnet / zone for the cluster | `us-central1` / `us-central1-a` |
| `<OWNER>/<REPO>` | the GitHub repository that deploys (your fork) | `octocat/ml-serving` |
| `<TAG>` | immutable image tag | `sha-0123abcd4567` |
| `<EXTERNAL_IP>` | the Service's load-balancer IP | `203.0.113.10` |

## Contents

1. [Deployment paths](#1-deployment-paths)
2. [Prerequisites](#2-prerequisites)
3. [Run locally](#3-run-locally)
4. [Validate on local Kubernetes (optional)](#4-validate-on-local-kubernetes-optional)
5. [Provision infrastructure with Terraform](#5-provision-infrastructure-with-terraform)
6. [Build and push the image](#6-build-and-push-the-image)
7. [Deploy to GKE](#7-deploy-to-gke)
8. [Verify](#8-verify)
9. [Enable workload alerts](#9-enable-workload-alerts)
10. [Load and resilience tests](#10-load-and-resilience-tests)
11. [Continuous delivery with GitHub Actions](#11-continuous-delivery-with-github-actions)
12. [Day-2 operations](#12-day-2-operations)
13. [Locked-down projects](#13-locked-down-projects)
14. [Teardown and cost](#14-teardown-and-cost)
15. [Reference](#15-reference)

---

## 1. Deployment paths

| Path | Use it for | Cloud cost |
|---|---|---|
| **Local Docker** (§3) | developing the model, client and image | none |
| **Local Kubernetes / kind** (§4) | validating manifests, probes, HPA behaviour | none |
| **GKE, manual** (§5-§10) | the first deployment, experiments, benchmarks | yes, while running |
| **GKE, CI/CD** (§11) | every change after that: merge → build → deploy → smoke test | yes |

## 2. Prerequisites

**Tools**

| Tool | Version used | Notes |
|---|---|---|
| Docker | 24+ with BuildKit | builds both images |
| Python | 3.12 | client, tests, Locust |
| Terraform | ≥ 1.9 (tested 1.16) | Google provider 8.x, pinned in `.terraform.lock.hcl` |
| Google Cloud CLI | recent | plus `gke-gcloud-auth-plugin` |
| kubectl | ≥ 1.30 | includes Kustomize |
| make, git, curl | any | |
| kind | optional | §4 only |

**Google Cloud**

- A project with **billing enabled**.
- Quota in `<REGION>`: at least **24 E2 vCPUs** (5 serving nodes + 1 optional load-generator
  node, 4 vCPUs each).
- An identity that can enable APIs, create networks, GKE clusters, Artifact Registry
  repositories, service accounts and monitoring resources (Owner, or Editor plus Project IAM
  Admin). To create the dedicated node service account, Terraform must also be able to **grant
  IAM roles**; if your organization blocks that, see §13.

```bash
gcloud auth login
gcloud auth application-default login     # credentials Terraform uses
gcloud config set project <PROJECT_ID>

export PROJECT_ID=<PROJECT_ID>
export REGION=<REGION>                     # default: us-central1
export ZONE=<ZONE>                         # default: us-central1-a
```

The Makefile reads `PROJECT_ID`, `REGION` and `ZONE` from the environment (`PROJECT_ID`
defaults to `gcloud config get project`). Terraform reads region and zone from
`terraform/terraform.tfvars`, so keep the two in sync.

## 3. Run locally

```bash
git clone https://github.com/sayem041088/ml-serving.git && cd ml-serving

make install          # client, test and load-test dependencies (.venv)
make install-model    # TensorFlow 2.21, for exporting the model
make export-model     # downloads ResNet101 weights (hash-checked) → model/resnet101/1
make build-local      # serving image from the exported model
make run-local        # runs it like a pod: 2 CPUs, 4 GiB, read-only FS, non-root
```

Check it:

```bash
curl -s localhost:8501/v1/models/resnet101          # "state": "AVAILABLE"
python -m resnet_client predict locust/images/grace_hopper.jpg
make smoke-local test test-integration
```

`make build` (without `-local`) exports the model *inside* Docker, so the image can be built
from the repository alone. CI/CD always builds this way. `make stop-local` stops the container.

## 4. Validate on local Kubernetes (optional)

The manifests run unchanged on [kind](https://kind.sigs.k8s.io/), which makes it a free way to
check probes, Pod Security, NetworkPolicy, HPA behaviour and rollouts. kind has no cluster
autoscaler, so pods the node can't fit stay Pending, which is the signal GKE would act on.

```bash
kind create cluster --name resnet
kind load docker-image resnet101-serving:local --name resnet

# metrics-server (HPA input) and the PodMonitoring CRD (applied by the kustomization)
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
kubectl apply --server-side -f https://raw.githubusercontent.com/GoogleCloudPlatform/prometheus-engine/main/charts/operator/crds/monitoring.googleapis.com_podmonitorings.yaml

IMAGE=resnet101-serving:local SKIP_SMOKE_TEST=1 scripts/deploy.sh

# The LoadBalancer stays <pending> on kind; use the node port instead.
NODE_IP=$(docker inspect resnet-control-plane -f '{{.NetworkSettings.Networks.kind.IPAddress}}')
PORT=$(kubectl -n ml-serving get svc resnet-service -o jsonpath='{.spec.ports[0].nodePort}')
scripts/smoke_test.sh http://$NODE_IP:$PORT
TARGET_HOST=http://$NODE_IP:$PORT LOAD_PROFILE=spike USER_SCALE=0.5 make load-test
```

Delete it with `kind delete cluster --name resnet`.

## 5. Provision infrastructure with Terraform

### 5.1 Configure

Non-secret settings live in `terraform/terraform.tfvars` (committed): region, zone, machine type,
node counts, autoscaler profile. The project ID is **not** committed; the Makefile passes it as
`TF_VAR_project_id`. Environment-specific overrides go in a git-ignored
`terraform/local.auto.tfvars` (see `terraform/local.auto.tfvars.example`).

| Variable | Default | Purpose |
|---|---|---|
| `project_id` | from `PROJECT_ID` | target project |
| `region` / `zone` | `us-central1` / `us-central1-a` | where everything runs |
| `machine_type` | `e2-standard-4` | node size (sizing is coupled to pod requests, see the README) |
| `min_node_count` / `max_node_count` | 2 / 5 | cluster-autoscaler bounds |
| `autoscaling_profile` | `BALANCED` | `OPTIMIZE_UTILIZATION` removes idle nodes faster |
| `use_spot` | `false` | Spot VMs for cheap test environments |
| `enable_loadgen_pool` | `false` | 1-node tainted pool for in-VPC Locust (§10, §13) |
| `node_service_account` | `""` | use an existing SA instead of creating a least-privilege one (§13) |
| `enable_workload_alerts` | `false` | second-phase alerts (§9) |
| `github_repository` | `""` | `<OWNER>/<REPO>` allowed to deploy via WIF (§11) |
| `alert_email` | `""` | email notification channel for alerts |
| `master_authorized_networks` | `[]` | restrict the control-plane endpoint |
| `deletion_protection` | `false` | set `true` for anything long-lived |

**Remote state** (recommended for anything shared or CI-driven):

```bash
gcloud storage buckets create gs://<PROJECT_ID>-tfstate --location=<REGION> --uniform-bucket-level-access
# uncomment the backend "gcs" block in terraform/providers.tf, then:
terraform -chdir=terraform init -backend-config="bucket=<PROJECT_ID>-tfstate"
```

### 5.2 Apply

```bash
make tf-init
make tf-plan     # review: about 25 resources to add, nothing to change or destroy
make tf-apply    # ~12 min, most of it GKE cluster creation
make credentials # kubectl → the new cluster
kubectl get nodes -L cloud.google.com/gke-nodepool
```

What gets created:

| Resource | Notes |
|---|---|
| VPC `resnet-vpc` + subnet | VPC-native ranges for pods/services, Private Google Access |
| Artifact Registry `ml-models` | immutable tags; cleanup keeps the 10 newest and deletes untagged > 7 d and anything > 90 d (images referenced by a tagged index are protected) |
| GKE cluster `resnet-inference` (zonal) | private nodes, Dataplane V2, Workload Identity, REGULAR channel, managed Prometheus, kube-state-metrics (pods, deployments, HPA), cost allocation, daily maintenance window |
| Node pool `ml-pool` | `e2-standard-4`, 2-5 nodes, surge upgrades (+1/−0), Shielded VM, image streaming |
| Node pool `loadgen` (optional) | 1 fixed node, tainted `dedicated=loadgen` |
| Node service account | `roles/container.defaultNodeServiceAccount` + `artifactregistry.reader` on the repo |
| Cloud Monitoring | dashboard "ResNet101 serving (resnet-inference)" + alert policies |
| GitHub WIF (optional) | pool, OIDC provider, `github-deployer` SA (§11) |

Optional Cloud NAT (`enable_cloud_nat`) is off: nodes pull from Artifact Registry over Private
Google Access, and the serving pods need no internet egress.

## 6. Build and push the image

Images are tagged immutably (`sha-<12-char commit>` by default); Artifact Registry rejects
re-pushing a tag, and `scripts/deploy.sh` refuses `:latest`.

```bash
git status                # make push requires a clean tree, so the tag matches the code
make push                 # docker build (model exported in-build) → <REGION>-docker.pkg.dev/<PROJECT_ID>/ml-models/resnet101:sha-<commit>
```

Equivalent without `make`:

```bash
IMAGE=<REGION>-docker.pkg.dev/<PROJECT_ID>/ml-models/resnet101:<TAG>
gcloud auth configure-docker <REGION>-docker.pkg.dev
docker build -t "$IMAGE" -f docker/Dockerfile .
docker push "$IMAGE"
```

## 7. Deploy to GKE

```bash
make deploy               # IMAGE defaults to the sha-<commit> tag from §6
```

`scripts/deploy.sh`:

1. renders `kubernetes/` through a temporary Kustomize overlay that sets the registry image and
   adds a hash of `configmap.yaml` to the pod template (so ConfigMap-only changes roll the pods);
2. creates the namespace, then validates everything with a server-side dry run;
3. applies, and waits for `rollout status` (`maxUnavailable: 0`, `maxSurge: 1`);
4. waits for the load-balancer IP and runs `scripts/smoke_test.sh`: model AVAILABLE, a known
   image classified as `military_uniform`, and a 400 for an invalid image;
5. runs `kubectl rollout undo` if the rollout or the smoke test fails (except on the first
   deployment, which has nothing to roll back to).

If the load balancer isn't reachable from where you deploy, smoke test through the control plane
instead (see §13):

```bash
SMOKE_VIA_PORT_FORWARD=1 make deploy
```

## 8. Verify

```bash
kubectl -n ml-serving get pods,hpa,pdb,svc
make smoke-test           # through the load balancer
make k8s-test             # API tests + Deployment/HPA/PDB/restart checks

# Without load-balancer access:
kubectl -n ml-serving port-forward svc/resnet-service 18501:80 &
ENDPOINT=http://localhost:18501 make k8s-test
```

Call the API:

```bash
curl -s http://<EXTERNAL_IP>/v1/models/resnet101
curl -s -X POST http://<EXTERNAL_IP>/v1/models/resnet101:predict -d @locust/test_payload.json
python -m resnet_client predict --endpoint http://<EXTERNAL_IP> path/to/image.jpg
```

Open the dashboard: Cloud Console → Monitoring → Dashboards → **ResNet101 serving
(resnet-inference)**, or follow `terraform -chdir=terraform output dashboard_url`. System metrics
(CPU, memory) can lag by a few minutes; TF Serving metrics appear once the first requests are
scraped.

## 9. Enable workload alerts

Cloud Monitoring refuses to create a PromQL alert for a metric it has never ingested. The two
alerts on TF Serving metrics (error rate > 1 %, P95 > 500 ms) are therefore created in a second
apply, after the service has served traffic:

```bash
echo 'enable_workload_alerts = true' >> terraform/local.auto.tfvars
make tf-plan tf-apply     # 2 alert policies to add
```

The other five alerts (no ready replicas, Pending pods, restarts, node pool at max, HPA at max)
are created in the first apply. Set `alert_email` to get notifications.

## 10. Load and resilience tests

### 10.1 From your machine

```bash
make load-test                                  # staged: 10 → 50 → 100 → 300 → 500 → 50 users, ~25 min
LOAD_PROFILE=performance make load-test         # 100 → 1000 users: find the saturation point
LOAD_PROFILE=spike make load-test               # 10 → 300 → 10: compare HPA settings
BATCH_SIZE=16 make load-test                    # batch-inference experiment
```

Locust runs headless while `scripts/record_cluster_metrics.py` samples HPA, pods and nodes
every 10 s. The run ends with a per-stage table and a list of scaling events. Everything is
written to `results/<timestamp>-<profile>-b<batch>/` (`summary.md`, `report.html`, CSVs, events).
Chart a run with `make plot RUN=results/<run>`.

For large tests, run from a VM in the same region; a laptop's uplink or CPU gives out before the
cluster does.

### 10.2 From inside the VPC

With `enable_loadgen_pool = true`, Locust runs in a pod on the dedicated `loadgen` node, targets
the Service in-cluster, and copies results back. This needs no internet ingress and doesn't skew
the serving nodes:

```bash
make push-loadgen                               # builds docker/loadgen.Dockerfile → .../ml-models/loadgen:<TAG>
RUNNER=cluster make load-test
```

### 10.3 Resilience

```bash
make chaos-test                                 # force-kill a pod under traffic; recovery vs 60 s SLO
DRAIN_NODE=1 make chaos-test                    # also drain a node running serving pods
RUNNER=cluster DRAIN_NODE=1 make chaos-test     # probe from inside the VPC
```

Watch it live with `make watch`.

## 11. Continuous delivery with GitHub Actions

### 11.1 One-time setup

1. Allow the repository to deploy, then apply:
   ```bash
   echo 'github_repository = "<OWNER>/<REPO>"' >> terraform/local.auto.tfvars
   make tf-plan tf-apply
   terraform -chdir=terraform output github_workload_identity_provider github_deployer_service_account
   ```
   This creates a Workload Identity pool and OIDC provider that only accept tokens for
   `refs/heads/main` and `refs/tags/v*` of that repository, and a `github-deployer` service
   account that can push to `ml-models` and deploy to GKE. No JSON keys are created.
2. In GitHub → *Settings → Secrets and variables → Actions → Variables*, add:

   | Variable | Value |
   |---|---|
   | `GCP_PROJECT_ID` | `<PROJECT_ID>` |
   | `GCP_WIF_PROVIDER` | output `github_workload_identity_provider` |
   | `GCP_DEPLOY_SA` | output `github_deployer_service_account` |
   | `GCP_REGION`, `GKE_ZONE`, `GKE_CLUSTER` | only if you changed the defaults |
   | `SMOKE_VIA_PORT_FORWARD`, `LOAD_TEST_RUNNER` | `1` / `cluster` in locked-down projects (§13) |

3. Create a GitHub **environment** named `production` with required reviewers. Both deploy jobs
   use it, so every deployment waits for approval.
4. Protect `main`: require pull requests, require the CI checks, and disallow force pushes.
5. In a fork, point the CI badge in `README.md` at your repository.

### 11.2 What runs

| Workflow | Trigger | Steps |
|---|---|---|
| `ci.yml` | pull request, push to `main` | ruff, pytest (unit + policy), shell syntax · `terraform fmt`/`validate` · kubeconform (strict, incl. CRDs) · image build (model exported in-build) → integration tests against the running container → smoke test → Locust smoke → Trivy (fails on fixable CRITICAL) |
| `deploy.yml` | push to `main` / `v*` tag affecting the image or manifests; manual | WIF auth → build and push `sha-<commit>` (skipped if it exists; `v1.2.3` tags also publish `1.2.3`) → Trivy → `scripts/deploy.sh` (dry run, apply, rollout, smoke test, auto-rollback) → cluster tests → optional staged load test (manual run, results as artifact and job summary) |

Third-party actions are pinned to commit SHAs. Dependabot (`.github/dependabot.yml`) proposes
updates for actions, Python packages, Docker base images and the Terraform provider.

## 12. Day-2 operations

| Task | How |
|---|---|
| Roll back | `kubectl -n ml-serving rollout undo deployment/resnet-serving`, or re-run Deploy for an older commit (its image still exists) |
| Change TF Serving tuning (threads, batching) | edit `kubernetes/configmap.yaml`, then deploy; the config hash rolls the pods |
| Change scaling bounds | `kubernetes/hpa.yaml` (pods), `max_node_count` in `terraform.tfvars` (nodes); keep the sizing invariant (README) |
| Release a version | `git tag v1.2.3 && git push --tags`: the image is published as `sha-<commit>` and `1.2.3` |
| Inspect autoscaler decisions | Cloud Logging, log `container.googleapis.com/cluster-autoscaler-visibility` (scale-up/down decisions and `noScaleDown` reasons) |
| Logs | `kubectl -n ml-serving logs -l app=resnet-serving --tail=100`, or the dashboard's log panel |

## 13. Locked-down projects

Enterprise projects often enforce organization policies that a sandbox doesn't have. These are
the ones met in the reference deployment, and how the repository handles them:

| Symptom | Cause | What to do |
|---|---|---|
| Pods are ready and the Service has an external IP, but requests to it **time out** | an **organization (hierarchical) firewall policy** denies internet ingress; it takes precedence over the VPC rules GKE creates | Don't work around it. Use `SMOKE_VIA_PORT_FORWARD=1` for deploys, `ENDPOINT=http://localhost:18501 make k8s-test` over a port-forward, and `RUNNER=cluster` with `enable_loadgen_pool = true` for load tests. To serve the public, ask the org admins to allowlist client ranges or front the service with an approved L7 load balancer. Check with `gcloud compute networks get-effective-firewalls resnet-vpc`. |
| `Error 403: Policy update access denied` when Terraform grants the node SA its roles | org policy blocks IAM changes, even if `testIamPermissions` reports `setIamPolicy` | Set `node_service_account = "<PROJECT_NUMBER>-compute@developer.gserviceaccount.com"` (or another existing SA with logging/monitoring write and registry read) in `local.auto.tfvars`. This is broader than the default least-privilege SA; pods still can't use it (GKE metadata server). |
| `The following PromQL metric(s) are invalid: tfserving_…` | metric not ingested yet | §9: apply again with `enable_workload_alerts = true` after traffic |
| `Error 409: … already exists` on re-apply | an interrupted `terraform apply` created the resource but never recorded it | Import it, e.g. `terraform -chdir=terraform import google_compute_network.vpc projects/<PROJECT_ID>/global/networks/resnet-vpc`, then plan again |
| Nodes aren't scaled down for a long time | the autoscaler needs ~10 min of unneeded time per node; kube-system pods without PDBs and our PDB serialize removals | expected with `BALANCED`; use `OPTIMIZE_UTILIZATION` for faster scale-down, and check the visibility logs (§12) |
| Rollout pods stay Pending on an otherwise idle cluster | the topology spread counts tainted nodes as empty domains | already handled (`nodeTaintsPolicy: Honor`); keep it if you add tainted pools |

## 14. Teardown and cost

Running cost is roughly the node pools (on-demand `e2-standard-4` ≈ USD 0.13/h each; 2-5
serving nodes plus the optional load-generator node), the cluster management fee (one zonal
cluster per billing account is covered by the GKE free tier), the load-balancer forwarding rule,
and Managed Prometheus samples (only `tfserving_*` metrics are kept). Check current pricing for
your region.

```bash
make cleanup-k8s          # delete the workload and its load balancer; keep the cluster
make cleanup              # delete everything Terraform created (asks for confirmation)
```

`scripts/cleanup.sh` deletes the LoadBalancer Service **before** `terraform destroy`, because
its forwarding and firewall rules are created by GKE outside Terraform and would block deleting
the VPC. Enabled project APIs are left enabled (`disable_on_destroy = false`), and Terraform
state is kept.

To save money between experiments without destroying anything: `make cleanup-k8s`, and set
`min_node_count = 0` or `use_spot = true`.

## 15. Reference

**Make targets** (`make help` lists them all)

| Target | Description |
|---|---|
| `install`, `install-model` | Python dependencies (+ TensorFlow) |
| `export-model`, `payloads` | export the SavedModel; regenerate Locust payloads |
| `build`, `build-local`, `run-local`, `stop-local` | serving image (from source / from local model), run locally |
| `lint`, `format`, `test`, `test-integration`, `smoke-local` | checks |
| `tf-init`, `tf-plan`, `tf-apply`, `credentials` | infrastructure |
| `push`, `push-loadgen`, `deploy`, `smoke-test`, `k8s-test` | release and verify |
| `load-test`, `chaos-test`, `plot`, `watch` | experiments |
| `cleanup-k8s`, `cleanup` | teardown |

**Script environment variables**

| Variable | Used by | Meaning |
|---|---|---|
| `IMAGE` | `deploy.sh` | `<registry>/resnet101:<TAG>` (required; `:latest` refused) |
| `SKIP_SMOKE_TEST`, `SMOKE_VIA_PORT_FORWARD` | `deploy.sh` | skip the smoke test / run it through a port-forward |
| `LOAD_PROFILE` | `load_test.sh`, Locust | `staged`, `performance`, `spike`, `smoke` |
| `BATCH_SIZE`, `USER_SCALE` | `load_test.sh`, Locust | images per request; multiply every stage's users |
| `WAIT_MIN`, `WAIT_MAX` | Locust | think time per user (default 5-15 s; see BENCHMARKS for why) |
| `RECONNECT_EVERY` | Locust | new TCP connection every N requests (default 1; 0 = keep-alive) |
| `RUNNER`, `LOADGEN_IMAGE` | `load_test.sh`, `chaos_test.sh` | `local` or `cluster` (in-VPC pod) |
| `TARGET_HOST`, `RECORD_CLUSTER`, `NODE_SELECTOR` | `load_test.sh` | override the target; disable/scope the cluster recorder |
| `DRAIN_NODE`, `RECOVERY_SLO_S` | `chaos_test.sh` | also drain a node; recovery SLO (60 s) |
