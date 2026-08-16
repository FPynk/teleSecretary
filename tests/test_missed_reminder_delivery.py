from __future__ import annotations

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
    RecoveredReminderRecord,
    ReminderDeliveryStateError,
    ReminderRecoveryDeliveryKind,
    claim_due_reminder_retries,
    record_claimed_reminder_summary_sent,
)
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.reminder_delivery import (
    MAX_TELEGRAM_MESSAGE_LENGTH,
    MissedReminderDeliveryMode,
    TelegramReminderDeliveryOutcome,
    build_missed_reminder_delivery_message,
    build_missed_reminder_summary_message,
    deliver_missed_reminders,
)
from tele_secretary.time_utils import to_storage_text


NOW = datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc)
NOW_TEXT = to_storage_text(NOW)
CLAIMED_AT = to_storage_text(NOW - timedelta(minutes=1))
EARLIER = to_storage_text(NOW - timedelta(days=1))


class MissedReminderDeliveryTestSupport:
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with open_test_database(Path(temp_dir) / "secretary.sqlite3") as conn:
                apply_migrations(conn)
                self._insert_user(conn, "user-a", 1001)
                self._insert_user(conn, "user-b", 2002)
                yield conn

    def _insert_user(self, conn, user_id: str, telegram_user_id: int) -> None:
        with conn:
            conn.execute(
                "INSERT INTO users (id, telegram_user_id, timezone) VALUES (?, ?, 'America/Chicago')",
                (user_id, telegram_user_id),
            )

    def _insert_task(self, conn, task_id: str, user_id: str, task_ref: str, title: str, *, status: str = "active") -> None:
        with conn:
            conn.execute(
                """
                INSERT INTO items (
                    id, user_id, item_type, pub_ref, title, status, source,
                    parse_status, created_at, updated_at
                ) VALUES (?, ?, 'task', ?, ?, ?, 'manual_entry', 'not_applicable', ?, ?)
                """,
                (task_id, user_id, task_ref, title, status, EARLIER, EARLIER),
            )
            conn.execute("INSERT INTO task_items (item_id) VALUES (?)", (task_id,))

    def _insert_reminder(self, conn, reminder_id: str, task_id: str, scheduled_at: str) -> None:
        with conn:
            conn.execute(
                """
                INSERT INTO reminders (
                    id, item_id, scheduled_at, status, delivery_channel, retry_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'processing', 'telegram', 0, ?, ?)
                """,
                (reminder_id, task_id, scheduled_at, EARLIER, CLAIMED_AT),
            )

    def _create_claims(self, conn, *, owner: str, count: int, start_index: int = 1) -> tuple[ClaimedReminderRecord, ...]:
        telegram_user_id = 1001 if owner == "user-a" else 2002
        claims: list[ClaimedReminderRecord] = []
        for offset in range(count):
            index = start_index + offset
            task_id = f"{owner}-task-{index}"
            reminder_id = f"{owner}-reminder-{index}"
            scheduled_at = to_storage_text(NOW - timedelta(hours=2, minutes=index))
            self._insert_task(conn, task_id, owner, f"T{index}", f"Task {index}")
            self._insert_reminder(conn, reminder_id, task_id, scheduled_at)
            claims.append(
                ClaimedReminderRecord(
                    reminder_id=reminder_id,
                    task_id=task_id,
                    user_id=owner,
                    telegram_user_id=telegram_user_id,
                    user_timezone="America/Chicago",
                    task_ref=f"T{index}",
                    task_title=f"Task {index}",
                    scheduled_at=scheduled_at,
                    status="processing",
                    delivery_channel="telegram",
                    retry_count=0,
                    claimed_at=CLAIMED_AT,
                )
            )
        return tuple(claims)

    def _missed(self, claims: tuple[ClaimedReminderRecord, ...]) -> tuple[RecoveredReminderRecord, ...]:
        return tuple(
            RecoveredReminderRecord(
                reminder=claim,
                delivery_kind=ReminderRecoveryDeliveryKind.MISSED,
            )
            for claim in claims
        )


