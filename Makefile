# Scalable ResNet101 serving on GKE. `make help` lists the targets.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

PROJECT_ID ?= $(shell gcloud config get-value project 2>/dev/null)
REGION     ?= us-central1
ZONE       ?= us-central1-a
CLUSTER    ?= resnet-inference
NAMESPACE  ?= ml-serving

VENV       ?= .venv
PYTHON     ?= $(VENV)/bin/python
BIN        := $(VENV)/bin

LOCAL_IMAGE := resnet101-serving:local
IMAGE_REPO  := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/ml-models/resnet101
GIT_SHA     := $(shell git rev-parse --short=12 HEAD 2>/dev/null)
IMAGE_TAG   ?= $(if $(GIT_SHA),sha-$(GIT_SHA),$(error IMAGE_TAG not set and no git commit to derive it from))
IMAGE        = $(IMAGE_REPO):$(IMAGE_TAG)
LOADGEN_IMAGE ?= $(REGION)-docker.pkg.dev/$(PROJECT_ID)/ml-models/loadgen:$(IMAGE_TAG)

TF := terraform -chdir=terraform
export TF_VAR_project_id := $(PROJECT_ID)

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- local development ---------------------------------------------------------

$(VENV):
	python3 -m venv $(VENV)

.PHONY: install
install: $(VENV) ## Install client, test and load-test dependencies
	$(BIN)/pip install -r requirements.txt

.PHONY: install-model
install-model: $(VENV) ## Install TensorFlow for model export
	$(BIN)/pip install -r requirements-model.txt

.PHONY: export-model
export-model: ## Download ResNet101 and export model/resnet101/1 (needs install-model)
	$(PYTHON) model/download_model.py
	$(PYTHON) model/export_model.py --output-dir model/resnet101 --version 1 --force

.PHONY: payloads
payloads: ## Regenerate Locust payloads from locust/images
	$(PYTHON) -m resnet_client payload locust/images/grace_hopper.jpg -o locust/test_payload.json
	$(PYTHON) -m resnet_client payload locust/images --batch-size 16 -o locust/test_payload_batch16.json

.PHONY: build
build: ## Build the serving image from source (exports the model inside Docker)
	docker build -t $(LOCAL_IMAGE) -f docker/Dockerfile .

.PHONY: build-local
build-local: ## Build the serving image reusing model/resnet101 (faster)
	docker build -t $(LOCAL_IMAGE) -f docker/Dockerfile --build-arg MODEL_SOURCE=local .

.PHONY: run-local
run-local: ## Run the image like a 2-CPU pod on :8501 (REST) / :8500 (gRPC)
	docker rm -f resnet-local >/dev/null 2>&1 || true
	docker run -d --name resnet-local -p 8501:8501 -p 8500:8500 \
	  --cpus=2 --memory=4g --read-only --tmpfs /tmp --cap-drop ALL \
	  -e TF_INTRA_OP_PARALLELISM=2 -e TF_INTER_OP_PARALLELISM=2 $(LOCAL_IMAGE)
	$(PYTHON) -m resnet_client status --endpoint http://localhost:8501 --wait 120 >/dev/null
	@echo "Serving on http://localhost:8501/v1/models/resnet101"

.PHONY: stop-local
stop-local: ## Stop the local container
	docker rm -f resnet-local

.PHONY: lint
lint: ## ruff + terraform fmt + manifest validation
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	terraform fmt -check -recursive terraform
	kubectl kustomize kubernetes >/dev/null

.PHONY: format
format: ## Auto-format Python and Terraform
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .
	terraform fmt -recursive terraform

.PHONY: test
test: ## Unit, policy and (if TF is installed) model tests
	$(BIN)/pytest -m "not integration and not k8s"

