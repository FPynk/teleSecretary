"""Small SQL migration runner."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from sqlite3 import Connection


@dataclass(frozen=True)
class MigrationResult:
    applied: tuple[str, ...]
    skipped: tuple[str, ...]


def apply_migrations(conn: Connection) -> MigrationResult:
    ensure_migration_table(conn)
    applied_before = set(get_applied_migrations(conn))
    applied_now: list[str] = []
    skipped: list[str] = []

    for migration_name, sql in iter_migration_sql():
        if migration_name in applied_before:
            skipped.append(migration_name)
            continue

        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (migration_name,),
            )
        applied_now.append(migration_name)

    return MigrationResult(applied=tuple(applied_now), skipped=tuple(skipped))


def ensure_migration_table(conn: Connection) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def get_applied_migrations(conn: Connection) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return tuple(row["version"] for row in rows)


def iter_migration_sql() -> tuple[tuple[str, str], ...]:
    migrations_dir = resources.files("tele_secretary.migrations")
    migration_files = sorted(
        path
        for path in migrations_dir.iterdir()
        if path.name.endswith(".sql") and path.is_file()
    )
    return tuple(
        (path.name, path.read_text(encoding="utf-8")) for path in migration_files
    )
