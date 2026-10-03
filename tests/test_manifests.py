"""Static checks on the Kubernetes manifests, Terraform settings and monitoring
definitions: design decisions that should fail CI if someone undoes them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "kubernetes"

# Rough schedulable CPU per node after GKE reservations (3.92 of 4 vCPU) and
# system DaemonSets/Deployments (~0.4 vCPU).
USABLE_CPU_PER_NODE = {"e2-standard-4": 3.5, "e2-standard-8": 7.4, "n2-standard-4": 3.5}


def _load(name: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all((K8S / name).read_text()) if doc]


def _one(name: str) -> dict:
    (doc,) = _load(name)
    return doc


def _cpu(value: str) -> float:
    return float(value[:-1]) / 1000 if value.endswith("m") else float(value)


@pytest.fixture(scope="module")
def deployment() -> dict:
    return _one("deployment.yaml")


@pytest.fixture(scope="module")
def container(deployment) -> dict:
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    return container


@pytest.fixture(scope="module")
def tfvars() -> dict[str, str]:
    text = (ROOT / "terraform" / "terraform.tfvars").read_text()
    return dict(re.findall(r'^\s*(\w+)\s*=\s*"?([^"\n#]+?)"?\s*(?:#.*)?$', text, re.MULTILINE))


def test_kustomization_includes_every_manifest():
    kustomization = yaml.safe_load((K8S / "kustomization.yaml").read_text())
    manifests = {p.name for p in K8S.glob("*.yaml")} - {"kustomization.yaml"}
    assert set(kustomization["resources"]) == manifests


def test_image_is_pinned(container):
    image = container["image"]
    assert ":" in image.rsplit("/", 1)[-1], "image needs an explicit tag"
    assert not image.endswith(":latest")
    # deploy.sh rewrites the image by this logical name.
    kustomization = yaml.safe_load((K8S / "kustomization.yaml").read_text())
    assert kustomization["images"][0]["name"] == image.split(":")[0]
    assert "resnet101-serving" in (ROOT / "scripts" / "deploy.sh").read_text()


def test_replicas_left_to_the_hpa(deployment):
    assert "replicas" not in deployment["spec"]


def test_rolling_update_never_reduces_capacity(deployment):
    rolling = deployment["spec"]["strategy"]["rollingUpdate"]
    assert rolling["maxUnavailable"] == 0
    assert rolling["maxSurge"] >= 1


def test_resources_are_set(container):
    resources = container["resources"]
    for kind in ("requests", "limits"):
        assert {"cpu", "memory"} <= set(resources[kind])


def test_tf_threads_match_cpu_limit(container):
    env = _load("configmap.yaml")[0]["data"]
    limit = _cpu(container["resources"]["limits"]["cpu"])
    assert int(env["TF_INTRA_OP_PARALLELISM"]) == limit
    assert int(env["TF_INTER_OP_PARALLELISM"]) == limit


def test_probes(container):
    # Traffic only starts once the model is loaded and warmed up...
    assert container["startupProbe"]["httpGet"]["path"] == "/v1/models/resnet101"
    # ...but readiness/liveness must not depend on the saturable REST thread
    # pool, or overload removes/restarts healthy pods (see deployment.yaml).
    for probe in ("readinessProbe", "livenessProbe"):
        assert "httpGet" not in container[probe], probe
        assert container[probe]["timeoutSeconds"] > 1


def test_graceful_shutdown(deployment, container):
    sleep = container["lifecycle"]["preStop"]["exec"]["command"]
    assert sleep[0] == "sleep"
    assert deployment["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] > int(sleep[1])


def test_replicas_are_spread_across_nodes(deployment):
    (spread,) = deployment["spec"]["template"]["spec"]["topologySpreadConstraints"]
    assert spread["topologyKey"] == "kubernetes.io/hostname"
    assert spread["whenUnsatisfiable"] == "DoNotSchedule"  # soft spreading failed on GKE
    # Otherwise the tainted loadgen node counts as an empty domain and every
    # replica after the first needs a new node.
    assert spread["nodeTaintsPolicy"] == "Honor"


def test_pod_security(deployment, container):
    pod = deployment["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["automountServiceAccountToken"] is False
    ctx = container["securityContext"]
    assert ctx["allowPrivilegeEscalation"] is False
    assert ctx["readOnlyRootFilesystem"] is True
    assert ctx["capabilities"]["drop"] == ["ALL"]


def test_selectors_match_pod_labels(deployment, container):
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    service = _one("service.yaml")
    assert service["spec"]["selector"].items() <= labels.items()
    port_names = {p["name"] for p in container["ports"]}
    assert service["spec"]["ports"][0]["targetPort"] in port_names
    assert _one("pdb.yaml")["spec"]["selector"]["matchLabels"].items() <= labels.items()
    netpol = _one("networkpolicy.yaml")
    assert netpol["spec"]["podSelector"]["matchLabels"].items() <= labels.items()


def test_hpa(deployment):
    hpa = _one("hpa.yaml")
    assert hpa["spec"]["scaleTargetRef"]["name"] == deployment["metadata"]["name"]
    assert hpa["spec"]["minReplicas"] >= 2, "one replica is a single point of failure"
    assert hpa["spec"]["maxReplicas"] > hpa["spec"]["minReplicas"]


def test_max_replicas_require_cluster_autoscaler(container, tfvars):
    """HPA max must not fit on the minimum node count (else node scaling is
    never exercised) but must fit on the maximum (else pods stay Pending)."""
    hpa = _one("hpa.yaml")
    per_node = USABLE_CPU_PER_NODE[tfvars["machine_type"]]
    pods_per_node = int(per_node // _cpu(container["resources"]["requests"]["cpu"]))
    max_replicas = hpa["spec"]["maxReplicas"]
    assert max_replicas > int(tfvars["min_node_count"]) * pods_per_node
    assert max_replicas <= int(tfvars["max_node_count"]) * pods_per_node


def test_pdb_scales_with_replicas():
    assert _one("pdb.yaml")["spec"]["maxUnavailable"] == 1


# --- monitoring ---------------------------------------------------------------

MONITORING = ROOT / "monitoring"
TEMPLATE_VARS = {
    "cluster_name": "c",
    "namespace": "ml-serving",
    "max_node_count": "5",
    "node_pool": "ml-pool",
}


def _render(path: Path) -> str:
    text = path.read_text()
    unknown = set(re.findall(r"\$\{(\w+)\}", text)) - TEMPLATE_VARS.keys()
    assert not unknown, f"{path.name}: undefined template variables {unknown}"
    return re.sub(r"\$\{(\w+)\}", lambda m: TEMPLATE_VARS[m.group(1)], text)


def _relabeled_metrics() -> set[str]:
    rules = _one("podmonitoring.yaml")["spec"]["endpoints"][0]["metricRelabeling"]
    tf_serving = [
        ":tensorflow:serving:request_count",
        ":tensorflow:serving:request_latency_bucket",
        ":tensorflow:serving:request_latency_count",
        ":tensorflow:serving:request_latency_sum",
        ":tensorflow:serving:runtime_latency_bucket",
    ]
    out = set()
    for name in tf_serving:
        for rule in rules:
            if rule["action"] == "replace" and re.fullmatch(rule["regex"], name):
                out.add(re.sub(rule["regex"], rule["replacement"].replace("${1}", r"\1"), name))
                break
    return out


def _referenced_tfserving_metrics(text: str) -> set[str]:
    return set(re.findall(r"\btfserving_[a-z_]+", text))


def test_dashboard_is_valid_and_uses_scraped_metrics():
    text = _render(MONITORING / "dashboards" / "resnet-serving.json")
    dashboard = json.loads(text)
    assert dashboard["mosaicLayout"]["tiles"]
    # The Monitoring API rejects color/direction on XY chart thresholds, and
    # drops zero-valued positions (which shows up as a permanent Terraform diff).
    for tile in dashboard["mosaicLayout"]["tiles"]:
        assert tile.get("xPos", 1) != 0 and tile.get("yPos", 1) != 0
        for threshold in tile["widget"].get("xyChart", {}).get("thresholds", []):
            assert not {"color", "direction"} & threshold.keys()
    assert _referenced_tfserving_metrics(text) <= _relabeled_metrics()


ALERT_FILES = sorted((MONITORING / "alerts").glob("*.yaml"))


@pytest.mark.parametrize("path", ALERT_FILES, ids=lambda p: p.stem)
def test_alert_policy(path):
    text = _render(path)
    alert = yaml.safe_load(text)
    assert {"displayName", "severity", "duration", "query", "documentation"} <= alert.keys()
    assert alert["severity"] in {"CRITICAL", "ERROR", "WARNING"}
    assert re.fullmatch(r"\d+s", alert["duration"])
    assert _referenced_tfserving_metrics(alert["query"]) <= _relabeled_metrics()
