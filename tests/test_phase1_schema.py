from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.persistence.migrations import apply_migrations


class Phase1SchemaTests(unittest.TestCase):
    def test_phase1_tables_use_application_provided_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with open_test_database(db_path) as conn:
                apply_migrations(conn)
                timestamp_columns = {
                    "items": ("created_at", "updated_at"),
                    "categories": ("created_at",),
                    "tags": ("created_at",),
                    "completion_logs": ("occurred_at",),
                }

                for table_name, column_names in timestamp_columns.items():
                    with self.subTest(table_name=table_name):
                        table_columns = {
                            row["name"]: row["dflt_value"]
                            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                        }
                        for column_name in column_names:
                            self.assertIsNone(table_columns[column_name])

    def test_database_constraints_reject_invalid_item_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with open_test_database(db_path) as conn:
                apply_migrations(conn)
                with conn:
                    conn.execute(
                        """
                        INSERT INTO users (id, telegram_user_id, timezone)
                        VALUES ('user-a', 1001, 'America/Chicago')
                        """
                    )

                invalid_insert_cases = (
                    ("bad-type", "habit", "active", "manual_entry", "not_applicable", None),
                    ("bad-status", "task", "waiting", "manual_entry", "not_applicable", None),
                    ("bad-source", "task", "active", "llm_parse", "not_applicable", None),
                    ("bad-parse", "task", "active", "manual_entry", "unknown", None),
                    ("bad-confidence", "task", "active", "manual_entry", "parsed", 1.5),
                )

                for item_id, item_type, status, source, parse_status, parse_confidence in invalid_insert_cases:
                    with self.subTest(item_id=item_id):
                        with self.assertRaises(sqlite3.IntegrityError):
                            with conn:
                                conn.execute(
                                    """
                                    INSERT INTO items (
                                        id, user_id, item_type, title, status, source,
                                        parse_status, parse_confidence, created_at, updated_at
                                    )
                                    VALUES (?, 'user-a', ?, 'Example', ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        item_id,
                                        item_type,
                                        status,
                                        source,
                                        parse_status,
                                        parse_confidence,
                                        "2026-07-02T01:00:00+00:00",
                                        "2026-07-02T01:00:00+00:00",
                                    ),
                                )

    def test_subtype_triggers_reject_mismatched_item_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with open_test_database(db_path) as conn:
                apply_migrations(conn)
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
                            'note-1', 'user-a', 'note', 'A note', 'active',
                            'manual_entry', 'not_applicable',
                            '2026-07-02T01:00:00+00:00',
                            '2026-07-02T01:00:00+00:00'
                        )
                        """
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    with conn:
                        conn.execute("INSERT INTO task_items (item_id) VALUES ('note-1')")


if __name__ == "__main__":
    unittest.main()
