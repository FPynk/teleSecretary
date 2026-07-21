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
                item_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(items)").fetchall()
                }
                task_item_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(task_items)").fetchall()
                }
                note_item_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(note_items)").fetchall()
                }
                reminder_indexes = {
                    row["name"]
                    for row in conn.execute("PRAGMA index_list(reminders)").fetchall()
                }

        self.assertEqual(
            first.applied,
            (
                "0001_foundation.sql",
                "0002_phase1_items.sql",
                "0003_task_refs.sql",
                "0004_subtype_public_references.sql",
                "0005_reminders.sql",
            ),
        )
        self.assertEqual(second.applied, ())
        self.assertEqual(
            second.skipped,
            (
                "0001_foundation.sql",
                "0002_phase1_items.sql",
                "0003_task_refs.sql",
                "0004_subtype_public_references.sql",
                "0005_reminders.sql",
            ),
        )
        self.assertEqual(
            migrations,
            (
                "0001_foundation.sql",
                "0002_phase1_items.sql",
                "0003_task_refs.sql",
                "0004_subtype_public_references.sql",
                "0005_reminders.sql",
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
        self.assertIn("reminders", tables)
        self.assertNotIn("task_refs", tables)
        self.assertIn("pub_ref", item_columns)
        self.assertNotIn("pub_ref", task_item_columns)
        self.assertNotIn("pub_ref", note_item_columns)
        self.assertEqual(
            reminder_indexes,
            {
                "idx_reminders_pending_schedule",
                "idx_reminders_item_status_schedule",
                "idx_reminders_unique_active_schedule",
                "sqlite_autoindex_reminders_1",
            },
        )

    def test_item_reference_migration_moves_task_refs_and_backfills_notes(self) -> None:
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
                    conn.execute(
                        """
                        INSERT INTO items (
                            id, user_id, item_type, title, status, source,
                            parse_status, created_at, updated_at
                        )
                        VALUES (
                            'note-a', 'user-a', 'note', 'Existing note', 'active',
                            'manual_entry', 'not_applicable',
                            '2026-07-02T00:00:00+00:00',
                            '2026-07-02T00:00:00+00:00'
                        )
                        """
                    )
                    conn.execute("INSERT INTO note_items (item_id) VALUES ('note-a')")

                migration_name, sql = iter_migration_sql()[2]
                with conn:
                    conn.executescript(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES (?)",
                        (migration_name,),
                    )

                result = apply_migrations(conn)
                task_ref = conn.execute(
                    """
                    SELECT pub_ref
                    FROM items
                    WHERE id = 'task-a'
                    """
                ).fetchone()["pub_ref"]
                note_ref = conn.execute(
                    """
                    SELECT pub_ref
                    FROM items
                    WHERE id = 'note-a'
                    """
                ).fetchone()["pub_ref"]
                next_values = {
                    row["ref_type"]: row["next_value"]
                    for row in conn.execute(
                        """
                        SELECT ref_type, next_value
                        FROM ref_sequences
                        WHERE user_id = 'user-a'
                        """
                    ).fetchall()
                }
                task_refs_table = conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'task_refs'
                    """
                ).fetchone()

        self.assertEqual(
            result.applied,
            ("0004_subtype_public_references.sql", "0005_reminders.sql"),
        )
        self.assertEqual(task_ref, "T1")
        self.assertEqual(note_ref, "N1")
        self.assertEqual(next_values, {"task": 2, "note": 2})
        self.assertIsNone(task_refs_table)

    def test_reminder_migration_preserves_existing_task_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with open_test_database(db_path) as conn:
                ensure_migration_table(conn)
                for migration_name, sql in iter_migration_sql()[:4]:
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
                            id, user_id, item_type, pub_ref, title, status, source,
                            parse_status, created_at, updated_at
                        )
                        VALUES (
                            'task-a', 'user-a', 'task', 'T1', 'Existing task',
                            'active', 'manual_entry', 'not_applicable',
                            '2026-07-01T00:00:00+00:00',
                            '2026-07-01T00:00:00+00:00'
                        )
                        """
                    )
                    conn.execute("INSERT INTO task_items (item_id) VALUES ('task-a')")

                result = apply_migrations(conn)
                task_row = conn.execute(
                    "SELECT pub_ref, title FROM items WHERE id = 'task-a'"
                ).fetchone()
                reminder_count = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]

        self.assertEqual(result.applied, ("0005_reminders.sql",))
        self.assertEqual((task_row["pub_ref"], task_row["title"]), ("T1", "Existing task"))
        self.assertEqual(reminder_count, 0)


if __name__ == "__main__":
    unittest.main()
