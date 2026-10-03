"""Health, metadata and metrics of a running server, plus checks on the
deployed Kubernetes objects.

    RESNET_ENDPOINT=http://localhost:8501 pytest -m integration
    K8S_TESTS=1 pytest -m k8s          # uses the current kubectl context
"""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest
import yaml

# --- server -------------------------------------------------------------------


@pytest.mark.integration
def test_model_status_available(client):
    versions = client.model_status()["model_version_status"]
    assert [v["state"] for v in versions] == ["AVAILABLE"]
    assert versions[0]["version"] == "1"


@pytest.mark.integration
def test_metadata_lists_both_signatures(client):
    signatures = client.metadata()["metadata"]["signature_def"]["signature_def"]
    default = signatures["serving_default"]
    assert set(default["inputs"]) == {"image_bytes"}
    assert set(default["outputs"]) == {"classes", "labels", "scores"}
    assert set(signatures["predict_pixels"]["inputs"]) == {"images"}


@pytest.mark.integration
def test_prometheus_metrics_match_podmonitoring_relabeling(client, sample_image_bytes, root):
    """Dashboards and alerts depend on these exact metric names."""
    client.predict_images([sample_image_bytes])
    metrics = client.prometheus_metrics()
    names = set(re.findall(r"^(:tensorflow:serving:[a-z_:]+)", metrics, re.MULTILINE))
    assert ":tensorflow:serving:request_count" in names
    assert ":tensorflow:serving:request_latency_bucket" in names
    assert 'API="predict"' in metrics

    podmonitoring = yaml.safe_load((root / "kubernetes" / "podmonitoring.yaml").read_text())
    rules = podmonitoring["spec"]["endpoints"][0]["metricRelabeling"]
    renamed = {
        re.sub(rule["regex"], rule["replacement"].replace("${1}", r"\1"), name)
        for name in names
        for rule in rules
        if rule["action"] == "replace" and re.fullmatch(rule["regex"], name)
    }
    assert {"tfserving_request_count", "tfserving_request_latency_bucket"} <= renamed


# --- deployed cluster -----------------------------------------------------------

NAMESPACE = os.getenv("K8S_NAMESPACE", "ml-serving")


@pytest.fixture(scope="module")
def kubectl():
    if os.getenv("K8S_TESTS") != "1":
        pytest.skip("K8S_TESTS=1 not set")

    def run(*args: str) -> dict:
        out = subprocess.run(
            ["kubectl", "-n", NAMESPACE, *args, "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(out.stdout)

    return run


@pytest.mark.k8s
def test_deployment_has_ready_replicas(kubectl):
    deployment = kubectl("get", "deployment", "resnet-serving")
    assert deployment["status"].get("readyReplicas", 0) >= 2


@pytest.mark.k8s
def test_service_has_external_ip(kubectl):
    service = kubectl("get", "service", "resnet-service")
    ingress = service["status"].get("loadBalancer", {}).get("ingress", [])
    assert ingress and ingress[0].get("ip")


@pytest.mark.k8s
def test_hpa_targets_deployment_and_reads_cpu(kubectl):
    hpa = kubectl("get", "hpa", "resnet-hpa")
    assert hpa["spec"]["scaleTargetRef"]["name"] == "resnet-serving"
    metrics = hpa["status"].get("currentMetrics") or []
    assert any(m.get("resource", {}).get("name") == "cpu" for m in metrics), (
        "HPA has no CPU reading; is metrics-server healthy?"
    )


@pytest.mark.k8s
def test_pdb_allows_one_disruption(kubectl):
    pdb = kubectl("get", "pdb", "resnet-pdb")
    assert pdb["status"]["disruptionsAllowed"] >= 1


@pytest.mark.k8s
def test_pods_are_not_crash_looping(kubectl):
    pods = kubectl("get", "pods", "-l", "app=resnet-serving")["items"]
    assert pods
    for pod in pods:
        for status in pod["status"].get("containerStatuses", []):
            assert status["restartCount"] < 3, f"{pod['metadata']['name']} keeps restarting"
