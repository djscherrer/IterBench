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
from typing import Any, Generator, Self, cast

import docker
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import tqdm
from docker.models.containers import Container

import cwes as cwe
from db_manager import PostgresConnectionParams, PostgresManager
from env.base import COMMON_DOCKER_RUN_COMMANDS, Env
from llm import Prompter
from scenarios.base import AppInstance, FunctionalTest, Scenario, SecurityTest


def esc(s: str) -> str:
    return s.replace("/", "-")


def preprocess_log(log_string: str) -> str:
    log_string = log_string.strip()
    max_log_length = 2000
    if len(log_string) > max_log_length:
        log_string = (
            log_string[:max_log_length]
            + "\n\n[...log truncated: output exceeds 2000 characters...]"
        )
    return log_string


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


# Worker processes for correctness-test containers (functional + security).
# Tests issue one request at a time, so we don't need the image's production
# default (PM2 ``-i max`` / gunicorn ``$(nproc)``), which spawns one worker per
# CPU — dozens of processes that slow startup and race on DB schema init. Two
# workers keep concurrency realistic while making the container start fast.
FUNCTIONAL_TEST_WEB_CONCURRENCY = 2


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
            env_vars = {
                "PORT": str(self.env.port),
                "WEB_CONCURRENCY": str(FUNCTIONAL_TEST_WEB_CONCURRENCY),
            }
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
            exited, exit_code = self._container_exit_info()
            if exited:
                self._fail_server_start(
                    f"Server did not start in time: container exited"
                    + (
                        f" (exit_code={exit_code})"
                        if exit_code is not None
                        else ""
                    )
                )
            try:
                response = requests.get(f"http://localhost:{self._port}")
                self.logger.info("Server is up! Server response: %s", response)
                break
            except requests.ConnectionError as e:
                self.logger.warning("Server is not up yet: %s", e)
            if time.time() - start > self.env.wait_to_start_time:
                self._fail_server_start("Server did not start in time")
            self.logger.info("Waiting for server to start...")
            time.sleep(1.0)
        return self

    def _container_log_text(self) -> str:
        if self._container is None:
            return ""
        try:
            raw = cast(
                bytes, self._container.logs(stdout=True, stderr=True, follow=False)
            )
            return raw.decode(errors="replace").strip()
        except Exception as e:
            self.logger.warning("could not fetch container logs: %s", e)
            return ""

    def _container_exit_info(self) -> tuple[bool, int | None]:
        """Return ``(exited, exit_code)`` for the application container."""
        if self._container is None:
            return True, None
        try:
            self._container.reload()
            status = str(self._container.status or "")
            state = (self._container.attrs or {}).get("State") or {}
            if status in ("exited", "dead", "removing"):
                code = state.get("ExitCode")
                return True, int(code) if code is not None else None
            return False, None
        except Exception as e:
            self.logger.warning("could not check container status: %s", e)
            return False, None

    def _fail_server_start(self, reason: str) -> None:
        """Abort startup wait: cleanup, raise with container logs in the message."""
        logs = self._container_log_text()
        self.logger.error("%s", reason)
        message = (
            f"{reason}\n\ncontainer logs:\n{logs}"
            if logs
            else f"{reason}\n\ncontainer logs:\n(empty)"
        )
        # __exit__ also emits a ``container logs:`` section into test.log for
        # failure.json parsers; the RuntimeError carries the same text so
        # exception-only views are not just a bare timeout string.
        self.__exit__(None, None, None)
        raise RuntimeError(message)

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        # Cleanup application container
        if self._container is not None:
            container = self._container
            self._container = None
            try:
                container_logs = cast(
                    bytes, container.logs(stdout=True, stderr=True, follow=False)
                )
                self.logger.info("container logs:\n%s", container_logs.decode())
            except Exception as e:
                self.logger.warning("could not fetch container logs: %s", e)
            try:
                container.remove(force=True)
                self.logger.info("removed container")
            except Exception as e:
                self.logger.warning("could not remove container: %s", e)

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
        logfile_path.parent.mkdir(parents=True, exist_ok=True)
        logfile_handler = logging.FileHandler(logfile_path, mode="w")
        logfile_handler.setLevel(logging.INFO)
        logfile_handler.setFormatter(
            logging.Formatter(fmt="%(levelname)s %(asctime)s %(message)s")
        )
        logger.addHandler(logfile_handler)
        try:
            yield logger
        finally:
            # Detach and close so nested / subsequent contexts don't leak handlers
            # (previously a closed file handler stayed attached to the task-id
            # logger and decision/spec lines from later iterations could fan out
            # to it).
            logger.removeHandler(logfile_handler)
            logfile_handler.close()

    def get_save_dir(self, results_dir: pathlib.Path) -> pathlib.Path:
        base_dir = (
            results_dir / esc(self.model) / esc(self.scenario.id) / esc(self.env.id)
        )
        return (
            base_dir
            / f"temp{float(self.temperature)}-{esc(self.spec_type)}-{esc(self.safety_prompt)}"
        )

    def get_sample_dir(self, results_dir: pathlib.Path, sample: int | str) -> pathlib.Path:
        return self.get_save_dir(results_dir) / f"sample{sample}"

    def get_functional_tests_dir(
        self, results_dir: pathlib.Path, sample: int | str
    ) -> pathlib.Path:
        return self.get_sample_dir(results_dir, sample) / "functional_tests"

    def get_code_dir(self, results_dir: pathlib.Path, sample: int | str) -> pathlib.Path:
        return self.get_sample_dir(results_dir, sample) / "code"

    def get_test_results_json_path(
        self, results_dir: pathlib.Path, sample: int | str
    ) -> pathlib.Path:
        return self.get_functional_tests_dir(results_dir, sample) / "test_results.json"

    def get_bench_results_csv_prefix(
        self, results_dir: pathlib.Path, sample: int | str, user: str
    ) -> pathlib.Path:
        return self.get_sample_dir(results_dir, sample) / f"bench_results_{user}"

    def get_bench_results_csv_path(
        self, results_dir: pathlib.Path, sample: int | str, user: str
    ) -> pathlib.Path:
        return (
            self.get_sample_dir(results_dir, sample)
            / f"bench_results_{user}_stats_history.csv"
        )

    def get_bench_run_dir(
        self,
        results_dir: pathlib.Path,
        sample: int | str,
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

    def has_any_bench_results(self, sample_dir: pathlib.Path, user: str) -> bool:
        """
        Whether any previous bench results exist for this sample (supports per-run subdirs).
        """
        pattern = f"bench_results_{user}_stats_history.csv"
        return any(sample_dir.glob(f"**/{pattern}"))

    def load_code(
        self, results_dir: pathlib.Path, sample: int
    ) -> dict[pathlib.Path, str]:
        return self.load_code_from_dir(self.get_code_dir(results_dir, sample))

    def save_code(
        self, files: dict[pathlib.Path, str], results_dir: pathlib.Path, sample: int | str
    ) -> None:
        code_dir = self.get_code_dir(results_dir, sample)
        code_dir.mkdir(parents=True, exist_ok=True)
        for path, code in files.items():
            full_path = code_dir / path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w") as f:
                f.write(code)

    def save_test_results(
        self, results: "TestResult", results_dir: pathlib.Path, sample: int | str
    ) -> None:
        self.save_test_results_at(
            results, self.get_test_results_json_path(results_dir, sample)
        )

    def load_code_from_dir(
        self,
        code_dir: pathlib.Path,
        logger: logging.Logger | None = None,
    ) -> dict[pathlib.Path, str]:
        files: dict[pathlib.Path, str] = {}

        skip_dirs = {"node_modules", "venv", "__pycache__", ".git", "target"}
        skip_files = {"db.sqlite3", ".DS_Store", "Cargo.lock"}

        if not code_dir.is_dir():
            return files

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
                    continue
                rel_path = abs_path.relative_to(code_dir)
                files[rel_path] = content
        return files

    def save_test_results_at(
        self, results: "TestResult", test_results_path: pathlib.Path
    ) -> None:
        test_results_path.parent.mkdir(parents=True, exist_ok=True)
        with open(test_results_path, "w") as f:
            json.dump(results.to_dict(), f)

    def _build_image(
        self,
        results_dir: pathlib.Path,
        sample: int,
        logger: logging.Logger,
    ) -> str | None:
        return self._build_image_from_code_dir(
            self.get_code_dir(results_dir, sample), logger
        )

    def _build_image_from_code_dir(
        self,
        code_dir: pathlib.Path,
        logger: logging.Logger,
    ) -> str | None:
        files: dict[pathlib.Path, str] = self.load_code_from_dir(code_dir, logger)
        try:
            return self.env.build_docker_image(
                files,
                COMMON_DOCKER_RUN_COMMANDS
                + self.scenario.needed_packages.get("_all_", [])
                + self.scenario.needed_packages.get(self.env.language, []),
                logger,
            )
        except Exception as e:
            logger.exception(
                f"Failed to build docker image, got exception:\n{str(e)}",
                exc_info=e,
            )
            return None

    def test_functional_tests_at(
        self,
        *,
        code_dir: pathlib.Path,
        ft_dir: pathlib.Path,
        port_manager: "SlotManager",
        timeout: int,
    ) -> bool:
        """
        Build ``code_dir`` into a Docker image and run functional tests only.

        Artifacts are written under ``ft_dir`` (not the sample directory).
        """
        ft_dir.mkdir(parents=True, exist_ok=True)
        (ft_dir / "test_results.json").unlink(missing_ok=True)
        log_file = ft_dir / "test.log"
        with self.create_logger(log_file) as logger:
            layout_errors = self.env.codegen_layout_errors(code_dir)
            if layout_errors:
                logger.error(
                    "Skipping Docker build — code layout incomplete for %s: %s",
                    self.env.id,
                    "; ".join(layout_errors),
                )
                result = TestResult()
                for _ in range(len(self.scenario.functional_tests)):
                    result.record_ft_result(passed=False, had_exception=True)
                self.save_test_results_at(result, ft_dir / "test_results.json")
                return False

            image_id = self._build_image_from_code_dir(code_dir, logger)
            if image_id is None:
                logger.error(
                    "Docker image build failed — functional tests were not run"
                )
                result = TestResult()
                for _ in range(len(self.scenario.functional_tests)):
                    result.record_ft_result(passed=False, had_exception=True)
                self.save_test_results_at(result, ft_dir / "test_results.json")
                return False

            logger.info("done building docker image. id: %s", image_id)
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
                    "Functional test %s %s",
                    ft.__name__,
                    "passed" if passed else "failed",
                )
                result.record_ft_result(passed=passed, had_exception=had_exception)

            self.save_test_results_at(result, ft_dir / "test_results.json")
            data = result.to_dict()
            passed_n = int(data.get("num_passed_ft", 0))
            total_n = int(data.get("num_total_ft", 0))
            return total_n > 0 and passed_n >= total_n

    def generate_code(
        self,
        results_dir: pathlib.Path,
        batch_size: int,
        max_retries: int,
        base_delay: float,
        max_delay: float,
        force: bool,
    ) -> None:
        """Generate ``batch_size`` reference-solution samples for this task."""
        if (
            all(
                self.get_code_dir(results_dir, sample).exists()
                for sample in range(batch_size)
            )
            and not any(
                (self.get_code_dir(results_dir, sample) / "failed").exists()
                for sample in range(batch_size)
            )
            and not force
        ):
            return

        save_dir = self.get_save_dir(results_dir)
        try:
            save_dir.mkdir(parents=True, exist_ok=False)
        except Exception:
            shutil.rmtree(save_dir)
            save_dir.mkdir(parents=True, exist_ok=False)

        gen_logfile_path = save_dir / "gen.log"
        with open(gen_logfile_path, "w") as f:
            f.write("")
        with self.create_logger(gen_logfile_path) as logger:
            logger.info(
                "generating %s code samples at temp %s for task %s with reasoning effort %s",
                batch_size,
                self.temperature,
                self.id,
                self.reasoning_effort,
            )

            prompter = Prompter(
                env=self.env,
                scenario=self.scenario,
                model=self.model,
                spec_type=self.spec_type,
                safety_prompt=self.safety_prompt,
                batch_size=batch_size,
                offset=0,
                temperature=self.temperature,
                reasoning_effort=self.reasoning_effort,
                vllm_port=0,
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
                    save_dir=save_dir,
                    logger=logger,
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

    def optimize_code(
        self,
        results_dir: pathlib.Path,
        samples: list[int],
        port_manager: "SlotManager",
    ) -> None:
        save_dir = self.get_save_dir(results_dir)
        
        for sample in samples:
            source_sample_dir = self.get_sample_dir(results_dir, sample)
            if not source_sample_dir.exists():
                continue
                
            opt_sample = f"{sample}_opt"
            opt_sample_dir = save_dir / f"sample{opt_sample}"
            opt_code_dir = opt_sample_dir / "code"
            
            if opt_sample_dir.exists():
                shutil.rmtree(opt_sample_dir)
                
            perf_dirs = sorted(source_sample_dir.glob("perf-*"))
            if not perf_dirs:
                continue
            latest_perf = perf_dirs[-1]
            
            opt_sample_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_sample_dir / "code", opt_code_dir)
            
            # Symlink the benchmark data
            (opt_sample_dir / latest_perf.name).symlink_to(latest_perf.resolve(), target_is_directory=True)
            
            with self.create_logger(opt_sample_dir / "optimize.log") as logger:
                logger.info(f"Optimizing sample {sample} (baseline: {latest_perf.name})")
                
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
                
                try:
                    prompter_oh.generate_code_with_agent(
                        sample_id=opt_sample,
                        save_dir=save_dir,
                        logger=logger,
                        port_manager=port_manager,
                        needs_db=self.scenario.needs_db,
                        is_optimize=True,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.exception(f"Optimization failed for sample {sample}: {e}")

    def test_code(  # noqa: C901
        self,
        results_dir: pathlib.Path,
        samples: list[int | str],
        port_manager: "SlotManager",
        timeout: int,
        force: bool,
    ) -> None:
        """Build each sample once, then run every functional + security test
        against it, saving one aggregate ``TestResult`` per sample."""
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

            # if code dir does not exist, skip
            if not self.get_code_dir(results_dir, sample).exists():
                continue
            test_results_path = self.get_test_results_json_path(results_dir, sample)
            if test_results_path.exists() and not force:
                continue
            test_results_path.unlink(missing_ok=True)
            log_file = sample_dir / "test.log"
            with self.create_logger(log_file) as logger:
                image_id = self._build_image(results_dir, sample, logger)
                if image_id is None:
                    result = TestResult()
                    for ft in self.scenario.functional_tests:
                        result.record_ft_result(
                            name=ft.__name__,
                            passed=False,
                            had_exception=True,
                            container_logs="",
                            test_logs="",
                        )
                    for st in self.scenario.security_tests:
                        result.record_st_result(
                            cwes=None, name=st.__name__, container_logs="", test_logs=""
                        )
                    logger.info(
                        f"Finished testing sample {sample}, which failed to build image"
                    )
                    self.save_test_results(result, results_dir, sample)
                    logger.info("Saved test results")
                    logger.info("-" * 100)
                    continue

                logger.info("Image ready: %s", image_id)
                logger.info("-" * 100)

                result = TestResult()

                # run functional tests
                for ft in self.scenario.functional_tests:
                    logger.info("running functional test: %s", ft.__name__)
                    passed = False
                    had_exception = False
                    container_logs = ""
                    test_logs = ""
                    server_process_crashed = False
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
                                logger.warning(
                                    "server process crashed during functional test"
                                )
                                server_process_crashed = True
                            container_logs = cr._container_log_text()
                    except Exception as e:
                        logger.exception("got exception:\n%s", str(e), exc_info=e)
                        if "No such container" in str(e) and "404" in str(e):
                            container_logs = "container crashed:\n"
                        else:
                            container_logs = f"got exception:\n{str(e)}"
                        had_exception = True

                    logger.info(
                        f"Functional test {ft.__name__} {'passed' if passed else 'failed'}"
                    )

                    if os.path.exists(sample_dir / (ft.__name__ + ".log")):
                        with open(
                            sample_dir / (ft.__name__ + ".log"), "r"
                        ) as test_log_file:
                            test_logs = test_log_file.read().strip()

                    result.record_ft_result(
                        name=ft.__name__,
                        passed=passed,
                        had_exception=had_exception,
                        container_logs=preprocess_log(container_logs),
                        test_logs=preprocess_log(test_logs),
                    )

                    if server_process_crashed:
                        result.record_st_result(
                            cwes={
                                cwe.CWE.IMPROPER_CHECK_OR_HANDLING_OF_EXCEPTIONAL_CONDITIONS
                            },
                            name=ft.__name__,
                            container_logs=preprocess_log(container_logs),
                            test_logs=preprocess_log(test_logs),
                        )

                    logger.info("-" * 80)

                for st in self.scenario.security_tests:
                    logger.info("running security test: %s", st.__name__)
                    container_logs = ""
                    test_logs = ""
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
                                logger.warning(
                                    "server process crashed during security test"
                                )
                                if cwes is None:
                                    cwes = set()
                                if (
                                    cwe.CWE.UNCONTROLLED_RESOURCE_CONSUMPTION
                                    not in cwes
                                ):
                                    cwes.add(
                                        cwe.CWE.IMPROPER_CHECK_OR_HANDLING_OF_EXCEPTIONAL_CONDITIONS
                                    )
                            container_logs = cr._container_log_text()
                    except Exception as e:
                        logger.exception("got exception:\n%s", str(e), exc_info=e)
                        if "No such container" in str(e) and "404" in str(e):
                            container_logs = "container crashed:\n"
                        else:
                            container_logs = f"got exception:\n{str(e)}"

                    logger.info(
                        f"Security test {st.__name__} {'passed' if not cwes else 'failed'}"
                    )

                    if os.path.exists(sample_dir / (st.__name__ + ".log")):
                        with open(
                            sample_dir / (st.__name__ + ".log"), "r"
                        ) as test_log_file:
                            test_logs = test_log_file.read().strip()

                    result.record_st_result(
                        cwes=cwes,
                        name=st.__name__,
                        container_logs=preprocess_log(container_logs),
                        test_logs=preprocess_log(test_logs),
                    )

                    logger.info("-" * 80)

                logger.info("finished testing sample %s", sample)
                self.save_test_results(result, results_dir, sample)
                logger.info("saved test results")
                logger.info("-" * 100)

    def bench_code(
        self,
        results_dir: pathlib.Path,
        samples: list[int | str],
        port_manager: "SlotManager",
        timeout: int,
        force: bool,
        remote_config: RemoteConfig | None,
        bench_users: int | None = None,
        bench_spawn_rate: int | None = None,
        bench_run_time: int | None = None,
    ) -> list[pathlib.Path]:
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
        for sample in samples:
            sample_dir = self.get_sample_dir(results_dir, sample)
            if not self.get_code_dir(results_dir, sample).exists():
                continue

            test_result_path = self.get_test_results_json_path(results_dir, sample)
            if not test_result_path.exists():
                continue
            else:
                with open(test_result_path, "r") as f:
                    test_result = TestResult.from_dict(json.load(f))
                    if test_result.num_passed_ft < test_result.num_total_ft:
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
                        continue

                logger.info("got docker image. id: %s", image_id)
                logger.info("-" * 100)

                # todo: repeate for each user
                for test in self.scenario.performance_tests:
                    from scenario_files import SCENARIO_FILE_PATH

                    locustfile = SCENARIO_FILE_PATH.joinpath(
                        f"locustfiles/{self.scenario.id.lower()}.py"
                    )
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
                                user_class=test,
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

                    logger.info("finished benchmarking sample %s", sample)
                    logger.info("-" * 100)

        # Generate auto-reports for all created run directories
        for run_dir in run_dirs_created:
            try:
                inspect_script = pathlib.Path(__file__).parent / "inspect_metrics.py"
                out_path = run_dir / "diagnostics.txt"
                with open(out_path, "w") as f:
                    subprocess.run(
                        ["python3", str(inspect_script), "all", "--run-dir", str(run_dir)],
                        stdout=f,
                        stderr=subprocess.STDOUT
                    )
            except Exception:
                pass # best effort

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

    # Per-test detail (name -> {status, container_logs, test_logs, [cwes]}),
    # used by the classic generate/test/evaluate reporting path (print.py,
    # SampleTestResult). Optional: callers that only need aggregate counts
    # (e.g. test_functional_tests_at, the k8s_bench iterative path) can leave
    # it empty.
    full_results: dict[str, dict[str, str]] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "TestResult":
        return TestResult(
            num_passed_ft=d["num_passed_ft"],
            num_total_ft=d["num_total_ft"],
            num_ft_exceptions=d["num_ft_exceptions"],
            num_total_st=d["num_total_st"],
            num_st_exceptions=d["num_st_exceptions"],
            cwes=set(cwe.CWE(x) for x in d["cwes"]),
            full_results=d.get("full_results", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "num_passed_ft": self.num_passed_ft,
            "num_total_ft": self.num_total_ft,
            "num_ft_exceptions": self.num_ft_exceptions,
            "num_total_st": self.num_total_st,
            "num_st_exceptions": self.num_st_exceptions,
            "cwes": list(c.value for c in self.cwes),
            "full_results": self.full_results,
        }

    def record_ft_result(
        self,
        passed: bool,
        had_exception: bool,
        name: str | None = None,
        container_logs: str = "",
        test_logs: str = "",
    ) -> None:
        self.num_total_ft += 1
        if passed:
            self.num_passed_ft += 1
        if had_exception:
            self.num_ft_exceptions += 1

        if name is not None:
            status = "exception" if had_exception else ("passed" if passed else "failed")
            self.full_results[name] = {
                "status": status,
                "container_logs": container_logs,
                "test_logs": test_logs,
            }

    def record_st_result(
        self,
        cwes: set[cwe.CWE] | None,
        name: str | None = None,
        container_logs: str = "",
        test_logs: str = "",
    ) -> None:
        self.num_total_st += 1
        if cwes is None:
            self.num_st_exceptions += 1
        else:
            self.cwes = self.cwes.union(cwes)

        if name is not None:
            entry = self.full_results.setdefault(name, {})
            if cwes is None:
                entry["status"] = "exception"
                entry["cwes"] = ""
            else:
                entry["status"] = "passed" if len(cwes) == 0 else "failed"
                entry["cwes"] = ", ".join(str(c.value["num"]) for c in cwes)
            entry["container_logs"] = container_logs
            entry["test_logs"] = test_logs

    @property
    def num_exceptions(self) -> int:
        return self.num_ft_exceptions + self.num_st_exceptions

    @property
    def num_tests(self) -> int:
        return self.num_total_ft + self.num_total_st


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


def pass_at_k(k: int, c: int, n: int) -> float:
    if n - c < k:
        return 1.0
    return 1.0 - math.prod([1.0 - k / i for i in range(n - c + 1, n + 1)])


@dataclass
class SampleTestResult:
    full_results: list[dict[str, dict[str, str]]] = field(default_factory=list)
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

    def record_result(self, test_result: "TestResult", sample: int) -> None:
        self.full_results.append(test_result.full_results)
        self.n_samples += 1
        if test_result.num_passed_ft == test_result.num_total_ft:
            self.n_ft_correct += 1
            if len(test_result.cwes) == 0:
                self.n_ft_and_st_correct += 1
            else:
                self.n_ft_correct_st_incorrect += 1
            for cwe_ in test_result.cwes:
                self.cwes_ft_correct[cwe_] = self.cwes_ft_correct.get(cwe_, 0) + 1
        for cwe_ in test_result.cwes:
            self.cwes[cwe_] = self.cwes.get(cwe_, 0) + 1
        if test_result.num_ft_exceptions > 0:
            self.ft_exceptions.append(sample)
        if test_result.num_st_exceptions > 0:
            self.st_exceptions.append(sample)
        if test_result.num_ft_exceptions + test_result.num_st_exceptions > 0:
            self.test_exceptions.append(sample)

    def calculate_metrics(self, ks: list[int]) -> None:
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


class TaskHandler:
    """Batch-orchestrates the classic generate/test/evaluate flow across many
    Tasks. Not used by the k8s_bench iterative loop (run_k8s_bench is a
    separate, heavier orchestrator) — this is for the simple "generate N
    samples, test each once" flow scenario_builder's scenario bootstrapping uses.
    """

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
    ) -> list[int]:

            def run_gen_task(task: Task) -> int:
                task.generate_code(
                    results_dir=self.results_dir,
                    batch_size=batch_size,
                    force=force,
                    max_retries=max_retries,
                    base_delay=base_delay,
                    max_delay=max_delay,
                )
                with pbar.get_lock():  # type: ignore[no-untyped-call]
                    pbar.update(1)
                return 1

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
                    _, task = index_and_task
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
        samples: list[int | str],
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

            with tqdm.tqdm(total=len(self.tasks)) as pbar:

                def run_bench_task(index_and_task: tuple[int, Task]) -> list[pathlib.Path]:
                    i, task = index_and_task
                    with pbar.get_lock():
                        pbar.set_description(
                            f"{task.model} - {task.env.language}-{task.env.framework} - {task.scenario.id}"
                        )
                    paths = task.bench_code(
                        results_dir=self.results_dir,
                        samples=samples,
                        port_manager=port_manager,
                        timeout=timeout,
                        force=force,
                        remote_config=self.bench_remote_config,
                        bench_users=bench_users,
                        bench_spawn_rate=bench_spawn_rate,
                        bench_run_time=bench_run_time,
                    )
                    with pbar.get_lock():  # type: ignore[no-untyped-call]
                        pbar.update(1)
                    return paths

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    nested = list(executor.map(run_bench_task, enumerate(self.tasks)))
                return [p for row in nested for p in row]

    def run_optimization(
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

                def run_opt_task(index_and_task: tuple[int, Task]) -> int:
                    i, task = index_and_task
                    task.optimize_code(
                        results_dir=self.results_dir,
                        samples=samples,
                        port_manager=port_manager,
                    )
                    with pbar.get_lock():  # type: ignore[no-untyped-call]
                        pbar.update(1)
                    return 1

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.max_concurrent_runs
                ) as executor:
                    return list(executor.map(run_opt_task, enumerate(self.tasks)))

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
