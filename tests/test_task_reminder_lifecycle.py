from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.reminders import (
    cancel_all_future_pending_reminders_for_task,
)
from tele_secretary.app.tasks import complete_task, create_task, reopen_task, soft_delete_task
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.time_utils import to_storage_text


TRANSITION_AT = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)
TRANSITION_AT_TEXT = to_storage_text(TRANSITION_AT)
EARLIER = to_storage_text(TRANSITION_AT - timedelta(days=1))


class TaskReminderLifecycleTests(unittest.TestCase):
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with open_test_database(Path(temp_dir) / "secretary.sqlite3") as conn:
                apply_migrations(conn)
                self._insert_user(conn, "user-a", 1001)
                self._insert_user(conn, "user-b", 2002)
                yield conn

    def test_completion_cancels_only_strictly_future_pending_reminders(self) -> None:
        with self.open_database() as conn:
            task = self._create_task(conn, "user-a", "Complete report")
            self._insert_reminder(conn, "past", task.id, TRANSITION_AT - timedelta(seconds=1))
            self._insert_reminder(conn, "at-cutoff", task.id, TRANSITION_AT)
            self._insert_reminder(conn, "future-later", task.id, TRANSITION_AT + timedelta(hours=2))
            self._insert_reminder(conn, "future-earlier", task.id, TRANSITION_AT + timedelta(hours=1))
            self._insert_reminder(
                conn,
                "processing",
                task.id,
                TRANSITION_AT + timedelta(hours=3),
                status="processing",
            )
            self._insert_reminder(
                conn,
                "sent",
                task.id,
                TRANSITION_AT + timedelta(hours=4),
                status="sent",
                sent_at=EARLIER,
            )
            self._insert_reminder(
                conn,
                "failed",
                task.id,
                TRANSITION_AT + timedelta(hours=5),
                status="failed",
                last_attempted_at=EARLIER,
                failure_reason="telegram_timeout",
            )
            self._insert_reminder(
                conn,
                "expired",
                task.id,
                TRANSITION_AT + timedelta(hours=6),
                status="expired",
                expired_at=EARLIER,
            )
            self._insert_reminder(
                conn,
                "cancelled",
                task.id,
                TRANSITION_AT + timedelta(hours=7),
                status="cancelled",
                cancelled_at=EARLIER,
            )

            completed_task = complete_task(
                conn,
                user_id="user-a",
                task_id=task.id,
                source="test_fixture",
                completed_at=TRANSITION_AT,
            )
            reminders = {
                row["id"]: tuple(row)
                for row in conn.execute(
                    "SELECT id, status, cancelled_at, updated_at FROM reminders"
                ).fetchall()
            }
            completion_log = conn.execute(
                "SELECT event_type, occurred_at FROM completion_logs WHERE item_id = ?",
                (task.id,),
            ).fetchone()

        self.assertEqual(
            (completed_task.status, completed_task.completed_at, completed_task.updated_at),
            ("completed", TRANSITION_AT_TEXT, TRANSITION_AT_TEXT),
        )
        self.assertEqual(
            reminders["future-earlier"],
            ("future-earlier", "cancelled", TRANSITION_AT_TEXT, TRANSITION_AT_TEXT),
        )
        self.assertEqual(reminders["future-later"][1:], ("cancelled", TRANSITION_AT_TEXT, TRANSITION_AT_TEXT))
        self.assertEqual(
            {reminder_id: values[1] for reminder_id, values in reminders.items() if reminder_id not in {"future-earlier", "future-later"}},
            {
                "past": "pending",
                "at-cutoff": "pending",
                "processing": "processing",
                "sent": "sent",
                "failed": "failed",
                "expired": "expired",
                "cancelled": "cancelled",
            },
        )
        self.assertEqual(tuple(completion_log), ("completed", TRANSITION_AT_TEXT))

    def test_soft_delete_cancels_only_its_own_future_pending_reminders(self) -> None:
        with self.open_database() as conn:
            target_task = self._create_task(conn, "user-a", "Delete me")
            other_task = self._create_task(conn, "user-b", "Keep me")
            self._insert_reminder(conn, "target-future", target_task.id, TRANSITION_AT + timedelta(minutes=1))
            self._insert_reminder(conn, "target-past", target_task.id, TRANSITION_AT)
            self._insert_reminder(conn, "other-future", other_task.id, TRANSITION_AT + timedelta(minutes=1))

            result = soft_delete_task(
                conn,
                user_id="user-a",
                task_id=target_task.id,
                source="test_fixture",
                deleted_at=TRANSITION_AT,
            )
            rows = {
                row["id"]: tuple(row)
                for row in conn.execute(
                    "SELECT id, status, cancelled_at, updated_at FROM reminders ORDER BY id"
                ).fetchall()
            }

        self.assertEqual((result.task_id, result.deleted_at), (target_task.id, TRANSITION_AT_TEXT))
        self.assertEqual(rows["target-future"][1:], ("cancelled", TRANSITION_AT_TEXT, TRANSITION_AT_TEXT))
        self.assertEqual(rows["target-past"][1], "pending")
        self.assertEqual(rows["other-future"][1], "pending")

    def test_reopening_keeps_cancelled_reminders_as_history(self) -> None:
        with self.open_database() as conn:
            task = self._create_task(conn, "user-a", "Reopen me")
            self._insert_reminder(conn, "future", task.id, TRANSITION_AT + timedelta(hours=1))

            complete_task(
                conn,
                user_id="user-a",
                task_id=task.id,
                source="test_fixture",
                completed_at=TRANSITION_AT,
            )
            reopened_task = reopen_task(
                conn,
                user_id="user-a",
                task_id=task.id,
                source="test_fixture",
                reopened_at=TRANSITION_AT + timedelta(minutes=1),
            )
            reminder = conn.execute(
                "SELECT status, cancelled_at FROM reminders WHERE id = 'future'"
            ).fetchone()

        self.assertEqual((reopened_task.status, reopened_task.completed_at), ("active", None))
        self.assertEqual(tuple(reminder), ("cancelled", TRANSITION_AT_TEXT))

    def test_lifecycle_transitions_succeed_when_no_future_reminders_match(self) -> None:
        with self.open_database() as conn:
            completed_task = self._create_task(conn, "user-a", "No completion reminders")
            deleted_task = self._create_task(conn, "user-a", "No deletion reminders")
            self._insert_reminder(
                conn,
                "past-only",
                completed_task.id,
                TRANSITION_AT,
            )

            completed = complete_task(
                conn,
                user_id="user-a",
                task_id=completed_task.id,
                source="test_fixture",
                completed_at=TRANSITION_AT,
            )
            deleted = soft_delete_task(
                conn,
                user_id="user-a",
                task_id=deleted_task.id,
                source="test_fixture",
                deleted_at=TRANSITION_AT,
            )
            past_reminder_status = conn.execute(
                "SELECT status FROM reminders WHERE id = 'past-only'"
            ).fetchone()[0]

        self.assertEqual(completed.status, "completed")
        self.assertEqual(deleted.deleted_at, TRANSITION_AT_TEXT)
        self.assertEqual(past_reminder_status, "pending")

    def test_lifecycle_and_reminder_changes_roll_back_together_when_cancellation_fails(self) -> None:
        with self.open_database() as conn:
            task = self._create_task(conn, "user-a", "Rollback me")
            self._insert_reminder(conn, "future", task.id, TRANSITION_AT + timedelta(hours=1))
            with conn:
                conn.execute(
                    """
                    CREATE TRIGGER fail_lifecycle_reminder_cancellation
                    BEFORE UPDATE OF status ON reminders
                    WHEN NEW.status = 'cancelled'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced reminder cancellation failure');
                    END;
                    """
                )

            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced reminder cancellation failure"):
                complete_task(
                    conn,
                    user_id="user-a",
                    task_id=task.id,
                    source="test_fixture",
                    completed_at=TRANSITION_AT,
                )
            task_row = conn.execute(
                """
                SELECT items.status, items.updated_at, task_items.completed_at
                FROM items JOIN task_items ON task_items.item_id = items.id
                WHERE items.id = ?
                """,
                (task.id,),
            ).fetchone()
            reminder_row = conn.execute(
                "SELECT status, cancelled_at FROM reminders WHERE id = 'future'"
            ).fetchone()
            completion_log_count = conn.execute(
                "SELECT COUNT(*) FROM completion_logs WHERE item_id = ?",
                (task.id,),
            ).fetchone()[0]

        self.assertEqual(tuple(task_row), ("active", task.updated_at, None))
        self.assertEqual(tuple(reminder_row), ("pending", None))
        self.assertEqual(completion_log_count, 0)

    def test_standalone_task_wide_cancellation_keeps_its_transaction_and_order(self) -> None:
        with self.open_database() as conn:
            task = self._create_task(conn, "user-a", "Standalone")
            self._insert_reminder(conn, "later", task.id, TRANSITION_AT + timedelta(hours=2))
            self._insert_reminder(conn, "earlier", task.id, TRANSITION_AT + timedelta(hours=1))

            cancelled = cancel_all_future_pending_reminders_for_task(
                conn,
                user_id="user-a",
                task_id=task.id,
                now=TRANSITION_AT,
            )

        self.assertEqual([reminder.id for reminder in cancelled], ["earlier", "later"])
        self.assertTrue(all(reminder.status == "cancelled" for reminder in cancelled))
        self.assertTrue(all(reminder.cancelled_at == TRANSITION_AT_TEXT for reminder in cancelled))

    def _insert_user(self, conn, user_id: str, telegram_user_id: int) -> None:
        with conn:
            conn.execute(
                "INSERT INTO users (id, telegram_user_id, timezone) VALUES (?, ?, 'America/Chicago')",
                (user_id, telegram_user_id),
            )

    def _create_task(self, conn, user_id: str, title: str):
        return create_task(conn, user_id=user_id, title=title, source="test_fixture")

    def _insert_reminder(
        self,
        conn,
        reminder_id: str,
        task_id: str,
        scheduled_at: datetime,
        *,
        status: str = "pending",
        last_attempted_at: str | None = None,
        sent_at: str | None = None,
        failure_reason: str | None = None,
        cancelled_at: str | None = None,
        expired_at: str | None = None,
    ) -> None:
        with conn:
            conn.execute(
                """
                INSERT INTO reminders (
                    id, item_id, scheduled_at, status, delivery_channel, retry_count,
                    last_attempted_at, sent_at, failure_reason, cancelled_at, expired_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'telegram', 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder_id,
                    task_id,
                    to_storage_text(scheduled_at),
                    status,
                    last_attempted_at,
                    sent_at,
                    failure_reason,
                    cancelled_at,
                    expired_at,
                    EARLIER,
                    EARLIER,
                ),
            )


if __name__ == "__main__":
    unittest.main()
