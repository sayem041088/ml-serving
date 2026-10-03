# Extensions and trade-offs

What to build next, and what each option costs. The priorities come from what the benchmarks
showed ([BENCHMARKS.md](BENCHMARKS.md)), not from a generic wish list:

- **Capacity tops out at ~23 images/s**, and under stress the limit was HPA `maxReplicas`, not
  nodes.
- **P95 < 500 ms only holds up to ~5 images/s**, because 3 burstable pods contend for each
  4-vCPU node.
- **CPU-based scaling reacts late**, and it was blind to connection pinning (a 66 % average
  hid two saturated pods).
- **The L4 load balancer** balances connections, not requests, and in locked-down projects it
  isn't reachable from the internet at all.

Effort is rough: **S** ≈ hours, **M** ≈ days, **L** ≈ weeks.

## Prioritized roadmap

| # | Extension | Addresses | Impact | Effort |
|---|---|---|---|---|
| 1 | [Raise the scaling ceilings](#11-raise-the-scaling-ceilings) | 23 images/s cap | more peak throughput, linear in cost | S |
| 2 | [Bigger nodes / Guaranteed pods](#12-more-cpu-per-request-bigger-nodes-guaranteed-qos) | P95 at moderate load | lower, steadier latency | S-M |
| 3 | [L7 Gateway with container-native LB](#31-l7-gateway-api-with-container-native-load-balancing) | connection pinning, public ingress, auth | per-request balancing, TLS, WAF | M |
| 4 | [Request-based autoscaling](#21-scale-on-requests-or-concurrency-instead-of-cpu) | late, CPU-only signal | earlier and fairer scaling | M |
| 5 | [Headroom via overprovisioning](#54-hide-node-provisioning-with-overprovisioning) | 40-70 s waits for a new node | bursts absorbed instantly | S |
| 6 | [Model optimization (INT8 / OpenVINO)](#14-make-each-inference-cheaper) | cost per image | 2-4× images per core (typical) | M |
| 7 | [Canary releases with automated analysis](#41-canary-and-blue-green-releases) | deployment risk | safer model/image changes | M |
| 8 | [Admission control / load shedding](#16-admission-control-and-load-shedding) | 30 s queues under overload | fast failures instead of timeouts | S-M |
| 9 | [SLO burn-rate alerting](#53-slo-objects-and-burn-rate-alerts) | threshold alerts are noisy | alerts that track user impact | S |
| 10 | [GPU serving](#15-gpu-serving) | throughput at scale | far higher images/s per node | L |

---

## 1. Capacity and latency

### 1.1 Raise the scaling ceilings

Increase HPA `maxReplicas` (10 → 15+) and keep `max_node_count` in step (3 pods per node). The
stress stage showed 10 pods using only 4 of 5 nodes, so this is the cheapest capacity available.

| Pros | Cons |
|---|---|
| One-line change; capacity scales linearly | Peak cost scales linearly too |
| The sizing policy test keeps HPA and node bounds consistent | Higher worst-case bill if traffic is abusive; pair with rate limiting (§3.3) |

### 1.2 More CPU per request: bigger nodes, Guaranteed QoS

At 10 images/s, P95 was 660 ms with CPU at only 89 % of request: three pods bursting on a 4-vCPU
node slow each other down. Options:

- **Fewer, larger pods**: request = limit = 2 CPU (Guaranteed QoS) on `e2-standard-8` or `n2-standard-8`, with
  the kubelet's **static CPU manager** so each pod gets dedicated cores.
- **Compute-optimized nodes** (C3/C4, newer Intel/AMD with AMX/AVX-512), which run oneDNN kernels
  much faster than E2. E2 is cost-optimized and doesn't guarantee a CPU generation.

| Pros | Cons |
|---|---|
| Predictable latency; no noisy neighbours | Half the pods per node, so more nodes for the same pod count |
| Higher per-core throughput on newer CPUs | C3/N2 cost more per vCPU than E2; larger nodes waste more when partly idle |
| HPA utilization becomes meaningful (no bursting above request) | Static CPU manager requires node-pool kubelet config and integer CPU requests |

### 1.3 A lower HPA target, or scale on latency

A 50 % target starts scaling earlier and keeps more headroom. Scaling directly on P95 latency
(via custom metrics, §2.1) ties capacity to the SLO itself.

| Pros | Cons |
|---|---|
| Better tail latency during ramps | More idle capacity, so higher steady-state cost |
| Latency-based scaling targets what users feel | Latency is a lagging, noisy signal; it can oscillate without careful stabilization windows |

### 1.4 Make each inference cheaper

| Option | Typical gain (CPU) | Trade-off |
|---|---|---|
| **INT8 post-training quantization** | 2-4× throughput | small accuracy loss; needs a calibration set and an accuracy gate (§4.4) |
| **OpenVINO Model Server / ONNX Runtime** | 1.5-3× on Intel CPUs | a different serving runtime and export path; loses TF Serving's native SavedModel workflow |
| **Smaller model** (ResNet50, EfficientNet-B0, MobileNetV3) | 2-8× fewer FLOPs | lower top-1 accuracy (ResNet50 ≈ −1.5 pts vs ResNet101 on ImageNet; MobileNet more) |
| **Mixed precision / bf16 on AMX-capable CPUs** | ~1.5-2× | requires C3/C4/N4-class nodes |

Measure with the same `make load-test` profiles so results stay comparable.

### 1.5 GPU serving

An NVIDIA L4 or T4 node pool with TF Serving GPU (or Triton) and dynamic batching. GPUs reward
batching (CPU batching gained only 8 % here, but GPUs gain much more).

| Pros | Cons |
|---|---|
| An order of magnitude more images/s per node | High cost floor: an idle GPU node is expensive; scale-to-zero means minutes of cold start |
| Lower per-image latency at high load | GPU quota and regional availability; larger image (CUDA) |
| Better cost per 1,000 images *at sustained high load* | Worse cost per image at low or bursty load; needs batching-aware load tests |

### 1.6 Admission control and load shedding

Under ~2× overload, requests queued until TF Serving's 30 s timeout. Two cheap improvements:

- Enable TF Serving batching with a bounded `max_enqueued_batches`. Excess requests then fail
  fast with `UNAVAILABLE` (HTTP 503) instead of waiting 30 s.
- Shorten `REST_API_TIMEOUT_IN_MS` to match client deadlines.

| Pros | Cons |
|---|---|
| Clients fail fast and can retry or degrade; queues stay bounded | More visible errors during overload, by design |
| Protects latency for the requests that are admitted | Thresholds need tuning per pod size; clients need retries with backoff and jitter |

## 2. Autoscaling signals

### 2.1 Scale on requests or concurrency instead of CPU

TF Serving's request metrics are already in Managed Prometheus (`tfserving_request_count`,
`tfserving_request_latency_*`). Expose them to the HPA through the **Custom Metrics Stackdriver
Adapter** and scale on requests/s per pod (e.g. a target of 3) or on in-flight requests.

| Pros | Cons |
|---|---|
| Reacts as soon as traffic arrives, before CPU saturates | One more component (the adapter) to run and secure |
| Per-pod request rate exposes imbalance that average CPU hid | The target must be re-measured whenever pod size or model changes |
| Can combine with CPU (the HPA takes the maximum of its metrics) | Metric pipeline latency (scrape plus ingestion) adds 30-60 s of lag |

### 2.2 KEDA

[KEDA](https://keda.sh) drives the HPA from Prometheus queries, Pub/Sub depth and other sources,
and supports **scale to zero**.

| Pros | Cons |
|---|---|
| Rich triggers without writing an adapter; scale to zero for dev/test | Cold start from zero = node provisioning (40-70 s) + pod start (~20 s) + warmup |
| Event-driven workloads (batch classification from a queue) fit naturally | Another operator in the cluster; overlaps with §2.1 |

### 2.3 Scheduled and predictive scaling

If traffic has daily or weekly shape, raise `minReplicas` ahead of known peaks (KEDA cron scaler,
or a CronJob patching the HPA).

| Pros | Cons |
|---|---|
| No cold capacity during predictable ramps | Wasted capacity if the forecast is wrong; needs historical data |

### 2.4 Right-sizing with VPA recommendations

Run the Vertical Pod Autoscaler in **recommendation-only** mode. Measured memory is ~0.5 GiB
against a 2 GiB request, so there's room to tighten.

| Pros | Cons |
|---|---|
| Data-driven requests; better bin-packing | VPA in auto mode conflicts with a CPU-based HPA, so keep it recommend-only |
| | Lower memory requests reduce headroom for batch experiments |

### 2.5 Node auto-provisioning, ComputeClasses, Autopilot

Let GKE choose machine shapes (node auto-provisioning / custom ComputeClasses with fallback
priorities, e.g. Spot first then on-demand), or move to **Autopilot** and pay per pod.

| Pros | Cons |
|---|---|
| No node-pool sizing to maintain; fallback across machine families | Less control over exact shapes; Autopilot restricts some settings (privileged pods, some node config) |
| Autopilot bills requested pod resources only | Autopilot's per-vCPU price is higher; bursting semantics differ from this design |

## 3. Traffic and networking

### 3.1 L7 Gateway API with container-native load balancing

Replace the L4 `LoadBalancer` Service with a GKE **Gateway** (global or regional external
Application Load Balancer) and NEG-backed routing to pods.

| Pros | Cons |
|---|---|
| **Per-request** balancing straight to pods: fixes connection pinning server-side | Adds a hop (typically a few ms) and LB cost per rule and per GB |
| TLS termination, Cloud Armor (WAF, rate limiting), IAP/JWT auth | More moving parts: health checks, backend policies, certificates |
| Request-level LB metrics and logs | Health checks must be tuned for slow, CPU-bound responses |
| An approved ingress path in projects that block raw L4 exposure | Global LBs need DNS and certificates to be useful |

### 3.2 gRPC clients

TF Serving's gRPC API carries binary tensors, so there's no base64 (−25 % payload) and no JSON
parsing on the server.

| Pros | Cons |
|---|---|
| Lower CPU per request on both sides; HTTP/2 multiplexing | HTTP/2 multiplexes many requests on one connection, which makes L4 pinning *worse*; needs an L7 LB or client-side balancing |
| Strongly typed contracts | Harder to debug with curl; clients need generated stubs |

### 3.3 Authentication and rate limiting

Today the endpoint is unauthenticated. Options: IAP or JWT validation at the Gateway, Cloud
Armor rate limits, or an API gateway (API Gateway, Apigee) with API keys and quotas.

| Pros | Cons |
|---|---|
| Protects capacity (and the bill) from abuse | Adds latency and cost per call (API gateways) |
| Per-client quotas and usage analytics | Key or identity management for every client |

## 4. Releases and model lifecycle

### 4.1 Canary and blue-green releases

Today a rollout replaces pods one at a time (`maxSurge: 1`) with an automatic rollback on a
failed smoke test. With a Gateway (§3.1), or **Argo Rollouts**, shift 5 % → 25 % → 100 % of
traffic and gate each step on P95 and error rate from Managed Prometheus.

| Pros | Cons |
|---|---|
| A bad model or image reaches a fraction of users, and rollback is automatic | Needs an L7 traffic splitter and an analysis controller |
| Gates on real traffic, not just a smoke test | Two versions run at once (2× capacity during blue-green); slower releases |

### 4.2 Version models separately from the image

Store SavedModels in GCS and let TF Serving load versions through `model_config_file`, with
version labels such as `stable` and `canary` routed by clients. Or adopt **KServe**.

| Pros | Cons |
|---|---|
| Ship a model without rebuilding the image; run several versions side by side | Loses the single immutable artifact (image tag = code + weights) |
| KServe adds canaries, transformers and scale-to-zero | Pods need GCS read access (Workload Identity) and egress; startup depends on GCS download time |

### 4.3 Shadow traffic

Mirror a copy of live requests to a new model and compare outputs without affecting users.

| Pros | Cons |
|---|---|
| Real-world validation of accuracy and latency before exposure | Doubles inference cost for the mirrored share; needs an L7 mirror and an output store |

### 4.4 Accuracy gate in CI

The current tests prove the model loads and classifies a known image. A gate on a small labelled
set (e.g. top-1 ≥ threshold on 500 ImageNet-val images) would catch silent preprocessing or
export regressions. That matters most once quantization (§1.4) is in play.

| Pros | Cons |
|---|---|
| Catches regressions that shape checks can't | Dataset licensing and storage; adds CI minutes |

### 4.5 GitOps

Argo CD or Config Sync reconciles `kubernetes/` from git instead of `kubectl apply` in CI.

| Pros | Cons |
|---|---|
| Drift detection and self-healing; an audit trail in git | Another controller; image tag updates need a write-back step or an image updater |

## 5. Reliability

### 5.1 Regional cluster

A regional control plane plus node pools in three zones.

| Pros | Cons |
|---|---|
| Survives a zone outage; control plane upgrades without API downtime | Node counts are per zone (min 2 means 6 nodes); higher cost; the regional cluster fee isn't free-tier |

### 5.2 Multi-region

Clusters in two regions behind a global Application Load Balancer (Multi-Cluster Gateway).

| Pros | Cons |
|---|---|
| Regional disaster tolerance; lower latency for distant users | Duplicated base cost; cross-region rollout coordination and config drift |

### 5.3 SLO objects and burn-rate alerts

Define availability and latency SLOs in Cloud Monitoring and alert on error-budget burn rate
(fast and slow windows) instead of fixed thresholds.

| Pros | Cons |
|---|---|
| Alerts track user impact; fewer false pages | Requires agreed SLO targets and some history to tune |

### 5.4 Hide node provisioning with overprovisioning

Run low-priority "balloon" pods (a negative `PriorityClass`) that reserve one node's worth of
capacity. Serving pods preempt them instantly, and the cluster autoscaler then replaces the
headroom in the background.

| Pros | Cons |
|---|---|
| Turns the measured 40-70 s Pending time into ~20 s (pod start only) | You pay for the reserved headroom permanently |
| Simple: one Deployment and a PriorityClass | Needs resizing as pod size changes |

### 5.5 Continuous chaos testing

Run `scripts/chaos_test.sh` (or Chaos Mesh experiments: pod kill, node drain, network latency) on
a schedule against a staging cluster.

| Pros | Cons |
|---|---|
| Catches resilience regressions like the replica co-location found here | Needs a staging environment; real risk if pointed at production without guardrails |

## 6. Security

| Extension | Pros | Cons |
|---|---|---|
| **Binary Authorization** with cosign/KMS-signed images from CI | only CI-built, scanned images can run | key management; break-glass procedure needed |
| **SBOM + provenance verification** (build-push-action already emits provenance) | supply-chain transparency | tooling to store and verify attestations |
| **Private control-plane endpoint** + Connect Gateway or a self-hosted runner | no public Kubernetes API | CI needs a network path; more setup |
| **Least-privilege node SA everywhere** (the default here; locked-down projects fall back to the Compute default SA) | limits node credential blast radius | requires permission to grant IAM roles |
| **Service mesh mTLS** (Cloud Service Mesh / Istio) | encrypted, identity-aware pod-to-pod traffic | heavy for a single service; sidecar CPU and latency overhead |
| **Request payload limits** at the Gateway | protects decode and memory from oversized images | must match legitimate client image sizes |

## 7. Observability

| Extension | Pros | Cons |
|---|---|---|
| **OpenTelemetry tracing** (client → LB → server) | locates queueing vs network vs inference time per request | TF Serving has no native tracing; needs a proxy or sidecar |
| **Grafana** on Managed Prometheus | richer dashboards, familiar to many teams | another service to host and secure |
| **Model monitoring**: confidence distribution, top-class drift, input statistics | detects data drift and silent quality loss | needs request/response sampling and storage; privacy review |
| **Structured, sampled request logs** | debugging specific failures | log volume cost; PII risk with image data |

## 8. Cost

| Extension | Pros | Cons |
|---|---|---|
| **Spot pool for burst capacity**, on-demand pool for the base (node affinity preference) | 60-90 % cheaper burst nodes | preemption with 30 s notice; needs PDB-aware capacity planning |
| **Scale to zero outside business hours** (dev/test) | near-zero idle cost | cold start of 1-2 minutes |
| **Committed use discounts** for the baseline nodes | significant savings on steady usage | 1-3 year commitment |
| **Cost per 1,000 images** as a tracked metric (GKE cost allocation is enabled) | makes every optimization comparable in money | needs a billing export join |
| **Right-size requests** (VPA recommendations, §2.4) | better bin-packing, fewer nodes | less headroom for spikes |

## 9. Platform alternatives

| Platform | Choose it when | You give up |
|---|---|---|
| **GKE Standard** (current) | you want full control of scaling, networking, node shapes and cost levers | operational effort: upgrades, node pools, policies |
| **GKE Autopilot** | you want Kubernetes APIs without managing nodes | some low-level control; different pricing model |
| **Vertex AI Endpoints** | managed model serving with built-in scaling, monitoring and traffic split | custom networking and autoscaling policy; per-node-hour pricing |
| **Cloud Run** (CPU or GPU) | spiky, low-to-moderate traffic; scale to zero; minimal ops | long cold starts for a 1.4 GB image and model load; less control over concurrency and CPU |
| **KServe on GKE** | many models, canaries, transformers, a standard inference protocol | an additional control plane (Knative or raw deployment mode) |
| **NVIDIA Triton** | multi-framework models, GPU efficiency, advanced batching | a larger, more complex runtime than TF Serving for a single TF model |
