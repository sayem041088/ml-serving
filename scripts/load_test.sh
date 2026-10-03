#!/usr/bin/env bash
# Run a staged Locust test against the cluster while recording HPA/pod/node
# state, then print the per-stage results table.
#
#   scripts/load_test.sh                         # staged profile, batch 1, LB IP from kubectl
#   LOAD_PROFILE=performance scripts/load_test.sh
#   BATCH_SIZE=16 scripts/load_test.sh           # batch-inference experiment
#   TARGET_HOST=http://localhost:8501 RECORD_CLUSTER=0 scripts/load_test.sh
#   RUNNER=cluster LOADGEN_IMAGE=.../loadgen:TAG scripts/load_test.sh
#
# RUNNER=local (default) runs Locust on this machine against the LoadBalancer.
# RUNNER=cluster runs it in a pod on the dedicated `loadgen` node pool
# (terraform: enable_loadgen_pool) against the in-cluster Service: for projects
# whose firewall policy blocks internet ingress, and to keep the load generator
# off a laptop uplink.
#
# Environment: LOAD_PROFILE (staged|performance|spike|smoke), BATCH_SIZE (1),
# TARGET_HOST, USER_SCALE, WAIT_MIN/WAIT_MAX, RECONNECT_EVERY, LOCUST_PROCESSES
# (-1 = one per core), RECORD_CLUSTER (1), NODE_SELECTOR (nodes to count;
# default: the ml-pool node pool if present), RUN_LABEL (results dir suffix).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-ml-serving}"
RUNNER="${RUNNER:-local}"
LOAD_PROFILE="${LOAD_PROFILE:-staged}"
BATCH_SIZE="${BATCH_SIZE:-1}"
RECORD_CLUSTER="${RECORD_CLUSTER:-1}"
LOCUST_PROCESSES="${LOCUST_PROCESSES:--1}"
PYTHON="${PYTHON:-python3}"

if [[ -z "${TARGET_HOST:-}" ]]; then
  if [[ "${RUNNER}" == "cluster" ]]; then
    TARGET_HOST="http://resnet-service.${NAMESPACE}.svc.cluster.local"
  else
    IP="$(kubectl -n "${NAMESPACE}" get svc resnet-service -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
    [[ -n "${IP}" ]] || { echo "resnet-service has no external IP; set TARGET_HOST" >&2; exit 1; }
    TARGET_HOST="http://${IP}"
  fi
fi

if [[ -z "${NODE_SELECTOR+x}" && "${RECORD_CLUSTER}" == "1" ]]; then
  NODE_SELECTOR=""
  if [[ -n "$(kubectl get nodes -l cloud.google.com/gke-nodepool=ml-pool -o name 2>/dev/null)" ]]; then
    NODE_SELECTOR="cloud.google.com/gke-nodepool=ml-pool"
  fi
fi

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${ROOT}/results/${RUN_ID}-${LOAD_PROFILE}-b${BATCH_SIZE}${RUN_LABEL:+-${RUN_LABEL}}"
mkdir -p "${RUN_DIR}"
echo "Target:  ${TARGET_HOST} (runner: ${RUNNER})"
echo "Profile: ${LOAD_PROFILE}, batch ${BATCH_SIZE}"
echo "Output:  ${RUN_DIR}"

RECORDER_PID=""
POD=""
cleanup() {
  if [[ -n "${RECORDER_PID}" ]]; then
    kill "${RECORDER_PID}" 2>/dev/null || true
    wait "${RECORDER_PID}" 2>/dev/null || true
  fi
  if [[ -n "${POD}" ]]; then
    kubectl -n loadgen delete pod "${POD}" --wait=false >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ "${RECORD_CLUSTER}" == "1" ]]; then
  kubectl -n "${NAMESPACE}" get hpa,deploy,pods -o wide > "${RUN_DIR}/cluster_before.txt" 2>&1 || true
  "${PYTHON}" "${ROOT}/scripts/record_cluster_metrics.py" \
    --namespace "${NAMESPACE}" --node-selector "${NODE_SELECTOR:-}" \
    --output "${RUN_DIR}/cluster.csv" --interval 10 &
  RECORDER_PID=$!
