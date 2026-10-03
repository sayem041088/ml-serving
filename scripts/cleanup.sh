#!/usr/bin/env bash
# Tear everything down: Kubernetes resources first (the LoadBalancer Service
# owns a forwarding rule and firewall rules that Terraform does not know about
# and that would block deleting the VPC), then the Terraform infrastructure.
#
#   scripts/cleanup.sh                # delete workload + infrastructure (asks first)
#   K8S_ONLY=1 scripts/cleanup.sh     # delete only the workload (stop LB charges)
#   AUTO_APPROVE=1 scripts/cleanup.sh # no prompt
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-ml-serving}"
: "${PROJECT_ID:=$(gcloud config get-value project 2>/dev/null)}"
export TF_VAR_project_id="${PROJECT_ID}"

if [[ "${AUTO_APPROVE:-0}" != "1" ]]; then
  scope="the ${NAMESPACE} workload"
  [[ "${K8S_ONLY:-0}" == "1" ]] || scope="${scope} AND all Terraform-managed infrastructure"
  read -r -p "Delete ${scope} in project ${PROJECT_ID}? [y/N] " answer
  [[ "${answer}" =~ ^[Yy]$ ]] || { echo "Aborted"; exit 1; }
fi

if kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  echo "==> Deleting Service (releases the load balancer)"
  kubectl -n "${NAMESPACE}" delete service resnet-service --ignore-not-found --wait=true --timeout=300s
  echo "==> Deleting namespace ${NAMESPACE}"
  kubectl delete namespace "${NAMESPACE}" --wait=true --timeout=300s
else
  echo "Namespace ${NAMESPACE} not found (or no cluster credentials); skipping Kubernetes cleanup"
fi

if [[ "${K8S_ONLY:-0}" == "1" ]]; then
  exit 0
fi

echo "==> terraform destroy"
terraform -chdir="${ROOT}/terraform" destroy -auto-approve -input=false

cat <<EOF
Done. Not deleted (outside Terraform or deliberately kept):
  - Terraform state in terraform/ (or your GCS backend bucket)
  - Enabled project APIs (disable_on_destroy = false)
EOF
