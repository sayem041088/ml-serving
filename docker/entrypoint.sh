#!/usr/bin/env bash
# Builds the tensorflow_model_server command line from environment variables so
# the same immutable image can be tuned per environment (Kubernetes ConfigMap)
# without a rebuild. Extra arguments are passed through unchanged.
set -euo pipefail

args=(
  --port="${GRPC_PORT:-8500}"
  --rest_api_port="${REST_PORT:-8501}"
  --model_name="${MODEL_NAME}"
  --model_base_path="${MODEL_BASE_PATH}/${MODEL_NAME}"
  # The model is baked into the image, so there is nothing to poll for.
  --file_system_poll_wait_seconds=0
)

# TF sizes its thread pools from the *node's* cores, not the container's CPU
# limit. On a 4-vCPU node with a 2-CPU limit that means CFS throttling, so
# these should match the CPU limit.
if [[ -n "${TF_INTRA_OP_PARALLELISM:-}" ]]; then
  args+=(--tensorflow_intra_op_parallelism="${TF_INTRA_OP_PARALLELISM}")
fi
if [[ -n "${TF_INTER_OP_PARALLELISM:-}" ]]; then
  args+=(--tensorflow_inter_op_parallelism="${TF_INTER_OP_PARALLELISM}")
fi
if [[ -n "${REST_API_NUM_THREADS:-}" ]]; then
  args+=(--rest_api_num_threads="${REST_API_NUM_THREADS}")
fi
if [[ -n "${REST_API_TIMEOUT_IN_MS:-}" ]]; then
  args+=(--rest_api_timeout_in_ms="${REST_API_TIMEOUT_IN_MS}")
fi

# tensorflow_model_server rejects --batching_parameters_file unless batching
# is enabled, so the file is only passed together with the flag.
if [[ "${ENABLE_BATCHING:-false}" == "true" ]]; then
  args+=(--enable_batching=true)
  if [[ -f "${BATCHING_PARAMETERS_FILE:-}" ]]; then
    args+=(--batching_parameters_file="${BATCHING_PARAMETERS_FILE}")
  fi
fi

if [[ -f "${MONITORING_CONFIG_FILE:-}" ]]; then
  args+=(--monitoring_config_file="${MONITORING_CONFIG_FILE}")
fi

echo "Starting tensorflow_model_server ${args[*]} $*"
exec tensorflow_model_server "${args[@]}" "$@"
