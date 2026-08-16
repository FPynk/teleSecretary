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
    ABANDONED_PROCESSING_LEASE,
    MAX_CLAIM_BATCH_SIZE,
    AbandonedReminderRecoveryAction,
    recover_abandoned_processing_reminders,
)
from tele_secretary.persistence.connection import connect
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.reminder_delivery import TELEGRAM_REQUEST_TIMEOUT_SECONDS
from tele_secretary.time_utils import to_storage_text


NOW = datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc)
NOW_TEXT = to_storage_text(NOW)


class AbandonedReminderRecoveryTests(unittest.TestCase):
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with open_test_database(db_path) as conn:
                apply_migrations(conn)
                self._insert_user(conn, "user-a", 1001, "America/Chicago")
                yield conn, db_path

    def test_telegram_request_timeout_is_shorter_than_the_processing_lease(self) -> None:
        self.assertLess(
            TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            ABANDONED_PROCESSING_LEASE.total_seconds(),
        )

    def test_recovers_a_claim_at_the_exact_lease_boundary_but_not_a_fresh_claim(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "T1", "Task")
            self._insert_reminder(
                conn,
                "at-boundary",
                "task-a",
                scheduled_at="2026-08-16T10:00:00+00:00",
                updated_at=to_storage_text(NOW - ABANDONED_PROCESSING_LEASE),
            )
            self._insert_reminder(
                conn,
                "fresh",
                "task-a",
                scheduled_at="2026-08-16T10:00:01+00:00",
                updated_at=to_storage_text(NOW - ABANDONED_PROCESSING_LEASE + timedelta(seconds=1)),
            )

            recovered = recover_abandoned_processing_reminders(conn, now=NOW)
            rows = self._rows_by_id(conn)

        self.assertEqual(
            [(result.reminder_id, result.action, result.retry_count) for result in recovered],
            [("at-boundary", AbandonedReminderRecoveryAction.REQUEUED, 0)],
        )
        self.assertEqual((rows["at-boundary"]["status"], rows["at-boundary"]["updated_at"]), ("pending", NOW_TEXT))
        self.assertEqual(
            (rows["fresh"]["status"], rows["fresh"]["updated_at"]),
            ("processing", "2026-08-16T14:55:01+00:00"),
        )

    def test_requeues_active_retries_and_cancels_inactive_tasks_without_losing_metadata(self) -> None:
        old_claim_time = "2026-08-16T14:50:00+00:00"
        attempted_at = "2026-08-16T14:40:00+00:00"
        with self.open_database() as (conn, _):
            self._insert_task(conn, "active", "T1", "Active")
            self._insert_task(conn, "completed", "T2", "Completed", status="completed")
            self._insert_task(conn, "archived", "T3", "Archived", status="archived")
            self._insert_task(
                conn,
                "deleted",
                "T4",
                "Deleted",
                status="deleted",
                deleted_at=NOW_TEXT,
            )
            for index, task_id in enumerate(("active", "completed", "archived", "deleted")):
                self._insert_reminder(
                    conn,
                    f"retry-{task_id}",
                    task_id,
                    scheduled_at=f"2026-08-16T10:00:0{index}+00:00",
                    retry_count=2,
                    last_attempted_at=attempted_at,
                    failure_reason="network",
                    updated_at=old_claim_time,
                )

            recovered = recover_abandoned_processing_reminders(conn, now=NOW)
            rows = self._rows_by_id(conn)

        actions = {result.reminder_id: result.action for result in recovered}
        self.assertEqual(actions["retry-active"], AbandonedReminderRecoveryAction.REQUEUED)
        self.assertEqual(
            {actions[reminder_id] for reminder_id in ("retry-completed", "retry-archived", "retry-deleted")},
            {AbandonedReminderRecoveryAction.CANCELLED},
        )
        self.assertEqual(
            tuple(rows["retry-active"][field] for field in ("status", "scheduled_at", "retry_count", "last_attempted_at", "failure_reason", "updated_at")),
            ("pending", "2026-08-16T10:00:00+00:00", 2, attempted_at, "network", NOW_TEXT),
        )
        for reminder_id in ("retry-completed", "retry-archived", "retry-deleted"):
            self.assertEqual(
                tuple(rows[reminder_id][field] for field in ("status", "retry_count", "last_attempted_at", "failure_reason", "cancelled_at", "updated_at")),
                ("cancelled", 2, attempted_at, "network", NOW_TEXT, NOW_TEXT),
            )

    def test_leaves_terminal_pending_and_fresh_processing_rows_unchanged(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "T1", "Task")
            rows = (
                ("pending", "pending", "2026-08-16T14:00:00+00:00"),
                ("sent", "sent", "2026-08-16T14:00:01+00:00"),
                ("failed", "failed", "2026-08-16T14:00:02+00:00"),
                ("cancelled", "cancelled", "2026-08-16T14:00:03+00:00"),
                ("expired", "expired", "2026-08-16T14:00:04+00:00"),
                ("fresh-processing", "processing", "2026-08-16T14:55:01+00:00"),
            )
            for reminder_id, status, updated_at in rows:
                self._insert_reminder(
                    conn,
                    reminder_id,
                    "task-a",
                    scheduled_at=f"2026-08-16T10:00:{len(reminder_id):02}+00:00",
                    status=status,
                    retry_count=1 if reminder_id == "failed" else 0,
                    last_attempted_at="2026-08-16T13:00:00+00:00" if reminder_id == "failed" else None,
                    failure_reason="network" if reminder_id == "failed" else None,
                    sent_at="2026-08-16T13:00:00+00:00" if reminder_id == "sent" else None,
                    cancelled_at="2026-08-16T13:00:00+00:00" if reminder_id == "cancelled" else None,
                    expired_at="2026-08-16T13:00:00+00:00" if reminder_id == "expired" else None,
                    updated_at=updated_at,
                )

            recovered = recover_abandoned_processing_reminders(conn, now=NOW)
            after_rows = self._rows_by_id(conn)

        self.assertEqual(recovered, ())
        self.assertEqual(
            {reminder_id: row["status"] for reminder_id, row in after_rows.items()},
            {reminder_id: status for reminder_id, status, _ in rows},
        )

    def test_recovers_oldest_leases_first_with_id_ties_and_batch_limits(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "T1", "Task")
            for reminder_id, claim_time, scheduled_at in (
                ("oldest", "2026-08-16T14:50:00+00:00", "2026-08-16T10:00:00+00:00"),
                ("tie-b", "2026-08-16T14:54:00+00:00", "2026-08-16T10:00:01+00:00"),
                ("tie-a", "2026-08-16T14:54:00+00:00", "2026-08-16T10:00:02+00:00"),
            ):
                self._insert_reminder(conn, reminder_id, "task-a", scheduled_at=scheduled_at, updated_at=claim_time)

            first_batch = recover_abandoned_processing_reminders(conn, now=NOW, batch_size=2)
            second_batch = recover_abandoned_processing_reminders(conn, now=NOW, batch_size=2)

        self.assertEqual([result.reminder_id for result in first_batch], ["oldest", "tie-a"])
        self.assertEqual([result.reminder_id for result in second_batch], ["tie-b"])

    def test_repeated_and_overlapping_recovery_calls_return_disjoint_batches(self) -> None:
        with self.open_database() as (conn, db_path):
            self._insert_task(conn, "task-a", "T1", "Task")
            for index in range(6):
                self._insert_reminder(
                    conn,
                    f"reminder-{index}",
                    "task-a",
                    scheduled_at=f"2026-08-16T10:00:{index:02}+00:00",
                    updated_at="2026-08-16T14:50:00+00:00",
                )

            barrier = Barrier(2)
            result_batches: list[tuple] = []
            errors: list[Exception] = []

            def recover_in_thread() -> None:
                thread_conn = connect(db_path)
                try:
                    barrier.wait()
                    result_batches.append(
                        recover_abandoned_processing_reminders(thread_conn, now=NOW, batch_size=3)
                    )
                except Exception as error:
                    errors.append(error)
                finally:
                    thread_conn.close()

            threads = [Thread(target=recover_in_thread) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            rows = self._rows_by_id(conn)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        recovered_ids = [result.reminder_id for batch in result_batches for result in batch]
        self.assertEqual(len(recovered_ids), 6)
        self.assertEqual(len(set(recovered_ids)), 6)
        self.assertTrue(all(row["status"] == "pending" for row in rows.values()))

    def test_recovery_rolls_back_every_transition_and_logs_nothing_when_an_update_fails(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "active", "T1", "Active")
            self._insert_task(conn, "completed", "T2", "Completed", status="completed")
            self._insert_reminder(
                conn,
                "requeue",
                "active",
                scheduled_at="2026-08-16T10:00:00+00:00",
                updated_at="2026-08-16T14:50:00+00:00",
            )
            self._insert_reminder(
                conn,
                "cancel",
                "completed",
                scheduled_at="2026-08-16T10:00:01+00:00",
                updated_at="2026-08-16T14:50:00+00:00",
            )
            with conn:
                conn.execute(
                    """
                    CREATE TRIGGER fail_abandoned_recovery
                    BEFORE UPDATE OF status ON reminders
                    WHEN NEW.status = 'cancelled'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced recovery failure');
                    END;
                    """
                )

            with self.assertNoLogs("tele_secretary.app.reminders", level="INFO"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "forced recovery failure"):
                    recover_abandoned_processing_reminders(conn, now=NOW)
            rows = self._rows_by_id(conn)

        self.assertEqual({reminder_id: row["status"] for reminder_id, row in rows.items()}, {"requeue": "processing", "cancel": "processing"})

    def test_invalid_input_and_caller_transaction_leave_processing_rows_unchanged(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "T1", "Task")
            self._insert_reminder(
                conn,
                "reminder",
                "task-a",
                scheduled_at="2026-08-16T10:00:00+00:00",
                updated_at="2026-08-16T14:50:00+00:00",
            )
            for batch_size in (0, -1, MAX_CLAIM_BATCH_SIZE + 1, 1.5, "1", True):
                with self.subTest(batch_size=batch_size):
                    with self.assertRaises(ValueError):
                        recover_abandoned_processing_reminders(conn, now=NOW, batch_size=batch_size)
                    self.assertFalse(conn.in_transaction)
            with self.assertRaises(ValueError):
                recover_abandoned_processing_reminders(conn, now=NOW.replace(tzinfo=None))
            conn.execute("BEGIN")
            with self.assertRaises(RuntimeError):
                recover_abandoned_processing_reminders(conn, now=NOW)
            conn.rollback()
            row = self._rows_by_id(conn)["reminder"]

        self.assertEqual((row["status"], row["updated_at"]), ("processing", "2026-08-16T14:50:00+00:00"))

    def _insert_user(self, conn, user_id, telegram_user_id, timezone_name):
        with conn:
            conn.execute(
                "INSERT INTO users (id, telegram_user_id, timezone) VALUES (?, ?, ?)",
                (user_id, telegram_user_id, timezone_name),
            )

    def _insert_task(self, conn, task_id, public_ref, title, *, status="active", deleted_at=None):
        with conn:
            conn.execute(
                """
                INSERT INTO items (
                    id, user_id, item_type, pub_ref, title, status, source,
                    parse_status, created_at, updated_at, deleted_at
                ) VALUES (?, 'user-a', 'task', ?, ?, ?, 'manual_entry', 'not_applicable', ?, ?, ?)
                """,
                (task_id, public_ref, title, status, NOW_TEXT, NOW_TEXT, deleted_at),
            )
            conn.execute("INSERT INTO task_items (item_id) VALUES (?)", (task_id,))

    def _insert_reminder(
        self,
        conn,
        reminder_id,
        task_id,
        *,
        scheduled_at,
        status="processing",
        retry_count=0,
        last_attempted_at=None,
        sent_at=None,
        failure_reason=None,
        cancelled_at=None,
        expired_at=None,
        updated_at,
    ):
        with conn:
            conn.execute(
                """
                INSERT INTO reminders (
                    id, item_id, scheduled_at, status, delivery_channel, retry_count,
                    last_attempted_at, sent_at, failure_reason, cancelled_at, expired_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'telegram', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder_id,
                    task_id,
                    scheduled_at,
                    status,
                    retry_count,
                    last_attempted_at,
                    sent_at,
                    failure_reason,
                    cancelled_at,
                    expired_at,
                    NOW_TEXT,
                    updated_at,
                ),
            )

    def _rows_by_id(self, conn):
        return {
            row["id"]: row
            for row in conn.execute(
                """
                SELECT id, status, scheduled_at, retry_count, last_attempted_at,
                    failure_reason, cancelled_at, updated_at
                FROM reminders
                """
            ).fetchall()
        }


if __name__ == "__main__":
    unittest.main()
