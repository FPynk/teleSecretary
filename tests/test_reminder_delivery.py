from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.reminders import (
    ClaimedReminderRecord,
    ReminderDeliveryPreparationOutcome,
    prepare_claimed_reminder_delivery,
    record_claimed_reminder_failure,
    record_claimed_reminder_sent,
)
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.reminder_delivery import (
    MAX_TELEGRAM_MESSAGE_LENGTH,
    TelegramReminderDeliveryOutcome,
    build_reminder_delivery_message,
    deliver_claimed_reminder,
)
from tele_secretary.time_utils import to_storage_text


NOW = datetime(2026, 8, 13, 15, 0, tzinfo=timezone.utc)
NOW_TEXT = to_storage_text(NOW)
CLAIMED_AT = to_storage_text(NOW - timedelta(minutes=1))
EARLIER = to_storage_text(NOW - timedelta(days=1))


class ReminderDeliveryTestSupport:
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with open_test_database(Path(temp_dir) / "secretary.sqlite3") as conn:
                apply_migrations(conn)
                self._insert_user(conn, "user-a", 1001)
                self._insert_user(conn, "user-b", 2002)
                yield conn

    def _insert_user(self, conn, user_id: str, telegram_user_id: int | None) -> None:
        with conn:
            conn.execute(
                "INSERT INTO users (id, telegram_user_id, timezone) VALUES (?, ?, 'America/Chicago')",
                (user_id, telegram_user_id),
            )

    def _insert_task(
        self,
        conn,
        task_id: str,
        user_id: str,
        task_ref: str,
        title: str,
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
                ) VALUES (?, ?, 'task', ?, ?, ?, 'manual_entry', 'not_applicable', ?, ?, ?)
                """,
                (task_id, user_id, task_ref, title, status, EARLIER, EARLIER, deleted_at),
            )
            conn.execute("INSERT INTO task_items (item_id) VALUES (?)", (task_id,))

    def _insert_reminder(
        self,
        conn,
        reminder_id: str,
        task_id: str,
        *,
        status: str = "processing",
        retry_count: int = 0,
        last_attempted_at: str | None = None,
        sent_at: str | None = None,
        failure_reason: str | None = None,
        cancelled_at: str | None = None,
        expired_at: str | None = None,
        updated_at: str = CLAIMED_AT,
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
                    EARLIER,
                    status,
                    retry_count,
                    last_attempted_at,
                    sent_at,
                    failure_reason,
                    cancelled_at,
                    expired_at,
                    EARLIER,
                    updated_at,
                ),
            )

    def _claimed_reminder(
        self,
        reminder_id: str,
        task_id: str,
        user_id: str,
        telegram_user_id: int | None,
        *,
        retry_count: int = 0,
        claimed_at: str = CLAIMED_AT,
    ) -> ClaimedReminderRecord:
        return ClaimedReminderRecord(
            reminder_id=reminder_id,
            task_id=task_id,
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            user_timezone="America/Chicago",
            task_ref="T1" if task_id == "task-a" else "T2",
            task_title="Buy milk" if task_id == "task-a" else "Pay bill",
            scheduled_at=EARLIER,
            status="processing",
            delivery_channel="telegram",
            retry_count=retry_count,
            claimed_at=claimed_at,
        )


class ReminderDeliveryServiceTests(ReminderDeliveryTestSupport, unittest.TestCase):
    def test_record_success_keeps_retry_history_and_clears_failure_reason(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a", "T1", "Buy milk")
            self._insert_reminder(
                conn,
                "reminder-a",
                "task-a",
                retry_count=2,
                last_attempted_at=EARLIER,
                failure_reason="telegram_timeout",
            )
            claim = self._claimed_reminder("reminder-a", "task-a", "user-a", 1001, retry_count=2)

            recorded = record_claimed_reminder_sent(conn, claimed_reminder=claim, sent_at=NOW)

        self.assertEqual(recorded.status, "sent")
        self.assertEqual(recorded.retry_count, 2)
        self.assertEqual(recorded.last_attempted_at, EARLIER)
        self.assertEqual(recorded.sent_at, NOW_TEXT)
        self.assertIsNone(recorded.failure_reason)
        self.assertEqual(recorded.updated_at, NOW_TEXT)

    def test_record_failure_moves_attempts_one_through_three_to_pending_and_four_to_failed(self) -> None:
        expected_results = ((0, "pending", 1), (1, "pending", 2), (2, "pending", 3), (3, "failed", 4))
        for retry_count, expected_status, expected_retry_count in expected_results:
            with self.subTest(retry_count=retry_count), self.open_database() as conn:
                self._insert_task(conn, "task-a", "user-a", "T1", "Buy milk")
                self._insert_reminder(
                    conn,
                    "reminder-a",
                    "task-a",
                    retry_count=retry_count,
                    last_attempted_at=EARLIER if retry_count else None,
                    failure_reason="telegram_timeout" if retry_count else None,
                )
                claim = self._claimed_reminder(
                    "reminder-a",
                    "task-a",
                    "user-a",
                    1001,
                    retry_count=retry_count,
                )

                recorded = record_claimed_reminder_failure(
                    conn,
                    claimed_reminder=claim,
                    failure_reason="telegram_network_error",
                    attempted_at=NOW,
                )

            self.assertEqual(recorded.status, expected_status)
            self.assertEqual(recorded.retry_count, expected_retry_count)
            self.assertEqual(recorded.last_attempted_at, NOW_TEXT)
            self.assertEqual(recorded.updated_at, NOW_TEXT)
            self.assertEqual(recorded.failure_reason, "telegram_network_error")

    def test_preflight_cancels_only_a_matching_claim_with_a_disallowed_recipient(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-b", "user-b", "T2", "Pay bill")
            self._insert_reminder(conn, "reminder-b", "task-b")
            claim = self._claimed_reminder("reminder-b", "task-b", "user-b", 2002)

            preparation = prepare_claimed_reminder_delivery(
                conn,
                claimed_reminder=claim,
                allowed_telegram_user_ids=(1001,),
                evaluated_at=NOW,
            )
            row = conn.execute(
                "SELECT status, cancelled_at, updated_at FROM reminders WHERE id = 'reminder-b'"
            ).fetchone()

        self.assertEqual(
            preparation.outcome,
            ReminderDeliveryPreparationOutcome.CANCELLED_DISALLOWED_RECIPIENT,
        )
        self.assertEqual(tuple(row), ("cancelled", NOW_TEXT, NOW_TEXT))


class ReminderDeliveryAdapterTests(ReminderDeliveryTestSupport, unittest.IsolatedAsyncioTestCase):
    def test_build_message_uses_plain_text_shape_and_truncates_only_the_title(self) -> None:
        claim = self._claimed_reminder("reminder-a", "task-a", "user-a", 1001)
        self.assertEqual(build_reminder_delivery_message(claim), "Reminder: Buy milk\nTask: T1")

        long_claim = ClaimedReminderRecord(
            **{**claim.__dict__, "task_title": "x" * MAX_TELEGRAM_MESSAGE_LENGTH}
        )
        message = build_reminder_delivery_message(long_claim)

        self.assertEqual(len(message), MAX_TELEGRAM_MESSAGE_LENGTH)
        self.assertTrue(message.endswith("…\nTask: T1"))

    async def test_delivery_sends_to_the_claimed_owner_without_a_database_transaction(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-b", "user-b", "T2", "Pay bill")
            self._insert_reminder(conn, "reminder-b", "task-b")
            claim = self._claimed_reminder("reminder-b", "task-b", "user-b", 2002)
            calls: list[dict[str, object]] = []

            async def send_message(**kwargs: object) -> None:
                self.assertFalse(conn.in_transaction)
                calls.append(kwargs)

            result = await deliver_claimed_reminder(
                conn,
                claimed_reminder=claim,
                allowed_telegram_user_ids=(1001, 2002),
                send_message=send_message,
                clock=lambda: NOW,
            )
            row = conn.execute(
                "SELECT status, sent_at, failure_reason FROM reminders WHERE id = 'reminder-b'"
            ).fetchone()

        self.assertEqual(result.outcome, TelegramReminderDeliveryOutcome.SENT)
        self.assertEqual(result.retry_count, 0)
        self.assertEqual(calls[0]["chat_id"], 2002)
        self.assertEqual(calls[0]["text"], "Reminder: Pay bill\nTask: T2")
        self.assertIsNone(calls[0]["parse_mode"])
        self.assertEqual(calls[0]["connect_timeout"], 30)
        self.assertEqual(tuple(row), ("sent", NOW_TEXT, None))

    async def test_delivery_cancels_disallowed_or_null_recipients_without_calling_telegram(self) -> None:
        for telegram_user_id in (2002, None):
            with self.subTest(telegram_user_id=telegram_user_id), self.open_database() as conn:
                self._insert_task(conn, "task-b", "user-b", "T2", "Pay bill")
                if telegram_user_id is None:
                    conn.execute("UPDATE users SET telegram_user_id = NULL WHERE id = 'user-b'")
                    conn.commit()
                self._insert_reminder(conn, "reminder-b", "task-b")
                claim = self._claimed_reminder("reminder-b", "task-b", "user-b", telegram_user_id)
                send_message = AsyncMock()

                result = await deliver_claimed_reminder(
                    conn,
                    claimed_reminder=claim,
                    allowed_telegram_user_ids=(1001,),
                    send_message=send_message,
                    clock=lambda: NOW,
                )
                row = conn.execute(
                    "SELECT status, cancelled_at FROM reminders WHERE id = 'reminder-b'"
                ).fetchone()

            self.assertEqual(
                result.outcome,
                TelegramReminderDeliveryOutcome.CANCELLED_DISALLOWED_RECIPIENT,
            )
            send_message.assert_not_awaited()
            self.assertEqual(tuple(row), ("cancelled", NOW_TEXT))

    async def test_delivery_skips_stale_terminal_and_inactive_claims_without_rewriting_rows(self) -> None:
        cases = (
            ("pending", None, "active", CLAIMED_AT, CLAIMED_AT),
            ("sent", EARLIER, "active", CLAIMED_AT, CLAIMED_AT),
            ("processing", None, "active", EARLIER, CLAIMED_AT),
            ("processing", None, "completed", CLAIMED_AT, CLAIMED_AT),
        )
        for status, sent_at, task_status, updated_at, claimed_at in cases:
            with self.subTest(status=status, task_status=task_status), self.open_database() as conn:
                self._insert_task(conn, "task-a", "user-a", "T1", "Buy milk", status=task_status)
                self._insert_reminder(
                    conn,
                    "reminder-a",
                    "task-a",
                    status=status,
                    sent_at=sent_at,
                    updated_at=updated_at,
                )
                claim = self._claimed_reminder(
                    "reminder-a",
                    "task-a",
                    "user-a",
                    1001,
                    claimed_at=claimed_at,
                )
                send_message = AsyncMock()

                result = await deliver_claimed_reminder(
                    conn,
                    claimed_reminder=claim,
                    allowed_telegram_user_ids=(1001,),
                    send_message=send_message,
                    clock=lambda: NOW,
                )
                row = conn.execute(
                    "SELECT status, updated_at FROM reminders WHERE id = 'reminder-a'"
                ).fetchone()

            self.assertEqual(result.outcome, TelegramReminderDeliveryOutcome.SKIPPED_INELIGIBLE)
            send_message.assert_not_awaited()
            self.assertEqual(tuple(row), (status, updated_at))

    async def test_delivery_skips_a_claim_when_the_persisted_owner_changed(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a", "T1", "Buy milk")
            self._insert_reminder(conn, "reminder-a", "task-a")
            changed_owner_claim = self._claimed_reminder(
                "reminder-a",
                "task-a",
                "user-b",
                2002,
            )
            send_message = AsyncMock()

            result = await deliver_claimed_reminder(
                conn,
                claimed_reminder=changed_owner_claim,
                allowed_telegram_user_ids=(1001, 2002),
                send_message=send_message,
                clock=lambda: NOW,
            )
            row = conn.execute(
                "SELECT status, updated_at FROM reminders WHERE id = 'reminder-a'"
            ).fetchone()

        self.assertEqual(result.outcome, TelegramReminderDeliveryOutcome.SKIPPED_INELIGIBLE)
        send_message.assert_not_awaited()
        self.assertEqual(tuple(row), ("processing", CLAIMED_AT))

    async def test_delivery_records_sanitized_failures_and_terminal_exhaustion(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a", "T1", "Buy milk")
            self._insert_reminder(
                conn,
                "reminder-a",
                "task-a",
                retry_count=3,
                last_attempted_at=EARLIER,
                failure_reason="telegram_timeout",
            )
            claim = self._claimed_reminder("reminder-a", "task-a", "user-a", 1001, retry_count=3)

            async def send_message(**_: object) -> None:
                raise RuntimeError("https://api.telegram.org/bot123:secret/sendMessage")

            with self.assertLogs("tele_secretary.telegram.reminder_delivery", level="INFO") as logs:
                result = await deliver_claimed_reminder(
                    conn,
                    claimed_reminder=claim,
                    allowed_telegram_user_ids=(1001,),
                    send_message=send_message,
                    clock=lambda: NOW,
                )
            row = conn.execute(
                "SELECT status, retry_count, failure_reason FROM reminders WHERE id = 'reminder-a'"
            ).fetchone()

        self.assertEqual(result.outcome, TelegramReminderDeliveryOutcome.TERMINAL_FAILURE)
        self.assertEqual(result.retry_count, 4)
        self.assertEqual(tuple(row), ("failed", 4, "telegram_delivery_error"))
        self.assertNotIn("bot123:secret", "\n".join(logs.output))

    async def test_delivery_returns_persistence_outcomes_without_another_send(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a", "T1", "Buy milk")
            self._insert_reminder(conn, "reminder-a", "task-a")
            claim = self._claimed_reminder("reminder-a", "task-a", "user-a", 1001)
            send_message = AsyncMock()

            with patch(
                "tele_secretary.telegram.reminder_delivery.record_claimed_reminder_sent",
                side_effect=sqlite3.OperationalError("forced persistence failure"),
            ):
                result = await deliver_claimed_reminder(
                    conn,
                    claimed_reminder=claim,
                    allowed_telegram_user_ids=(1001,),
                    send_message=send_message,
                    clock=lambda: NOW,
                )
            status = conn.execute("SELECT status FROM reminders WHERE id = 'reminder-a'").fetchone()[0]

        self.assertEqual(result.outcome, TelegramReminderDeliveryOutcome.RESULT_PERSISTENCE_ERROR)
        send_message.assert_awaited_once()
        self.assertEqual(status, "processing")

    async def test_delivery_returns_a_preflight_error_and_propagates_cancellation(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a", "T1", "Buy milk")
            self._insert_reminder(conn, "reminder-a", "task-a")
            claim = self._claimed_reminder("reminder-a", "task-a", "user-a", 1001)
            send_message = AsyncMock()

            with patch(
                "tele_secretary.telegram.reminder_delivery.prepare_claimed_reminder_delivery",
                side_effect=sqlite3.OperationalError("forced preflight failure"),
            ):
                result = await deliver_claimed_reminder(
                    conn,
                    claimed_reminder=claim,
                    allowed_telegram_user_ids=(1001,),
                    send_message=send_message,
                    clock=lambda: NOW,
                )

            self.assertEqual(
                result.outcome,
                TelegramReminderDeliveryOutcome.PREFLIGHT_PERSISTENCE_ERROR,
            )
            send_message.assert_not_awaited()

            async def cancelled_send_message(**_: object) -> None:
                raise asyncio.CancelledError()

            with self.assertRaises(asyncio.CancelledError):
                await deliver_claimed_reminder(
                    conn,
                    claimed_reminder=claim,
                    allowed_telegram_user_ids=(1001,),
                    send_message=cancelled_send_message,
                    clock=lambda: NOW,
                )
            status = conn.execute("SELECT status FROM reminders WHERE id = 'reminder-a'").fetchone()[0]

        self.assertEqual(status, "processing")


if __name__ == "__main__":
    unittest.main()
