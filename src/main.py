import argparse
import os
import pathlib
from dataclasses import replace
from typing import Any

import docker
from dotenv import load_dotenv

load_dotenv()

from env import all_envs
from print import tasks_and_results_to_table, tasks_and_results_to_table_averages
from bench_models import RemoteConfig
from scenarios import all_scenarios
from tasks import Task, TaskHandler

_DEFAULT_SAVE_PATH = pathlib.Path(__file__).parent.parent / "results"

import shlex

from env.templates import prepare_all_templates
from distributed_bench.system_configs import resolve_system_topology


def _plot_per_run_dir_best_effort(
    run_dir: pathlib.Path,
    out_dir: pathlib.Path | None,
    *,
    throughput_rps_bucket_width: float = 25.0,
) -> None:
    """Per-run plots matching ``--mode plot --plot-run-dir`` (skips missing metrics)."""
    import plot as plot_mod
    import plot_remote_perf as plot_remote_perf_mod

    run_dir = pathlib.Path(run_dir)
    for name, fn, extra_kwargs in (
        ("backend_vs_db_latency", plot_mod.plot_backend_vs_db_latency_for_run_dir, {}),
        ("throughput_over_time", plot_mod.plot_throughput_over_time_for_run_dir, {}),
        (
            "throughput_by_rps",
            plot_mod.plot_throughput_by_rps_for_run_dir,
            {"rps_bucket_width": throughput_rps_bucket_width},
        ),
        ("remote_perf", plot_remote_perf_mod.plot_remote_perf_for_run_dir, {}),
    ):
        try:
            fn(run_dir=run_dir, out_dir=out_dir, **extra_kwargs)  # type: ignore[misc]
        except (FileNotFoundError, ValueError) as e:
            print(f"[plot-run-dir] Skipping {name}: {e}")


class ArgFileParser(argparse.ArgumentParser):
    def convert_arg_line_to_args(self, arg_line: str) -> list[str]:
        return shlex.split(arg_line, comments=True, posix=True)


def main(args: Any) -> None:

    print(args)

    # ----- Preparation -----#
    # Override port for all environments with the value from args, if not provided defaults to 5001
    envs = [replace(e, port=args.port) for e in all_envs]
    exclude_envs = args.exclude_envs if args.exclude_envs else []
    envs = [e for e in envs if e.id not in exclude_envs]
    if args.envs:
        envs = [e for e in envs if e.id in args.envs]
    envs = sorted(envs, key=lambda e: e.id)

    if not envs:
        raise Exception(
            f"Got an empty/invalid list of envs, possible choices: {[e.id for e in all_envs]}",
        )

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

    if args.ks:
        ks = args.ks
    else:
        ks = [1, 5]

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
                use_openhands=args.use_openhands,
                use_claude_agent=args.use_claude_agent,
                agent_cls=args.agent_cls,
                agent_max_iterations=args.agent_max_iterations,
                agent_max_cost=args.agent_max_cost,
                agent_max_tokens=args.agent_max_tokens,
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

    bench_remote_config = None
    if args.bench_app_host or args.bench_app_hosts or args.bench_load_master:
        if not args.bench_load_master:
            raise ValueError(
                "--bench-load-master must be provided for remote benchmarking"
            )
        if not args.bench_app_host and not args.bench_app_hosts:
            raise ValueError(
                "Either --bench-app-host or --bench-app-hosts must be provided for remote benchmarking"
            )
        remote_kwargs: dict[str, Any] = {
            "backend_hosts": (
                tuple(args.bench_app_hosts)
                if args.bench_app_hosts
                else ((args.bench_app_host,) if args.bench_app_host else ())
            ),
            "app_private_addr": args.bench_app_private_addr,
            "load_master": args.bench_load_master,
            "load_workers": tuple(args.bench_load_workers or ()),
            "remote_base_dir": args.bench_remote_dir,
            "app_port": args.bench_remote_port,
            "lb_host": args.bench_lb_host,
            "db_hosts": ((args.bench_db_host,) if args.bench_db_host else ()),
        }
        bench_remote_config = RemoteConfig(**remote_kwargs)
    elif args.mode == "bench":
        topology_name = os.environ.get("BAXBENCH_SYSTEM_TOPOLOGY", "default").strip()
        topology = resolve_system_topology(topology_name)
        if topology.has_host_mapping():
            bench_remote_config = topology.to_remote_config(
                remote_base_dir=args.bench_remote_dir,
                app_private_addr=args.bench_app_private_addr,
                app_port=args.bench_remote_port,
            )

    task_handler = TaskHandler(
        tasks=tasks,
        results_dir=args.results_dir,
        max_concurrent_runs=args.max_concurrent_runs,
        bench_remote_config=bench_remote_config,
    )

    # ----- Prepare environment templates -----#
    if args.mode == "generate":
        prepare_all_templates(envs)

    # ----- Run tasks -----#

    def _run_plotting(*, plot_run_dir: pathlib.Path | None) -> None:
        """Shared plotting logic for --mode plot and post-bench plotting."""
        if plot_run_dir is not None:
            _plot_per_run_dir_best_effort(
                plot_run_dir,
                None,
                throughput_rps_bucket_width=float(args.throughput_rps_bucket_width),
            )
        else:
            task_handler.plot_bench(
                samples=samples,
            )

    if args.mode == "generate":
        task_handler.run_generation(
            batch_size=args.n_samples,
            max_retries=args.max_retries,
            base_delay=args.base_delay,
            max_delay=args.max_delay,
            force=args.force,
            skip_failed=args.skip_failed,
            vllm_port=args.vllm_port,
            num_ports=args.num_ports,
            min_port=args.min_port,
        )
    elif args.mode == "test":
        task_handler.run_tests(
            samples=samples,
            timeout=args.timeout,
            num_ports=args.num_ports,
            min_port=args.min_port,
            force=args.force,
        )
        if args.prune_docker:
            docker.from_env().containers.prune()

    elif args.mode == "bench":
        bench_run_dirs = task_handler.run_bench(
            samples=samples,
            timeout=args.timeout,
            num_ports=args.num_ports,
            min_port=args.min_port,
            force=args.force,
            bench_users=args.bench_users,
            bench_spawn_rate=args.bench_spawn_rate,
            bench_run_time=args.bench_run_time,
        )
        if getattr(args, "plot_after_bench", False) and bench_run_dirs:
            for rd in bench_run_dirs:
                print(f"[bench] Post-bench plots for {rd}")
                _run_plotting(plot_run_dir=pathlib.Path(rd))
    elif args.mode == "plot":
        run_dir = pathlib.Path(args.plot_run_dir) if getattr(args, "plot_run_dir", None) else None
        _run_plotting(plot_run_dir=run_dir)
    elif args.mode == "optimize":
        task_handler.run_optimization(
            samples=samples,
            timeout=args.timeout,
            num_ports=args.num_ports,
            min_port=args.min_port,
            force=args.force,
        )
        # Automatically run benchmark on the optimized samples
        opt_samples = [f"{s}_opt" for s in samples]
        bench_run_dirs = task_handler.run_bench(
            samples=opt_samples,
            timeout=args.timeout,
            num_ports=args.num_ports,
            min_port=args.min_port,
            force=args.force,
            bench_users=args.bench_users,
            bench_spawn_rate=args.bench_spawn_rate,
            bench_run_time=args.bench_run_time,
        )
    elif args.mode == "evaluate":
        r = task_handler.evaluate_results(
            ks=ks,
            samples=samples,
        )
        print(tasks_and_results_to_table_averages(r))
        print()
        print(tasks_and_results_to_table(r, verbose=False))
        task_handler.plot_functional_tests(
            tasks_and_results=r,
        )
    else:
        raise Exception(f"Invalid mode: {args.mode}")


