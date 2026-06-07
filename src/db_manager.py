import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator

import docker
from docker.models.containers import Container


@dataclass
class PostgresConnectionParams:
    host: str
    port: int
    user: str
    password: str
    database: str
    
    def to_env_dict(self) -> dict[str, str]:
        return {
            "DB_HOST": self.host,
            "DB_PORT": str(self.port),
            "DB_USER": self.user,
            "DB_PASSWORD": self.password,
            "DB_NAME": self.database,
        }


class PostgresManager:
    DEFAULT_USER = "postgres"
    DEFAULT_PASSWORD = "postgres"
    DEFAULT_DATABASE = "testdb"
    
    def __init__(self, port: int, logger: logging.Logger):
        self.port = port
        self.logger = logger
        self.container_id = f"baxbench-postgres-{uuid.uuid4()}"
        self.volume_name = f"baxbench-pgdata-{uuid.uuid4()}"
        self._container: Container | None = None
        self._docker_client = docker.from_env()
    
    def start(self) -> PostgresConnectionParams:
        self.logger.info(f"Starting PostgreSQL container {self.container_id} on port {self.port}")
        
        try:
            # Create volume with unique name
            self._docker_client.volumes.create(name=self.volume_name)
            self.logger.info(f"Created volume {self.volume_name}")
            
            # Start Postgres with unique name
            self._container = self._docker_client.containers.run(
                "postgres:17-alpine",
                name=self.container_id,
                detach=True,
                ports={"5432/tcp": self.port},
                # Enable query-level timing so we can attribute latency to DB work during benchmarks.
                # NOTE: Requires restart (hence set at process start).
                command=[
                    "postgres",
                    "-c",
                    "shared_preload_libraries=pg_stat_statements",
                    "-c",
                    "pg_stat_statements.track=all",
                    "-c",
                    "track_io_timing=on",
                ],
                environment={
                    "POSTGRES_USER": self.DEFAULT_USER,
                    "POSTGRES_PASSWORD": self.DEFAULT_PASSWORD,
                    "POSTGRES_DB": self.DEFAULT_DATABASE,
                },
                network="baxbench-net",
                volumes={self.volume_name: {"bind": "/var/lib/postgresql/data", "mode": "rw"}},
                auto_remove=False,
                mem_limit=512 * 1024 * 1024,
            )
            
            self.logger.info(f"Postgres container started: {self._container.id}")
            
            self._wait_for_ready()
            self._ensure_stats_extensions()
            
            return PostgresConnectionParams(
                host=self.container_id,
                port=self.port,
                user=self.DEFAULT_USER,
                password=self.DEFAULT_PASSWORD,
                database=self.DEFAULT_DATABASE,
            )
            
        except Exception as e:
            self.logger.exception(f"Failed to start Postgres container: {e}")
            self.cleanup()
            raise
    
    def _wait_for_ready(self, timeout: float = 30.0, check_interval: float = 0.5) -> None:
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                # Use pg_isready to check if Postgres is ready
                exit_code, output = self._container.exec_run(
                    f"pg_isready -h localhost -p 5432 -U {self.DEFAULT_USER} -d {self.DEFAULT_DATABASE}"
                )
                
                if exit_code == 0:
                    self.logger.info("PostgreSQL is ready to accept connections")
                    return
                
                self.logger.debug(f"Postgres not ready yet: {output.decode()}")
                
            except Exception as e:
                self.logger.debug(f"Health check failed: {e}")
            
            time.sleep(check_interval)
        
        raise TimeoutError(
            f"Postgres container {self.container_id} did not become ready within {timeout}s"
        )

    def _ensure_stats_extensions(self) -> None:
        """
        Enable pg_stat_statements inside the database (best-effort).
        This is used for DB attribution during load benchmarking.
        """
        if self._container is None:
            return
        try:
            exit_code, output = self._container.exec_run(
                [
                    "psql",
                    "-U",
                    self.DEFAULT_USER,
                    "-d",
                    self.DEFAULT_DATABASE,
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;",
                ]
            )
            if exit_code != 0:
                self.logger.warning(
                    "Failed to create pg_stat_statements extension: %s",
                    output.decode(errors="replace"),
                )
        except Exception as e:
            self.logger.warning("Failed to enable pg_stat_statements: %s", e)

    @property
    def container(self) -> Container:
        assert self._container is not None
        return self._container
    
    def cleanup(self) -> None:
        if self._container is not None:
            try:
                self.logger.info(f"Stopping PostgreSQL container {self.container_id}")
                self._container.remove(force=True)
                self.logger.info(f"Removed container {self.container_id}")
            except Exception as e:
                self.logger.warning(f"Failed to remove container {self.container_id}: {e}")
        
        # Remove volume
        try:
            volume = self._docker_client.volumes.get(self.volume_name)
            volume.remove(force=True)
            self.logger.info(f"Removed volume {self.volume_name}")
        except Exception as e:
            self.logger.warning(f"Failed to remove volume {self.volume_name}: {e}")
    
    def __enter__(self) -> PostgresConnectionParams:
        return self.start()
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()


@contextmanager
def get_postgres_for_test(
    port: int, logger: logging.Logger
) -> Generator[PostgresConnectionParams, None, None]:
    manager = PostgresManager(port, logger)
    try:
        params = manager.start()
        yield params
    finally:
        manager.cleanup()
