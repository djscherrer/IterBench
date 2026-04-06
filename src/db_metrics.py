import csv
import threading
import time
from dataclasses import dataclass

from docker.models.containers import Container


@dataclass(frozen=True)
class DbSample:
    ts: float
    numbackends: int | None
    xact_commit: int | None
    xact_rollback: int | None
    tup_returned: int | None
    tup_fetched: int | None
    tup_inserted: int | None
    tup_updated: int | None
    tup_deleted: int | None
    blks_read: int | None
    blks_hit: int | None
    blk_read_time_ms: float | None
    blk_write_time_ms: float | None
    stmt_calls: int | None
    stmt_total_exec_time_ms: float | None


def _psql_csv(container: Container, sql: str, user: str, db: str) -> str | None:
    """
    Run a SQL query inside the Postgres container and return a single CSV line (no header).
    Best-effort: returns None on failure.
    """
    try:
        exit_code, out = container.exec_run(
            [
                "psql",
                "-U",
                user,
                "-d",
                db,
                "-q",
                "-t",
                "-A",
                "-F",
                ",",
                "-c",
                sql,
            ]
        )
        if exit_code != 0:
            return None
        s = out.decode(errors="replace").strip()
        if not s:
            return None
        # If multiple lines, keep the last non-empty one.
        lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
        return lines[-1] if lines else None
    except Exception:
        return None


class PostgresSampler:
    """
    Periodically samples Postgres internal counters into a CSV.

    This is designed to answer: "Is my latency increase coming from DB or backend?"
    - stmt_total_exec_time_ms / stmt_calls over time approximates average query time.
    - database-level counters show IO timing and transaction/tuple throughput.
    """

    def __init__(
        self,
        *,
        container: Container,
        out_csv_path: str,
        interval_s: float = 1.0,
        user: str = "postgres",
        database: str = "testdb",
    ):
        self._container = container
        self._out_csv_path = out_csv_path
        self._interval_s = interval_s
        self._user = user
        self._db = database

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="postgres-sampler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout_s)

    def _run(self) -> None:
        fieldnames = list(DbSample.__annotations__.keys())
        with open(self._out_csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            while not self._stop.is_set():
                ts = time.time()

                # Database-level counters (single row)
                db_row = _psql_csv(
                    self._container,
                    sql=(
                        "SELECT "
                        "numbackends,"
                        "xact_commit,"
                        "xact_rollback,"
                        "tup_returned,"
                        "tup_fetched,"
                        "tup_inserted,"
                        "tup_updated,"
                        "tup_deleted,"
                        "blks_read,"
                        "blks_hit,"
                        "blk_read_time,"
                        "blk_write_time "
                        "FROM pg_stat_database WHERE datname = current_database();"
                    ),
                    user=self._user,
                    db=self._db,
                )

                # Statement-level aggregate (may be missing if extension fails)
                stmt_row = _psql_csv(
                    self._container,
                    sql=(
                        "SELECT "
                        "COALESCE(SUM(calls),0),"
                        "COALESCE(SUM(total_exec_time),0) "
                        "FROM pg_stat_statements;"
                    ),
                    user=self._user,
                    db=self._db,
                )

                sample = {
                    "ts": ts,
                    "numbackends": None,
                    "xact_commit": None,
                    "xact_rollback": None,
                    "tup_returned": None,
                    "tup_fetched": None,
                    "tup_inserted": None,
                    "tup_updated": None,
                    "tup_deleted": None,
                    "blks_read": None,
                    "blks_hit": None,
                    "blk_read_time_ms": None,
                    "blk_write_time_ms": None,
                    "stmt_calls": None,
                    "stmt_total_exec_time_ms": None,
                }

                if db_row is not None:
                    parts = db_row.split(",")
                    if len(parts) >= 12:
                        try:
                            sample["numbackends"] = int(parts[0])
                            sample["xact_commit"] = int(parts[1])
                            sample["xact_rollback"] = int(parts[2])
                            sample["tup_returned"] = int(parts[3])
                            sample["tup_fetched"] = int(parts[4])
                            sample["tup_inserted"] = int(parts[5])
                            sample["tup_updated"] = int(parts[6])
                            sample["tup_deleted"] = int(parts[7])
                            sample["blks_read"] = int(parts[8])
                            sample["blks_hit"] = int(parts[9])
                            # pg_stat_database reports ms already; keep explicit units
                            sample["blk_read_time_ms"] = float(parts[10]) if parts[10] else 0.0
                            sample["blk_write_time_ms"] = float(parts[11]) if parts[11] else 0.0
                        except Exception:
                            pass

                if stmt_row is not None:
                    parts = stmt_row.split(",")
                    if len(parts) >= 2:
                        try:
                            sample["stmt_calls"] = int(float(parts[0]))
                            sample["stmt_total_exec_time_ms"] = float(parts[1])
                        except Exception:
                            pass

                w.writerow(sample)
                f.flush()

                self._stop.wait(self._interval_s)
