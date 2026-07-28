from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Thread
from unittest.mock import patch
from uuid import UUID

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.reminders import claim_due_reminders, create_reminder
from tele_secretary.app.tasks import create_task
from tele_secretary.app.users import (
    LEGACY_SINGLE_OWNER_USER_ID,
    bind_unassigned_legacy_single_owner,
    get_or_create_telegram_user,
)
from tele_secretary.persistence.connection import connect
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.config import AppConfig
from tele_secretary.telegram.bot import run_bot


NOW = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)


class TelegramUserServiceTests(unittest.TestCase):
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with open_test_database(Path(temp_dir) / "secretary.sqlite3") as conn:
                apply_migrations(conn)
                yield conn

    def test_users_are_created_per_telegram_id_without_overwriting_timezones(self) -> None:
        with self.open_database() as conn:
            first = get_or_create_telegram_user(
                conn,
                telegram_user_id=1001,
                default_timezone="America/Chicago",
            )
            repeated = get_or_create_telegram_user(
                conn,
                telegram_user_id=1001,
                default_timezone="Asia/Singapore",
            )
            second = get_or_create_telegram_user(
                conn,
                telegram_user_id=2002,
                default_timezone="Asia/Singapore",
            )

        UUID(first.user_id)
        UUID(second.user_id)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first.user_id, second.user_id)
        self.assertEqual((first.telegram_user_id, first.timezone), (1001, "America/Chicago"))
        self.assertEqual((second.telegram_user_id, second.timezone), (2002, "Asia/Singapore"))

    def test_concurrent_first_requests_create_one_shared_user_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with open_test_database(db_path) as conn:
                apply_migrations(conn)

            barrier = Barrier(2)
            users = []
            errors = []

            def resolve_user() -> None:
                thread_conn = connect(db_path)
                try:
                    barrier.wait()
                    users.append(
                        get_or_create_telegram_user(
                            thread_conn,
                            telegram_user_id=1001,
                            default_timezone="America/Chicago",
                        )
                    )
                except Exception as error:
                    errors.append(error)
                finally:
                    thread_conn.close()

            threads = [Thread(target=resolve_user) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            with open_test_database(db_path) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE telegram_user_id = 1001"
                ).fetchone()[0]

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(count, 1)
        self.assertEqual({user.user_id for user in users}, {users[0].user_id})

    def test_legacy_binding_is_idempotent_and_keeps_existing_child_ownership(self) -> None:
        with self.open_database() as conn:
            self._insert_legacy_owner(conn)
            task = create_task(
                conn,
                user_id=LEGACY_SINGLE_OWNER_USER_ID,
                title="Legacy task",
                source="test_fixture",
            )

            bound = bind_unassigned_legacy_single_owner(
                conn,
                allowed_telegram_user_ids=(1001, 2002),
            )
            repeated = bind_unassigned_legacy_single_owner(
                conn,
                allowed_telegram_user_ids=(2002,),
            )
            task_owner = conn.execute(
                "SELECT user_id FROM items WHERE id = ?", (task.id,)
            ).fetchone()[0]

        self.assertEqual(bound, repeated)
        self.assertEqual(
            (bound.user_id, bound.telegram_user_id, bound.timezone),
            (LEGACY_SINGLE_OWNER_USER_ID, 1001, "America/Chicago"),
        )
        self.assertEqual(task_owner, LEGACY_SINGLE_OWNER_USER_ID)

    def test_legacy_binding_leaves_empty_and_bound_states_unchanged_and_rejects_conflicts(self) -> None:
        with self.open_database() as conn:
            self._insert_legacy_owner(conn)
            self.assertIsNone(
                bind_unassigned_legacy_single_owner(conn, allowed_telegram_user_ids=())
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT telegram_user_id FROM users WHERE id = ?",
                    (LEGACY_SINGLE_OWNER_USER_ID,),
                ).fetchone()[0]
            )
            get_or_create_telegram_user(
                conn,
                telegram_user_id=1001,
                default_timezone="Europe/London",
            )
            with self.assertRaisesRegex(RuntimeError, "already belongs"):
                bind_unassigned_legacy_single_owner(
                    conn,
                    allowed_telegram_user_ids=(1001,),
                )
            legacy_telegram_id = conn.execute(
                "SELECT telegram_user_id FROM users WHERE id = ?",
                (LEGACY_SINGLE_OWNER_USER_ID,),
            ).fetchone()[0]

        self.assertIsNone(legacy_telegram_id)

    def test_bot_startup_binds_an_unassigned_legacy_owner_before_polling(self) -> None:
        class FakeApplication:
            def __init__(self) -> None:
                self.did_poll = False

            def run_polling(self) -> None:
                self.did_poll = True

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = AppConfig(
                telegram_bot_token="test-token",
                telegram_allowed_user_ids=(1001, 2002),
                data_dir=root / "data",
                log_dir=root / "logs",
                db_path=root / "data" / "secretary.sqlite3",
                user_timezone="America/Chicago",
                log_level="INFO",
            )
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                self._insert_legacy_owner(conn)

            application = FakeApplication()
            with patch("tele_secretary.telegram.bot.build_application", return_value=application):
                run_bot(config)

            with open_test_database(config.db_path) as conn:
                legacy_telegram_id = conn.execute(
                    "SELECT telegram_user_id FROM users WHERE id = ?",
                    (LEGACY_SINGLE_OWNER_USER_ID,),
                ).fetchone()[0]

        self.assertTrue(application.did_poll)
        self.assertEqual(legacy_telegram_id, 1001)

    def test_claimed_reminders_keep_each_owners_telegram_recipient_and_timezone(self) -> None:
        with self.open_database() as conn:
            chicago_user = get_or_create_telegram_user(
                conn,
                telegram_user_id=1001,
                default_timezone="America/Chicago",
            )
            singapore_user = get_or_create_telegram_user(
                conn,
                telegram_user_id=2002,
                default_timezone="Asia/Singapore",
            )
            chicago_task = create_task(
                conn,
                user_id=chicago_user.user_id,
                title="Chicago task",
                source="test_fixture",
            )
            singapore_task = create_task(
                conn,
                user_id=singapore_user.user_id,
                title="Singapore task",
                source="test_fixture",
            )
            create_reminder(
                conn,
                user_id=chicago_user.user_id,
                task_id=chicago_task.id,
                scheduled_at=NOW + timedelta(minutes=1),
                now=NOW,
            )
            create_reminder(
                conn,
                user_id=singapore_user.user_id,
                task_id=singapore_task.id,
                scheduled_at=NOW + timedelta(minutes=2),
                now=NOW,
            )

            claimed = claim_due_reminders(conn, now=NOW + timedelta(minutes=2))

        self.assertEqual(
            [
                (record.user_id, record.telegram_user_id, record.user_timezone)
                for record in claimed
            ],
            [
                (chicago_user.user_id, 1001, "America/Chicago"),
                (singapore_user.user_id, 2002, "Asia/Singapore"),
            ],
        )

    def _insert_legacy_owner(self, conn) -> None:
        with conn:
            conn.execute(
                "INSERT INTO users (id, telegram_user_id, timezone) VALUES (?, NULL, ?)",
                (LEGACY_SINGLE_OWNER_USER_ID, "America/Chicago"),
            )


if __name__ == "__main__":
    unittest.main()
