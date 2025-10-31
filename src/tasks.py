import concurrent.futures
import inspect
import json
import logging
import math
import multiprocessing
import multiprocessing.managers
import os
import pathlib
import shutil
import time
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from sys import exc_info
from typing import Any, Generator, Self, cast

import requests
import tqdm
from docker.models.containers import Container

import cwes as cwe
from env.base import COMMON_DOCKER_RUN_COMMANDS, Env
from prompts import Prompter
from scenarios.base import AppInstance, FunctionalTest, Scenario, SecurityTest
from prompts_openhands import OpenHandsPrompter

def esc(s: str) -> str:
    return s.replace("/", "-")


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
    locustfile: pathlib.Path, csv_prefix: pathlib.Path, port: int, timeout: int
) -> bytes:
    try:
        result = subprocess.run([
            "locust", "--headless",
            "--locustfile", locustfile,
            "--host", f"http://localhost:{port}",
            "--users", "1800",
            "--spawn-rate", "10",
            "--run-time", "3m",
            "--csv", csv_prefix,
            "--csv-full-history",
            "--only-summary",
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return result.stdout
    except subprocess.TimeoutExpired:
        raise TimeoutError("Benchmarking timed out")

import pandas as pd
import matplotlib.pyplot as plt
from typing import Optional, Tuple

def plot_requests_vs_percentile(
    csv_path: str,
    x_col: str = "Requests/s",
    x_col2: str = "Failures/s",
    y_col: str = "99%",                # any percentile column, e.g. "95%", "99.9%", etc.
    name_col: str = "Name",
    name_value: str = "Aggregated",
    decreasing_run: int = 5,           # consecutive strictly-decreasing points to trigger cutoff
    cutoff_delta: int = 0,             # keep rows up to (start_index_of_run + cutoff_delta), inclusive
    ax: Optional[plt.Axes] = None,     # pass an existing axes to draw on, or leave None to create one
    **plot_kwargs,                     # e.g. linewidth=2, marker="o"
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
    _container: Container | None = None
    _port: int | None = None

    def __enter__(self) -> Self:
        while self._port is None:
            self._port = self.port_manager.acquire_slot()
            time.sleep(0.1)
        try:
            self._container = self.env.run_docker_container(self.image_id, self._port)
        except Exception as e:
            self.logger.exception("could not start container %s", e, exc_info=e)
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
        assert self.container is not None
        assert self._port is not None
        container_logs = cast(
            bytes, self.container.logs(stdout=True, stderr=True, follow=False)
        )
        self.logger.info("container logs:\n%s", container_logs.decode())
        self.container.remove(force=True)
        self.port_manager.release_slot(self._port)
        self.logger.info("-" * 100)
        self.logger.info("removed container")
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
    openhands_agent_cls: str
    openhands_max_iterations: int
    openhands_max_cost: float | None 
    openhands_max_tokens: int | None
    provider: str | None

    @property
    def id(self) -> str:
        base_id = f"{self.model}-{self.env.id}-{self.scenario.id}-{self.spec_type}-{self.safety_prompt}-{self.temperature}"
        if self.use_openhands:
            return f"{base_id}-openhands-{self.openhands_agent_cls}"
        return base_id

    @contextmanager
    def create_logger(
        self, logfile_path: pathlib.Path
    ) -> Generator[logging.Logger, None, None]:
        logger = logging.getLogger(self.id)
        logger.setLevel(logging.INFO)
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
            results_dir
            / esc(self.model)
            / esc(self.scenario.id)
            / esc(self.env.id)
        )
        if self.use_openhands:
            save_dir = base_dir / f"temp{float(self.temperature)}-{esc(self.spec_type)}-{esc(self.safety_prompt)}-openhands"
        else:
            save_dir = base_dir / f"temp{float(self.temperature)}-{esc(self.spec_type)}-{esc(self.safety_prompt)}"
        return save_dir

    def get_sample_dir(self, results_dir: pathlib.Path, sample: int) -> pathlib.Path:
        return self.get_save_dir(results_dir) / f"sample{sample}"

    def get_code_dir(self, results_dir: pathlib.Path, sample: int) -> pathlib.Path:
        return self.get_sample_dir(results_dir, sample) / "code"

    def get_test_results_json_path(
        self, results_dir: pathlib.Path, sample: int
    ) -> pathlib.Path:
        return self.get_sample_dir(results_dir, sample) / "test_results.json"

    def get_bench_results_csv_prefix(
        self, results_dir: pathlib.Path, sample: int
    ) -> pathlib.Path:
        return self.get_sample_dir(results_dir, sample) / "bench_results"

    def get_bench_results_csv_path(
        self, results_dir: pathlib.Path, sample: int
    ) -> pathlib.Path:
        return self.get_sample_dir(results_dir, sample) / "bench_results_stats_history.csv"

    def load_code(
        self,
        results_dir: pathlib.Path,
        sample: int,
        logger: logging.Logger | None = None,
    ) -> dict[pathlib.Path, str]:
        code_dir = self.get_code_dir(results_dir, sample)
        files: dict[pathlib.Path, str] = {}
        for root, _, file_names in os.walk(code_dir):
            for file in file_names:
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
                logger.info(F"Using OpenHands agent for code generation")

                prompter_oh = OpenHandsPrompter(
                    env=self.env,
                    scenario=self.scenario,
                    model=self.model,
                    spec_type=self.spec_type,
                    safety_prompt=self.safety_prompt,
                    temperature=self.temperature,
                    agent_cls=self.openhands_agent_cls,
                    max_iterations=self.openhands_max_iterations,
                    provider=self.provider,
                    max_cost=self.openhands_max_cost,
                    max_tokens=self.openhands_max_tokens,
                )
                logger.info("Built agent task:\n%s", prompter_oh.task)
                for sample in range(last_sample + 1, last_sample + 1 + batch_size):
                    try:
                        logger.info(f"Generating sample {sample} with OpenHands...")
                        code_dir = prompter_oh.generate_code_with_agent(
                            sample_id=sample,
                            save_dir=self.get_save_dir(results_dir),
                            logger=logger,
                        )
                        logger.info(f"Generated code saved to {code_dir}")
                        logger.info("-" * 80)
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        logger.exception(f"OpenHands agent failed for sample {sample}: {e}", exc_info=e)
                        continue

            else:
                logger.info(F"Using single-prompt LLM ({self.model}) for code generation")

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
                    for extension in ("*.log", "*.json"):
                        for file_path in sample_dir.glob(extension):
                            if file_path.is_file():
                                file_path.unlink()
        for sample in samples:
            sample_dir = self.get_sample_dir(results_dir, sample)
            if not self.get_code_dir(results_dir, sample).exists():
                continue
            if (
                self.get_test_results_json_path(results_dir, sample).exists()
                and not force
            ):
                continue
            self.get_test_results_json_path(results_dir, sample).unlink(missing_ok=True)
            log_file = sample_dir / "test.log"
            with self.create_logger(log_file) as logger:
                files: dict[pathlib.Path, str] = self.load_code(
                    results_dir, sample, logger
                )
                try:
                    image_id = self.env.build_docker_image(
                        files,
                        COMMON_DOCKER_RUN_COMMANDS
                        + self.scenario.needed_packages.get("_all_", [])
                        + self.scenario.needed_packages.get(self.env.language, []),
                        logger,
                        no_cache=False,
                    )
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
                    except Exception as e:
                        logger.exception(
                            f"Failed to build docker image without cache, got exception:\n{str(e)}",
                            exc_info=e,
                        )
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
                for ft in self.scenario.functional_tests:
                    logger.info("running functional test:\n%s", inspect.getsource(ft))

                    passed = False
                    had_exception = False
                    try:
                        with ContainerRunner(
                            self.env, port_manager, image_id, logger
                        ) as cr:
                            server_ran_before = self.env.process_still_running(
                                cr.container.id, logger
                            )
                            passed = run_test_with_timeout(
                                ft,
                                AppInstance(
                                    port=cr.port,
                                    log_file_path=sample_dir / (ft.__name__ + ".log"),
                                    container_id=cr.container.id,
                                    env=self.env,
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
                                    log_file_path=sample_dir / (st.__name__ + ".log"),
                                    container_id=cr.container.id,
                                    env=self.env,
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
    ) -> None:
        # clean the directory from bench artifacts if entered by force
        if force:
            for sample in samples:
                sample_dir = self.get_sample_dir(results_dir, sample)
                if sample_dir.exists():
                    for extension in ("bench.log", "*.csv"):
                        for file_path in sample_dir.glob(extension):
                            if file_path.is_file():
                                file_path.unlink()
        for sample in samples:
            sample_dir = self.get_sample_dir(results_dir, sample)
            if not self.get_code_dir(results_dir, sample).exists():
                continue
            if (
                self.get_bench_results_csv_path(results_dir, sample).exists()
                and not force
            ):
                continue

            test_result_path = self.get_test_results_json_path(results_dir, sample)
            if not test_result_path.exists():
                continue
            else:
                with open(test_result_path, "r") as f:
                    test_result = TestResult.from_dict(json.load(f))
                    if test_result.num_passed_ft < test_result.num_total_ft:
                        continue
            test_log_file = sample_dir / "test.log"
            pattern = re.compile(r"sha256:[0-9a-f]{64}")
            with open(test_log_file, "r", encoding="utf-8") as f:
                for line in f:
                    match = pattern.search(line)
                    if match:
                        image_id = match.group(0)
                        break
                if image_id is None:
                    continue

            log_file = sample_dir / "bench.log"
            with self.create_logger(log_file) as logger:
                logger.info("got docker image. id: %s", image_id)
                logger.info("-" * 100)

                from scenario_files import SCENARIO_FILE_PATH
                locustfile = SCENARIO_FILE_PATH.joinpath(f"locustfiles/{self.scenario.id.lower()}.py")
                logger.info("running load benchmark:\n%s", locustfile.read_text())
                csv_prefix = self.get_bench_results_csv_prefix(results_dir, sample)

                try:
                    with ContainerRunner(
                        self.env, port_manager, image_id, logger
                    ) as cr:
                        server_ran_before = self.env.process_still_running(
                            cr.container.id, logger
                        )
                        locust_logs = run_bench_with_timeout(
                            locustfile,
                            csv_prefix,
                            cr.port,
                            timeout,
                        )
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

    def plot_one(
        self,
        results_dir: pathlib.Path,
        samples: list[int],
        ax: plt.Axes,
    ) -> None:
        for sample in samples:
            csv_path = self.get_bench_results_csv_path(results_dir, sample)
            if not csv_path.exists():
                continue
            plot_requests_vs_percentile(csv_path, ax=ax, label=self.env.id)

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
    ):
        self.tasks = tasks
        self.results_dir = results_dir
        self.max_concurrent_runs = max_concurrent_runs

    def run_generation(
        self,
        batch_size: int,
        max_retries: int,
        base_delay: float,
        max_delay: float,
        force: bool,
        skip_failed: bool,
        vllm_port: int,
    ) -> list[int]:
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
    ) -> list[int]:
        with multiprocessing.Manager() as manager:
            port_manager = SlotManager(manager, num_ports, min_port)

            with tqdm.tqdm(total=len(self.tasks)) as pbar:

                def run_bench_task(index_and_task: tuple[int, Task]) -> int:
                    i, task = index_and_task
                    task.bench_code(
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
                    max_workers=1
                ) as executor:
                    return list(executor.map(run_bench_task, enumerate(self.tasks)))

    def plot_bench(
        self,
        samples: list[int],
    ) -> list[int]:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10,8))

        for task in self.tasks:
            task.plot_one(
                results_dir=self.results_dir,
                samples=samples,
                ax=ax,
            )
        
        ax.set_xlabel("Achived RPS")
        ax.set_ylabel("P99 [ms]")
        ax.legend()
        ax.set_ylim((0, 500))
        fig.savefig("test_plot.png")

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
            

def pass_at_k(k: int, c: int, n: int) -> float:
    if n - c < k:
        return 1.0
    return 1.0 - math.prod([1.0 - k / i for i in range(n - c + 1, n + 1)])
