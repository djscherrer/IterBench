# Phase: Deploy & Readiness Probe

Corresponds to `04-deploy/`.

---

## Role

Take the validated `spec.yaml` and built `image_id`, deploy to Kubernetes, and
verify the stack is **ready to receive load**.

---

## Steps (framework)

1. **Namespace** — create isolated `baxbench-*` namespace for this iteration.
2. **Cleanup** — delete prior `baxbench-*` namespaces (default on) to free cluster CPU.
3. **Apply manifests** — Deployment(s), Service(s), ConfigMaps, DB resources as rendered from spec.
4. **Readiness probe** — wait for:
   - Pods reaching Ready state
   - Service endpoints populated
   - (No Locust traffic at this stage)

---

## Separation from benchmark

Methods should state clearly:

| Stage | Question answered |
|-------|-------------------|
| Deploy probe | *Can this layout be scheduled and become ready?* |
| Locust bench | *How does it perform under load?* |

This two-stage gate prevents benchmarking broken deployments and gives the baseline spec loop a crisp success criterion.

---

## Failure behaviour

Deploy failure → iteration `deploy-failed`.

- Does **not** force code or spec retry automatically.
- Next iteration: **decision LLM** chooses code vs. deployment (agent may diagnose probe logs, events, OOM, scheduling failures).

**Thesis note:** This differs from code/spec failures — deploy failures may have multiple root causes (spec too aggressive, image issue, transient cluster).

---

## Artifacts

- `04-deploy/probe.json` — probe outcome, timing, failure reasons
- `04-deploy/phase.log`
- Kubernetes events / pod status captured in diagnostics (if enabled)

---

## Optional details

- Image pull policy (`IfNotPresent`) with private registry on control node.
- Postgres topology: official image vs. Bitnami replication when `database.replicas > 1`.
