import inspect
import json
import logging
import multiprocessing
import multiprocessing.managers
import os
import pathlib
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator, Self, cast

import requests
from docker.models.containers import Container

import cwes as cwe
from db_manager import PostgresConnectionParams, PostgresManager
from env.base import COMMON_DOCKER_RUN_COMMANDS, Env
from scenarios.base import AppInstance, FunctionalTest, Scenario, SecurityTest


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
        return f"{self.model}-{self.env.id}-{self.scenario.id}-{self.spec_type}-{self.safety_prompt}-{self.temperature}"

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

    def get_sample_dir(self, results_dir: pathlib.Path, sample: int) -> pathlib.Path:
        return self.get_save_dir(results_dir) / f"sample{sample}"

    def get_code_dir(self, results_dir: pathlib.Path, sample: int) -> pathlib.Path:
        return self.get_sample_dir(results_dir, sample) / "code"

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