fi

LOCUST_ARGS=(
  -f locustfile.py --headless --host "${TARGET_HOST}" --processes "${LOCUST_PROCESSES}"
  --csv-full-history --only-summary
)

# Locust exits non-zero if any request failed, which is expected under stress;
# the summary below reports the error rate instead.
locust_rc=0
if [[ "${RUNNER}" == "cluster" ]]; then
  : "${LOADGEN_IMAGE:?set LOADGEN_IMAGE (built from docker/loadgen.Dockerfile)}"
  POD="locust-$(echo "${RUN_ID}" | tr '[:upper:]' '[:lower:]')"
  kubectl apply -f "${ROOT}/kubernetes/loadgen/namespace.yaml" >/dev/null
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${POD}
  namespace: loadgen
  labels:
    app: locust
spec:
  restartPolicy: Never
  nodeSelector:
    cloud.google.com/gke-nodepool: loadgen
  tolerations:
    - key: dedicated
      value: loadgen
      effect: NoSchedule
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: locust
      image: ${LOADGEN_IMAGE}
      command: ["bash", "-c"]
      # Keep the pod alive after the run so the results can be copied out.
      args:
        - >-
          set -o pipefail;
          locust ${LOCUST_ARGS[*]} --csv /results/locust --html /results/report.html
          2>&1 | tee /results/locust.log;
          echo \$? > /results/exit_code;
          sleep 3600
      env:
        - {name: LOAD_PROFILE, value: "${LOAD_PROFILE}"}
        - {name: BATCH_SIZE, value: "${BATCH_SIZE}"}
        - {name: USER_SCALE, value: "${USER_SCALE:-1.0}"}
        - {name: WAIT_MIN, value: "${WAIT_MIN:-5}"}
        - {name: WAIT_MAX, value: "${WAIT_MAX:-15}"}
        - {name: RECONNECT_EVERY, value: "${RECONNECT_EVERY:-1}"}
      resources:
        requests: {cpu: "2", memory: 2Gi}
        limits: {cpu: "3", memory: 4Gi}
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - {name: results, mountPath: /results}
  volumes:
    - name: results
      emptyDir: {}
EOF
  kubectl -n loadgen wait --for=condition=Ready "pod/${POD}" --timeout=300s >/dev/null
  # Follow the logs until Locust finishes (the container itself keeps running).
  kubectl -n loadgen logs -f "${POD}" &
  LOGS_PID=$!
  until kubectl -n loadgen exec "${POD}" -- test -f /results/exit_code 2>/dev/null; do sleep 10; done
  kill "${LOGS_PID}" 2>/dev/null || true
  locust_rc="$(kubectl -n loadgen exec "${POD}" -- cat /results/exit_code)"
  kubectl -n loadgen exec "${POD}" -- tar cf - -C /results . | tar xf - -C "${RUN_DIR}"
else
  (
    cd "${ROOT}/locust"
    LOAD_PROFILE="${LOAD_PROFILE}" BATCH_SIZE="${BATCH_SIZE}" locust "${LOCUST_ARGS[@]}" \
      --csv "${RUN_DIR}/locust" --html "${RUN_DIR}/report.html"
  ) 2>&1 | tee "${RUN_DIR}/locust.log" || locust_rc=$?
fi

cleanup
RECORDER_PID=""
POD=""
if [[ "${RECORD_CLUSTER}" == "1" ]]; then
  kubectl -n "${NAMESPACE}" get hpa,deploy,pods -o wide > "${RUN_DIR}/cluster_after.txt" 2>&1 || true
  kubectl -n "${NAMESPACE}" get events --sort-by=.lastTimestamp > "${RUN_DIR}/events.txt" 2>&1 || true
fi

"${PYTHON}" "${ROOT}/scripts/summarize_results.py" "${RUN_DIR}"
echo "Locust exit code: ${locust_rc} (non-zero when any request failed)"
echo "HTML report: ${RUN_DIR}/report.html"
