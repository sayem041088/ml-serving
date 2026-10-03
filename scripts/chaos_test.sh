#!/usr/bin/env bash
# Resilience tests: keep sending requests while (1) a serving pod is killed and
# (2) optionally a node running serving pods is drained. Reports recovery time
# against the "< 60 s" SLO and the success rate seen by clients meanwhile.
#
#   scripts/chaos_test.sh             # pod kill only
#   DRAIN_NODE=1 scripts/chaos_test.sh
#   RUNNER=cluster LOADGEN_IMAGE=.../loadgen:TAG scripts/chaos_test.sh   # probe from inside the VPC
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-ml-serving}"
DEPLOYMENT="resnet-serving"
SELECTOR="app=resnet-serving"
PAYLOAD="${ROOT}/locust/test_payload.json"
RECOVERY_SLO_S="${RECOVERY_SLO_S:-60}"

RUNNER="${RUNNER:-local}"
if [[ "${RUNNER}" == "cluster" ]]; then
  URL="http://resnet-service.${NAMESPACE}.svc.cluster.local/v1/models/resnet101:predict"
else
  IP="$(kubectl -n "${NAMESPACE}" get svc resnet-service -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
  [[ -n "${IP}" ]] || { echo "resnet-service has no external IP" >&2; exit 1; }
  URL="http://${IP}/v1/models/resnet101:predict"
fi

PROBE_LOG="$(mktemp)"
PROBE_PID=""
PROBE_POD="chaos-probe-$$"
cleanup() {
  [[ -n "${PROBE_PID}" ]] && kill "${PROBE_PID}" 2>/dev/null || true
  if [[ "${RUNNER}" == "cluster" ]]; then
    kubectl -n loadgen delete pod "${PROBE_POD}" --wait=false >/dev/null 2>&1 || true
  fi
  rm -f "${PROBE_LOG}"
}
trap cleanup EXIT

# Client: one request every 0.5 s, logging the HTTP status (000 = no response).
PROBE_LOOP='while true; do
  curl -s -o /dev/null -w "%{http_code}\n" --max-time 30 -H "Content-Type: application/json" \
    -d @"$PAYLOAD" "$URL" || echo 000
  sleep 0.5
done'

probe() {
  PAYLOAD="${PAYLOAD}" URL="${URL}" bash -c "${PROBE_LOOP}" >> "${PROBE_LOG}"
}

start_probe() {
  if [[ "${RUNNER}" == "cluster" ]]; then
    : "${LOADGEN_IMAGE:?set LOADGEN_IMAGE (built from docker/loadgen.Dockerfile)}"
    kubectl apply -f "${ROOT}/kubernetes/loadgen/namespace.yaml" >/dev/null
    kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${PROBE_POD}
  namespace: loadgen
spec:
  restartPolicy: Never
  nodeSelector:
    cloud.google.com/gke-nodepool: loadgen
  tolerations:
    - {key: dedicated, value: loadgen, effect: NoSchedule}
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: probe
      image: ${LOADGEN_IMAGE}
      command: ["bash", "-c"]
      args:
        - |
$(sed 's/^/          /' <<<"${PROBE_LOOP}")
      env:
        - {name: URL, value: "${URL}"}
        - {name: PAYLOAD, value: /app/locust/test_payload.json}
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: {drop: ["ALL"]}
EOF
    kubectl -n loadgen wait --for=condition=Ready "pod/${PROBE_POD}" --timeout=120s >/dev/null
  else
    probe &
    PROBE_PID=$!
  fi
}

collect_probe() {
  if [[ "${RUNNER}" == "cluster" ]]; then
    kubectl -n loadgen logs "${PROBE_POD}" > "${PROBE_LOG}"
  fi
}

desired() { kubectl -n "${NAMESPACE}" get deploy "${DEPLOYMENT}" -o jsonpath='{.spec.replicas}'; }
ready() { kubectl -n "${NAMESPACE}" get deploy "${DEPLOYMENT}" -o jsonpath='{.status.readyReplicas}'; }

wait_recovered() {
  local target="$1" start="$2"
  until [[ "$(ready)" -ge "${target}" ]]; do
    sleep 1
    if (( SECONDS - start > 600 )); then
      echo "not recovered after 600 s" >&2
      return 1
    fi
  done
  echo $((SECONDS - start))
}

report_probe() {
  local total ok
  total="$(wc -l < "${PROBE_LOG}")"
  ok="$(grep -c '^200$' "${PROBE_LOG}" || true)"
  echo "client requests during test: ${total}, successful: ${ok} ($(( total ? 100 * ok / total : 0 ))%)"
  sort "${PROBE_LOG}" | uniq -c | sed 's/^/  HTTP /'
}

start_probe
sleep 5

# 1. Kill one pod (no grace period: simulates a crash).
target="$(desired)"
victim="$(kubectl -n "${NAMESPACE}" get pods -l "${SELECTOR}" -o jsonpath='{.items[0].metadata.name}')"
echo "==> Deleting pod ${victim} (desired replicas: ${target})"
start=${SECONDS}
kubectl -n "${NAMESPACE}" delete pod "${victim}" --grace-period=0 --force --wait=false >/dev/null 2>&1
sleep 2
recovery="$(wait_recovered "${target}" "${start}")"
verdict="PASS"; (( recovery <= RECOVERY_SLO_S )) || verdict="FAIL"
echo "pod recovery: ${recovery}s (SLO ${RECOVERY_SLO_S}s) ${verdict}"

# 2. Drain a node that hosts a serving pod. The PDB allows one pod down at a
#    time; the cluster autoscaler may add a node if the rest are full.
if [[ "${DRAIN_NODE:-0}" == "1" ]]; then
  node="$(kubectl -n "${NAMESPACE}" get pods -l "${SELECTOR}" -o jsonpath='{.items[0].spec.nodeName}')"
  echo "==> Draining node ${node}"
  start=${SECONDS}
  kubectl drain "${node}" --ignore-daemonsets --delete-emptydir-data --timeout=600s >/dev/null
  recovery="$(wait_recovered "${target}" "${start}")"
  echo "replicas restored ${recovery}s after drain started"
  kubectl uncordon "${node}" >/dev/null
fi

sleep 5
collect_probe
report_probe
[[ "${verdict}" == "PASS" ]]
