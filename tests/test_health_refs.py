from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from tele_secretary.app.health import perform_healthcheck
from tele_secretary.config import AppConfig
from tele_secretary.persistence.connection import connect
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.persistence.refs import allocate_ref


class HealthAndRefsTests(unittest.TestCase):
    def test_healthcheck_writes_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = AppConfig(
                telegram_bot_token=None,
                telegram_allowed_user_ids=(),
                data_dir=root / "data",
                log_dir=root / "logs",
                db_path=root / "data" / "secretary.sqlite3",
                user_timezone="America/Chicago",
                log_level="INFO",
            )

            result = perform_healthcheck(config)
            with connect(config.db_path) as conn:
                count = conn.execute("SELECT COUNT(*) AS count FROM health_checks").fetchone()[
                    "count"
                ]

        self.assertEqual(result.status, "ok")
        self.assertEqual(count, 1)

    def test_task_refs_are_sequential_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with connect(db_path) as conn:
                apply_migrations(conn)
                with conn:
                    conn.execute(
                        """
                        INSERT INTO users (id, telegram_user_id, timezone)
                        VALUES (?, ?, ?), (?, ?, ?)
                        """,
                        (
                            "user-a",
                            1001,
                            "America/Chicago",
                            "user-b",
                            1002,
                            "America/Chicago",
                        ),
                    )

                first = allocate_ref(conn, user_id="user-a", ref_type="task")
                second = allocate_ref(conn, user_id="user-a", ref_type="task")
                other_user = allocate_ref(conn, user_id="user-b", ref_type="task")

        self.assertEqual(first, "T1")
        self.assertEqual(second, "T2")
        self.assertEqual(other_user, "T1")


if __name__ == "__main__":
    unittest.main()
