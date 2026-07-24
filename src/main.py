import argparse
import os
import pathlib
import sys
from dataclasses import replace
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from env import all_envs
from scenarios import all_scenarios
from tasks import Task

_DEFAULT_SAVE_PATH = pathlib.Path(__file__).parent.parent / "results"

import shlex


class ArgFileParser(argparse.ArgumentParser):
    def convert_arg_line_to_args(self, arg_line: str) -> list[str]:
        return shlex.split(arg_line, comments=True, posix=True)


def main(args: Any) -> None:
    if args.mode == "k8s-preflight":
        from k8s_bench.cluster import run_preflight_from_args as run_k8s_preflight_from_args

        run_k8s_preflight_from_args(args)
        return
    if args.mode == "k8s-setup-cluster":
        from k8s_bench.cluster import run_setup_from_args

        run_setup_from_args(args)
        return
    if args.mode == "k8s-setup-registry":
        from k8s_bench.cluster import run_registry_setup_from_args

        run_registry_setup_from_args(args)
        return
    if args.mode == "k8s-plot":
        from plots import regenerate_experiment_plots

        if not getattr(args, "k8s_experiment_dir", None):
            raise SystemExit("--k8s-experiment-dir is required for --mode k8s-plot")
        experiment_dir = pathlib.Path(args.k8s_experiment_dir).expanduser().resolve()
        plot_paths = regenerate_experiment_plots(experiment_dir)
        if not plot_paths:
            raise SystemExit(
                f"No plots generated for {experiment_dir} "
                "(no completed benchmark iterations found)."
            )
        for plot_path in plot_paths:
            print(f"[k8s-plot] Wrote {plot_path}")
        return
    # ----- Preparation -----#
    # Override port for all environments with the value from args, if not provided defaults to 5001
    envs = [replace(e, port=args.port) for e in all_envs]
    exclude_envs = args.exclude_envs if args.exclude_envs else []
    envs = [e for e in envs if e.id not in exclude_envs]
    if args.envs:
        envs = [e for e in envs if e.id in args.envs]
    envs = sorted(envs, key=lambda e: e.id)

    # if not envs:
    #     raise Exception(
    #         f"Got an empty/invalid list of envs, possible choices: {[e.id for e in all_envs]}",
    #     )

    exclude_scenarios = args.exclude_scenarios if args.exclude_scenarios else []
    scenarios = [e for e in all_scenarios if e.id not in exclude_scenarios]
    if args.scenarios:
        scenarios = [
            e
            for e in all_scenarios
            if e.id in args.scenarios and e.id not in exclude_scenarios
        ]
    scenarios = sorted(scenarios, key=lambda s: s.id)
    if not scenarios:
        raise Exception(
            f"Got an empty/invalid list of scenarios, possible choices: {[s.id for s in all_scenarios]}",
        )

    if not args.models:
        raise Exception("Got an empty list of models")

    if args.only_samples:
        samples = args.only_samples
    else:
        samples = list(range(args.n_samples))

    tasks = sorted(
        [
            Task(
                env=env,
                scenario=scenario,
                model=model,
                temperature=args.temperature,
                spec_type=args.spec_type,
                safety_prompt=args.safety_prompt,
                reasoning_effort=args.reasoning_effort,
                provider=args.provider,
                use_stubs=args.use_stubs,
                run_security_tests=args.run_security_tests,
            )
            for env in envs
            for scenario in scenarios
            for model in args.models
        ],
        key=lambda t: t.id,
    )

    # ----- Run tasks -----#

    if args.mode == "k8s-bench":
        import logging

        from k8s_bench.cluster import ensure_k8s_cluster_ready

        if not args.k8s_cluster:
            raise SystemExit("--k8s-cluster is required for --mode k8s-bench")
        logging.basicConfig(level=logging.INFO)
        ensure_k8s_cluster_ready(
            logger=logging.getLogger("baxbench.k8s.bench"),
            profile_name=str(args.k8s_cluster).strip(),
        )
        from k8s_bench.loop import run_k8s_bench

        k8s_iteration_path = (
            pathlib.Path(args.k8s_iteration_path).expanduser().resolve()
            if getattr(args, "k8s_iteration_path", None)
            else None
        )
        k8s_run_dirs = run_k8s_bench(
            tasks,
            args.results_dir,
            samples=samples,
            deploy_only=getattr(args, "deploy_only", False),
            timeout=args.timeout,
            force=args.force,
            k8s_cluster=str(args.k8s_cluster).strip(),
            k8s_iteration=args.k8s_iteration,
            k8s_iteration_path=k8s_iteration_path,
            k8s_iterations=getattr(args, "k8s_iterations", 1),
            k8s_wait_timeout=args.k8s_wait_timeout,
            k8s_auto_init=args.k8s_auto_init,
            k8s_refinement=args.k8s_refinement,
            load_profile=args.load_profile,
            k8s_experiment_id=args.k8s_experiment,
            llm_max_cost_usd=getattr(args, "llm_max_cost", None),
            ft_timeout=args.timeout,
            num_ports=args.num_ports,
            min_port=args.min_port,
            bench_users=args.bench_users,
            bench_spawn_rate=args.bench_spawn_rate,
            bench_run_time=args.bench_run_time,
            max_retries=args.max_retries,
            base_delay=args.base_delay,
            max_delay=args.max_delay,
            baseline_code_max_attempts=getattr(
                args, "baseline_code_max_attempts", 3
            ),
            baseline_spec_max_attempts=getattr(
                args, "baseline_spec_max_attempts", 5
            ),
        )
    else:
        raise Exception(f"Invalid mode: {args.mode}")


