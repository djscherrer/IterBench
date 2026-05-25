import concurrent.futures
import inspect
import json
import logging
import math
import multiprocessing
import multiprocessing.managers
import os
import pathlib
import re
import shutil
import subprocess
import time
from collections import defaultdict
from contextlib import contextmanager
import datetime
from dataclasses import dataclass, field
from sys import exc_info
from typing import Any, Generator, Optional, Self, Tuple, cast

import docker
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import tqdm
from docker.models.containers import Container

import cwes as cwe
from distributed_bench.analysis import plot
from db_metrics import PostgresSampler
from db_manager import PostgresConnectionParams, PostgresManager
from env.base import COMMON_DOCKER_RUN_COMMANDS, Env
from prompts import Prompter
from bench_models import RemoteConfig
from distributed_bench import run_remote_bench
from scenarios.base import AppInstance, FunctionalTest, Scenario, SecurityTest


def esc(s: str) -> str:
    return s.replace("/", "-")


def _slugify_run_part(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in s.strip()) or "default"


def run_test_with_timeout(
    f: SecurityTest | FunctionalTest, app_instance: AppInstance, timeout: int
) -> Any:
    with multiprocessing.Pool(processes=1) as pool:
        async_result = pool.apply_async(f, [app_instance])
        try:
            return async_result.get(timeout=timeout)
        except multiprocessing.TimeoutError:
            pool.terminate()
            raise TimeoutError("Functional test timed out")


