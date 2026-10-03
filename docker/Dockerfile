# syntax=docker/dockerfile:1
#
# TensorFlow Serving image with ResNet101 baked in.
#
#   docker build -t resnet101-serving -f docker/Dockerfile .                           # export model in the build
#   docker build -t resnet101-serving -f docker/Dockerfile --build-arg MODEL_SOURCE=local .  # reuse model/resnet101
#
# The default (MODEL_SOURCE=export) builds the image from the repository alone,
# which is what CI/CD uses: model artifacts are never committed to git.

ARG TF_SERVING_VERSION=2.21.0
ARG PYTHON_VERSION=3.12
ARG MODEL_SOURCE=export

# ---------------------------------------------------------------------------
# Export the SavedModel (weights are downloaded and hash-checked by Keras).
FROM python:${PYTHON_VERSION}-slim AS model-export
WORKDIR /build
COPY requirements-model.txt .
RUN pip install --no-cache-dir -r requirements-model.txt
COPY model/download_model.py model/export_model.py model/
RUN python model/download_model.py \
 && python model/export_model.py --output-dir /export/resnet101 --version 1

# ---------------------------------------------------------------------------
# Use a model exported locally with `make export-model`.
FROM scratch AS model-local
COPY model/resnet101 /export/resnet101

FROM model-${MODEL_SOURCE} AS model

# ---------------------------------------------------------------------------
FROM tensorflow/serving:${TF_SERVING_VERSION}

ENV MODEL_NAME=resnet101 \
    MODEL_BASE_PATH=/models \
    MONITORING_CONFIG_FILE=/etc/tfserving/monitoring.config \
    BATCHING_PARAMETERS_FILE=/etc/tfserving/batching_parameters.txt

COPY --from=model /export/resnet101 /models/resnet101
COPY docker/monitoring.config /etc/tfserving/monitoring.config
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/serving-entrypoint.sh

# Nothing in the server needs root; Kubernetes enforces this as well.
USER 1000:1000

# 8500: gRPC, 8501: REST + Prometheus metrics
EXPOSE 8500 8501
ENTRYPOINT ["/usr/local/bin/serving-entrypoint.sh"]
