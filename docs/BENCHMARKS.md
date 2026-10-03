# Benchmarks

Every number in this repository comes from one of the runs below, and each run states its
environment. Reproduce any of them with the commands in
[Reproducing](#9-reproducing); raw outputs land in `results/` (git-ignored).

## Contents

1. [Methodology](#1-methodology)
2. [Single pod](#2-single-pod)
3. [Configuration experiments](#3-configuration-experiments)
4. [Local Kubernetes: HPA behaviour and two fixes](#4-local-kubernetes-hpa-behaviour-and-two-fixes)
5. [GKE: staged load test](#5-gke-staged-load-test)
6. [GKE: scale-down](#6-gke-scale-down)
7. [GKE: resilience](#7-gke-resilience)
8. [Monitoring cross-check](#8-monitoring-cross-check)
9. [Reproducing](#9-reproducing)

---

## 1. Methodology

**Workload.** Each request carries one 512×600 JPEG (~80 KB as base64 JSON) to the
`serving_default` signature, which decodes, resizes, classifies and returns the top 5. Locust
`FastHttpUser` reads and serializes the payload once, then validates every response (HTTP 200,
well-formed predictions, one per image). Anything else counts as a failure.

**Think time: 5-15 s per user.** A 2-CPU pod handles ~5.2 images/s (§2). With the 0.1-1 s think
time typical of web tests, 10 users already saturate two pods, and 500 users would offer
~900 req/s to a cluster that peaks at ~23. At ~10 s per user, 10 → 500 users offer ~1 → ~50
req/s: idle, HPA scale-out, node scale-out, then saturation. Recalibrate with `WAIT_MIN`,
`WAIT_MAX` and `USER_SCALE` for other hardware.

**One TCP connection per request** (`RECONNECT_EVERY=1`). The Service is an L4 load balancer,
which balances connections rather than requests. With keep-alive, users stay pinned to the pods
that existed when they connected (§4 shows how badly that distorts results).

**Profiles** (`locust/profiles.py`, duration × users):

| Profile | Stages |
|---|---|
| `staged` | 2 min × 10 → 3 min × 50 → 5 min × 100 → 5 min × 300 → 5 min × 500 → 5 min × 50 |
| `performance` | 3 min × 100 → 5 min × 300 → 5 min × 500 → 5 min × 1000 |
| `spike` | 1 min × 10 → 5 min × 300 → 4 min × 10 |
| `smoke` | 1 min × 5 |

**Cluster side.** `scripts/record_cluster_metrics.py` samples HPA current/desired replicas and
CPU, ready and Pending pods, nodes (serving pool only) and pod CPU/memory every 10 s.
`scripts/summarize_results.py` joins this with Locust's history on timestamps:

- Stages are plateaus of constant user count lasting ≥ 30 s.
- **RPS and error rates are exact** (deltas of Locust's cumulative counters).
- **Percentiles** are the median of Locust's rolling ~10 s percentiles over the second half of
  each stage, after autoscaling had time to react.

Server-side TF Serving histograms are also available, but their buckets grow by ×1.8
(e.g. 340 → 612 ms), so client-side percentiles are the precise ones.

## 2. Single pod

Docker on an 8-vCPU Intel Xeon @ 2.2 GHz VM, container limited to **2 CPUs and 4 GiB** (the pod
limits), TF threads pinned to 2, closed loop with no think time.

| Concurrent clients | Images/s | P50 | P95 | P99 | Errors |
|---|---|---|---|---|---|
| 1 | 4.78 | 210 ms | 220 ms | 230 ms | 0 % |
| 2 | **5.22** | 380 ms | 410 ms | 410 ms | 0 % |
| 4 | 5.20 | 770 ms | 1,100 ms | 1,200 ms | 0 % |
| 8 | 5.05 | 1,500 ms | 2,700 ms | 3,400 ms | 0 % |

- **A pod saturates at ~5.2 images/s.** Beyond that, concurrency only adds queueing, so latency
  SLOs require scaling out rather than more threads.
- **Memory ~450 MiB** under load (0.50-0.59 GiB on GKE).
- **Cold start 18 s** from container start to serving, of which 10.3 s is warmup (batch 1 and 16,
  both signatures), so new pods never serve a slow first request.
- The same measurement on a GKE `e2-standard-4` node gives P50 230 ms at low load, so the VM is
  representative.

## 3. Configuration experiments

| Experiment | Result | Decision |
|---|---|---|
| **TF thread pools**: pinned to the CPU limit vs TF defaults (sized from the node's 8 cores) | pinned: **5.2 images/s, 210 ms**; defaults: 2.9 images/s, 390 ms. That's **1.8× throughput**, lost to CFS throttling when threads exceed the CPU limit | `TF_INTRA_OP_PARALLELISM = TF_INTER_OP_PARALLELISM = 2` in the ConfigMap; a policy test keeps it equal to the limit |
| **Batch 16 per request** vs single images | 0.35 req/s × 16 = **5.6 images/s (+8 %)**, but **5.7 s per request** | single-image requests by default; batching mostly pays off on GPUs |
| **Duplicated weights** (`ExportArchive.track()`) | variables 342 MB vs 171 MB; RSS ~700 vs ~450 MiB; same throughput | `track()` removed; a test fails if the variables exceed 200 MB |
| **CPU request** 500m / 1 / 2 | by capacity arithmetic (README): 500m never triggers node scaling at 10 pods; 2 CPU strands pods at 5 nodes | **1 CPU**; a policy test encodes the invariant |

## 4. Local Kubernetes: HPA behaviour and two fixes

Same manifests and tooling on a single-node kind cluster (8 vCPU), using the `spike` profile at
`USER_SCALE=0.5` (5 → 150 → 5 users). There's no cluster autoscaler, so this checks behaviour,
not capacity. The first run exposed two problems:

| 150-user stage | Run 1: keep-alive (reconnect every 20 requests), HTTP readiness | Run 2: reconnect per request, TCP readiness |
|---|---|---|
| Throughput | 7.85 req/s | **10.06 req/s (+28 %)** |
| P50 / P95 | 9.0 s / 12.0 s | **2.3 s** / 9.2 s |
| Total pod CPU | flat at **4,000m** = 2 pods × 2-CPU limit | ~7,300m across all pods |
| HPA desired (max) | 7; the **66 % average** looked healthy | **10**, the correct signal |
| Ready pods | **dropped 2 → 1** during overload | only increased |
| Pending pods (max) | 1 | 4 (the cluster autoscaler's trigger on GKE) |
| Errors | 0 % | 0 % |

- **Connection pinning.** Per-pod counters after run 1: an original pod served **1,291**
  predictions, while a pod added by the HPA served **2** over the same four minutes. The HPA
  averages CPU across pods, so two saturated pods and four idle ones read as "66 %, fine".
- **Readiness cascade.** The HTTP readiness probe waited in the same queue as inference requests,
  timed out, and removed a healthy pod, which pushed its share onto the remaining pod. Readiness
  and liveness are now TCP checks. The startup probe still gates traffic on the model being
  AVAILABLE, and because the model is baked in, that state can't regress later.

## 5. GKE: staged load test

GKE 1.35.8, zonal, `ml-pool` of `e2-standard-4` nodes (2-5), HPA 2-10 pods at 60 % CPU, image
`resnet101:1.0.0`. Locust ran in-cluster on the dedicated `loadgen` node (`RUNNER=cluster`), with
the `staged` profile, 5-15 s think time and one connection per request.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/gke-staged-scaling-dark.png">
  <img alt="Four panels over 25 minutes: users step from 10 to 500 and back to 50; throughput plateaus at about 23 per second; ready pods rise from 2 to the HPA maximum of 10 while nodes rise from 2 to 4; P95 latency stays near 500 ms until the 300-user stage, then rises to 10-30 s, and recovers to below 500 ms." src="images/gke-staged-scaling-light.png">
</picture>

| Stage | Users | Images/s | P50 | P95 | P99 | Errors | Pods (max ready) | Nodes | CPU (% of request) |
|---|---|---|---|---|---|---|---|---|---|
| Baseline | 10 | 0.99 | 220 ms | 280 ms | 280 ms | 0 % | 2 | 2 | 16 % |
| Low | 50 | 4.88 | 290 ms | 560 ms | 690 ms | 0 % | 4 | 2 | 67 % |
| Medium | 100 | 9.55 | 320 ms | 660 ms | 800 ms | 0 % | 9 | **4** | 89 % |
| High | 300 | 22.19 | 1.6 s | 11 s | 14 s | 0 % | **10 (max)** | 4 | 112 % |
| Stress | 500 | 23.40 | 13 s | 26 s | 29 s | 0.22 % | 10 | 4 | 121 % |
| Recovery | 50 | 4.78 | 240 ms | 420 ms | 480 ms | 0 % | 10 → 5 | 4 | 35 % |

**Scaling timeline** (stages change at t+120, 300, 600, 900 and 1200 s):

```
t+ 148s  HPA desired 2 -> 4                       (50 users)
t+ 168s  ready pods 2 -> 4                        20 s from decision to serving
t+ 320s  HPA desired 4 -> 6 -> 8, 3 pods Pending  (100 users: 2 nodes hold 6 pods)
t+ 360s  nodes 2 -> 3                             Pending -> new node in 40 s
t+ 380s  Pending 3 -> 0
t+ 441s  HPA desired 9, 1 Pending
t+ 481s  nodes 3 -> 4; t+511s Pending -> 0        70 s from Pending to running, node included
t+ 633s  HPA desired 10 (maxReplicas)             (300 users)
t+1439s  HPA desired 10 -> 8 -> 5                 214 s after the load dropped
```

**Conclusions**

- **Both autoscaling layers work.** Pods went 2 → 10 and nodes 2 → 4. Pods waited for new nodes
  for only 60-70 s; image streaming starts the 1.4 GB image without a full pull.
- **Capacity is ~23 images/s.** That's below 10 × 5.2 because 3 pods share each 4-vCPU node,
  giving each ~1.2 cores rather than its 2-core limit. Going from 300 to 500 users adds almost no
  throughput, so this is the plateau.
- **The binding limit under stress was `maxReplicas: 10`, not node capacity.** The 10 pods fit on
  4 of the 5 allowed nodes.
- **The error SLO (< 1 %) held at ~2× overload** (0.22 %, all client timeouts at 30 s). No pod
  restarted, and the load-tolerant probes kept saturated pods serving.
- **P95 < 500 ms holds up to ~5 images/s.** At ~10 images/s P95 is 660 ms with CPU at only 89 % of
  request: bursting pods contend for each node's 4 vCPUs. **Operating point: ~10 images/s at
  P95 ≈ 0.7 s; peak ~23 images/s.** How to move it is in [EXTENSIONS.md](EXTENSIONS.md).

## 6. GKE: scale-down

From the cluster-autoscaler visibility logs in Cloud Logging, after the run:

- The HPA reached its 2-replica minimum ~4 minutes after the load ended.
- The autoscaler removed nodes **one at a time**, 4 → 3 → 2, about 16 and 27 minutes after the
  pods scaled in. That's `BALANCED`'s ~10 minutes of unneeded time per node.
- While one node drained, the logs show `no.scale.down.node.pod.not.enough.pdb` for the next: the
  PDB (`maxUnavailable: 1`) held the second removal until the moved serving pod was ready again.
- It removed an *original* node rather than a new one. The autoscaler picks whichever node is
  cheapest to empty.

## 7. GKE: resilience

`scripts/chaos_test.sh`, with a probe sending one request every 0.5 s from inside the VPC:

| Test | Result |
|---|---|
| Force-kill a serving pod (`--grace-period=0`) | replacement ready in **22 s** (SLO 60 s), measured twice; **45/45** client requests succeeded |
| Kill a pod, then drain a node running a serving pod | replicas restored **49 s** after the drain began; **107/107** requests succeeded |
| Placement after autoscaler scale-down | found **both replicas on one node** (soft `ScheduleAnyway` spread). Fixed with `DoNotSchedule`, which then exposed a second issue: with the default `nodeTaintsPolicy: Ignore`, the tainted `loadgen` node counted as an empty domain, so every new pod demanded a new node. With `nodeTaintsPolicy: Honor`, 6 replicas spread **2/2/2** across 3 nodes with nothing Pending and no node added |

## 8. Monitoring cross-check

During the 300-user stage (t ≈ 14 min), Cloud Monitoring reported **22.5 predictions/s**
(Managed Prometheus, `tfserving_request_latency_count`), CPU at **122 % of request**
(`kubernetes.io/container/cpu/request_utilization`) and HPA desired = **10** (kube-state-metrics).
That matches Locust (22.2 req/s) and the HPA's own reading (117-122 %). All 12 query families
behind the dashboard and alerts were checked against the live Prometheus API. System metrics lag
by a few minutes, so the dashboard trails the HPA slightly.

## 9. Reproducing

```bash
# Single pod (§2): run-local, then a closed loop at N clients
make build-local run-local
cd locust && WAIT_MIN=0 WAIT_MAX=0 locust -f locustfile.py --headless \
  --host http://localhost:8501 -u 2 -r 2 -t 60s --only-summary

# Batch experiment (§3)
BATCH_SIZE=16 WAIT_MIN=0 WAIT_MAX=0 locust -f locustfile.py --headless --host http://localhost:8501 -u 2 -r 2 -t 60s

# kind (§4): see DEPLOYMENT.md §4, then
TARGET_HOST=http://<NODE_IP>:<NODE_PORT> LOAD_PROFILE=spike USER_SCALE=0.5 make load-test
RECONNECT_EVERY=20 ...                         # to reproduce the pinning problem

# GKE (§5-§7)
make push-loadgen
RUNNER=cluster make load-test                  # staged profile, ~25 min
RUNNER=cluster DRAIN_NODE=1 make chaos-test
make plot RUN=results/<run>                    # the chart above
```
