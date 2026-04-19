from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from tele_secretary.persistence.connection import connect
from tele_secretary.persistence.migrations import apply_migrations, get_applied_migrations


class MigrationTests(unittest.TestCase):
    def test_migrations_apply_once_and_can_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with connect(db_path) as conn:
                first = apply_migrations(conn)
                second = apply_migrations(conn)
                migrations = get_applied_migrations(conn)
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }

        self.assertEqual(first.applied, ("0001_foundation.sql",))
        self.assertEqual(second.applied, ())
        self.assertEqual(second.skipped, ("0001_foundation.sql",))
        self.assertEqual(migrations, ("0001_foundation.sql",))
        self.assertIn("users", tables)
        self.assertIn("ref_sequences", tables)
        self.assertIn("health_checks", tables)


if __name__ == "__main__":
    unittest.main()
