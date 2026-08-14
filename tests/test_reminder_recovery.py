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
    ClaimedReminderRecord,
    ReminderRecoveryDeliveryKind,
    apply_reminder_downtime_recovery,
)
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.time_utils import to_storage_text


NOW = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
EARLIER = to_storage_text(NOW - timedelta(days=1))


class ReminderRecoveryTests(unittest.TestCase):
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with open_test_database(Path(temp_dir) / "secretary.sqlite3") as conn:
                apply_migrations(conn)
                self._insert_user(conn, "user-a")
                yield conn

    def test_recovery_uses_exact_downtime_boundaries_and_expires_oldest_band(self) -> None:
        schedules = (
            ("now", NOW, ReminderRecoveryDeliveryKind.NORMAL),
            ("fifty-nine-fifty-nine", NOW - timedelta(minutes=59, seconds=59), ReminderRecoveryDeliveryKind.NORMAL),
            ("sixty", NOW - timedelta(minutes=60), ReminderRecoveryDeliveryKind.NORMAL),
            ("sixty-one", NOW - timedelta(minutes=60, seconds=1), ReminderRecoveryDeliveryKind.MISSED),
            ("eleven-fifty-nine-fifty-nine", NOW - timedelta(hours=11, minutes=59, seconds=59), ReminderRecoveryDeliveryKind.MISSED),
            ("twelve", NOW - timedelta(hours=12), None),
            ("twelve-one", NOW - timedelta(hours=12, seconds=1), None),
        )
        with self.open_database() as conn:
            self._insert_task(conn, "task-a")
            for reminder_id, scheduled_at, _ in schedules:
                self._insert_reminder(conn, reminder_id, "task-a", scheduled_at)

            recovered = apply_reminder_downtime_recovery(
                conn,
                claimed_reminders=tuple(
                    self._claimed_reminder(reminder_id, "task-a", scheduled_at)
                    for reminder_id, scheduled_at, _ in schedules
                ),
                now=NOW,
            )
            expiration_rows = conn.execute(
                "SELECT id, status, expired_at, updated_at FROM reminders WHERE id IN ('twelve', 'twelve-one') ORDER BY id"
            ).fetchall()

        self.assertEqual(
            [record.reminder.reminder_id for record in recovered],
            ["now", "fifty-nine-fifty-nine", "sixty", "sixty-one", "eleven-fifty-nine-fifty-nine"],
        )
        self.assertEqual(
            [record.delivery_kind for record in recovered],
            [
                ReminderRecoveryDeliveryKind.NORMAL,
                ReminderRecoveryDeliveryKind.NORMAL,
                ReminderRecoveryDeliveryKind.NORMAL,
                ReminderRecoveryDeliveryKind.MISSED,
                ReminderRecoveryDeliveryKind.MISSED,
            ],
        )
        self.assertEqual(
            [(row["status"], row["expired_at"], row["updated_at"]) for row in expiration_rows],
            [("expired", to_storage_text(NOW), to_storage_text(NOW))] * 2,
        )

    def test_recovery_uses_one_normalized_second_precision_clock(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a")
            scheduled_at = NOW - timedelta(minutes=60)
            self._insert_reminder(conn, "boundary", "task-a", scheduled_at)

            recovered = apply_reminder_downtime_recovery(
                conn,
                claimed_reminders=(self._claimed_reminder("boundary", "task-a", scheduled_at),),
                now=datetime(2026, 7, 23, 10, 0, 0, 999999, tzinfo=timezone(timedelta(hours=-5))),
            )

        self.assertEqual(recovered[0].delivery_kind, ReminderRecoveryDeliveryKind.NORMAL)

    def test_recovery_preserves_claim_order_after_filtering_mixed_bands(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a")
            reminder_data = (
                ("missed-first", NOW - timedelta(hours=2)),
                ("expired", NOW - timedelta(hours=12)),
                ("normal-second", NOW - timedelta(minutes=10)),
            )
            for reminder_id, scheduled_at in reminder_data:
                self._insert_reminder(conn, reminder_id, "task-a", scheduled_at)

            recovered = apply_reminder_downtime_recovery(
                conn,
                claimed_reminders=tuple(
                    self._claimed_reminder(reminder_id, "task-a", scheduled_at)
                    for reminder_id, scheduled_at in reminder_data
                ),
                now=NOW,
            )

        self.assertEqual(
            [(record.reminder.reminder_id, record.delivery_kind) for record in recovered],
            [
                ("missed-first", ReminderRecoveryDeliveryKind.MISSED),
                ("normal-second", ReminderRecoveryDeliveryKind.NORMAL),
            ],
        )

    def test_recovery_omits_missing_terminal_attempted_and_inactive_rows(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "active-task")
            self._insert_task(conn, "completed-task", status="completed")
            schedule = NOW - timedelta(minutes=5)
            self._insert_reminder(conn, "active", "active-task", schedule)
            self._insert_reminder(conn, "sent", "active-task", schedule, status="sent", sent_at=EARLIER)
            self._insert_reminder(
                conn,
                "failed",
                "active-task",
                schedule,
                status="failed",
                last_attempted_at=EARLIER,
                failure_reason="network",
            )
            self._insert_reminder(
                conn,
                "cancelled",
                "active-task",
                schedule,
                status="cancelled",
                cancelled_at=EARLIER,
            )
            self._insert_reminder(
                conn,
                "expired",
                "active-task",
                schedule,
                status="expired",
                expired_at=EARLIER,
            )
            self._insert_reminder(
                conn,
                "retry",
                "active-task",
                schedule - timedelta(seconds=1),
                retry_count=1,
                last_attempted_at=EARLIER,
            )
            self._insert_reminder(conn, "inactive", "completed-task", schedule)

            with self.assertLogs("tele_secretary.app.reminders", level="INFO") as logs:
                recovered = apply_reminder_downtime_recovery(
                    conn,
                    claimed_reminders=(
                        self._claimed_reminder("active", "active-task", schedule),
                        self._claimed_reminder("sent", "active-task", schedule),
                        self._claimed_reminder("failed", "active-task", schedule),
                        self._claimed_reminder("cancelled", "active-task", schedule),
                        self._claimed_reminder("expired", "active-task", schedule),
                        self._claimed_reminder(
                            "retry",
                            "active-task",
                            schedule - timedelta(seconds=1),
                        ),
                        self._claimed_reminder("inactive", "completed-task", schedule),
                        self._claimed_reminder("missing", "missing-task", schedule),
                    ),
                    now=NOW,
                )
            statuses = {
                row["id"]: row["status"]
                for row in conn.execute("SELECT id, status FROM reminders ORDER BY id").fetchall()
            }

        self.assertEqual([record.reminder.reminder_id for record in recovered], ["active"])
        self.assertEqual(
            statuses,
            {
                "active": "processing",
                "cancelled": "cancelled",
                "expired": "expired",
                "failed": "failed",
                "inactive": "processing",
                "retry": "processing",
                "sent": "sent",
            },
        )
        log_text = "\n".join(logs.output)
        self.assertIn("reminder_id=active", log_text)
        self.assertIn("action=normal", log_text)
        self.assertIn("reminder_id=sent action=unchanged current_status=sent", log_text)
        self.assertIn("reminder_id=failed action=unchanged current_status=failed", log_text)
        self.assertIn("reminder_id=cancelled action=unchanged current_status=cancelled", log_text)
        self.assertIn("reminder_id=expired action=unchanged current_status=expired", log_text)
        self.assertIn("reminder_id=retry action=unchanged_retry", log_text)
        self.assertIn("reminder_id=inactive action=unchanged_inactive", log_text)
        self.assertIn("reminder_id=missing action=unchanged_missing", log_text)

    def test_recovery_rejects_invalid_input_before_changing_rows(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a")
            scheduled_at = NOW - timedelta(hours=12)
            self._insert_reminder(conn, "expired", "task-a", scheduled_at)
            claim = self._claimed_reminder("expired", "task-a", scheduled_at)

            with self.assertRaisesRegex(ValueError, "duplicate"):
                apply_reminder_downtime_recovery(conn, claimed_reminders=(claim, claim), now=NOW)
            with self.assertRaisesRegex(ValueError, "future"):
                apply_reminder_downtime_recovery(
                    conn,
                    claimed_reminders=(
                        self._claimed_reminder("expired", "task-a", NOW + timedelta(seconds=1)),
                    ),
                    now=NOW,
                )
            with self.assertRaises(ValueError):
                apply_reminder_downtime_recovery(conn, claimed_reminders=(claim,), now=NOW.replace(tzinfo=None))
            status = conn.execute("SELECT status FROM reminders WHERE id = 'expired'").fetchone()[0]

        self.assertEqual(status, "processing")

    def test_recovery_rejects_active_caller_transaction_without_changing_rows(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a")
            scheduled_at = NOW - timedelta(hours=12)
            self._insert_reminder(conn, "expired", "task-a", scheduled_at)
            conn.execute("BEGIN")
            with self.assertRaisesRegex(RuntimeError, "active transaction"):
                apply_reminder_downtime_recovery(
                    conn,
                    claimed_reminders=(self._claimed_reminder("expired", "task-a", scheduled_at),),
                    now=NOW,
                )
            conn.rollback()
            status = conn.execute("SELECT status FROM reminders WHERE id = 'expired'").fetchone()[0]

        self.assertEqual(status, "processing")

    def test_recovery_rolls_back_when_persisted_schedule_does_not_match_claim(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a")
            expired_schedule = NOW - timedelta(hours=12)
            mismatch_schedule = NOW - timedelta(minutes=10)
            self._insert_reminder(conn, "expired", "task-a", expired_schedule)
            self._insert_reminder(conn, "mismatch", "task-a", mismatch_schedule)

            with conn:
                conn.execute(
                    "UPDATE reminders SET scheduled_at = ? WHERE id = 'mismatch'",
                    (to_storage_text(NOW - timedelta(minutes=11)),),
                )
            with self.assertRaisesRegex(RuntimeError, "did not match"):
                apply_reminder_downtime_recovery(
                    conn,
                    claimed_reminders=(
                        self._claimed_reminder("expired", "task-a", expired_schedule),
                        self._claimed_reminder("mismatch", "task-a", mismatch_schedule),
                    ),
                    now=NOW,
                )
            status = conn.execute("SELECT status FROM reminders WHERE id = 'expired'").fetchone()[0]

        self.assertEqual(status, "processing")

    def test_recovery_rolls_back_expiration_and_logs_nothing_when_update_fails(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a")
            scheduled_at = NOW - timedelta(hours=12)
            self._insert_reminder(conn, "expired", "task-a", scheduled_at)
            with conn:
                conn.execute(
                    """
                    CREATE TRIGGER fail_recovery_expiration
                    BEFORE UPDATE OF status ON reminders
                    WHEN NEW.status = 'expired'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced expiration failure');
                    END;
                    """
                )

            with self.assertNoLogs("tele_secretary.app.reminders", level="INFO"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "forced expiration failure"):
                    apply_reminder_downtime_recovery(
                        conn,
                        claimed_reminders=(self._claimed_reminder("expired", "task-a", scheduled_at),),
                        now=NOW,
                    )
            row = conn.execute(
                "SELECT status, expired_at FROM reminders WHERE id = 'expired'"
            ).fetchone()

        self.assertEqual((row["status"], row["expired_at"]), ("processing", None))

    def _claimed_reminder(
        self,
        reminder_id: str,
        task_id: str,
        scheduled_at: datetime,
    ) -> ClaimedReminderRecord:
        return ClaimedReminderRecord(
            reminder_id=reminder_id,
            task_id=task_id,
            user_id="user-a",
            telegram_user_id=1001,
            user_timezone="America/Chicago",
            task_ref="T1",
            task_title="Task",
            scheduled_at=to_storage_text(scheduled_at),
            status="processing",
            delivery_channel="telegram",
            retry_count=0,
            claimed_at=to_storage_text(NOW),
        )

    def _insert_user(self, conn, user_id: str) -> None:
        with conn:
            conn.execute(
                "INSERT INTO users (id, telegram_user_id, timezone) VALUES (?, ?, 'America/Chicago')",
                (user_id, 1001),
            )

    def _insert_task(
        self,
        conn,
        task_id: str,
        *,
        status: str = "active",
        deleted_at: str | None = None,
    ) -> None:
        with conn:
            conn.execute(
                """
                INSERT INTO items (
                    id, user_id, item_type, pub_ref, title, status, source,
                    parse_status, created_at, updated_at, deleted_at
                ) VALUES (?, 'user-a', 'task', ?, ?, ?, 'manual_entry',
                    'not_applicable', ?, ?, ?)
                """,
                (
                    task_id,
                    f"T{sum(ord(character) for character in task_id)}",
                    task_id,
                    status,
                    EARLIER,
                    EARLIER,
                    deleted_at,
                ),
            )
            conn.execute("INSERT INTO task_items (item_id) VALUES (?)", (task_id,))

    def _insert_reminder(
        self,
        conn,
        reminder_id: str,
        task_id: str,
        scheduled_at: datetime,
        *,
        status: str = "processing",
        retry_count: int = 0,
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
                ) VALUES (?, ?, ?, ?, 'telegram', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder_id,
                    task_id,
                    to_storage_text(scheduled_at),
                    status,
                    retry_count,
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
