from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Thread

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.reminders import (
    ReminderNotFoundError,
    ReminderSelectionError,
    ReminderValidationError,
    cancel_all_future_pending_reminders_for_task,
    cancel_pending_reminder,
    cancel_selected_pending_reminders,
    claim_due_reminders,
)
from tele_secretary.persistence.connection import connect
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.time_utils import to_storage_text


NOW = datetime(2026, 7, 24, 15, 0, tzinfo=timezone.utc)
NOW_TEXT = to_storage_text(NOW)


class ReminderCancellationTests(unittest.TestCase):
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with open_test_database(db_path) as conn:
                apply_migrations(conn)
                self._insert_user(conn, "user-a", 1001)
                self._insert_user(conn, "user-b", 1002)
                yield conn, db_path

    def test_cancel_pending_reminder_persists_one_timestamp_for_both_cancellation_fields(self) -> None:
        cancellation_time = datetime(
            2026,
            7,
            24,
            10,
            30,
            59,
            900000,
            tzinfo=timezone(timedelta(hours=-5)),
        )
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a")
            self._insert_reminder(conn, "reminder-a", "task-a", NOW_TEXT)

            result = cancel_pending_reminder(
                conn,
                user_id="user-a",
                reminder_id="reminder-a",
                cancelled_at=cancellation_time,
            )

        self.assertTrue(result.was_cancelled)
        self.assertEqual(result.reminder.status, "cancelled")
        self.assertEqual(
            (result.reminder.cancelled_at, result.reminder.updated_at),
            ("2026-07-24T15:30:59+00:00",) * 2,
        )
        self.assertEqual(
            (
                result.reminder.scheduled_at,
                result.reminder.delivery_channel,
                result.reminder.retry_count,
                result.reminder.last_attempted_at,
                result.reminder.sent_at,
                result.reminder.failure_reason,
                result.reminder.expired_at,
                result.reminder.created_at,
            ),
            (NOW_TEXT, "telegram", 0, None, None, None, None, NOW_TEXT),
        )

    def test_repeated_single_cancellation_is_idempotent_without_rewriting_the_original_time(self) -> None:
        first_time = NOW + timedelta(minutes=1)
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a")
            self._insert_reminder(conn, "reminder-a", "task-a", NOW_TEXT)

            first_result = cancel_pending_reminder(
                conn,
                user_id="user-a",
                reminder_id="reminder-a",
                cancelled_at=first_time,
            )
            second_result = cancel_pending_reminder(
                conn,
                user_id="user-a",
                reminder_id="reminder-a",
                cancelled_at=NOW + timedelta(hours=1),
            )

        self.assertTrue(first_result.was_cancelled)
        self.assertFalse(second_result.was_cancelled)
        self.assertEqual(second_result.reminder, first_result.reminder)

    def test_single_cancellation_hides_missing_and_cross_owner_reminders(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a")
            self._insert_reminder(conn, "reminder-a", "task-a", NOW_TEXT)

            with self.assertRaises(ReminderNotFoundError) as missing_error:
                cancel_pending_reminder(
                    conn,
                    user_id="user-a",
                    reminder_id="missing",
                    cancelled_at=NOW,
                )
            with self.assertRaises(ReminderNotFoundError) as cross_owner_error:
                cancel_pending_reminder(
                    conn,
                    user_id="user-b",
                    reminder_id="reminder-a",
                    cancelled_at=NOW,
                )
            status = conn.execute(
                "SELECT status FROM reminders WHERE id = 'reminder-a'"
            ).fetchone()[0]

        self.assertEqual(missing_error.exception.code, cross_owner_error.exception.code)
        self.assertEqual(status, "pending")

    def test_single_cancellation_rejects_terminal_or_processing_reminders_without_rewriting_them(self) -> None:
        terminal_details = {
            "processing": {},
            "sent": {"sent_at": NOW_TEXT},
            "failed": {"last_attempted_at": NOW_TEXT, "failure_reason": "network"},
            "expired": {"expired_at": NOW_TEXT},
        }
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a")
            for status, details in terminal_details.items():
                self._insert_reminder(
                    conn,
                    f"reminder-{status}",
                    "task-a",
                    NOW_TEXT,
                    status=status,
                    **details,
                )
            snapshots = {
                row["id"]: tuple(row)
                for row in conn.execute("SELECT * FROM reminders").fetchall()
            }

            for status in terminal_details:
                with self.subTest(status=status):
                    with self.assertRaises(ReminderValidationError) as error:
                        cancel_pending_reminder(
                            conn,
                            user_id="user-a",
                            reminder_id=f"reminder-{status}",
                            cancelled_at=NOW,
                        )
                    self.assertEqual(error.exception.code, "reminder_not_cancellable")
            final_rows = {
                row["id"]: tuple(row)
                for row in conn.execute("SELECT * FROM reminders").fetchall()
            }

        self.assertEqual(final_rows, snapshots)

    def test_cancellation_clock_must_be_timezone_aware_before_any_write_transaction(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a")
            self._insert_reminder(conn, "reminder-a", "task-a", NOW_TEXT)

            with self.assertRaises(ReminderValidationError) as error:
                cancel_pending_reminder(
                    conn,
                    user_id="user-a",
                    reminder_id="reminder-a",
                    cancelled_at=datetime(2026, 7, 24, 15, 0),
                )

            status = conn.execute(
                "SELECT status FROM reminders WHERE id = 'reminder-a'"
            ).fetchone()[0]
            self.assertFalse(conn.in_transaction)

        self.assertEqual(error.exception.code, "invalid_cancelled_at")
        self.assertEqual(status, "pending")

    def test_strict_selected_cancellation_cancels_all_and_preserves_selection_order(self) -> None:
        cancellation_time = NOW + timedelta(minutes=1)
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a")
            self._insert_reminder(conn, "first", "task-a", NOW + timedelta(hours=1))
            self._insert_reminder(conn, "second", "task-a", NOW + timedelta(hours=2))
            self._insert_reminder(conn, "third", "task-a", NOW + timedelta(hours=3))

            cancelled = cancel_selected_pending_reminders(
                conn,
                user_id="user-a",
                task_id="task-a",
                reminder_ids=("third", "first"),
                cancelled_at=cancellation_time,
            )
            states = {
                row["id"]: (row["status"], row["cancelled_at"], row["updated_at"])
                for row in conn.execute(
                    "SELECT id, status, cancelled_at, updated_at FROM reminders"
                ).fetchall()
            }

        self.assertEqual([reminder.id for reminder in cancelled], ["third", "first"])
        self.assertTrue(all(reminder.status == "cancelled" for reminder in cancelled))
        self.assertEqual(states["second"][0], "pending")
        self.assertEqual(
            states["first"],
            ("cancelled", "2026-07-24T15:01:00+00:00", "2026-07-24T15:01:00+00:00"),
        )
        self.assertEqual(states["third"], states["first"])

    def test_strict_selected_cancellation_rejects_invalid_input_without_a_write(self) -> None:
        invalid_selections = ((), ("reminder-a", "reminder-a"))
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a")
            self._insert_reminder(conn, "reminder-a", "task-a", NOW_TEXT)

            for reminder_ids in invalid_selections:
                with self.subTest(reminder_ids=reminder_ids):
                    with self.assertRaises(ReminderValidationError) as error:
                        cancel_selected_pending_reminders(
                            conn,
                            user_id="user-a",
                            task_id="task-a",
                            reminder_ids=reminder_ids,
                            cancelled_at=NOW,
                        )
                    self.assertEqual(error.exception.code, "invalid_reminder_selection")
                    self.assertFalse(conn.in_transaction)
            status = conn.execute(
                "SELECT status FROM reminders WHERE id = 'reminder-a'"
            ).fetchone()[0]

        self.assertEqual(status, "pending")

    def test_strict_selected_cancellation_fails_closed_for_missing_wrong_task_cross_owner_and_stale_rows(self) -> None:
        cases = (
            ("missing", "task-a", ("pending-a", "missing")),
            ("wrong-task", "task-a", ("pending-a", "pending-other-task")),
            ("cross-owner", "task-a", ("pending-a", "pending-other-user")),
            ("cancelled", "task-a", ("pending-a", "already-cancelled")),
            ("processing", "task-a", ("pending-a", "processing")),
        )
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a")
            self._insert_task(conn, "task-other", "user-a")
            self._insert_task(conn, "task-b", "user-b")
            self._insert_reminder(conn, "pending-a", "task-a", NOW_TEXT)
            self._insert_reminder(conn, "pending-other-task", "task-other", NOW_TEXT)
            self._insert_reminder(conn, "pending-other-user", "task-b", NOW_TEXT)
            self._insert_reminder(
                conn,
                "already-cancelled",
                "task-a",
                NOW_TEXT,
                status="cancelled",
                cancelled_at=NOW_TEXT,
            )
            self._insert_reminder(
                conn,
                "processing",
                "task-a",
                NOW + timedelta(seconds=1),
                status="processing",
            )

            for name, task_id, reminder_ids in cases:
                with self.subTest(case=name):
                    before = self._statuses(conn)
                    with self.assertRaises(ReminderSelectionError) as error:
                        cancel_selected_pending_reminders(
                            conn,
                            user_id="user-a",
                            task_id=task_id,
                            reminder_ids=reminder_ids,
                            cancelled_at=NOW,
                        )
                    self.assertEqual(error.exception.code, "reminder_selection_unavailable")
                    self.assertEqual(self._statuses(conn), before)

    def test_selected_cancellation_rolls_back_all_rows_when_the_write_fails(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a")
            self._insert_reminder(conn, "first", "task-a", NOW_TEXT)
            self._insert_reminder(conn, "second", "task-a", NOW + timedelta(seconds=1))
            conn.execute(
                """
                CREATE TRIGGER reject_reminder_cancellation
                BEFORE UPDATE OF status ON reminders
                WHEN NEW.status = 'cancelled'
                BEGIN
                    SELECT RAISE(ABORT, 'forced cancellation failure');
                END
                """
            )

            with self.assertRaises(sqlite3.IntegrityError):
                cancel_selected_pending_reminders(
                    conn,
                    user_id="user-a",
                    task_id="task-a",
                    reminder_ids=("first", "second"),
                    cancelled_at=NOW,
                )
            statuses = self._statuses(conn)

        self.assertEqual(statuses["first"], "pending")
        self.assertEqual(statuses["second"], "pending")

    def test_all_future_cancellation_leaves_due_past_terminal_and_other_owner_rows_unchanged(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a")
            self._insert_task(conn, "task-b", "user-b")
            self._insert_reminder(conn, "past", "task-a", NOW - timedelta(seconds=1))
            self._insert_reminder(conn, "now", "task-a", NOW)
            self._insert_reminder(conn, "future-later", "task-a", NOW + timedelta(hours=2))
            self._insert_reminder(conn, "future-earlier", "task-a", NOW + timedelta(hours=1))
            self._insert_reminder(conn, "processing", "task-a", NOW + timedelta(hours=3), status="processing")
            self._insert_reminder(conn, "other-user", "task-b", NOW + timedelta(hours=1))

            cancelled = cancel_all_future_pending_reminders_for_task(
                conn,
                user_id="user-a",
                task_id="task-a",
                now=NOW,
            )
            statuses = self._statuses(conn)

        self.assertEqual([reminder.id for reminder in cancelled], ["future-earlier", "future-later"])
        self.assertEqual(statuses["future-earlier"], "cancelled")
        self.assertEqual(statuses["future-later"], "cancelled")
        self.assertEqual(statuses["past"], "pending")
        self.assertEqual(statuses["now"], "pending")
        self.assertEqual(statuses["processing"], "processing")
        self.assertEqual(statuses["other-user"], "pending")

    def test_all_future_cancellation_requires_the_owned_task_and_returns_empty_for_no_future_rows(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a")
            self._insert_task(conn, "task-b", "user-b")
            self._insert_reminder(conn, "other-user", "task-b", NOW + timedelta(hours=1))

            self.assertEqual(
                cancel_all_future_pending_reminders_for_task(
                    conn,
                    user_id="user-a",
                    task_id="task-a",
                    now=NOW,
                ),
                (),
            )
            with self.assertRaises(ReminderNotFoundError):
                cancel_all_future_pending_reminders_for_task(
                    conn,
                    user_id="user-a",
                    task_id="task-b",
                    now=NOW,
                )
            status = conn.execute(
                "SELECT status FROM reminders WHERE id = 'other-user'"
            ).fetchone()[0]

        self.assertEqual(status, "pending")

    def test_cancellation_rejects_connections_with_an_active_caller_transaction(self) -> None:
        operations = (
            lambda conn: cancel_pending_reminder(
                conn,
                user_id="user-a",
                reminder_id="reminder-a",
                cancelled_at=NOW,
            ),
            lambda conn: cancel_selected_pending_reminders(
                conn,
                user_id="user-a",
                task_id="task-a",
                reminder_ids=("reminder-a",),
                cancelled_at=NOW,
            ),
            lambda conn: cancel_all_future_pending_reminders_for_task(
                conn,
                user_id="user-a",
                task_id="task-a",
                now=NOW,
            ),
        )
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a")
            self._insert_reminder(conn, "reminder-a", "task-a", NOW + timedelta(hours=1))

            for operation in operations:
                conn.execute("BEGIN")
                with self.assertRaises(RuntimeError):
                    operation(conn)
                conn.rollback()
            status = conn.execute(
                "SELECT status FROM reminders WHERE id = 'reminder-a'"
            ).fetchone()[0]

        self.assertEqual(status, "pending")

    def test_cancellation_and_due_claim_follow_first_writer_wins(self) -> None:
        with self.open_database() as (conn, db_path):
            self._insert_task(conn, "task-a", "user-a")
            self._insert_reminder(conn, "due", "task-a", NOW - timedelta(seconds=1))
            barrier = Barrier(2)
            claim_results: list[tuple] = []
            cancellation_results: list[bool] = []
            cancellation_errors: list[ReminderValidationError] = []
            unexpected_errors: list[Exception] = []

            def claim_in_thread() -> None:
                thread_conn = connect(db_path)
                try:
                    barrier.wait()
                    claim_results.append(claim_due_reminders(thread_conn, now=NOW, batch_size=1))
                except Exception as error:
                    unexpected_errors.append(error)
                finally:
                    thread_conn.close()

            def cancel_in_thread() -> None:
                thread_conn = connect(db_path)
                try:
                    barrier.wait()
                    result = cancel_pending_reminder(
                        thread_conn,
                        user_id="user-a",
                        reminder_id="due",
                        cancelled_at=NOW,
                    )
                    cancellation_results.append(result.was_cancelled)
                except ReminderValidationError as error:
                    cancellation_errors.append(error)
                except Exception as error:
                    unexpected_errors.append(error)
                finally:
                    thread_conn.close()

            claim_thread = Thread(target=claim_in_thread)
            cancel_thread = Thread(target=cancel_in_thread)
            claim_thread.start()
            cancel_thread.start()
            claim_thread.join(timeout=10)
            cancel_thread.join(timeout=10)
            final_status = conn.execute(
                "SELECT status FROM reminders WHERE id = 'due'"
            ).fetchone()[0]

        self.assertFalse(unexpected_errors)
        self.assertFalse(claim_thread.is_alive())
        self.assertFalse(cancel_thread.is_alive())
        claimed_ids = [
            reminder.reminder_id for result in claim_results for reminder in result
        ]
        if final_status == "cancelled":
            self.assertEqual(cancellation_results, [True])
            self.assertEqual(cancellation_errors, [])
            self.assertEqual(claimed_ids, [])
        else:
            self.assertEqual(final_status, "processing")
            self.assertEqual(cancellation_results, [])
            self.assertEqual(
                [error.code for error in cancellation_errors],
                ["reminder_not_cancellable"],
            )
            self.assertEqual(claimed_ids, ["due"])

    def _statuses(self, conn):
        return {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM reminders").fetchall()
        }

    def _insert_user(self, conn, user_id, telegram_user_id):
        with conn:
            conn.execute(
                "INSERT INTO users (id, telegram_user_id, timezone) VALUES (?, ?, 'America/Chicago')",
                (user_id, telegram_user_id),
            )

    def _insert_task(self, conn, task_id, user_id):
        with conn:
            conn.execute(
                """
                INSERT INTO items (
                    id, user_id, item_type, pub_ref, title, status, source,
                    parse_status, created_at, updated_at
                ) VALUES (?, ?, 'task', ?, ?, 'active', 'manual_entry', 'not_applicable', ?, ?)
                """,
                (
                    task_id,
                    user_id,
                    f"T{sum(ord(character) for character in task_id)}",
                    task_id,
                    NOW_TEXT,
                    NOW_TEXT,
                ),
            )
            conn.execute("INSERT INTO task_items (item_id) VALUES (?)", (task_id,))

    def _insert_reminder(
        self,
        conn,
        reminder_id,
        task_id,
        scheduled_at,
        *,
        status="pending",
        last_attempted_at=None,
        sent_at=None,
        failure_reason=None,
        cancelled_at=None,
        expired_at=None,
    ):
        scheduled_at_text = (
            to_storage_text(scheduled_at)
            if isinstance(scheduled_at, datetime)
            else scheduled_at
        )
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
                    scheduled_at_text,
                    status,
                    last_attempted_at,
                    sent_at,
                    failure_reason,
                    cancelled_at,
                    expired_at,
                    NOW_TEXT,
                    NOW_TEXT,
                ),
            )


if __name__ == "__main__":
    unittest.main()
