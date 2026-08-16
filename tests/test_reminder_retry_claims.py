from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, Thread
from unittest.mock import patch

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app import reminders
from tele_secretary.app.reminders import (
    DEFAULT_CLAIM_BATCH_SIZE,
    MAX_CLAIM_BATCH_SIZE,
    claim_due_reminder_retries,
    claim_due_reminders,
)
from tele_secretary.persistence.connection import connect
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.time_utils import to_storage_text


NOW = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)
NOW_TEXT = to_storage_text(NOW)


class ReminderRetryClaimTests(unittest.TestCase):
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "secretary.sqlite3"
            with open_test_database(db_path) as conn:
                apply_migrations(conn)
                self._insert_user(conn, "user-a", 1001, "America/Chicago")
                self._insert_user(conn, "user-b", None, "Europe/London")
                yield conn, db_path

    def test_retry_claims_include_exact_boundaries_and_exclude_just_before_them(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            for retry_count, eligible_at, ineligible_at in (
                (1, "2026-07-22T14:59:00+00:00", "2026-07-22T14:59:01+00:00"),
                (2, "2026-07-22T14:55:00+00:00", "2026-07-22T14:55:01+00:00"),
                (3, "2026-07-22T14:45:00+00:00", "2026-07-22T14:45:01+00:00"),
            ):
                self._insert_reminder(
                    conn,
                    f"retry-{retry_count}-eligible",
                    "task-a",
                    f"2026-07-22T12:00:{retry_count * 2:02}+00:00",
                    retry_count=retry_count,
                    last_attempted_at=eligible_at,
                )
                self._insert_reminder(
                    conn,
                    f"retry-{retry_count}-early",
                    "task-a",
                    f"2026-07-22T12:00:{retry_count * 2 + 1:02}+00:00",
                    retry_count=retry_count,
                    last_attempted_at=ineligible_at,
                )

            claimed = claim_due_reminder_retries(conn, now=NOW)
            statuses = self._reminder_statuses(conn)

        self.assertEqual(
            [record.reminder_id for record in claimed],
            ["retry-1-eligible", "retry-2-eligible", "retry-3-eligible"],
        )
        self.assertEqual(
            {reminder_id for reminder_id, status in statuses.items() if status == "pending"},
            {"retry-1-early", "retry-2-early", "retry-3-early"},
        )

    def test_first_attempt_and_retry_selectors_are_mutually_exclusive(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            self._insert_reminder(conn, "first", "task-a", "2026-07-22T14:59:00+00:00")
            self._insert_reminder(
                conn,
                "retry",
                "task-a",
                "2026-07-22T10:00:00+00:00",
                retry_count=1,
                last_attempted_at="2026-07-22T14:59:00+00:00",
            )

            first_attempts = claim_due_reminders(conn, now=NOW)
            retries = claim_due_reminder_retries(conn, now=NOW)

        self.assertEqual([record.reminder_id for record in first_attempts], ["first"])
        self.assertEqual([record.reminder_id for record in retries], ["retry"])

    def test_retry_claims_exclude_ineligible_lifecycle_rows_and_keep_them_unchanged(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-active", "user-a", "T1", "Active")
            self._insert_task(conn, "task-completed", "user-a", "T2", "Completed", status="completed")
            self._insert_task(conn, "task-deleted", "user-a", "T3", "Deleted", status="deleted", deleted_at=NOW_TEXT)
            self._insert_reminder(
                conn,
                "eligible",
                "task-active",
                "2026-07-22T10:00:00+00:00",
                retry_count=1,
                last_attempted_at="2026-07-22T14:59:00+00:00",
                failure_reason="network",
            )
            excluded_rows = (
                ("first-attempt", "task-active", "pending", 0, None),
                ("terminal", "task-active", "failed", 4, "2026-07-22T14:00:00+00:00"),
                ("processing", "task-active", "processing", 1, "2026-07-22T14:00:00+00:00"),
                ("fresh", "task-active", "pending", 1, "2026-07-22T14:59:01+00:00"),
                ("missing-attempt", "task-active", "pending", 1, None),
                ("malformed-attempt", "task-active", "pending", 1, "not-a-timestamp"),
                ("inactive", "task-completed", "pending", 1, "2026-07-22T14:00:00+00:00"),
                ("deleted", "task-deleted", "pending", 1, "2026-07-22T14:00:00+00:00"),
            )
            for index, (reminder_id, task_id, status, retry_count, attempted_at) in enumerate(excluded_rows):
                self._insert_reminder(
                    conn,
                    reminder_id,
                    task_id,
                    f"2026-07-22T10:00:{index + 1:02}+00:00",
                    status=status,
                    retry_count=retry_count,
                    last_attempted_at=attempted_at,
                    failure_reason="network" if status == "failed" else None,
                )

            claimed = claim_due_reminder_retries(conn, now=NOW)
            statuses = self._reminder_statuses(conn)

        self.assertEqual([record.reminder_id for record in claimed], ["eligible"])
        self.assertTrue(all(statuses[reminder_id] == status for reminder_id, _, status, _, _ in excluded_rows))

    def test_retry_claims_order_by_next_attempt_then_id_and_obey_batch_limit(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            self._insert_reminder(
                conn,
                "same-next-b",
                "task-a",
                "2026-07-22T10:00:00+00:00",
                retry_count=1,
                last_attempted_at="2026-07-22T14:58:00+00:00",
            )
            self._insert_reminder(
                conn,
                "same-next-a",
                "task-a",
                "2026-07-22T14:59:59+00:00",
                retry_count=2,
                last_attempted_at="2026-07-22T14:54:00+00:00",
            )
            self._insert_reminder(
                conn,
                "later-next",
                "task-a",
                "2026-07-22T09:00:00+00:00",
                retry_count=3,
                last_attempted_at="2026-07-22T14:45:00+00:00",
            )

            first_batch = claim_due_reminder_retries(conn, now=NOW, batch_size=2)
            second_batch = claim_due_reminder_retries(conn, now=NOW, batch_size=2)

        self.assertEqual([record.reminder_id for record in first_batch], ["same-next-a", "same-next-b"])
        self.assertEqual([record.reminder_id for record in second_batch], ["later-next"])

    def test_retry_claim_preserves_attempt_metadata_and_returns_delivery_context(self) -> None:
        attempted_at = "2026-07-22T14:59:00+00:00"
        scheduled_at = "2026-07-20T10:00:00+00:00"
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            self._insert_reminder(
                conn,
                "retry",
                "task-a",
                scheduled_at,
                retry_count=1,
                last_attempted_at=attempted_at,
                failure_reason="network",
            )

            claimed = claim_due_reminder_retries(conn, now=NOW)
            row = conn.execute(
                """
                SELECT status, scheduled_at, retry_count, last_attempted_at,
                    failure_reason, delivery_channel, updated_at
                FROM reminders WHERE id = 'retry'
                """
            ).fetchone()

        self.assertEqual(
            claimed[0].reminder_id,
            "retry",
        )
        self.assertEqual(
            (claimed[0].task_id, claimed[0].user_id, claimed[0].task_ref, claimed[0].task_title),
            ("task-a", "user-a", "T1", "Task"),
        )
        self.assertEqual(
            (claimed[0].status, claimed[0].retry_count, claimed[0].claimed_at),
            ("processing", 1, NOW_TEXT),
        )
        self.assertEqual(
            tuple(row),
            ("processing", scheduled_at, 1, attempted_at, "network", "telegram", NOW_TEXT),
        )

    def test_repeated_and_overlapping_retry_claims_are_disjoint(self) -> None:
        with self.open_database() as (conn, db_path):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            for index in range(6):
                self._insert_reminder(
                    conn,
                    f"retry-{index}",
                    "task-a",
                    f"2026-07-22T10:00:{index:02}+00:00",
                    retry_count=1,
                    last_attempted_at="2026-07-22T14:59:00+00:00",
                )

            barrier = Barrier(2)
            result_batches: list[tuple] = []
            errors: list[Exception] = []

            def claim_in_thread() -> None:
                thread_conn = connect(db_path)
                try:
                    barrier.wait()
                    result_batches.append(claim_due_reminder_retries(thread_conn, now=NOW, batch_size=3))
                except Exception as error:
                    errors.append(error)
                finally:
                    thread_conn.close()

            threads = [Thread(target=claim_in_thread) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            statuses = self._reminder_statuses(conn)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        claimed_ids = [record.reminder_id for batch in result_batches for record in batch]
        self.assertEqual(len(claimed_ids), 6)
        self.assertEqual(len(set(claimed_ids)), 6)
        self.assertTrue(all(status == "processing" for status in statuses.values()))

    def test_context_failure_rolls_back_the_selected_retry_batch(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            self._insert_reminder(
                conn,
                "retry",
                "task-a",
                "2026-07-22T10:00:00+00:00",
                retry_count=1,
                last_attempted_at="2026-07-22T14:59:00+00:00",
            )

            with patch.object(reminders, "_claimed_reminder_from_row", side_effect=RuntimeError("forced")):
                with self.assertRaisesRegex(RuntimeError, "forced"):
                    claim_due_reminder_retries(conn, now=NOW)
            status = self._reminder_statuses(conn)["retry"]

        self.assertEqual(status, "pending")

    def test_invalid_inputs_and_caller_transaction_do_not_change_retry_rows(self) -> None:
        with self.open_database() as (conn, _):
            self._insert_task(conn, "task-a", "user-a", "T1", "Task")
            self._insert_reminder(
                conn,
                "retry",
                "task-a",
                "2026-07-22T10:00:00+00:00",
                retry_count=1,
                last_attempted_at="2026-07-22T14:59:00+00:00",
            )
            for batch_size in (0, -1, MAX_CLAIM_BATCH_SIZE + 1, 1.5, "1", True):
                with self.subTest(batch_size=batch_size):
                    with self.assertRaises(ValueError):
                        claim_due_reminder_retries(conn, now=NOW, batch_size=batch_size)
                    self.assertFalse(conn.in_transaction)
            with self.assertRaises(ValueError):
                claim_due_reminder_retries(conn, now=NOW.replace(tzinfo=None))
            conn.execute("BEGIN")
            with self.assertRaises(RuntimeError):
                claim_due_reminder_retries(conn, now=NOW)
            conn.rollback()
            status = self._reminder_statuses(conn)["retry"]

        self.assertEqual(status, "pending")

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

    def _reminder_statuses(self, conn) -> dict[str, str]:
        return {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM reminders").fetchall()
        }


if __name__ == "__main__":
    unittest.main()