if __name__ == "__main__":
    # build-scenarios has its own separate CLI (scenario_builder/orchestrator.py,
    # via --generate_scenarios/--generate_tests/--difficulty/etc., not this
    # parser's flags) — dispatch to it directly, before the flags below get a
    # chance to choke on a mode they don't recognize.
    if "--mode" in sys.argv:
        _mode_idx = sys.argv.index("--mode")
        if _mode_idx + 1 < len(sys.argv) and sys.argv[_mode_idx + 1] == "build-scenarios":
            import subprocess

            _scenario_builder_dir = pathlib.Path(__file__).resolve().parent / "scenario_builder"
            _passthrough_args = sys.argv[1:_mode_idx] + sys.argv[_mode_idx + 2 :]
            raise SystemExit(
                subprocess.run(
                    [sys.executable, "orchestrator.py", *_passthrough_args],
                    cwd=_scenario_builder_dir,
                ).returncode
            )

    parser = ArgFileParser(fromfile_prefix_chars="@")
    parser.add_argument(
        "--models", type=str, nargs="+", default=[], help="List of models"
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=[
            "k8s-bench",
            "k8s-preflight",
            "k8s-setup-cluster",
            "k8s-setup-registry",
            "k8s-plot",
            "build-scenarios",
        ],
        required=True,
        help=(
            "Mode in which to run the code. build-scenarios dispatches straight to "
            "scenario_builder/orchestrator.py (see scripts/build_scenarios.sh) — it has its "
            "own separate CLI, intercepted below before the rest of these flags are parsed."
        ),
    )
    parser.add_argument(
        "--temperature", type=float, default=0.2, help="Temperature for sampling"
    )
    parser.add_argument(
        "--n_samples",
        type=int,
        default=5,
        help="The number of samples to run. Will index from 0.",
    )
    parser.add_argument(
        "--reasoning_effort",
        type=str,
        default="high",
        choices=["low", "medium", "high"],
        help="The reasoning effort to use for reasoning models.",
    )
    parser.add_argument(
        "--only_samples",
        type=str,
        nargs="+",
        default=None,
        help="If given, it will restrict operations to these sample indices.",
    )
    parser.add_argument(
        "--envs",
        type=str,
        default=None,
        nargs="+",
        help="List of environments (if empty, then all environments are used)",
    )
    parser.add_argument(
        "--exclude_envs",
        type=str,
        default=None,
        nargs="+",
        help="List of environments to exclude",
    )
    parser.add_argument(
        "--scenarios",
        type=str,
        default=None,
        nargs="+",
        help="List of scenarios (if empty, then all scenarios are used)",
    )
    parser.add_argument(
        "--exclude_scenarios",
        type=str,
        default=None,
        nargs="+",
        help="List of scenarios to exclude",
    )
    parser.add_argument(
        "--spec_type",
        choices=["openapi", "text", "json_api"],
        default="openapi",
        type=str,
        help="The type of specifications to use.",
    )
    parser.add_argument(
        "--safety_prompt",
        choices=["none", "generic", "specific", "performance", "high_performance"],
        default="none",
        type=str,
        help="The type of additional safety cue to use.",
    )
    parser.add_argument(
        "--results_dir",
        type=pathlib.Path,
        default=_DEFAULT_SAVE_PATH,
        help="Directory to save the results",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout for each test run in seconds.",
    )
    parser.add_argument(
        "--num_ports",
        type=int,
        default=10000,
        help="Number of ports to use for docker containers",
    )
    parser.add_argument(
        "--min_port",
        type=int,
        default=12345,
        help="Minimum port number to use for docker containers",
    )
    parser.add_argument(
        "--max_retries",
        type=int,
        default=2,
        help="Maximum number of retries for backoff during generation",
    )
    parser.add_argument(
        "--base_delay",
        type=float,
        default=1.0,
        help="Base delay for backoff during generation",
    )
    parser.add_argument(
        "--max_delay",
        type=float,
        default=128.0,
        help="Maximum delay for backoff during generation",
    )
    parser.add_argument(
        "--bench-users",
        type=int,
        default=None,
        help="Number of concurrent users for benchmarking",
    )
    parser.add_argument(
        "--bench-spawn-rate",
        type=int,
        default=None,
        help="Rate to spawn users (users per second)",
    )
    parser.add_argument(
        "--bench-run-time",
        type=int,
        default=None,
        help="Duration of the benchmark in seconds (integer).",
    )
    parser.add_argument(
        "--k8s-iteration",
        type=str,
        default=None,
        help=(
            "Pin a single Kubernetes iteration (e.g. iteration-000 or iteration-003). "
            "If omitted, --k8s-iterations controls iteration-000 (baseline) plus "
            "iteration-001..NNN refinement phases."
        ),
    )
    parser.add_argument(
        "--k8s-iteration-path",
        type=str,
        default=None,
        help=(
            "Deploy+bench this iteration directory in place (any path under sampleN/). "
            "Requires --deploy-only. Overrides --k8s-iteration path resolution."
        ),
    )
    parser.add_argument(
        "--k8s-iterations",
        type=int,
        default=1,
        help=(
            "K8s iterative benchmark: number of refinement phases after baseline. "
            "Runs iteration-000 (baseline deploy probe + benchmark) then "
            "iteration-001 .. iteration-NNN (single-attempt code or spec refinement)."
        ),
    )
    parser.add_argument(
        "--deploy-only",
        action="store_true",
        help=(
            "k8s-bench: deploy and benchmark existing iteration folders only "
            "(no LLM codegen, spec generation, or refinement). Use when code "
            "and spec.yaml are already on disk."
        ),
    )
    parser.add_argument(
        "--k8s-wait-timeout",
        type=int,
        default=300,
        help="Seconds to wait for Kubernetes deployments to become available.",
    )
    parser.add_argument(
        "--k8s-auto-init",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Deploy-only fallback: create a default spec.yaml when missing "
            "(only with --deploy-only). Default: false."
        ),
    )
    parser.add_argument(
        "--load-profile",
        type=str,
        default="quick-check",
        help=(
            "Locust load profile name for k8s-bench (default: quick-check). "
            "Deploy-only re-bench scripts typically pass default."
        ),
    )
    parser.add_argument(
        "--k8s-cluster",
        type=str,
        default=None,
        help="Select cluster profile from k8s_bench/cluster/profiles.py.",
    )
    parser.add_argument(
        "--llm-max-cost",
        type=float,
        default=None,
        help=(
            "Stop k8s-bench LLM calls when estimated experiment spend (USD) "
            "reaches this cap. Omit for no limit."
        ),
    )
    parser.add_argument(
        "--k8s-experiment",
        type=str,
        default=None,
        help=(
            "Group iterative k8s configs and perf runs under "
            "sampleN/k8s-experiments/<slug>/. Omit for the ``default`` slug."
        ),
    )
    parser.add_argument(
        "--k8s-experiment-dir",
        type=str,
        default=None,
        help=(
            "Path to a k8s experiment workspace (sampleN/k8s-experiments/<slug>/). "
            "Required for --mode k8s-plot."
        ),
    )
    parser.add_argument(
        "--k8s-refinement",
        type=str,
        default="auto",
        choices=["auto", "deployment", "code"],
        help=(
            "Between k8s phases (iteration 001+): auto = LLM chooses deployment vs "
            "code refinement; deployment/code = force that path (default: auto)."
        ),
    )
    parser.add_argument(
        "--baseline-code-max-attempts",
        type=int,
        default=3,
        help=(
            "Max LLM codegen attempts for iteration-000 baseline (default 3). "
            "Failed attempts are preserved under iteration-000-baseline/02-code/attempts/."
        ),
    )
    parser.add_argument(
        "--baseline-spec-max-attempts",
        type=int,
        default=5,
        help=(
            "Max baseline spec-generation attempts for iteration-000 (default 5). "
            "Each attempt is one LLM call + static validation + deploy probe; "
            "failures are preserved under iteration-000-baseline/03-spec/attempts/. "
            "Refinement iterations (001+) always use a single spec attempt — they "
            "rely on later iterations for recovery, not per-iteration retries."
        ),
    )
    parser.add_argument(
        "--k8s-install-prerequisites",
        action="store_true",
        help=(
            "Install packages on profile hosts: kubeadm stack on control+workers, "
            "python tooling on Locust hosts. Does NOT run kubeadm init/join."
        ),
    )
    parser.add_argument(
        "--k8s-skip-cluster-checks",
        action="store_true",
        help="Skip kubectl cluster API checks (only run SSH node checks).",
    )
    parser.add_argument(
        "--k8s-pod-network-cidr",
        type=str,
        default=None,
        help="Pod network CIDR for kubeadm init (default: 10.244.0.0/16 for Flannel).",
    )
    parser.add_argument(
        "--k8s-cni",
        type=str,
        default=None,
        help="CNI plugin to install after init (default: flannel).",
    )
    parser.add_argument(
        "--k8s-skip-cni",
        action="store_true",
        help="Skip CNI install (cluster already has networking).",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force generation even if the file already exists",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5001,
        help="Port number for the application to listen on (default: 5001).",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        choices=["openai", "anthropic", "together_ai", "openrouter", "swissai", "vllm"],
        help="Explicitly specify the LLM provider. If not provided, the provider will be inferred from the model name.",
    )
    parser.add_argument(
        "--use_stubs",
        action="store_true",
        default=False,
        help="Whether to use code stubs.",
    )
    parser.add_argument(
        "--run_security_tests",
        action="store_true",
        default=False,
        help="Whether to run security tests. By default, security tests are skipped.",
    )
    parser.add_argument(
        "--use_openhands",
        action="store_true",
        help="Use OpenHands for code generation instead of single LLM prompting",
    )
    parser.add_argument(
        "--use_claude_agent",
        action="store_true",
        help="Use Claude Agent SDK for code generation instead of single LLM prompting",
    )
    parser.add_argument(
        "--agent_cls",
        type=str,
        default="CodeActAgent",
        help="Agent class to use for OpenHands (e.g., CodeActAgent, BrowserAgent). Only used with --use_openhands.",
    )
    parser.add_argument(
        "--agent_max_iterations",
        type=int,
        default=50,
        help="Maximum number of iterations for agent execution.",
    )
    parser.add_argument(
        "--agent_max_cost",
        type=float,
        default=None,
        help="Maximum cost for agent execution. Agent will stop if this limit is exceeded.",
    )
    parser.add_argument(
        "--agent_max_tokens",
        type=int,
        default=None,
        help="Maximum total tokens (input + output) for agent execution. Agent will stop if this limit is exceeded.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5001,
        help="Port number for the application to listen on (default: 5001).",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        choices=["openai", "anthropic", "together_ai", "openrouter", "swissai", "vllm"],
        help="Explicitly specify the LLM provider. If not provided, the provider will be inferred from the model name.",
    )
    parser.add_argument(
        "--use_stubs",
        action="store_true",
        default=False,
        help="Whether to use code stubs.",
    )
    parser.add_argument(
        "--run_security_tests",
        action="store_true",
        default=False,
        help="Whether to run security tests. By default, security tests are skipped.",
    )
    main(parser.parse_args())
