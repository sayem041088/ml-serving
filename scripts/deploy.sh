#!/usr/bin/env bash
# Deploy kubernetes/ with an immutable image and wait for the rollout.
#
#   IMAGE=<REGION>-docker.pkg.dev/<PROJECT_ID>/ml-models/resnet101:<TAG> scripts/deploy.sh
#
# Optional: NAMESPACE (ml-serving), ROLLOUT_TIMEOUT (600s), SKIP_SMOKE_TEST=1,
# SMOKE_VIA_PORT_FORWARD=1 (smoke test through `kubectl port-forward` instead of
# the load balancer, for projects whose firewall policy blocks internet ingress).
# On a failed rollout or smoke test the Deployment is rolled back.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-ml-serving}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-600s}"
: "${IMAGE:?set IMAGE to <repository>:<immutable tag>}"

rollback() {
  # The first revision has nothing to roll back to.
  local revisions
  revisions="$(kubectl -n "${NAMESPACE}" get replicasets -l app=resnet-serving --no-headers 2>/dev/null | wc -l)"
  if (( revisions < 2 )); then
    echo "First deployment: no previous revision to roll back to" >&2
    return
  fi
  kubectl -n "${NAMESPACE}" rollout undo deployment/resnet-serving
  kubectl -n "${NAMESPACE}" rollout status deployment/resnet-serving --timeout="${ROLLOUT_TIMEOUT}" || true
}

IMAGE_NAME="${IMAGE%:*}"
IMAGE_TAG="${IMAGE##*:}"
if [[ "${IMAGE_NAME}" == "${IMAGE}" || -z "${IMAGE_TAG}" ]]; then
  echo "IMAGE must include a tag: ${IMAGE}" >&2
  exit 1
fi
if [[ "${IMAGE_TAG}" == "latest" ]]; then
  echo "Refusing to deploy :latest; use an immutable tag (e.g. sha-<git sha>)." >&2
  exit 1
fi

# Render through a throwaway overlay so the committed kustomization stays
# untouched. The config hash annotation rolls the pods when only the ConfigMap
# changed (Kubernetes does not restart pods for ConfigMap updates).
CONFIG_HASH="$(sha256sum "${ROOT}/kubernetes/configmap.yaml" | cut -c1-16)"
OVERLAY="$(mktemp -d)"
trap 'rm -rf "${OVERLAY}"' EXIT
cp -r "${ROOT}/kubernetes" "${OVERLAY}/base"
cat > "${OVERLAY}/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - base
images:
  - name: resnet101-serving
    newName: ${IMAGE_NAME}
    newTag: "${IMAGE_TAG}"
patches:
  - target:
      kind: Deployment
      name: resnet-serving
    patch: |-
      - op: add
        path: /spec/template/metadata/annotations/resnet-inference~1config-hash
        value: "${CONFIG_HASH}"
EOF

# The namespace must exist before a server-side dry run of namespaced objects.
kubectl apply -f "${ROOT}/kubernetes/namespace.yaml"

echo "==> Validating against the API server"
kubectl apply -k "${OVERLAY}" --dry-run=server >/dev/null

echo "==> Applying ${IMAGE}"
kubectl apply -k "${OVERLAY}"

echo "==> Waiting for rollout (timeout ${ROLLOUT_TIMEOUT})"
if ! kubectl -n "${NAMESPACE}" rollout status deployment/resnet-serving --timeout="${ROLLOUT_TIMEOUT}"; then
  echo "Rollout failed; rolling back" >&2
  rollback
  exit 1
fi

if [[ "${SKIP_SMOKE_TEST:-0}" == "1" ]]; then
  exit 0
fi

if [[ "${SMOKE_VIA_PORT_FORWARD:-0}" == "1" ]]; then
  kubectl -n "${NAMESPACE}" port-forward svc/resnet-service 18501:80 >/dev/null 2>&1 &
  PF_PID=$!
  trap 'kill "${PF_PID}" 2>/dev/null; rm -rf "${OVERLAY}"' EXIT
  sleep 3
  ENDPOINT="http://localhost:18501"
else
  echo "==> Waiting for the load balancer IP"
  for _ in $(seq 1 60); do
    IP="$(kubectl -n "${NAMESPACE}" get svc resnet-service -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
    [[ -n "${IP}" ]] && break
    sleep 5
  done
  if [[ -z "${IP:-}" ]]; then
    echo "No external IP assigned after 5 minutes" >&2
    exit 1
  fi
  ENDPOINT="http://${IP}"
fi
echo "Endpoint: ${ENDPOINT}"

if ! "${ROOT}/scripts/smoke_test.sh" "${ENDPOINT}"; then
  echo "Smoke test failed; rolling back" >&2
  rollback
  exit 1
fi
