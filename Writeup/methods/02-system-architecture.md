# System Architecture

Methods should describe **roles**, not lab hostnames/CPU counts.
Hardware topology (Emulab, which `node*`, cores/GiB) belongs in **Experimental setup**.

---

## Purpose

Answer: *Which components run the experiment, and who is responsible for what?*

---

## Motivation (Docker → multi-node K8s)

Keep: multi-replica CPU/memory competition, scheduler placement, DB topology /
connection pressure.
Drop from this block: Locust↔Service network-path effects (setup detail, not a
studied lever); caches (agent strategy, not a reason to use Kubernetes).
Prefer “this work” over “our extension” in chapter voice.

---

## Components (conceptual)

| Component | Responsibility |
|-----------|----------------|
| **K8s control plane** | API server, scheduler; orchestrator talks via `kubectl` |
| **K8s worker nodes** | Run experiment pods (app + DB/pooler/cache); scheduler places from manifests. Agent knobs → § spec |
| **Private container registry** | Host iteration images for worker pulls; tags recorded at deploy |
| **BaxBench orchestrator** | LLM calls, FT, build/push, validate/render/apply, Locust, diagnostics, conversation |
| **Locust master** | Coordinate distributed load test, aggregate stats |
| **Locust workers** | Emit HTTP load (hosts separate from app workers) |
| **External LLM API** | Persistent conversation per experiment |

**Load path:** Locust → backend Service reachable from outside the cluster (we use **NodePort**).

**Isolation:** per-iteration `baxbench-*` namespaces, cleaned after bench.

Defer hardware with one sentence; do **not** put `spec.yaml` replica/resource
lists under workers (that is the agent interface → deployment-spec phase).

Do **not** add an “Image and Deployment Flow” subsection under Architecture —
build/push/pull/readiness live in the code and deploy phases.

---

## Responsibility split

| Layer | Controls |
|-------|----------|
| **LLM agent** | App code; high-level knobs in `spec.yaml` |
| **Framework** | Scenarios/FT, YAML rendering, validation, probes, Locust, telemetry |
