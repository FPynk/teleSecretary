from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.reminders import (
    AbandonedReminderRecoveryAction,
    ReminderDeliveryStateError,
    claim_due_reminder_retries,
    claim_due_reminders,
    record_claimed_reminder_sent,
    recover_abandoned_processing_reminders,
)
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.reminder_delivery import (
    TelegramReminderDeliveryOutcome,
    deliver_claimed_reminder,
)
from tele_secretary.time_utils import to_storage_text


INITIAL_ATTEMPT_TIME = datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc)
EARLIER = to_storage_text(INITIAL_ATTEMPT_TIME - timedelta(days=2))


class ReminderRetryLifecycleTestSupport:
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with open_test_database(Path(temp_dir) / "secretary.sqlite3") as conn:
                apply_migrations(conn)
                self._insert_user(conn)
                yield conn

    def _insert_user(self, conn) -> None:
        with conn:
            conn.execute(
                "INSERT INTO users (id, telegram_user_id, timezone) VALUES ('user-a', 1001, 'America/Chicago')"
            )

    def _insert_task(self, conn, task_id: str, *, status: str = "active") -> None:
        task_ref = f"T{conn.execute('SELECT COUNT(*) FROM items').fetchone()[0] + 1}"
        with conn:
            conn.execute(
                """
                INSERT INTO items (
                    id, user_id, item_type, pub_ref, title, status, source,
                    parse_status, created_at, updated_at
                ) VALUES (?, 'user-a', 'task', ?, ?, ?, 'manual_entry', 'not_applicable', ?, ?)
                """,
                (task_id, task_ref, f"Task {task_id}", status, EARLIER, EARLIER),
            )
            conn.execute("INSERT INTO task_items (item_id) VALUES (?)", (task_id,))

    def _insert_pending_reminder(
        self,
        conn,
        reminder_id: str,
        task_id: str,
        *,
        scheduled_at: datetime | None = None,
    ) -> None:
        with conn:
            conn.execute(
                """
                INSERT INTO reminders (
                    id, item_id, scheduled_at, status, delivery_channel, retry_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', 'telegram', 0, ?, ?)
                """,
                (
                    reminder_id,
                    task_id,
                    to_storage_text(scheduled_at or INITIAL_ATTEMPT_TIME - timedelta(minutes=1)),
                    EARLIER,
                    EARLIER,
                ),
            )

    async def _fail_delivery(self, conn, claimed_reminder, attempted_at: datetime):
        async def failing_sender(**_: object) -> None:
            raise RuntimeError("Telegram service unavailable")

        return await deliver_claimed_reminder(
            conn,
            claimed_reminder=claimed_reminder,
            allowed_telegram_user_ids=(1001,),
            send_message=failing_sender,
            clock=lambda: attempted_at,
        )