class MissedReminderMessageTests(MissedReminderDeliveryTestSupport, unittest.TestCase):
    def test_individual_and_summary_messages_are_plain_text_and_bounded(self) -> None:
        claim = ClaimedReminderRecord(
            reminder_id="reminder",
            task_id="task",
            user_id="user-a",
            telegram_user_id=1001,
            user_timezone="America/Chicago",
            task_ref="T1",
            task_title="x" * MAX_TELEGRAM_MESSAGE_LENGTH,
            scheduled_at=EARLIER,
            status="processing",
            delivery_channel="telegram",
            retry_count=0,
            claimed_at=CLAIMED_AT,
        )
        individual_message = build_missed_reminder_delivery_message(claim)
        summary_message = build_missed_reminder_summary_message(
            tuple(
                ClaimedReminderRecord(
                    **{**claim.__dict__, "reminder_id": f"reminder-{index}", "task_ref": f"T{index}"}
                )
                for index in range(1, 5)
            )
        )

        self.assertEqual(len(individual_message), MAX_TELEGRAM_MESSAGE_LENGTH)
        self.assertTrue(individual_message.endswith("…\nTask: T1"))
        self.assertLessEqual(len(summary_message), MAX_TELEGRAM_MESSAGE_LENGTH)
        self.assertTrue(all(f"T{index}" in summary_message for index in range(1, 5)))


