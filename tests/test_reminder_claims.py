from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Thread
from unittest.mock import patch

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.persistence.connection import connect
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.app import reminders
from tele_secretary.app.reminders import (
    DEFAULT_CLAIM_BATCH_SIZE,
    MAX_CLAIM_BATCH_SIZE,
    claim_due_reminders,
)
from tele_secretary.time_utils import to_storage_text


NOW = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)
NOW_TEXT = to_storage_text(NOW)


class ReminderClaimTests(unittest.TestCase):
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with open_test_database(db_path) as conn:
                apply_migrations(conn)
                self._insert_user(conn, "user-a", 1001, "America/Chicago")
                self._insert_user(conn, "user-b", None, "Europe/London")
                yield conn, db_path

    def test_claims_due_pending_active_reminders_in_deterministic_order_with_context(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "First task")
            self._insert_task(conn, "task-b", "user-b", "T2", "Second task")
            self._insert_task(conn, "task-completed", "user-a", "T3", "Completed", status="completed")
            self._insert_task(conn, "task-archived", "user-a", "T4", "Archived", status="archived")
            self._insert_task(conn, "task-deleted", "user-a", "T5", "Deleted", status="deleted", deleted_at=NOW_TEXT)
            self._insert_reminder(conn, "due-earlier", "task-b", "2026-07-22T14:59:00+00:00")
            self._insert_reminder(conn, "due-now", "task-a", NOW_TEXT)
            self._insert_reminder(conn, "future", "task-a", "2026-07-22T15:00:01+00:00")
            self._insert_reminder(conn, "already-processing", "task-a", "2026-07-22T14:58:00+00:00", status="processing", retry_count=2)
            self._insert_reminder(conn, "sent", "task-a", "2026-07-22T14:57:00+00:00", status="sent", sent_at=NOW_TEXT)
            self._insert_reminder(conn, "failed", "task-a", "2026-07-22T14:56:00+00:00", status="failed", last_attempted_at=NOW_TEXT, failure_reason="network")
            self._insert_reminder(conn, "cancelled", "task-a", "2026-07-22T14:55:00+00:00", status="cancelled", cancelled_at=NOW_TEXT)
            self._insert_reminder(conn, "expired", "task-a", "2026-07-22T14:54:00+00:00", status="expired", expired_at=NOW_TEXT)
            self._insert_reminder(conn, "inactive-completed", "task-completed", "2026-07-22T14:53:00+00:00")
            self._insert_reminder(conn, "inactive-archived", "task-archived", "2026-07-22T14:52:00+00:00")
            self._insert_reminder(conn, "inactive-deleted", "task-deleted", "2026-07-22T14:51:00+00:00")

            claimed = claim_due_reminders(conn, now=NOW)
            status_by_id = {
                row["id"]: row["status"]
                for row in conn.execute("SELECT id, status FROM reminders").fetchall()
            }

        self.assertEqual([record.reminder_id for record in claimed], ["due-earlier", "due-now"])
        self.assertEqual(claimed[0].telegram_user_id, None)
        self.assertEqual(claimed[0].user_timezone, "Europe/London")
        self.assertEqual((claimed[1].task_ref, claimed[1].task_title), ("T1", "First task"))
        self.assertEqual((claimed[1].status, claimed[1].claimed_at), ("processing", NOW_TEXT))
        self.assertEqual(status_by_id["due-earlier"], "processing")
        self.assertEqual(status_by_id["due-now"], "processing")
        self.assertEqual(status_by_id["future"], "pending")
        self.assertEqual(status_by_id["inactive-completed"], "pending")
        self.assertEqual(status_by_id["inactive-archived"], "pending")
        self.assertEqual(status_by_id["inactive-deleted"], "pending")

    def test_batch_limits_leave_remaining_due_reminders_for_later_claims(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            for index in range(DEFAULT_CLAIM_BATCH_SIZE + 1):
                self._insert_reminder(
                    conn,
                    f"due-{index:03}",
                    "task-a",
                    to_storage_text(NOW - timedelta(seconds=index + 1)),
                )

            first_batch = claim_due_reminders(conn, now=NOW)
            second_batch = claim_due_reminders(conn, now=NOW)

        self.assertEqual(len(first_batch), DEFAULT_CLAIM_BATCH_SIZE)
        self.assertEqual(len(second_batch), 1)
        self.assertTrue(set(record.reminder_id for record in first_batch).isdisjoint(record.reminder_id for record in second_batch))

    def test_batch_size_validation_happens_before_a_transaction_or_state_change(self) -> None:
        invalid_batch_sizes = (0, -1, MAX_CLAIM_BATCH_SIZE + 1, 1.5, "1", True, False)
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            self._insert_reminder(conn, "due", "task-a", "2026-07-22T14:59:00+00:00")
            for batch_size in invalid_batch_sizes:
                with self.subTest(batch_size=batch_size):
                    with self.assertRaises(ValueError):
                        claim_due_reminders(conn, now=NOW, batch_size=batch_size)
                    self.assertFalse(conn.in_transaction)
            one_claim = claim_due_reminders(conn, now=NOW, batch_size=1)

        self.assertEqual([record.reminder_id for record in one_claim], ["due"])

    def test_batch_size_boundaries_and_empty_eligible_set_commit_cleanly(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            self._insert_reminder(conn, "due", "task-a", "2026-07-22T14:59:00+00:00")

            claimed_at_one = claim_due_reminders(conn, now=NOW, batch_size=1)
            empty_claim = claim_due_reminders(conn, now=NOW, batch_size=MAX_CLAIM_BATCH_SIZE)

        self.assertEqual(len(claimed_at_one), 1)
        self.assertEqual(empty_claim, ())

    def test_repeated_claims_are_disjoint_and_keep_existing_processing_rows(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            self._insert_reminder(conn, "first", "task-a", "2026-07-22T14:58:00+00:00")
            self._insert_reminder(conn, "second", "task-a", "2026-07-22T14:59:00+00:00")

            first_claim = claim_due_reminders(conn, now=NOW, batch_size=1)
            second_claim = claim_due_reminders(conn, now=NOW, batch_size=1)
            statuses = conn.execute("SELECT id, status FROM reminders ORDER BY id").fetchall()

        self.assertNotEqual(first_claim[0].reminder_id, second_claim[0].reminder_id)
        self.assertEqual({row["status"] for row in statuses}, {"processing"})

    def test_forced_context_failure_rolls_back_all_claim_state_changes(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            self._insert_reminder(conn, "due", "task-a", "2026-07-22T14:59:00+00:00")

            with patch.object(reminders, "_claimed_reminder_from_row", side_effect=RuntimeError("forced")):
                with self.assertRaisesRegex(RuntimeError, "forced"):
                    claim_due_reminders(conn, now=NOW)
            status = conn.execute("SELECT status FROM reminders WHERE id = 'due'").fetchone()[0]

        self.assertEqual(status, "pending")

    def test_an_active_caller_transaction_is_rejected_before_claiming(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            self._insert_reminder(conn, "due", "task-a", "2026-07-22T14:59:00+00:00")
            conn.execute("BEGIN")
            with self.assertRaises(RuntimeError):
                claim_due_reminders(conn, now=NOW)
            conn.rollback()
            status = conn.execute("SELECT status FROM reminders WHERE id = 'due'").fetchone()[0]

        self.assertEqual(status, "pending")

    def test_overlapping_claimers_return_disjoint_batches(self) -> None:
        with self.open_database() as (conn, db_path):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            for index in range(6):
                self._insert_reminder(conn, f"due-{index}", "task-a", f"2026-07-22T14:5{index}:00+00:00")

            barrier = Barrier(2)
            result_batches: list[tuple] = []
            errors: list[Exception] = []

            def claim_in_thread() -> None:
                thread_conn = connect(db_path)
                try:
                    barrier.wait()
                    result_batches.append(claim_due_reminders(thread_conn, now=NOW, batch_size=3))
                except Exception as error:
                    errors.append(error)
                finally:
                    thread_conn.close()

            threads = [Thread(target=claim_in_thread) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            final_statuses = conn.execute("SELECT id, status FROM reminders ORDER BY id").fetchall()

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        claimed_ids = [record.reminder_id for batch in result_batches for record in batch]
        self.assertEqual(len(claimed_ids), 6)
        self.assertEqual(len(set(claimed_ids)), 6)
        self.assertTrue(all(row["status"] == "processing" for row in final_statuses))

    def test_claim_and_pending_only_cancellation_follow_first_writer_wins(self) -> None:
        with self.open_database() as (conn, db_path):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            self._insert_reminder(conn, "due", "task-a", "2026-07-22T14:59:00+00:00")

            barrier = Barrier(2)
            claimed_batches: list[tuple] = []
            cancellation_counts: list[int] = []
            errors: list[Exception] = []

            def claim_in_thread() -> None:
                thread_conn = connect(db_path)
                try:
                    barrier.wait()
                    claimed_batches.append(claim_due_reminders(thread_conn, now=NOW, batch_size=1))
                except Exception as error:
                    errors.append(error)
                finally:
                    thread_conn.close()

            def cancel_in_thread() -> None:
                thread_conn = connect(db_path)
                try:
                    barrier.wait()
                    thread_conn.execute("BEGIN IMMEDIATE")
                    cursor = thread_conn.execute(
                        """
                        UPDATE reminders
                        SET status = 'cancelled', cancelled_at = ?, updated_at = ?
                        WHERE id = 'due' AND status = 'pending'
                        """,
                        (NOW_TEXT, NOW_TEXT),
                    )
                    thread_conn.commit()
                    cancellation_counts.append(cursor.rowcount)
                except Exception as error:
                    thread_conn.rollback()
                    errors.append(error)
                finally:
                    thread_conn.close()

            claim_thread = Thread(target=claim_in_thread)
            cancel_thread = Thread(target=cancel_in_thread)
            claim_thread.start()
            cancel_thread.start()
            claim_thread.join(timeout=10)
            cancel_thread.join(timeout=10)
            final_status = conn.execute("SELECT status FROM reminders WHERE id = 'due'").fetchone()[0]

        self.assertFalse(errors)
        self.assertFalse(claim_thread.is_alive())
        self.assertFalse(cancel_thread.is_alive())
        claimed_ids = [record.reminder_id for batch in claimed_batches for record in batch]
        if final_status == "cancelled":
            self.assertEqual(claimed_ids, [])
            self.assertEqual(cancellation_counts, [1])
        else:
            self.assertEqual(final_status, "processing")
            self.assertEqual(claimed_ids, ["due"])
            self.assertEqual(cancellation_counts, [0])

    def _insert_user(self, conn, user_id, telegram_user_id, timezone_name):
        with conn:
            conn.execute(
                "INSERT INTO users (id, telegram_user_id, timezone) VALUES (?, ?, ?)",
                (user_id, telegram_user_id, timezone_name),
            )

    def _insert_task(self, conn, task_id, user_id, public_ref, title, *, status="active", deleted_at=None):
        with conn:
            conn.execute(
                """
                INSERT INTO items (
                    id, user_id, item_type, pub_ref, title, status, source,
                    parse_status, created_at, updated_at, deleted_at
                ) VALUES (?, ?, 'task', ?, ?, ?, 'manual_entry', 'not_applicable', ?, ?, ?)
                """,
                (task_id, user_id, public_ref, title, status, NOW_TEXT, NOW_TEXT, deleted_at),
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
        retry_count=0,
        last_attempted_at=None,
        sent_at=None,
        failure_reason=None,
        cancelled_at=None,
        expired_at=None,
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
                    "2026-07-22T14:00:00+00:00",
                ),
            )


if __name__ == "__main__":
    unittest.main()