def run_bench_with_timeout(
    locustfile: pathlib.Path,
    csv_prefix: pathlib.Path,
    port: int,
    timeout: int,
    user: str,
    bench_users: int | None = None,
    bench_spawn_rate: int | None = None,
    bench_run_time: int | None = None,
    host: str | None = None,
) -> bytes:
    """Local docker ``bench`` mode only (Locust on this machine → localhost container)."""
    import os
    import subprocess

    from locust_bench.load_profiles import resolve_load_profile
    from locust_bench.load_profiles.env import build_baxbench_locust_env
    from locust_bench.locust_run import prepare_locust_run_dir, resolve_locust_user_class

    profile = resolve_load_profile(os.environ.get("BAXBENCH_LOAD_PROFILE", "default"))
    run_time_s = (
        int(bench_run_time) if bench_run_time is not None else int(profile.effective_run_time_s)
    )
    users = int(bench_users) if bench_users is not None else int(profile.effective_users)
    spawn_rate = (
        int(bench_spawn_rate) if bench_spawn_rate is not None else int(profile.effective_spawn_rate)
    )
    target_host = host if host is not None else f"http://localhost:{port}"
    run_dir = csv_prefix.parent
    locustfile = prepare_locust_run_dir(run_dir, locustfile)
    user_class = resolve_locust_user_class(locustfile, user)
    proc_env = os.environ.copy()
    proc_env.update(
        build_baxbench_locust_env(profile, bench_run_time_s=run_time_s, bench_users=users)
    )
    try:
        result = subprocess.run(
            [
                "locust",
                "--headless",
                "--locustfile",
                str(locustfile),
                "--host",
                target_host,
                "--users",
                str(users),
                "--spawn-rate",
                str(spawn_rate),
                "--run-time",
                f"{run_time_s}s",
                "--csv",
                str(csv_prefix),
                "--csv-full-history",
                "--only-summary",
                user_class,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=proc_env,
            cwd=str(run_dir),
        )
        return result.stdout
    except subprocess.TimeoutExpired:
        raise TimeoutError("Benchmarking timed out") from None


def plot_requests_vs_percentile(
    csv_path: str,
    x_col: str = "Requests/s",
    x_col2: str = "Failures/s",
    y_col: str = "99%",  # any percentile column, e.g. "95%", "99.9%", etc.
    name_col: str = "Name",
    name_value: str = "Aggregated",
    decreasing_run: int = 5,  # consecutive strictly-decreasing points to trigger cutoff
    cutoff_delta: int = 0,  # keep rows up to (start_index_of_run + cutoff_delta), inclusive
    ax: Optional[
        plt.Axes
    ] = None,  # pass an existing axes to draw on, or leave None to create one
    **plot_kwargs,  # e.g. linewidth=2, marker="o"
) -> Tuple[plt.Axes, pd.DataFrame]:
    """
    Read a CSV of load-test stats and plot y_col vs x_col for rows where name_col == name_value.
    Additionally, if x_col strictly decreases for `decreasing_run` consecutive rows, drop all rows
    AFTER (start_index_of_run + cutoff_delta).

    Parameters
    ----------
    csv_path : str
        Path to the CSV file.
    x_col : str
        Column name to use for the x-axis (default: "Requests/s").
    y_col : str
        Column name to use for the y-axis (default: "99%").
    name_col : str
        Column that identifies series/groups (default: "Name").
    name_value : str
        Required value in name_col to keep (default: "Aggregated").
    decreasing_run : int
        Length of a strictly decreasing run in x_col that triggers cutoff (default: 5).
    cutoff_delta : int
        Keep rows up to (start_index_of_run + cutoff_delta), inclusive (default: 0).
    ax : matplotlib.axes.Axes or None
        Existing axes to plot on; if None, a new figure/axes is created.
    **plot_kwargs :
        Passed through to `ax.plot(...)`.

    Returns
    -------
    ax : matplotlib.axes.Axes
        The axes the line was drawn on.
    df_used : pandas.DataFrame
        The filtered DataFrame that was actually plotted (after cutoff & NaN removal).

    Notes
    -----
    - The CSV may contain non-numeric cells; this coerces x_col and y_col to numeric.
    - Cutoff uses the *first* occurrence of a strictly-decreasing run of the requested length.
    - Comparison is strict: x[i] > x[i+1] > ... > x[i+decreasing_run-1].
    """
    # Read & filter
    df = pd.read_csv(csv_path)
    df = df[df[name_col] == name_value].copy()

    # Ensure numeric for x and y; drop rows with NaNs afterwards
    df[x_col] = pd.to_numeric(df[x_col], errors="coerce")
    df[x_col2] = pd.to_numeric(df[x_col2], errors="coerce")
    df[y_col] = pd.to_numeric(df[y_col], errors="coerce")
    df = df.dropna(subset=[x_col, x_col2, y_col])

    # Preserve existing order; find first strictly-decreasing run in x_col
    x = (df[x_col] - df[x_col2]).to_numpy()
    start_idx = None
    if len(x) >= decreasing_run:
        # scan windows of size `decreasing_run`
        for s in range(0, len(x) - decreasing_run + 1):
            # strictly decreasing over the window?
            if all(x[s + k] > x[s + k + 1] for k in range(decreasing_run - 1)):
                start_idx = s
                break

    # Apply cutoff if a run was found
    if start_idx is not None:
        last_keep = max(0, min(len(df) - 1, start_idx + cutoff_delta))
        df = df.iloc[: last_keep + 1]  # inclusive

    # Prepare axes
    created_fig = False
    if ax is None:
        fig, ax = plt.subplots()
        created_fig = True

    # Plot
    ax.plot((df[x_col] - df[x_col2]).to_numpy(), df[y_col].to_numpy(), **plot_kwargs)
    ax.set_xlabel(x_col)
    ax.set_ylabel(y_col)
    ax.set_title(f"{name_value}: {y_col} vs {x_col}")

    # Optionally tighten layout if we created the figure
    if created_fig:
        try:
            fig.tight_layout()
        except Exception:
            pass

    return ax, df


@dataclass
class ContainerRunner:
    env: Env
    port_manager: "SlotManager"
    image_id: str
    logger: logging.Logger
    needs_db: bool = True
    _container: Container | None = None
    _port: int | None = None
    _db_port: int | None = None
    _postgres_manager: PostgresManager | None = None
    _db_params: PostgresConnectionParams | None = None

    def __enter__(self) -> Self:
        while self._port is None:
            self._port = self.port_manager.acquire_slot()
            time.sleep(0.1)

        if self.needs_db:
            while self._db_port is None:
                self._db_port = self.port_manager.acquire_slot()
                time.sleep(0.1)

            self.logger.info(f"Starting PostgreSQL on port {self._db_port}")
            self._postgres_manager = PostgresManager(self._db_port, self.logger)
            try:
                self._db_params = self._postgres_manager.start()
                self.logger.info(f"PostgreSQL ready: {self._db_params.to_env_dict()}")
            except Exception as e:
                self.logger.exception(f"Failed to start PostgreSQL: {e}")
                if self._db_port:
                    self.port_manager.release_slot(self._db_port)
                if self._port:
                    self.port_manager.release_slot(self._port)
                raise

        # Start backend container
        try:
            # Build environment variables, add db variables if db needed
            env_vars = {"PORT": str(self.env.port)}
            if self.needs_db and self._db_params:
                env_vars.update(self._db_params.to_env_dict())

            link = (
                {self._postgres_manager.container_id: "postgres"}
                if self.needs_db and self._postgres_manager
                else None
            )

            self._container = self.env.run_docker_container(
                self.image_id, self._port, additional_env=env_vars, link=link
            )
        except Exception as e:
            self.logger.exception("could not start container %s", e, exc_info=e)
            # Cleanup database if it was started
            if self._postgres_manager:
                self._postgres_manager.cleanup()
            if self._db_port:
                self.port_manager.release_slot(self._db_port)
            if self._port:
                self.port_manager.release_slot(self._port)
            raise ValueError("Could not start docker container")

        self.logger.info("started container, port=%d", self._port)

        # make sure that the server is online before we process, otherwise let it fail
        start = time.time()
        while True:
            try:
                response = requests.get(f"http://localhost:{self._port}")
                self.logger.info("Server is up! Server response: %s", response)
                break
            except requests.ConnectionError as e:
                self.logger.warning("Server is not up yet: %s", e)
            if time.time() - start > self.env.wait_to_start_time:
                self.logger.error("Server did not start in time")
                self.__exit__(*exc_info())
            self.logger.info("Waiting for server to start...")
            time.sleep(1.0)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        # Cleanup application container
        if self.container is not None:
            container_logs = cast(
                bytes, self.container.logs(stdout=True, stderr=True, follow=False)
            )
            self.logger.info("container logs:\n%s", container_logs.decode())
            self.container.remove(force=True)
            self.logger.info("removed container")

        # Cleanup Postgres container
        if self._postgres_manager is not None:
            self._postgres_manager.cleanup()

        # Release ports
        if self._port is not None:
            self.port_manager.release_slot(self._port)
        if self._db_port is not None:
            self.port_manager.release_slot(self._db_port)

        self.logger.info("-" * 100)
        self.logger.info("cleanup complete")
        self.logger.info("-" * 100)

    @property
    def port(self) -> int:
        assert self._port is not None
        return self._port

    @property
    def container(self) -> Container:
        assert self._container is not None
        return self._container


@dataclass
class Task:
    env: Env
    scenario: Scenario
    model: str
    temperature: float
    reasoning_effort: str
    spec_type: str
    safety_prompt: str
    use_openhands: bool
    use_claude_agent: bool
    agent_cls: str
    agent_max_iterations: int
    agent_max_cost: float | None
    agent_max_tokens: int | None
    provider: str | None
    use_stubs: bool = True
    run_security_tests: bool = False

    @property
    def id(self) -> str:
        base_id = f"{self.model}-{self.env.id}-{self.scenario.id}-{self.spec_type}-{self.safety_prompt}-{self.temperature}"
        if self.use_openhands:
            return f"{base_id}-openhands-{self.agent_cls}"
        if self.use_claude_agent:
            return f"{base_id}-claude-agent"
        return base_id

    @contextmanager
    def create_logger(
        self, logfile_path: pathlib.Path
    ) -> Generator[logging.Logger, None, None]:
        logger = logging.getLogger(self.id)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logfile_handler = logging.FileHandler(logfile_path, mode="w")
        logfile_handler.setLevel(logging.INFO)
        logfile_handler.setFormatter(
            logging.Formatter(fmt="%(levelname)s %(asctime)s %(message)s")
        )
        logger.addHandler(logfile_handler)
        try:
            yield logger
        finally:
            logfile_handler.close()

    def get_save_dir(self, results_dir: pathlib.Path) -> pathlib.Path:
        base_dir = (
            results_dir / esc(self.model) / esc(self.scenario.id) / esc(self.env.id)
        )
        if self.use_openhands:
            save_dir = (
                base_dir
                / f"temp{float(self.temperature)}-{esc(self.spec_type)}-{esc(self.safety_prompt)}-openhands"
            )
        elif self.use_claude_agent:
            save_dir = (
                base_dir
                / f"temp{float(self.temperature)}-{esc(self.spec_type)}-{esc(self.safety_prompt)}-claude-agent"
            )
        else:
            save_dir = (
                base_dir
                / f"temp{float(self.temperature)}-{esc(self.spec_type)}-{esc(self.safety_prompt)}"
            )
        return save_dir

    def get_sample_dir(self, results_dir: pathlib.Path, sample: int) -> pathlib.Path:
        return self.get_save_dir(results_dir) / f"sample{sample}"

    def get_k8s_configs_dir(self, results_dir: pathlib.Path, sample: int) -> pathlib.Path:
        """``sampleN/k8s_configs`` or ``sampleN/k8s-experiments/<slug>/k8s_configs``."""
        from k8s_bench.paths import k8s_configs_root

        return k8s_configs_root(self.get_sample_dir(results_dir, sample))

    def get_k8s_iteration_dir(
        self, results_dir: pathlib.Path, sample: int, iteration_id: str
    ) -> pathlib.Path:
        from k8s_bench.paths import iteration_dir

        return iteration_dir(self.get_sample_dir(results_dir, sample), iteration_id)

    def get_k8s_bench_run_dir(
        self,
        results_dir: pathlib.Path,
        sample: int,
        iteration_id: str,
    ) -> pathlib.Path:
        from k8s_bench.iteration import make_k8s_perf_run_dir

        return make_k8s_perf_run_dir(
            self.get_sample_dir(results_dir, sample),
            iteration_id,
        )

    def has_k8s_perf_run_for_iteration(
        self,
        sample_dir: pathlib.Path,
        *,
        iteration_id: str,
        load_profile: str,
    ) -> bool:
        from k8s_bench.paths import normalize_iteration_id

        from k8s_bench.paths import k8s_workspace_root

        iid = normalize_iteration_id(iteration_id)
        safe_profile = _slugify_run_part(load_profile)
        pattern = f"perf-k8s-{iid}-{safe_profile}-*"
        for run_dir in k8s_workspace_root(sample_dir).glob(pattern):
            if run_dir.is_dir() and (run_dir / "config.json").exists():
                return True
        return False

    def get_functional_tests_dir(
        self, results_dir: pathlib.Path, sample: int
    ) -> pathlib.Path:
        return self.get_sample_dir(results_dir, sample) / "functional_tests"

    def get_code_dir(self, results_dir: pathlib.Path, sample: int) -> pathlib.Path:
        return self.get_sample_dir(results_dir, sample) / "code"

    def get_test_results_json_path(
        self, results_dir: pathlib.Path, sample: int
    ) -> pathlib.Path:
        return self.get_functional_tests_dir(results_dir, sample) / "test_results.json"

    def get_bench_results_csv_prefix(
        self, results_dir: pathlib.Path, sample: int, user: str
    ) -> pathlib.Path:
        return self.get_sample_dir(results_dir, sample) / f"bench_results_{user}"

    def get_bench_results_csv_path(
        self, results_dir: pathlib.Path, sample: int, user: str
    ) -> pathlib.Path:
        return (
            self.get_sample_dir(results_dir, sample)
            / f"bench_results_{user}_stats_history.csv"
        )

    def get_bench_run_dir(
        self,
        results_dir: pathlib.Path,
        sample: int,
        bench_users: int | None,
        bench_spawn_rate: int | None,
        bench_run_time: int | None,
    ) -> pathlib.Path:
        """
        Per-run output directory within the sample folder.
        Example: sample9/perf-default-db-pressure-20260408-071239
        """
        _ = (bench_users, bench_spawn_rate, bench_run_time)
        topology = _slugify_run_part(os.environ.get("BAXBENCH_SYSTEM_TOPOLOGY", "default"))
        load_profile = _slugify_run_part(os.environ.get("BAXBENCH_LOAD_PROFILE", "default"))
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"perf-{topology}-{load_profile}-{ts}"
        return self.get_sample_dir(results_dir, sample) / name

    def has_perf_run_for_profile(
        self, sample_dir: pathlib.Path, *, topology: str, load_profile: str
    ) -> bool:
        """
        Whether any perf run directory exists for this sample and (topology, load_profile),
        regardless of timestamp.

        This supports the common workflow of skipping already-benched samples when re-running
        benches with the same profile.
        """
        # Prefer a config-based match when possible. The directory name is a convenience, but
        # can drift if environment variables contain different formatting than the persisted config.
        for run_dir in sample_dir.glob("perf-*"):
            if not run_dir.is_dir():
                continue
            cfg_path = run_dir / "config.json"
            if not cfg_path.exists():
                continue
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                continue

            def _get_str(*path: str) -> str | None:
                cur: Any = cfg
                for key in path:
                    if not isinstance(cur, dict) or key not in cur:
                        return None
                    cur = cur[key]
                if isinstance(cur, str):
                    return cur.strip()
                return None

            # We accept either "requested_profiles" or "resolved_*" names (some runs may differ
            # in what they persist depending on runner/version).
            cfg_topo = _get_str("requested_profiles", "system_topology") or _get_str(
                "resolved_system_topology", "name"
            )
            cfg_prof = _get_str("requested_profiles", "load_profile") or _get_str(
                "resolved_load_profile", "name"
            )

            if cfg_topo == topology and cfg_prof == load_profile:
                return True

        return False

    def has_any_bench_results(self, sample_dir: pathlib.Path, user: str) -> bool:
        """
        Whether any previous bench results exist for this sample (supports per-run subdirs).
        """
        pattern = f"bench_results_{user}_stats_history.csv"
        return any(sample_dir.glob(f"**/{pattern}"))

    def load_code(
        self,
        results_dir: pathlib.Path,
        sample: int,
        logger: logging.Logger | None = None,
    ) -> dict[pathlib.Path, str]:
        code_dir = self.get_code_dir(results_dir, sample)
        files: dict[pathlib.Path, str] = {}

        skip_dirs = {"node_modules", "venv", "__pycache__", ".git", "target"}
        skip_files = {"db.sqlite3", ".DS_Store", "Cargo.lock"}

        for root, dir_names, file_names in os.walk(code_dir):
            dir_names[:] = [d for d in dir_names if d not in skip_dirs]

            for file in file_names:
                if file in skip_files:
                    continue
                abs_path = pathlib.Path(root) / file
                try:
                    with open(abs_path, "r") as f:
                        content = f.read()
                except Exception as e:
                    if logger is not None:
                        logger.exception(
                            "Error reading file %s: %s", abs_path, e, exc_info=e
                        )
                    # print(f"Error reading file {abs_path}: {e}")
                    # with open(abs_path, "rb") as f:
                    #     content = str(f.read())
                    continue
                rel_path = abs_path.relative_to(code_dir)
                files[rel_path] = content
        return files

    def save_code(
        self, files: dict[pathlib.Path, str], results_dir: pathlib.Path, sample: int
    ) -> None:
        code_dir = self.get_code_dir(results_dir, sample)
        code_dir.mkdir(parents=True, exist_ok=True)
        for path, code in files.items():
            full_path = code_dir / path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w") as f:
                f.write(code)

    def save_test_results(
        self, results: "TestResult", results_dir: pathlib.Path, sample: int
    ) -> None:
        sample_dir = self.get_sample_dir(results_dir, sample)
        sample_dir.mkdir(parents=True, exist_ok=True)
        self.get_functional_tests_dir(results_dir, sample).mkdir(parents=True, exist_ok=True)
        test_result_path = self.get_test_results_json_path(results_dir, sample)
        with open(test_result_path, "w") as f:
            json.dump(results.to_dict(), f)

    def generate_code(
        self,
        results_dir: pathlib.Path,
        batch_size: int,
        max_retries: int,
        base_delay: float,
        max_delay: float,
        force: bool,
        skip_failed: bool,
        vllm_port: int,
        port_manager: "SlotManager",
    ) -> None:
        # check if there are already some results generated
        last_sample = -1
        for sample in range(batch_size):
            sample_dir = self.get_sample_dir(results_dir, sample)
            if sample_dir.exists() and (
                not (self.get_code_dir(results_dir, sample) / "failed").exists()
                or skip_failed
            ):
                last_sample = sample
            else:
                break

        last_sample = -1 if force else last_sample

        if last_sample == batch_size - 1:
            return
        else:
            # remove all samples after the last_sample
            for sample in range(last_sample + 1, batch_size):
                sample_dir = self.get_sample_dir(results_dir, sample)
                if sample_dir.exists():
                    shutil.rmtree(sample_dir)

        save_dir = self.get_save_dir(results_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

        # reduce the batch size
        batch_size = batch_size - (last_sample + 1)

        gen_logfile_path = save_dir / "gen.log"
        if gen_logfile_path.exists() and not force:
            with open(gen_logfile_path, "r") as f:
                prior_log = f.read()
        elif force:
            prior_log = ""
            gen_logfile_path.unlink(missing_ok=True)
        else:
            prior_log = ""
        with self.create_logger(gen_logfile_path) as logger:
            logger.info("Prior Log:\n%s", prior_log)
            logger.info(100 * "-")
            logger.info(
                "generating %s code samples at temp %s for task %s with reasoning effort %s",
                batch_size,
                self.temperature,
                self.id,
                self.reasoning_effort,
            )

            if self.use_openhands:
                logger.info(f"Using OpenHands agent for code generation")

                # Lazy import so single-shot runs don't require OpenHands deps.
                from prompts_openhands import OpenHandsPrompter

                prompter_oh = OpenHandsPrompter(
                    env=self.env,
                    scenario=self.scenario,
                    model=self.model,
                    spec_type=self.spec_type,
                    safety_prompt=self.safety_prompt,
                    temperature=self.temperature,
                    agent_cls=self.agent_cls,
                    max_iterations=self.agent_max_iterations,
                    provider=self.provider,
                    max_cost=self.agent_max_cost,
                    max_tokens=self.agent_max_tokens,
                    use_stubs=self.use_stubs,
                )
                logger.info("Built agent task:\n%s", prompter_oh.task)
                for sample in range(last_sample + 1, last_sample + 1 + batch_size):
                    try:
                        logger.info(f"Generating sample {sample} with OpenHands...")
                        code_dir = prompter_oh.generate_code_with_agent(
                            sample_id=sample,
                            save_dir=self.get_save_dir(results_dir),
                            logger=logger,
                            port_manager=port_manager,
                            needs_db=self.scenario.needs_db,
                        )
                        logger.info(f"Generated code saved to {code_dir}")
                        logger.info("-" * 80)
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        logger.exception(
                            f"OpenHands agent failed for sample {sample}: {e}",
                            exc_info=e,
                        )
                        continue

            elif self.use_claude_agent:
                logger.info(f"Using Claude Agent SDK for code generation")

                prompter_ca = ClaudeAgentPrompter(
                    env=self.env,
                    scenario=self.scenario,
                    model=self.model,
                    spec_type=self.spec_type,
                    safety_prompt=self.safety_prompt,
                    temperature=self.temperature,
                    max_iterations=self.agent_max_iterations,
                    max_cost=self.agent_max_cost,
                    max_tokens=self.agent_max_tokens,
                )
                logger.info("Built agent task:\n%s", prompter_ca.task)
                for sample in range(last_sample + 1, last_sample + 1 + batch_size):
                    try:
                        logger.info(f"Generating sample {sample} with Claude Agent...")
                        code_dir = prompter_ca.generate_code_with_agent(
                            sample_id=sample,
                            save_dir=self.get_save_dir(results_dir),
                            logger=logger,
                        )
                        logger.info(f"Generated code saved to {code_dir}")
                        logger.info("-" * 80)
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        logger.exception(
                            f"Claude Agent failed for sample {sample}: {e}", exc_info=e
                        )
                        continue

            else:
                logger.info(
                    f"Using single-prompt LLM ({self.model}) for code generation"
                )

                prompter = Prompter(
                    env=self.env,
                    scenario=self.scenario,
                    model=self.model,
                    spec_type=self.spec_type,
                    safety_prompt=self.safety_prompt,
                    batch_size=batch_size,
                    offset=last_sample + 1,
                    temperature=self.temperature,
                    reasoning_effort=self.reasoning_effort,
                    vllm_port=vllm_port,
                    provider=self.provider,
                    use_stubs=self.use_stubs,
                )
                logger.info("built prompt:\n%s", prompter.prompt)
                logger.info("-" * 100)

                try:
                    prompter.prompt_model_batch_with_exp_backoff(
                        max_retries=max_retries,
                        base_delay=base_delay,
                        max_delay=max_delay,
                        save_dir=self.get_save_dir(results_dir),
                        logger=logger,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.exception("got exception:\n%s", str(e), exc_info=e)
                    return

    def _build_image(
        self,
        results_dir: pathlib.Path,
        sample: int,
        logger: logging.Logger,
    ) -> str | None:
        files: dict[pathlib.Path, str] = self.load_code(results_dir, sample, logger)
        try:
            image_id = self.env.build_docker_image(
                files,
                COMMON_DOCKER_RUN_COMMANDS
                + self.scenario.needed_packages.get("_all_", [])
                + self.scenario.needed_packages.get(self.env.language, []),
                logger,
                no_cache=False,
            )
            return image_id
        except Exception as e:
            logger.exception(
                f"Failed to build docker image with cache, got exception:\n{str(e)}",
                exc_info=e,
            )
            try:
                logger.info("Retrying without cache")
                image_id = self.env.build_docker_image(
                    files,
                    COMMON_DOCKER_RUN_COMMANDS
                    + self.scenario.needed_packages.get("_all_", [])
                    + self.scenario.needed_packages.get(self.env.language, []),
                    logger,
                    no_cache=True,
                )
                return image_id
            except Exception as e:
                logger.exception(
                    f"Failed to build docker image without cache, got exception:\n{str(e)}",
                    exc_info=e,
                )
                return None

    def test_code(
        self,
        results_dir: pathlib.Path,
        samples: list[int],
        port_manager: "SlotManager",
        timeout: int,
        force: bool,
    ) -> None:
        # clean the directory from test artifacts if entered by force
        if force:
            for sample in samples:
                sample_dir = self.get_sample_dir(results_dir, sample)
                if sample_dir.exists():
                    # New layout: functional test artifacts live under functional_tests/.
                    ft_dir = self.get_functional_tests_dir(results_dir, sample)
                    if ft_dir.exists():
                        shutil.rmtree(ft_dir, ignore_errors=True)

        # for each sample
        for sample in samples:
            sample_dir = self.get_sample_dir(results_dir, sample)

            # if code dir does not exist, skip
            if not self.get_code_dir(results_dir, sample).exists():
                continue

            # if test results exist and force is not set, skip
            if (
                self.get_test_results_json_path(results_dir, sample).exists()
                and not force
            ):
                continue

            self.get_test_results_json_path(results_dir, sample).unlink(missing_ok=True)
            ft_dir = self.get_functional_tests_dir(results_dir, sample)
            ft_dir.mkdir(parents=True, exist_ok=True)
            log_file = ft_dir / "test.log"
            with self.create_logger(log_file) as logger:
                code_dir = self.get_code_dir(results_dir, sample)
                layout_errors = self.env.codegen_layout_errors(code_dir)
                if layout_errors:
                    logger.error(
                        "Skipping Docker build — generated code layout incomplete for %s: %s",
                        self.env.id,
                        "; ".join(layout_errors),
                    )
                    result = TestResult()
                    for _ in range(len(self.scenario.functional_tests)):
                        result.record_ft_result(passed=False, had_exception=True)
                    for _ in range(len(self.scenario.security_tests)):
                        result.record_st_result(None)
                    logger.info(
                        "Finished testing sample %d, skipped build (incomplete codegen layout)",
                        sample,
                    )
                    self.save_test_results(result, results_dir, sample)
                    logger.info("Saved test results")
                    logger.info("-" * 100)
                    continue

                image_id = self._build_image(results_dir, sample, logger)

                # if image build fails, all tests are failed
                if image_id is None:
                    result = TestResult()
                    for _ in range(len(self.scenario.functional_tests)):
                        result.record_ft_result(passed=False, had_exception=True)
                    for _ in range(len(self.scenario.security_tests)):
                        result.record_st_result(None)
                    logger.info(
                        f"Finished testing sample {sample}, which failed to build"
                    )
                    self.save_test_results(result, results_dir, sample)
                    logger.info("Saved test results")
                    logger.info("-" * 100)
                    continue

                logger.info("done building docker image. id: %s", image_id)
                logger.info("-" * 100)

                result = TestResult()

                # run functional tests
                for ft in self.scenario.functional_tests:
                    logger.info("running functional test:\n%s", inspect.getsource(ft))

                    passed = False
                    had_exception = False
                    try:
                        # start backend and database containers
                        with ContainerRunner(
                            self.env, port_manager, image_id, logger
                        ) as cr:
                            server_ran_before = self.env.process_still_running(
                                cr.container.id, logger
                            )
                            # run functional test
                            passed = run_test_with_timeout(
                                ft,
                                AppInstance(
                                    port=cr.port,
                                    log_file_path=ft_dir / (ft.__name__ + ".log"),
                                    container_id=cr.container.id,
                                    env=self.env,
                                    db_params=cr._db_params,
                                ),
                                timeout,
                            )
                            if (
                                not self.env.process_still_running(
                                    cr.container.id, logger
                                )
                                and server_ran_before
                            ):
                                # something crashed the server process, this should be logged as cwe 703
                                logger.warning(
                                    "server process crashed during functional test"
                                )
                                result.record_st_result(
                                    {
                                        cwe.CWE.IMPROPER_CHECK_OR_HANDLING_OF_EXCEPTIONAL_CONDITIONS
                                    }
                                )
                    except Exception as e:
                        logger.exception("got exception:\n%s", str(e), exc_info=e)
                        had_exception = True
                    logger.info(
                        f"Functional test {ft.__name__} {'passed' if passed else 'failed'}"
                    )
                    result.record_ft_result(passed=passed, had_exception=had_exception)
                    logger.info("-" * 80)

                # run security tests
                if self.run_security_tests:
                    for st in self.scenario.security_tests:
                        logger.info("running security test:\n%s", inspect.getsource(st))
                        cwes: set[cwe.CWE] | None = None
                        try:
                            with ContainerRunner(
                                self.env, port_manager, image_id, logger
                            ) as cr:
                                server_ran_before = self.env.process_still_running(
                                    cr.container.id, logger
                                )
                                cwes = run_test_with_timeout(
                                    st,
                                    AppInstance(
                                        port=cr.port,
                                        log_file_path=sample_dir
                                        / (st.__name__ + ".log"),
                                        container_id=cr.container.id,
                                        env=self.env,
                                        db_params=cr._db_params,
                                    ),
                                    timeout,
                                )
                                if (
                                    not self.env.process_still_running(
                                        cr.container.id, logger
                                    )
                                    and server_ran_before
                                ):
                                    # something crashed the server process, this should be logged as cwe 703
                                    logger.warning(
                                        "server process crashed during security test"
                                    )
                                    if cwes is None:
                                        cwes = set()
                                    # Only add 703 if 400 (uncontrolled resource consumption) did not cause the crash
                                    if (
                                        not cwe.CWE.UNCONTROLLED_RESOURCE_CONSUMPTION
                                        in cwes
                                    ):
                                        cwes.add(
                                            cwe.CWE.IMPROPER_CHECK_OR_HANDLING_OF_EXCEPTIONAL_CONDITIONS
                                        )
                        except Exception as e:
                            logger.exception("got exception:\n%s", str(e), exc_info=e)
                        logger.info(
                            f"Security test {st.__name__} {'passed' if not cwes else 'failed'}"
                        )
                        result.record_st_result(cwes)
                        logger.info("-" * 80)
                else:
                    logger.info("Skipping security tests (run_security_tests=False)")

                logger.info("finished testing sample %d", sample)
                self.save_test_results(result, results_dir, sample)
                logger.info("saved test results")
                logger.info("-" * 100)

    def bench_code(
        self,
        results_dir: pathlib.Path,
        samples: list[int],
        port_manager: "SlotManager",
        timeout: int,
        force: bool,
        remote_config: RemoteConfig | None,
        bench_users: int | None = None,
        bench_spawn_rate: int | None = None,
        bench_run_time: int | None = None,
    ) -> list[pathlib.Path]:
        def _append_bench_skip(sample: int, reason: str) -> None:
            """
            Best-effort per-task skip logging.
            Written even when bench never creates a run_dir (so you can debug "instant" benches).
            """
            try:
                save_dir = self.get_save_dir(results_dir)
                save_dir.mkdir(parents=True, exist_ok=True)
                p = save_dir / "bench_skips.log"
                ts = datetime.datetime.now().isoformat(timespec="seconds")
                p.write_text(
                    (p.read_text(encoding="utf-8") if p.exists() else "")
                    + f"[{ts}] sample{sample}: {reason}\n",
                    encoding="utf-8",
                )
            except Exception:
                # Skip logging must never break the bench run itself.
                pass

        # clean the directory from bench artifacts if entered by force
        if force:
            for sample in samples:
                sample_dir = self.get_sample_dir(results_dir, sample)
                if sample_dir.exists():
                    # Old layout cleanup
                    for extension in ("bench.log", "*.csv"):
                        for file_path in sample_dir.glob(extension):
                            if file_path.is_file():
                                file_path.unlink()
        run_dirs_created: list[pathlib.Path] = []
        bench_logger = logging.getLogger(self.id)
        for sample in samples:
            sample_dir = self.get_sample_dir(results_dir, sample)

            # 1) Skip if a perf run already exists for the same topology + load profile.
            # (Timestamp differs, but the profile is the same.)
            topo = os.environ.get("BAXBENCH_SYSTEM_TOPOLOGY", "default")
            prof = os.environ.get("BAXBENCH_LOAD_PROFILE", "default")
            if not force and self.has_perf_run_for_profile(
                sample_dir, topology=topo, load_profile=prof
            ):
                _append_bench_skip(
                    sample,
                    f"skipped: perf run already exists for topology={topo!r} load_profile={prof!r}",
                )
                continue

            # 2) Only benchmark if all functional tests passed.
            # This intentionally makes bench depend on "test" results, rather than on incidental artifacts
            # like the generated code dir existing.
            test_result_path = self.get_test_results_json_path(results_dir, sample)
            if not test_result_path.exists():
                _append_bench_skip(
                    sample,
                    "skipped: missing functional test results (functional_tests/test_results.json)",
                )
                continue

            try:
                with open(test_result_path, "r", encoding="utf-8") as f:
                    test_result = TestResult.from_dict(json.load(f))
            except Exception:
                _append_bench_skip(sample, "skipped: unreadable functional test results")
                continue

            if test_result.num_passed_ft < test_result.num_total_ft:
                _append_bench_skip(
                    sample,
                    f"skipped: functional tests not all passing ({test_result.num_passed_ft}/{test_result.num_total_ft})",
                )
                continue

            # Image id is recorded during the functional test stage.
            # Newer layout stores it under functional_tests/test.log; keep a fallback to the old path.
            test_log_file = self.get_functional_tests_dir(results_dir, sample) / "test.log"
                
            pattern = re.compile(r"sha256:[0-9a-f]{64}")
            image_id = None
            try:
                with open(test_log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        match = pattern.search(line)
                        if match:
                            image_id = match.group(0)
                            break
            except FileNotFoundError:
                pass
            if image_id is None:
                _append_bench_skip(sample, f"skipped: no docker image id found in {test_log_file}")
                continue

            run_dir = self.get_bench_run_dir(
                results_dir=results_dir,
                sample=sample,
                bench_users=bench_users,
                bench_spawn_rate=bench_spawn_rate,
                bench_run_time=bench_run_time,
            )
            run_dir.mkdir(parents=True, exist_ok=True)
            run_dirs_created.append(run_dir)
            log_file = run_dir / "bench.log"
            with self.create_logger(log_file) as logger:
                # Check if image exists in docker
                image_exists = False
                if image_id:
                    try:
                        client = docker.from_env()
                        client.images.get(image_id)
                        image_exists = True
                    except Exception:
                        logger.warning(
                            f"Image {image_id} found in logs but not in Docker. Rebuilding..."
                        )
                        image_exists = False

                if not image_exists:
                    logger.info("Image not found or missing. Building...")
                    image_id = self._build_image(results_dir, sample, logger)
                    if image_id is None:
                        logger.error("Failed to build image for benchmarking")
                        _append_bench_skip(sample, "skipped: failed to build docker image for bench")
                        continue

                logger.info("got docker image. id: %s", image_id)
                logger.info("-" * 100)

                from scenario_files import SCENARIO_FILE_PATH

                shared_locustfile = SCENARIO_FILE_PATH.joinpath(
                    f"locustfiles/{self.scenario.id.lower()}.py"
                )
                has_locustfile = shared_locustfile.exists() or bool(self.scenario.locustfile)

                tests_to_run = list(self.scenario.performance_tests)
                if not tests_to_run:
                    # Some scenarios only provide a locustfile (either shared or inline)
                    # but do not define named performance_tests. Treat that as a single
                    # default bench run.
                    if has_locustfile:
                        tests_to_run = ["default"]
                    else:
                        _append_bench_skip(sample, "skipped: no performance tests configured")
                        continue

                # todo: repeate for each user
                for test in tests_to_run:
                    # Prefer inline scenario.locustfile when present (scenario-provided),
                    # fall back to shared scenario_files/locustfiles/<scenario>.py.
                    if self.scenario.locustfile:
                        locustfile = run_dir / f"locustfile-{self.scenario.id.lower()}.py"
                        locustfile.write_text(self.scenario.locustfile, encoding="utf-8")
                    elif shared_locustfile.exists():
                        locustfile = shared_locustfile
                    else:
                        _append_bench_skip(sample, "skipped: missing locustfile")
                        continue

                    logger.info("running load benchmark:\n%s", locustfile.read_text())
                    csv_prefix = self.get_bench_results_csv_prefix(
                        results_dir, sample, test
                    )
                    # Put locust CSVs into the per-run directory
                    csv_prefix = run_dir / csv_prefix.name

                    try:
                        if remote_config is not None:
                            sample_slug = f"{esc(self.model)}-{esc(self.env.id)}-{esc(self.scenario.id)}-sample{sample}"
                            run_remote_bench(
                                config=remote_config,
                                env=self.env,
                                sample_slug=sample_slug,
                                sample_dir=run_dir,
                                image_cache_dir=self.get_sample_dir(results_dir, sample),
                                image_id=image_id,
                                locustfile=locustfile,
                                csv_prefix=csv_prefix,
                                timeout=timeout,
                                logger=logger,
                                needs_db=self.scenario.needs_db,
                                bench_users=bench_users,
                                bench_spawn_rate=bench_spawn_rate,
                                bench_run_time=bench_run_time,
                            )
                        else:
                            with ContainerRunner(
                                self.env,
                                port_manager,
                                image_id,
                                logger,
                                needs_db=self.scenario.needs_db,
                            ) as cr:
                                server_ran_before = self.env.process_still_running(
                                    cr.container.id, logger
                                )
                                sampler: PostgresSampler | None = None
                                if self.scenario.needs_db and cr._postgres_manager is not None:
                                    db_csv = str(run_dir / "db_performance.csv")
                                    sampler = PostgresSampler(
                                        container=cr._postgres_manager.container,
                                        out_csv_path=db_csv,
                                        interval_s=1.0,
                                        user=PostgresManager.DEFAULT_USER,
                                        database=PostgresManager.DEFAULT_DATABASE,
                                    )
                                    sampler.start()
                                locust_logs = run_bench_with_timeout(
                                    locustfile,
                                    csv_prefix,
                                    cr.port,
                                    timeout,
                                    test,
                                    bench_users=bench_users,
                                    bench_spawn_rate=bench_spawn_rate,
                                    bench_run_time=bench_run_time,
                                )
                                if sampler is not None:
                                    sampler.stop()
                                logger.info("loader logs:\n%s", locust_logs.decode())
                                if (
                                    not self.env.process_still_running(
                                        cr.container.id, logger
                                    )
                                    and server_ran_before
                                ):
                                    # something crashed the server process, this should be logged as cwe 703
                                    logger.warning(
                                        "server process crashed during functional test"
                                    )
                    except Exception as e:
                        logger.exception("got exception:\n%s", str(e), exc_info=e)
                    logger.info("-" * 100)

                    logger.info("finished benchmarking sample %d", sample)
                    logger.info("-" * 100)

        return run_dirs_created

    def evaluate_results(
        self, results_dir: pathlib.Path, samples: list[int], ks: list[int]
    ) -> "SampleTestResult":
        r = SampleTestResult()
        for sample in samples:
            test_result_path = self.get_test_results_json_path(results_dir, sample)
            if test_result_path.exists():
                with open(test_result_path, "r") as f:
                    test_result = TestResult.from_dict(json.load(f))
                    r.record_result(test_result, sample)

        r.calculate_metrics(ks=ks)
        return r


@dataclass
class TestResult:
    # The number of functional tests that completed successfully
    num_passed_ft: int = 0

    # The total number of functional tests
    num_total_ft: int = 0

    # The number of functional tests that were terminated unexpectedly
    num_ft_exceptions: int = 0

    # The total number of security tests.
    num_total_st: int = 0

    # The number of security tests that were terminated unexpectedly
    num_st_exceptions: int = 0

    # The set of CWEs that were identified in the generated code
    cwes: set[cwe.CWE] = field(default_factory=set)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "TestResult":
        return TestResult(
            num_passed_ft=d["num_passed_ft"],
            num_total_ft=d["num_total_ft"],
            num_ft_exceptions=d["num_ft_exceptions"],
            num_total_st=d["num_total_st"],
            num_st_exceptions=d["num_st_exceptions"],
            cwes=set(cwe.CWE(x) for x in d["cwes"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "num_passed_ft": self.num_passed_ft,
            "num_total_ft": self.num_total_ft,
            "num_ft_exceptions": self.num_ft_exceptions,
            "num_total_st": self.num_total_st,
            "num_st_exceptions": self.num_st_exceptions,
            "cwes": list(c.value for c in self.cwes),
        }

    def record_ft_result(self, passed: bool, had_exception: bool) -> None:
        self.num_total_ft += 1
        if passed:
            self.num_passed_ft += 1
        if had_exception:
            self.num_ft_exceptions += 1

    def record_st_result(self, cwes: set[cwe.CWE] | None) -> None:
        self.num_total_st += 1
        if cwes is None:
            self.num_st_exceptions += 1
        else:
            self.cwes = self.cwes.union(cwes)

    @property
    def num_exceptions(self) -> int:
        return self.num_ft_exceptions + self.num_st_exceptions

    @property
    def num_tests(self) -> int:
        return self.num_total_ft + self.num_total_st


@dataclass
class SampleTestResult:
    n_samples: int = 0
    n_ft_correct: int = 0
    n_ft_and_st_correct: int = 0
    n_ft_correct_st_incorrect: int = 0
    cwes: dict[cwe.CWE, int] = field(default_factory=dict)
    cwes_ft_correct: dict[cwe.CWE, int] = field(default_factory=dict)
    ft_exceptions: list[int] = field(default_factory=list)
    st_exceptions: list[int] = field(default_factory=list)
    test_exceptions: list[int] = field(default_factory=list)

    pass_at_k: dict[int, float] = field(default_factory=dict)
    secure_pass_at_k: dict[int, float] = field(default_factory=dict)
    insec_pass: float = field(default_factory=float)
    cwe_percentages: dict[str, float] = field(default_factory=dict)
    cwe_ft_correct_percentages: dict[str, float] = field(default_factory=dict)

    def record_result(
        self,
        test_result: "TestResult",
        sample: int,
    ) -> None:
        self.n_samples += 1
        if test_result.num_passed_ft == test_result.num_total_ft:
            self.n_ft_correct += 1
            if len(test_result.cwes) == 0:
                self.n_ft_and_st_correct += 1
            else:
                self.n_ft_correct_st_incorrect += 1
            for cwe in test_result.cwes:
                self.cwes_ft_correct[cwe] = self.cwes_ft_correct.get(cwe, 0) + 1
        for cwe in test_result.cwes:
            self.cwes[cwe] = self.cwes.get(cwe, 0) + 1
        if test_result.num_ft_exceptions > 0:
            self.ft_exceptions.append(sample)
        if test_result.num_st_exceptions > 0:
            self.st_exceptions.append(sample)
        if test_result.num_ft_exceptions + test_result.num_st_exceptions > 0:
            self.test_exceptions.append(sample)

    def calculate_metrics(
        self,
        ks: list[int],
    ) -> None:
        self.pass_at_k = {
            k: pass_at_k(k, self.n_ft_correct, self.n_samples)
            for k in ks
            if self.n_samples >= k
        }
        self.secure_pass_at_k = {
            k: pass_at_k(k, self.n_ft_and_st_correct, self.n_samples)
            for k in ks
            if self.n_samples >= k
        }
        if self.n_ft_correct == 0:
            self.insec_pass = float("nan")
        else:
            self.insec_pass = self.n_ft_correct_st_incorrect / self.n_ft_correct
        self.cwe_percentages = {
            str(cwe.value["num"]): count / self.n_samples
            for cwe, count in self.cwes.items()
            if self.n_samples > 0
        }
        self.cwe_ft_correct_percentages = {
            str(cwe.value["num"]): count / self.n_ft_correct
            for cwe, count in self.cwes_ft_correct.items()
            if self.n_ft_correct > 0
        }


type TasksAndSampleResults = list[tuple[Task, SampleTestResult]]


class SlotManager:
    def __init__(
        self,
        manager: multiprocessing.managers.SyncManager,
        num_slots: int,
        min: int = 0,
    ):
        self.slots = manager.list([True for _ in range(num_slots)])
        self.lock = manager.Lock()
        self.min = min

    def acquire_slot(self) -> int | None:
        with self.lock:
            for i, is_free in enumerate(self.slots):
                if is_free:
                    self.slots[i] = False
                    return i + self.min
            return None  # No free slot available

    def release_slot(self, slot_index: int) -> None:
        slot_index -= self.min
        with self.lock:
            if 0 <= slot_index < len(self.slots):
                self.slots[slot_index] = True


class TaskHandler:
    def __init__(
        self,
        tasks: list[Task],
        results_dir: pathlib.Path,
        max_concurrent_runs: int | None,
        bench_remote_config: RemoteConfig | None = None,
    ):
        self.tasks = tasks
        self.results_dir = results_dir
        self.max_concurrent_runs = max_concurrent_runs
        self.bench_remote_config = bench_remote_config

    def run_generation(
        self,
        batch_size: int,
        max_retries: int,
        base_delay: float,
        max_delay: float,
        force: bool,
        skip_failed: bool,
        vllm_port: int,
        num_ports: int,
        min_port: int,
    ) -> list[int]:

        with multiprocessing.Manager() as manager:
            port_manager = SlotManager(manager, num_ports, min_port)

            with tqdm.tqdm(total=len(self.tasks)) as pbar:
                pbar.get_lock()  # type: ignore[no-untyped-call]

                def run_gen_task(task: Task) -> int:
                    task.generate_code(
                        results_dir=self.results_dir,
                        batch_size=batch_size,
                        force=force,
                        max_retries=max_retries,
                        base_delay=base_delay,
                        max_delay=max_delay,
                        skip_failed=skip_failed,
                        vllm_port=vllm_port,
                        port_manager=port_manager,
                    )
                    with pbar.get_lock():  # type: ignore[no-untyped-call]
                        pbar.update(1)
                    return 1

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.max_concurrent_runs
                ) as executor:
                    return list(executor.map(run_gen_task, self.tasks))

    def run_tests(
        self,
        samples: list[int],
        timeout: int,
        num_ports: int,
        min_port: int,
        force: bool,
    ) -> list[int]:
        _docker_client = docker.from_env()
        existing = [
            n for n in _docker_client.networks.list() if n.name == "baxbench-net"
        ]
        if not existing:
            _docker_client.networks.create(name="baxbench-net", driver="bridge")

        with multiprocessing.Manager() as manager:
            port_manager = SlotManager(manager, num_ports, min_port)

            with tqdm.tqdm(total=len(self.tasks)) as pbar:

                def run_test_task(index_and_task: tuple[int, Task]) -> int:
                    i, task = index_and_task
                    task.test_code(
                        results_dir=self.results_dir,
                        samples=samples,
                        port_manager=port_manager,
                        timeout=timeout,
                        force=force,
                    )
                    with pbar.get_lock():  # type: ignore[no-untyped-call]
                        pbar.update(1)
                    return 1

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.max_concurrent_runs
                ) as executor:
                    return list(executor.map(run_test_task, enumerate(self.tasks)))

    def run_bench(
        self,
        samples: list[int],
        timeout: int,
        num_ports: int,
        min_port: int,
        force: bool,
        bench_users: int | None = None,
        bench_spawn_rate: int | None = None,
        bench_run_time: int | None = None,
    ) -> list[pathlib.Path]:
        with multiprocessing.Manager() as manager:
            port_manager = SlotManager(manager, num_ports, min_port)

            total = len(self.tasks) * max(1, len(samples))
            all_paths: list[pathlib.Path] = []
            with tqdm.tqdm(total=total) as pbar:
                # Run sequentially (max_workers was already 1) so the progress bar can show
                # per-sample status deterministically.
                for task in self.tasks:
                    model_label = f"{task.model}"
                    env_label = task.env.id  # e.g. "Rust-Actix"
                    scenario_label = task.scenario.id
                    openhands_label = "true" if task.use_openhands else "false"

                    for si, sample in enumerate(samples):
                        with pbar.get_lock():  # type: ignore[no-untyped-call]
                            pbar.set_description(
                                f"{model_label} - {scenario_label} - {env_label} - openhands={openhands_label} - sample {si + 1}/{len(samples)}"
                            )
                        all_paths.extend(
                            task.bench_code(
                                results_dir=self.results_dir,
                                samples=[sample],
                                port_manager=port_manager,
                                timeout=timeout,
                                force=force,
                                remote_config=self.bench_remote_config,
                                bench_users=bench_users,
                                bench_spawn_rate=bench_spawn_rate,
                                bench_run_time=bench_run_time,
                            )
                        )
                        with pbar.get_lock():  # type: ignore[no-untyped-call]
                            pbar.update(1)

            return all_paths

    def plot_bench(
        self,
        samples: list[int],
    ) -> list[int]:
        import matplotlib.pyplot as plt

        # compare performance of different LLMs. For each LLM, we take the best performing implementation
        df = pd.DataFrame(columns=["model", "scenario", "framework", "task"])
        for task in self.tasks:
            suffix = ""
            if task.use_openhands:
                suffix = "-openhands"
            df.loc[len(df)] = [
                f"{task.model}{suffix}",
                task.scenario.id,
                f"{task.env.language}-{task.env.framework}",
                task,
            ]

        # Write aggregate plots into each save_dir/aggregate_plots/ so they live next to sample*/.
        for save_dir, df_save in df.groupby(
            df["task"].apply(lambda t: t.get_save_dir(self.results_dir))
        ):
            save_dir = pathlib.Path(save_dir)
            out_root = save_dir / "aggregate_plots"
            out_root.mkdir(parents=True, exist_ok=True)

            for (scenario,), data_s in df_save.groupby(["scenario"]):
                fig, axes = plt.subplots(1, 2, figsize=(20, 8))
                for (model,), data in data_s.groupby(["model"]):
                    plot.plot_best(data, samples, axes, self.results_dir, model)
                if axes[0].get_legend_handles_labels()[0]:
                    axes[0].legend(title="Model")
                if axes[1].get_legend_handles_labels()[0]:
                    axes[1].legend(title="Model")
                by_llm = out_root / "by_llm"
                by_llm.mkdir(parents=True, exist_ok=True)
                fig.savefig(by_llm / f"{esc(scenario)}_RPS_latency_plot.png")

                fig, axes = plt.subplots(1, 2, figsize=(20, 8))
                for (framework,), data in data_s.groupby(["framework"]):
                    plot.plot_best(data, samples, axes, self.results_dir, framework)
                if axes[0].get_legend_handles_labels()[0]:
                    axes[0].legend(title="Framework")
                if axes[1].get_legend_handles_labels()[0]:
                    axes[1].legend(title="Framework")
                by_fw = out_root / "by_framework"
                by_fw.mkdir(parents=True, exist_ok=True)
                fig.savefig(by_fw / f"{esc(scenario)}_RPS_latency_plot.png")

            plot.compare_frameworks_and_models(
                df_save, self.results_dir, samples, output_dir=out_root
            )
            plot.error_rate_vs_rps_over_time(
                df_save, self.results_dir, samples, output_dir=out_root
            )
            plot.detailed_single_app_performance(
                df_save, self.results_dir, samples, output_dir=out_root
            )

            # Additional aggregate plot: backend vs DB latency distribution by achieved RPS.
            out_dir = out_root / "backend_vs_db_latency"
            out_dir.mkdir(parents=True, exist_ok=True)
            for task in df_save["task"].tolist():
                safe_name = f"{esc(task.scenario.id)}_{esc(task.env.id)}_{esc(task.model)}"
                out_path = out_dir / f"{safe_name}_latency_by_rps.png"
                plot.plot_backend_vs_db_latency_by_rps(
                    task=task,
                    samples=samples,
                    results_dir=self.results_dir,
                    out_path=str(out_path),
                )

    def evaluate_results(
        self, samples: list[int], ks: list[int]
    ) -> TasksAndSampleResults:
        with tqdm.tqdm(total=len(self.tasks)) as pbar:
            pbar.get_lock()  # type: ignore[no-untyped-call]

            def evaluate_results_task(task: Task) -> tuple[Task, SampleTestResult]:
                rs = task.evaluate_results(
                    results_dir=self.results_dir, samples=samples, ks=ks
                )
                with pbar.get_lock():  # type: ignore[no-untyped-call]
                    pbar.update(1)
                return (task, rs)

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_concurrent_runs
            ) as executor:
                return list(executor.map(evaluate_results_task, self.tasks))

    def plot_functional_tests(self, tasks_and_results: TasksAndSampleResults) -> None:

        data: dict[str, dict[str, dict[str, float]]] = defaultdict(
            lambda: defaultdict(dict)
        )

        for task, result in tasks_and_results:
            if 1 in result.pass_at_k:
                pass_rate = result.pass_at_k[1]
                data[task.model][task.scenario.id][task.env.id] = pass_rate

        if not data:
            print("No data to plot")
            return

        output_dir = self.results_dir / "functional_tests"
        output_dir.mkdir(parents=True, exist_ok=True)

        models = sorted([m for m, scenarios_data in data.items() if scenarios_data])

        if not models:
            print("No models with data to plot")
            return

        all_scenarios = sorted(
            set(
                scenario
                for scenarios_data in data.values()
                for scenario in scenarios_data.keys()
            )
        )
        all_envs = sorted(
            set(
                env
                for scenarios_data in data.values()
                for scenario_envs in scenarios_data.values()
                for env in scenario_envs.keys()
            )
        )

        num_scenarios = len(all_scenarios)
        num_envs = len(all_envs)
        num_models = len(models)

        if num_scenarios == 0 or num_envs == 0:
            print("No scenarios or environments to plot")
            return

        fig, axes = plt.subplots(
            num_models,
            1,
            figsize=(max(12, num_scenarios * 2), 6 * num_models),
            squeeze=False,
        )

        axes = axes.flatten()

        for model_idx, model in enumerate(models):
            ax = axes[model_idx]
            scenarios_data = data[model]

            x = np.arange(num_scenarios)
            width = 0.8 / num_envs

            for env_idx, env in enumerate(all_envs):
                pass_rates = []
                for scenario in all_scenarios:
                    if scenario in scenarios_data and env in scenarios_data[scenario]:
                        pass_rates.append(scenarios_data[scenario][env])
                    else:
                        pass_rates.append(0.0)

                offset = (env_idx - num_envs / 2) * width + width / 2
                ax.bar(x + offset, pass_rates, width, label=env, alpha=0.8)

            ax.set_xlabel("Scenario", fontsize=11, fontweight="bold")
            ax.set_ylabel("Pass Rate (pass@1)", fontsize=11, fontweight="bold")
            ax.set_title(f"{model}", fontsize=13, fontweight="bold")
            ax.set_xticks(x)
            ax.set_xticklabels(all_scenarios, rotation=45, ha="right")
            ax.set_ylim(0, 1.05)
            ax.legend(
                title="Environment",
                bbox_to_anchor=(1.02, 1),
                loc="upper left",
                fontsize=9,
            )
            ax.grid(axis="y", alpha=0.3, linestyle="--")
            ax.axhline(y=1.0, color="green", linestyle="--", alpha=0.5, linewidth=1)

        fig.suptitle(
            "Functional Test Pass Rates - All Models",
            fontsize=16,
            fontweight="bold",
            y=0.995,
        )

        plt.tight_layout()

        output_path = output_dir / "all_models_functional_tests.png"
        plt.savefig(output_path, dpi=300, bbox_inches="tight")
        plt.close()

        print(f"Saved combined functional test graph to {output_path}")


def pass_at_k(k: int, c: int, n: int) -> float:
    if n - c < k:
        return 1.0
    return 1.0 - math.prod([1.0 - k / i for i in range(n - c + 1, n + 1)])