class MissedReminderDeliveryTests(MissedReminderDeliveryTestSupport, unittest.IsolatedAsyncioTestCase):
    async def test_zero_missed_reminders_do_not_call_telegram(self) -> None:
        with self.open_database() as conn:
            normal_claim = self._create_claims(conn, owner="user-a", count=1)[0]
            send_message = AsyncMock()

            results = await deliver_missed_reminders(
                conn,
                recovered_reminders=(
                    RecoveredReminderRecord(
                        reminder=normal_claim,
                        delivery_kind=ReminderRecoveryDeliveryKind.NORMAL,
                    ),
                ),
                allowed_telegram_user_ids=(1001,),
                send_message=send_message,
                clock=lambda: NOW,
            )

        self.assertEqual(results, ())
        send_message.assert_not_awaited()

    async def test_one_through_three_ready_misses_are_delivered_individually_in_schedule_order(self) -> None:
        for count in (1, 2, 3):
            with self.subTest(count=count), self.open_database() as conn:
                claims = self._create_claims(conn, owner="user-a", count=count)
                sent_messages: list[dict[str, object]] = []

                async def send_message(**kwargs: object) -> None:
                    self.assertFalse(conn.in_transaction)
                    sent_messages.append(kwargs)

                results = await deliver_missed_reminders(
                    conn,
                    recovered_reminders=self._missed(tuple(reversed(claims))),
                    allowed_telegram_user_ids=(1001,),
                    send_message=send_message,
                    clock=lambda: NOW,
                )
                statuses = conn.execute("SELECT status FROM reminders ORDER BY id").fetchall()

            self.assertEqual(len(sent_messages), count)
            self.assertEqual(
                [message["text"] for message in sent_messages],
                [f"Missed reminder from earlier: Task {index}\nTask: T{index}" for index in range(count, 0, -1)],
            )
            self.assertTrue(all(result.mode is MissedReminderDeliveryMode.INDIVIDUAL for result in results))
            self.assertTrue(all(result.outcome is TelegramReminderDeliveryOutcome.SENT for result in results))
            self.assertEqual({row[0] for row in statuses}, {"sent"})

    async def test_four_ready_misses_send_one_summary_and_mark_every_reminder_sent(self) -> None:
        with self.open_database() as conn:
            claims = self._create_claims(conn, owner="user-a", count=4)
            sent_messages: list[dict[str, object]] = []

            async def send_message(**kwargs: object) -> None:
                self.assertFalse(conn.in_transaction)
                sent_messages.append(kwargs)

            results = await deliver_missed_reminders(
                conn,
                recovered_reminders=self._missed(tuple(reversed(claims))),
                allowed_telegram_user_ids=(1001,),
                send_message=send_message,
                clock=lambda: NOW,
            )
            rows = conn.execute(
                "SELECT id, status, sent_at, updated_at FROM reminders ORDER BY id"
            ).fetchall()

        self.assertEqual(len(sent_messages), 1)
        self.assertEqual(sent_messages[0]["chat_id"], 1001)
        self.assertEqual(
            sent_messages[0]["text"],
            "You had 4 reminders while I was offline:\n- T4: Task 4\n- T3: Task 3\n- T2: Task 2\n- T1: Task 1",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].mode, MissedReminderDeliveryMode.SUMMARY)
        self.assertEqual(results[0].outcome, TelegramReminderDeliveryOutcome.SENT)
        self.assertEqual({tuple(row[1:]) for row in rows}, {("sent", NOW_TEXT, NOW_TEXT)})

    async def test_preflight_reduces_four_candidates_to_three_individual_deliveries(self) -> None:
        with self.open_database() as conn:
            claims = self._create_claims(conn, owner="user-a", count=4)
            conn.execute("UPDATE items SET status = 'completed' WHERE id = ?", (claims[0].task_id,))
            conn.commit()
            send_message = AsyncMock()

            results = await deliver_missed_reminders(
                conn,
                recovered_reminders=self._missed(claims),
                allowed_telegram_user_ids=(1001,),
                send_message=send_message,
                clock=lambda: NOW,
            )

        self.assertEqual(send_message.await_count, 3)
        self.assertEqual(
            [result.mode for result in results],
            [
                MissedReminderDeliveryMode.INDIVIDUAL,
                MissedReminderDeliveryMode.INDIVIDUAL,
                MissedReminderDeliveryMode.INDIVIDUAL,
                MissedReminderDeliveryMode.INDIVIDUAL,
            ],
        )
        self.assertIn(TelegramReminderDeliveryOutcome.SKIPPED_INELIGIBLE, [result.outcome for result in results])

    async def test_mixed_owners_receive_separate_summaries(self) -> None:
        with self.open_database() as conn:
            owner_a = self._create_claims(conn, owner="user-a", count=4)
            owner_b = self._create_claims(conn, owner="user-b", count=4, start_index=10)
            send_message = AsyncMock()

            results = await deliver_missed_reminders(
                conn,
                recovered_reminders=self._missed((*owner_a, *owner_b)),
                allowed_telegram_user_ids=(1001, 2002),
                send_message=send_message,
                clock=lambda: NOW,
            )

        self.assertEqual(send_message.await_count, 2)
        self.assertEqual(
            [call.kwargs["chat_id"] for call in send_message.await_args_list],
            [1001, 2002],
        )
        self.assertEqual([result.mode for result in results], [MissedReminderDeliveryMode.SUMMARY] * 2)
        self.assertTrue(all(result.outcome is TelegramReminderDeliveryOutcome.SENT for result in results))

    async def test_summary_failure_makes_all_reminders_retryable_after_one_minute(self) -> None:
        with self.open_database() as conn:
            claims = self._create_claims(conn, owner="user-a", count=4)

            async def send_message(**_: object) -> None:
                raise RuntimeError("https://api.telegram.org/bot123:secret/sendMessage")

            results = await deliver_missed_reminders(
                conn,
                recovered_reminders=self._missed(claims),
                allowed_telegram_user_ids=(1001,),
                send_message=send_message,
                clock=lambda: NOW,
            )
            rows = conn.execute(
                "SELECT status, retry_count, last_attempted_at, failure_reason FROM reminders"
            ).fetchall()
            retries = claim_due_reminder_retries(conn, now=NOW + timedelta(minutes=1))

        self.assertEqual(results[0].outcome, TelegramReminderDeliveryOutcome.RETRY_SCHEDULED)
        self.assertEqual({tuple(row) for row in rows}, {("pending", 1, NOW_TEXT, "telegram_delivery_error")})
        self.assertEqual({retry.reminder_id for retry in retries}, {claim.reminder_id for claim in claims})

    async def test_summary_result_persistence_failure_does_not_send_individual_messages(self) -> None:
        with self.open_database() as conn:
            claims = self._create_claims(conn, owner="user-a", count=4)
            send_message = AsyncMock()
            with patch(
                "tele_secretary.telegram.reminder_delivery.record_claimed_reminder_summary_sent",
                side_effect=sqlite3.OperationalError("forced persistence failure"),
            ):
                results = await deliver_missed_reminders(
                    conn,
                    recovered_reminders=self._missed(claims),
                    allowed_telegram_user_ids=(1001,),
                    send_message=send_message,
                    clock=lambda: NOW,
                )
            statuses = conn.execute("SELECT status FROM reminders").fetchall()

        self.assertEqual(send_message.await_count, 1)
        self.assertEqual(results[0].outcome, TelegramReminderDeliveryOutcome.RESULT_PERSISTENCE_ERROR)
        self.assertEqual({row[0] for row in statuses}, {"processing"})


class MissedReminderSummaryPersistenceTests(MissedReminderDeliveryTestSupport, unittest.TestCase):
    def test_summary_success_rolls_back_every_row_when_one_claim_is_stale(self) -> None:
        with self.open_database() as conn:
            claims = self._create_claims(conn, owner="user-a", count=4)
            conn.execute(
                "UPDATE reminders SET updated_at = ? WHERE id = ?",
                (EARLIER, claims[-1].reminder_id),
            )
            conn.commit()

            with self.assertRaises(ReminderDeliveryStateError):
                record_claimed_reminder_summary_sent(
                    conn,
                    claimed_reminders=claims,
                    sent_at=NOW,
                )
            rows = conn.execute("SELECT status, sent_at FROM reminders").fetchall()

        self.assertEqual({tuple(row) for row in rows}, {("processing", None)})


if __name__ == "__main__":
    unittest.main()
