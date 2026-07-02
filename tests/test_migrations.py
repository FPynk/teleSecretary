from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.persistence.migrations import apply_migrations, get_applied_migrations


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

        self.assertEqual(first.applied, ("0001_foundation.sql", "0002_phase1_items.sql"))
        self.assertEqual(second.applied, ())
        self.assertEqual(second.skipped, ("0001_foundation.sql", "0002_phase1_items.sql"))
        self.assertEqual(migrations, ("0001_foundation.sql", "0002_phase1_items.sql"))
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


if __name__ == "__main__":
    unittest.main()