class ReminderRetryLifecycleTests(
    ReminderRetryLifecycleTestSupport,
    unittest.IsolatedAsyncioTestCase,
):
    async def test_failed_initial_attempt_retries_at_the_exact_boundary_and_succeeds(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a")
            self._insert_pending_reminder(conn, "reminder-a", "task-a")
            first_claim = claim_due_reminders(conn, now=INITIAL_ATTEMPT_TIME)
            initial_result = await self._fail_delivery(
                conn,
                first_claim[0],
                INITIAL_ATTEMPT_TIME,
            )

            before_boundary = claim_due_reminder_retries(
                conn,
                now=INITIAL_ATTEMPT_TIME + timedelta(minutes=1, seconds=-1),
            )
            first_attempts_after_failure = claim_due_reminders(
                conn,
                now=INITIAL_ATTEMPT_TIME + timedelta(days=1),
            )
            retry_claim = claim_due_reminder_retries(
                conn,
                now=INITIAL_ATTEMPT_TIME + timedelta(minutes=1),
            )
            sender = AsyncMock()
            retry_result = await deliver_claimed_reminder(
                conn,
                claimed_reminder=retry_claim[0],
                allowed_telegram_user_ids=(1001,),
                send_message=sender,
                clock=lambda: INITIAL_ATTEMPT_TIME + timedelta(minutes=1),
            )
            row = conn.execute(
                """
                SELECT status, retry_count, last_attempted_at, sent_at, failure_reason
                FROM reminders WHERE id = 'reminder-a'
                """
            ).fetchone()

        self.assertEqual(initial_result.outcome, TelegramReminderDeliveryOutcome.RETRY_SCHEDULED)
        self.assertEqual(before_boundary, ())
        self.assertEqual(first_attempts_after_failure, ())
        self.assertEqual(len(retry_claim), 1)
        self.assertEqual(retry_result.outcome, TelegramReminderDeliveryOutcome.SENT)
        sender.assert_awaited_once()
        self.assertEqual(
            tuple(row),
            (
                "sent",
                1,
                to_storage_text(INITIAL_ATTEMPT_TIME),
                to_storage_text(INITIAL_ATTEMPT_TIME + timedelta(minutes=1)),
                None,
            ),
        )

    async def test_initial_attempt_and_three_retries_end_in_terminal_failure(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a")
            self._insert_pending_reminder(conn, "reminder-a", "task-a")
            claimed_reminder = claim_due_reminders(conn, now=INITIAL_ATTEMPT_TIME)[0]
            delivery_times = (
                INITIAL_ATTEMPT_TIME,
                INITIAL_ATTEMPT_TIME + timedelta(minutes=1),
                INITIAL_ATTEMPT_TIME + timedelta(minutes=6),
                INITIAL_ATTEMPT_TIME + timedelta(minutes=21),
            )
            outcomes = []
            for index, attempted_at in enumerate(delivery_times):
                if index:
                    claimed_reminder = claim_due_reminder_retries(
                        conn,
                        now=attempted_at,
                    )[0]
                outcomes.append(
                    await self._fail_delivery(conn, claimed_reminder, attempted_at)
                )
            row = conn.execute(
                """
                SELECT status, retry_count, last_attempted_at, failure_reason
                FROM reminders WHERE id = 'reminder-a'
                """
            ).fetchone()
            later_claims = claim_due_reminder_retries(
                conn,
                now=INITIAL_ATTEMPT_TIME + timedelta(days=1),
            )

        self.assertEqual(
            [outcome.outcome for outcome in outcomes],
            [
                TelegramReminderDeliveryOutcome.RETRY_SCHEDULED,
                TelegramReminderDeliveryOutcome.RETRY_SCHEDULED,
                TelegramReminderDeliveryOutcome.RETRY_SCHEDULED,
                TelegramReminderDeliveryOutcome.TERMINAL_FAILURE,
            ],
        )
        self.assertEqual(
            tuple(row),
            (
                "failed",
                4,
                to_storage_text(INITIAL_ATTEMPT_TIME + timedelta(minutes=21)),
                "telegram_delivery_error",
            ),
        )
        self.assertEqual(later_claims, ())

    async def test_inactive_and_cancelled_retries_are_not_sent(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "cancelled-task")
            self._insert_task(conn, "completed-task", status="completed")
            self._insert_task(conn, "claimed-task")
            self._insert_pending_reminder(conn, "cancelled", "cancelled-task")
            self._insert_pending_reminder(conn, "completed", "completed-task")
            self._insert_pending_reminder(conn, "claimed", "claimed-task")
            retry_time = INITIAL_ATTEMPT_TIME - timedelta(minutes=1)
            conn.execute(
                """
                UPDATE reminders
                SET retry_count = 1, last_attempted_at = ?, failure_reason = 'telegram_timeout'
                WHERE id IN ('cancelled', 'completed', 'claimed')
                """,
                (to_storage_text(retry_time),),
            )
            conn.execute(
                """
                UPDATE reminders
                SET status = 'cancelled', cancelled_at = ?, updated_at = ?
                WHERE id = 'cancelled'
                """,
                (to_storage_text(INITIAL_ATTEMPT_TIME), to_storage_text(INITIAL_ATTEMPT_TIME)),
            )
            conn.commit()

            retry_claims = claim_due_reminder_retries(conn, now=INITIAL_ATTEMPT_TIME)
            conn.execute("UPDATE items SET status = 'completed' WHERE id = 'claimed-task'")
            conn.commit()
            sender = AsyncMock()
            result = await deliver_claimed_reminder(
                conn,
                claimed_reminder=retry_claims[0],
                allowed_telegram_user_ids=(1001,),
                send_message=sender,
                clock=lambda: INITIAL_ATTEMPT_TIME,
            )

        self.assertEqual([claim.reminder_id for claim in retry_claims], ["claimed"])
        self.assertEqual(result.outcome, TelegramReminderDeliveryOutcome.SKIPPED_INELIGIBLE)
        sender.assert_not_awaited()

    async def test_abandoned_retry_preserves_its_stage_and_invalidates_the_stale_lease(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a")
            self._insert_pending_reminder(conn, "reminder-a", "task-a")
            first_claim = claim_due_reminders(conn, now=INITIAL_ATTEMPT_TIME)[0]
            await self._fail_delivery(conn, first_claim, INITIAL_ATTEMPT_TIME)
            retry_time = INITIAL_ATTEMPT_TIME + timedelta(minutes=1)
            stale_retry_claim = claim_due_reminder_retries(conn, now=retry_time)[0]
            recovery_time = retry_time + timedelta(minutes=5)

            recovery_results = recover_abandoned_processing_reminders(
                conn,
                now=recovery_time,
            )
            with self.assertRaises(ReminderDeliveryStateError):
                record_claimed_reminder_sent(
                    conn,
                    claimed_reminder=stale_retry_claim,
                    sent_at=recovery_time,
                )
            reclaimed_retry = claim_due_reminder_retries(conn, now=recovery_time)

        self.assertEqual(
            [(result.reminder_id, result.action, result.retry_count) for result in recovery_results],
            [("reminder-a", AbandonedReminderRecoveryAction.REQUEUED, 1)],
        )
        self.assertEqual(len(reclaimed_retry), 1)
        self.assertEqual(reclaimed_retry[0].retry_count, 1)
        self.assertNotEqual(reclaimed_retry[0].claimed_at, stale_retry_claim.claimed_at)


if __name__ == "__main__":
    unittest.main()
