# syntax=docker/dockerfile:1
#
# Locust load generator, for running load tests from inside the VPC
# (scripts/load_test.sh with RUNNER=cluster). Needed where an organization
# firewall policy blocks internet ingress to external load balancers, and
# useful anywhere to keep the client close to the cluster.
#
#   docker build -t loadgen -f docker/loadgen.Dockerfile .

FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir "$(grep '^locust==' requirements.txt)"

COPY locust/ locust/
COPY scripts/smoke_test.sh scripts/

USER 1000:1000
WORKDIR /app/locust
ENTRYPOINT ["locust"]
