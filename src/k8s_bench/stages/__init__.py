"""
Per-iteration stages of the k8s benchmark loop.

Each stage is invoked from :mod:`k8s_bench.orchestration.execute`, which creates
the per-phase logger and calls ``run_*_stage(ctx, plan, cfg, logger, ...)``:

- :mod:`decision` – choose code vs spec path; rename iteration folder.
- :mod:`code`    – baseline codegen, code refinement, or copied lineage.
- :mod:`spec`    – produce ``spec.yaml`` (baseline / reuse / generate).
- :mod:`deploy`  – cluster deploy + readiness probe (``04-deploy/``).
- :mod:`bench`   – Locust load test against the deployed iteration.
- :mod:`outcome` – collect feedback, write artifacts, append summary block.

Import stage callables from their submodules (e.g. ``stages.decision``), not
from this package, to avoid import cycles with ``orchestration.config``.
"""