if __name__ == "__main__":
    parser = ArgFileParser(fromfile_prefix_chars="@")
    parser.add_argument(
        "--models", type=str, nargs="+", required=True, help="List of models"
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["generate", "test", "bench", "plot", "evaluate", "optimize"],
        required=True,
        help="Mode in which to run the code.",
    )
    parser.add_argument(
        "--plot-run-dir",
        type=str,
        default=None,
        help="Plot directly from a specific per-run directory (e.g. sample0/perf-default-db-pressure-...).",
    )
    parser.add_argument(
        "--plot-after-bench",
        action="store_true",
        help=(
            "After bench mode completes, run the same per-run plots as "
            "--plot-run-dir for each benched perf directory (best-effort)."
        ),
    )
    parser.add_argument(
        "--throughput-rps-bucket-width",
        type=float,
        default=25.0,
        help=(
            "Bucket width (in req/s) for plots/throughput_by_rps.png when using "
            "--plot-run-dir or --plot-after-bench."
        ),
    )
    parser.add_argument(
        "--temperature", type=float, default=0.2, help="Temperature for sampling"
    )
    parser.add_argument(
        "--n_samples",
        type=int,
        default=5,
        help="The number of samples to generate or test. Will index from 0.",
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
        "--ks", type=int, nargs="+", default=None, help="List of k for pass@k score."
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
        "--max_concurrent_runs",
        type=int,
        default=None,
        help="Maximum number of concurrent runs",
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
        "--bench-app-host",
        type=str,
        default=None,
        help="SSH host (e.g. user@host) where the application container should run",
    )
    parser.add_argument(
        "--bench-app-hosts",
        type=str,
        default=None,
        nargs="+",
        help="List of SSH hosts (e.g. user@host) where backend containers should run (one per host).",
    )
    parser.add_argument(
        "--bench-app-private-addr",
        type=str,
        default=None,
        help="Private IP address for the app host, used by the load generator to reach the app",
    )
    parser.add_argument(
        "--bench-load-master",
        type=str,
        default=None,
        help="SSH host where the Locust master should run",
    )
    parser.add_argument(
        "--bench-load-workers",
        type=str,
        default=None,
        nargs="+",
        help="List of SSH hosts where Locust workers should run (may include the master host).",
    )
    parser.add_argument(
        "--bench-lb-host",
        type=str,
        default=None,
        help="SSH host where the load balancer (nginx) should run. Defaults to --bench-load-master.",
    )
    parser.add_argument(
        "--bench-db-host",
        type=str,
        default=None,
        help="SSH host where the database should run. Defaults to the first backend host.",
    )
    parser.add_argument(
        "--bench-remote-dir",
        type=str,
        default="/tmp/baxbench",
        help="Remote base directory to store benchmark artifacts when using remote hosts",
    )
    parser.add_argument(
        "--bench-remote-port",
        type=int,
        default=None,
        help="Override application port when running benchmarks on remote hosts",
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
        "--force",
        "-f",
        action="store_true",
        help="Force generation even if the file already exists",
    )
    parser.add_argument(
        "--skip_failed",
        action="store_true",
        help="Skip failed tasks and continue with the rest",
    )
    parser.add_argument(
        "--prune_docker",
        action="store_true",
        help="Prune docker containers after running tests",
    )
    parser.add_argument(
        "--vllm_port",
        type=int,
        default=8000,
        help="Port for VLLM server",
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
