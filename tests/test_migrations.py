from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.persistence.migrations import (
    apply_migrations,
    ensure_migration_table,
    get_applied_migrations,
    iter_migration_sql,
)


class MigrationTests(unittest.TestCase):
    def test_migrations_apply_once_and_can_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with open_test_database(db_path) as conn:
                first = apply_migrations(conn)
                second = apply_migrations(conn)
                migrations = get_applied_migrations(conn)
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }

        self.assertEqual(
            first.applied,
            (
                "0001_foundation.sql",
                "0002_phase1_items.sql",
                "0003_task_refs.sql",
            ),
        )
        self.assertEqual(second.applied, ())
        self.assertEqual(
            second.skipped,
            (
                "0001_foundation.sql",
                "0002_phase1_items.sql",
                "0003_task_refs.sql",
            ),
        )
        self.assertEqual(
            migrations,
            (
                "0001_foundation.sql",
                "0002_phase1_items.sql",
                "0003_task_refs.sql",
            ),
        )
        self.assertIn("users", tables)
        self.assertIn("ref_sequences", tables)
        self.assertIn("health_checks", tables)
        self.assertIn("items", tables)
        self.assertIn("task_items", tables)
        self.assertIn("note_items", tables)
        self.assertIn("categories", tables)
        self.assertIn("tags", tables)
        self.assertIn("item_tags", tables)
        self.assertIn("completion_logs", tables)
        self.assertIn("task_refs", tables)

    def test_task_ref_migration_backfills_existing_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with open_test_database(db_path) as conn:
                ensure_migration_table(conn)
                for migration_name, sql in iter_migration_sql()[:2]:
                    with conn:
                        conn.executescript(sql)
                        conn.execute(
                            "INSERT INTO schema_migrations (version) VALUES (?)",
                            (migration_name,),
                        )
                with conn:
                    conn.execute(
                        """
                        INSERT INTO users (id, telegram_user_id, timezone)
                        VALUES ('user-a', 1001, 'America/Chicago')
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO items (
                            id, user_id, item_type, title, status, source,
                            parse_status, created_at, updated_at
                        )
                        VALUES (
                            'task-a', 'user-a', 'task', 'Existing task', 'active',
                            'manual_entry', 'not_applicable',
                            '2026-07-01T00:00:00+00:00',
                            '2026-07-01T00:00:00+00:00'
                        )
                        """
                    )
                    conn.execute("INSERT INTO task_items (item_id) VALUES ('task-a')")

                result = apply_migrations(conn)
                task_ref = conn.execute(
                    "SELECT task_ref FROM task_refs WHERE task_id = 'task-a'"
                ).fetchone()["task_ref"]
                next_value = conn.execute(
                    """
                    SELECT next_value
                    FROM ref_sequences
                    WHERE user_id = 'user-a' AND ref_type = 'task'
                    """
                ).fetchone()["next_value"]

        self.assertEqual(result.applied, ("0003_task_refs.sql",))
        self.assertEqual(task_ref, "T1")
        self.assertEqual(next_value, 2)


if __name__ == "__main__":
    unittest.main()
