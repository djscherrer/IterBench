from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .cluster.deploy import delete_iteration_namespace, deploy_iteration, render_and_deploy
from .workspace import (
    default_k8s_namespace,
    find_iteration_spec_path,
    iteration_dir,
    new_iteration_id,
    normalize_iteration_id,
    require_iteration_spec_path,
)
from .spec.dirs import prepare_iteration
from .spec.models import BackendSpec, DatabaseSpec, K8sWorkloadSpec
from .spec.render import render_iteration


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="k8s_bench",
        description="Generate and deploy BaxBench K8s workload iterations.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Create a new iteration directory with a starter spec.yaml")
    init_p.add_argument("sample_dir", type=Path, help="Path to sampleN directory")
    init_p.add_argument("--iteration", default=None, help="iteration id (default: next iteration-NNN)")
    init_p.add_argument("--namespace", default=None, help="Kubernetes namespace")
    init_p.add_argument("--backend-image", required=True, help="Container image for the app Deployment")
    init_p.add_argument("--backend-replicas", type=int, default=1)
    init_p.add_argument("--backend-port", type=int, default=8080)
    init_p.add_argument("--no-database", action="store_true")

    render_p = sub.add_parser("render", help="Generate manifests from spec.yaml")
    render_p.add_argument(
        "target",
        type=Path,
        help="iteration directory or sample_dir (with --iteration)",
    )
    render_p.add_argument("--iteration", default=None)

    deploy_p = sub.add_parser("deploy", help="kubectl apply manifests for an iteration")
    deploy_p.add_argument("target", type=Path)
    deploy_p.add_argument("--iteration", default=None)
    deploy_p.add_argument("--wait-timeout", type=int, default=300)

    run_p = sub.add_parser("run", help="render + deploy")
    run_p.add_argument("target", type=Path)
    run_p.add_argument("--iteration", default=None)
    run_p.add_argument("--wait-timeout", type=int, default=300)

    del_p = sub.add_parser("delete", help="Delete the iteration namespace")
    del_p.add_argument("target", type=Path)
    del_p.add_argument("--iteration", default=None)

    return p


def _resolve_iteration_path(target: Path, iteration: str | None) -> Path:
    if find_iteration_spec_path(target) is not None:
        return target
    if iteration is None:
        raise SystemExit(f"{target} is not an iteration directory; pass --iteration")
    return iteration_dir(target, iteration)


def _default_spec(
    iteration_id: str,
    *,
    namespace: str | None,
    backend_image: str,
    backend_replicas: int,
    backend_port: int,
    with_database: bool,
) -> K8sWorkloadSpec:
    iid = normalize_iteration_id(iteration_id)
    return K8sWorkloadSpec(
        iteration_id=iid,
        namespace=namespace or default_k8s_namespace(iid),
        backend=BackendSpec(
            image=backend_image,
            replicas=backend_replicas,
            port=backend_port,
        ),
        database=DatabaseSpec(enabled=with_database),
        labels={"baxbench.dev/sample": "true"},
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)

    if args.command == "init":
        iid = args.iteration or None
        sample_dir = args.sample_dir
        resolved_id = normalize_iteration_id(iid or new_iteration_id(sample_dir))
        spec = _default_spec(
            resolved_id,
            namespace=args.namespace,
            backend_image=args.backend_image,
            backend_replicas=args.backend_replicas,
            backend_port=args.backend_port,
            with_database=not args.no_database,
        )
        path = prepare_iteration(sample_dir, resolved_id, spec=spec)
        render_iteration(path)
        print(path)
        return 0

    iteration_path = _resolve_iteration_path(args.target, getattr(args, "iteration", None))

    if args.command == "render":
        out = render_iteration(iteration_path)
        print(out)
        return 0
    if args.command == "deploy":
        result = deploy_iteration(iteration_path, wait_timeout_s=args.wait_timeout)
        print(result.backend_service_url)
        return 0 if result.success else 1
    if args.command == "run":
        render_and_deploy(iteration_path, wait_timeout_s=args.wait_timeout)
        return 0
    if args.command == "delete":
        spec = K8sWorkloadSpec.from_yaml_file(require_iteration_spec_path(iteration_path))
        delete_iteration_namespace(spec.namespace)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
