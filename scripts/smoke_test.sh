#!/usr/bin/env bash
# Post-deploy smoke test: model is AVAILABLE, a known image classifies
# correctly, and a bad payload is rejected with 400. Needs only curl + python3.
#
#   scripts/smoke_test.sh [ENDPOINT]     # default: the resnet-service LoadBalancer IP
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-ml-serving}"
MODEL="${MODEL_NAME:-resnet101}"
PAYLOAD="${ROOT}/locust/test_payload.json"   # grace_hopper.jpg
EXPECTED_LABEL="${EXPECTED_LABEL:-military_uniform}"
READY_TIMEOUT="${READY_TIMEOUT:-120}"

ENDPOINT="${1:-}"
if [[ -z "${ENDPOINT}" ]]; then
  IP="$(kubectl -n "${NAMESPACE}" get svc resnet-service -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
  [[ -n "${IP}" ]] || { echo "resnet-service has no external IP yet" >&2; exit 1; }
  ENDPOINT="http://${IP}"
fi
ENDPOINT="${ENDPOINT%/}"
MODEL_URL="${ENDPOINT}/v1/models/${MODEL}"

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "Smoke testing ${MODEL_URL}"

# 1. Model status (retry: a fresh LoadBalancer can take a minute to program).
deadline=$((SECONDS + READY_TIMEOUT))
until status="$(curl -fsS --max-time 5 "${MODEL_URL}" 2>/dev/null)" && [[ "${status}" == *'"AVAILABLE"'* ]]; do
  (( SECONDS < deadline )) || fail "model not AVAILABLE after ${READY_TIMEOUT}s"
  sleep 3
done
echo "ok   model status AVAILABLE"

# 2. Known image -> expected top-1 label.
response="$(curl -fsS --max-time 30 -H 'Content-Type: application/json' -d @"${PAYLOAD}" "${MODEL_URL}:predict")" \
  || fail "predict request failed"
top1="$(python3 -c 'import json,sys; p=json.load(sys.stdin)["predictions"][0]; print(p["labels"][0], round(p["scores"][0], 3))' <<<"${response}")" \
  || fail "unexpected response: ${response:0:300}"
[[ "${top1%% *}" == "${EXPECTED_LABEL}" ]] || fail "expected ${EXPECTED_LABEL}, got ${top1}"
echo "ok   predict: ${top1}"

# 3. Invalid payload is a client error, not a crash or a 5xx.
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 -H 'Content-Type: application/json' \
  -d '{"instances": [{"b64": "bm90IGFuIGltYWdl"}]}' "${MODEL_URL}:predict")"
[[ "${code}" == "400" ]] || fail "invalid image returned HTTP ${code}, expected 400"
echo "ok   invalid payload rejected (400)"

echo "Smoke test passed"
