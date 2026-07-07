# System Architecture

Draft content for Methods §2 (or §3.1 depending on your outline).

---

## Purpose

Describe the **physical and logical components** and who is responsible for what.
This section answers: *Where does the experiment run, and which machines do which jobs?*

---

## Components

### 1. Kubernetes cluster

| Role | Typical name in thesis | Responsibility |
|------|------------------------|----------------|
| Control-plane node | *control-plane* (avoid bare “master”) | API server, scheduler; may also host BaxBench + container registry |
| Worker nodes | *workers* | Run application and database pods |
| Private registry (optional) | *local registry* | Store built app images (`host:5000`); workers pull with `IfNotPresent` |

**Cluster profile** (`K8sClusterProfile` in code): encodes kubeconfig path, control node hostname, worker hostnames, registry settings, and Locust hostnames for a given lab setup.

Example (Emulab `baxbench-emulab`):

- `node0` — control plane + BaxBench orchestration + registry
- `node3`, `node4`, `node5` — Kubernetes workers
- Database and backend pods are scheduled onto workers according to the agent’s `spec.yaml`.

**What to write:**

- How the cluster was provisioned (e.g. kubeadm, reference setup script).
- Fixed cluster capacity exposed to the agent (total CPU/memory requests, number of workers).
- That workloads are isolated in per-iteration namespaces (`baxbench-*`), cleaned up after each bench.

---

### 2. Load generation (Locust)

Separate from Kubernetes scheduling:

| Role | Responsibility |
|------|----------------|
| Locust master | Coordinates test, aggregates RPS/latency stats |
| Locust workers | Generate HTTP load against the backend Service |

Load generators may run on dedicated hosts (e.g. `node1` master, `node2` worker) so that benchmark traffic does not saturate the same CPUs as application pods.

**What to write:**

- Distributed Locust over SSH.
- Traffic targets the Kubernetes Service endpoint (ClusterIP or equivalent), not individual pods directly.
- Adaptive load profile (e.g. `k8s-goodput-plateau`): ramp until SLA break, measure sustainable throughput.

---

### 3. BaxBench orchestrator

Runs on the machine with Docker, LLM API access, and `kubectl` (often the control-plane host):

- Invokes the LLM for code, spec, and refinement decisions.
- Builds container images from generated code.
- Renders Kubernetes manifests from validated `spec.yaml`.
- Deploys via `kubectl`, runs readiness probes.
- Triggers Locust bench, collects diagnostics, writes `iteration_feedback.json`.

---

## Responsibility split (preview of §11)

| Layer | Controls |
|-------|----------|
| **LLM agent** | Application source code; high-level deployment parameters (`replicas`, CPU/memory requests/limits, placement hints, DB replica count, …) |
| **Framework (BaxBench k8s extension)** | OpenAPI scenario, functional tests, YAML/manifest generation, deploy orchestration, validation rules, benchmarking, telemetry, iteration state |

This split keeps the search space bounded and experiments reproducible.

---

## Suggested narrative flow

1. One paragraph: *why a real cluster* (placement, resource limits, multi-replica behaviour).
2. Diagram: boxes for orchestrator, K8s control plane, K8s workers, Locust, external LLM API.
3. Table: node names / roles for your actual lab setup.
4. Short paragraph on image flow: codegen → Docker build → push to registry → pull on workers.

---

## Bullet points to expand

- [ ] Cluster size (number of workers, CPU/RAM per node) — use **your** measured values.
- [ ] Network assumptions (lab LAN, no ingress controller vs. NodePort/LoadBalancer if used).
- [ ] Postgres deployment model (single pod vs. primary + read replicas when `database.replicas > 1`).
- [ ] Environment variable / CLI entry point: `./scripts/bench_k8s.sh`, `--k8s-cluster baxbench-emulab`.