.PHONY: test-integration
test-integration: ## API tests against ENDPOINT (default: local container)
	RESNET_ENDPOINT=$${ENDPOINT:-http://localhost:8501} $(BIN)/pytest -m integration

.PHONY: smoke-local
smoke-local: ## Smoke test the local container
	scripts/smoke_test.sh http://localhost:8501

# --- infrastructure --------------------------------------------------------------

.PHONY: tf-init
tf-init: ## terraform init
	$(TF) init

.PHONY: tf-plan
tf-plan: ## terraform plan (project from gcloud config or PROJECT_ID=...)
	$(TF) plan -out=tfplan

.PHONY: tf-apply
tf-apply: ## terraform apply the saved plan
	$(TF) apply tfplan

.PHONY: credentials
credentials: ## Point kubectl at the cluster
	gcloud container clusters get-credentials $(CLUSTER) --zone $(ZONE) --project $(PROJECT_ID)

# --- release ---------------------------------------------------------------------

.PHONY: push
push: ## Build from source and push $(IMAGE) (immutable sha-<git sha> tag)
	@git diff --quiet HEAD -- || { echo "Uncommitted changes: commit first so the tag matches the code"; exit 1; }
	gcloud auth configure-docker $(REGION)-docker.pkg.dev --quiet
	docker build -t $(IMAGE) -f docker/Dockerfile .
	docker push $(IMAGE)

.PHONY: push-loadgen
push-loadgen: ## Build and push the Locust image for in-VPC load tests (RUNNER=cluster)
	docker build -t $(LOADGEN_IMAGE) -f docker/loadgen.Dockerfile .
	docker push $(LOADGEN_IMAGE)

.PHONY: deploy
deploy: ## Deploy $(IMAGE) to GKE, smoke test, roll back on failure
	IMAGE=$(IMAGE) NAMESPACE=$(NAMESPACE) scripts/deploy.sh

.PHONY: smoke-test
smoke-test: ## Smoke test the GKE load balancer endpoint
	scripts/smoke_test.sh

.PHONY: k8s-test
k8s-test: ## Infrastructure + API tests against the cluster (ENDPOINT=... to override the LB IP)
	RESNET_ENDPOINT="$${ENDPOINT:-http://$$(kubectl -n $(NAMESPACE) get svc resnet-service -o jsonpath='{.status.loadBalancer.ingress[0].ip}')}" \
	  K8S_TESTS=1 $(BIN)/pytest -m "k8s or integration"

# --- experiments -----------------------------------------------------------------

.PHONY: load-test
load-test: ## Staged load test (LOAD_PROFILE=..., BATCH_SIZE=..., RUNNER=local|cluster)
	PATH=$(abspath $(BIN)):$$PATH PYTHON=$(abspath $(PYTHON)) \
	  $(if $(filter cluster,$(RUNNER)),LOADGEN_IMAGE=$(LOADGEN_IMAGE)) scripts/load_test.sh

.PHONY: chaos-test
chaos-test: ## Kill a pod (DRAIN_NODE=1: also drain a node) while serving traffic
	$(if $(filter cluster,$(RUNNER)),LOADGEN_IMAGE=$(LOADGEN_IMAGE)) scripts/chaos_test.sh

.PHONY: plot
plot: ## Chart a load-test run: make plot RUN=results/<run>
	$(PYTHON) scripts/plot_results.py $(RUN) --output $(RUN)/scaling

.PHONY: watch
watch: ## Watch HPA, pods and nodes during a test
	watch -n 5 "kubectl -n $(NAMESPACE) get hpa; echo; kubectl -n $(NAMESPACE) get pods -o wide; echo; kubectl get nodes"

# --- teardown --------------------------------------------------------------------

.PHONY: cleanup-k8s
cleanup-k8s: ## Delete the workload and its load balancer (keeps the cluster)
	K8S_ONLY=1 scripts/cleanup.sh

.PHONY: cleanup
cleanup: ## Delete the workload AND all Terraform infrastructure
	PROJECT_ID=$(PROJECT_ID) scripts/cleanup.sh
