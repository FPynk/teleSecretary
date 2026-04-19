"""Application health checks."""

from __future__ import annotations

from dataclasses import dataclass

from tele_secretary.config import AppConfig
from tele_secretary.persistence.connection import connect
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.time_utils import utc_now_iso


@dataclass(frozen=True)
class HealthCheckResult:
    status: str
    details: str


def perform_healthcheck(config: AppConfig) -> HealthCheckResult:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)

    with connect(config.db_path) as conn:
        apply_migrations(conn)
        checked_at = utc_now_iso()
        details = "database connection and write path ok"
        with conn:
            conn.execute(
                """
                INSERT INTO health_checks (checked_at, status, details)
                VALUES (?, ?, ?)
                """,
                (checked_at, "ok", details),
            )

    return HealthCheckResult(status="ok", details=details)
