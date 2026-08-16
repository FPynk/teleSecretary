from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import _path  # noqa: F401
from tele_secretary.app.reminders import (
    ClaimedReminderRecord,
    RecoveredReminderRecord,
    ReminderRecoveryDeliveryKind,
)
from tele_secretary.scheduler import runner
from tele_secretary.telegram.reminder_delivery import (
    MissedReminderDeliveryMode,
    MissedReminderDeliveryResult,
    TelegramReminderDeliveryOutcome,
    TelegramReminderDeliveryResult,
)


CYCLE_TIME = datetime(2026, 8, 16, 15, 0, 0, 456789, tzinfo=timezone(timedelta(hours=-5)))
NORMALIZED_CYCLE_TIME = datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc)


def build_claim(reminder_id: str) -> ClaimedReminderRecord:
    return ClaimedReminderRecord(
        reminder_id=reminder_id,
        task_id=f"task-{reminder_id}",
        user_id="user-a",
        telegram_user_id=1001,
        user_timezone="America/Chicago",
        task_ref=f"T-{reminder_id}",
        task_title=f"Task {reminder_id}",
        scheduled_at="2026-08-16T18:00:00+00:00",
        status="processing",
        delivery_channel="telegram",
        retry_count=0,
        claimed_at="2026-08-16T20:00:00+00:00",
    )


class ReminderProcessingCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cycle_uses_required_order_and_one_normalized_timestamp(self) -> None:
        connection = SimpleNamespace(in_transaction=False, close=Mock())
        normal_claim = build_claim("normal")
        missed_claim = build_claim("missed")
        retry_reminder = build_claim("retry")
        events: list[str] = []
        received_times: list[datetime] = []

        def recover(_, *, now: datetime):
            events.append("recover")
            received_times.append(now)
            return ()

        def first_claim(_, *, now: datetime):
            events.append("first_claim")
            received_times.append(now)
            return (normal_claim, missed_claim)

        def classify(_, *, claimed_reminders, now: datetime):
            events.append("classify")
            received_times.append(now)
            self.assertEqual(claimed_reminders, (normal_claim, missed_claim))
            return (
                RecoveredReminderRecord(normal_claim, ReminderRecoveryDeliveryKind.NORMAL),
                RecoveredReminderRecord(missed_claim, ReminderRecoveryDeliveryKind.MISSED),
            )

        async def deliver_individual(_, *, claimed_reminder, clock, **__):
            self.assertFalse(connection.in_transaction)
            received_times.append(clock())
            events.append(f"deliver_{claimed_reminder.reminder_id}")
            return TelegramReminderDeliveryResult(
                reminder_id=claimed_reminder.reminder_id,
                outcome=TelegramReminderDeliveryOutcome.SENT,
            )

        async def deliver_missed(_, *, recovered_reminders, clock, **__):
            self.assertFalse(connection.in_transaction)
            received_times.append(clock())
            events.append("deliver_missed")
            self.assertEqual(recovered_reminders[0].reminder.reminder_id, "missed")
            return (
                MissedReminderDeliveryResult(
                    reminder_ids=("missed",),
                    mode=MissedReminderDeliveryMode.INDIVIDUAL,
                    outcome=TelegramReminderDeliveryOutcome.RETRY_SCHEDULED,
                ),
            )

        def claim_retries(_, *, now: datetime):
            events.append("retry_claim")
            received_times.append(now)
            return (retry_reminder,)

        with (
            patch.object(runner, "connect", return_value=connection),
            patch.object(runner, "recover_abandoned_processing_reminders", side_effect=recover),
            patch.object(runner, "claim_due_reminders", side_effect=first_claim),
            patch.object(runner, "apply_reminder_downtime_recovery", side_effect=classify),
            patch.object(runner, "deliver_claimed_reminder", side_effect=deliver_individual),
            patch.object(runner, "deliver_missed_reminders", side_effect=deliver_missed),
            patch.object(runner, "claim_due_reminder_retries", side_effect=claim_retries),
        ):
            await runner.process_reminder_cycle(
                db_path=Path("secretary.sqlite3"),
                allowed_telegram_user_ids=(1001,),
                send_message=AsyncMock(),
                cycle_time=CYCLE_TIME,
            )

        self.assertEqual(
            events,
            ["recover", "first_claim", "classify", "deliver_normal", "deliver_missed", "retry_claim", "deliver_retry"],
        )
        self.assertEqual(received_times, [NORMALIZED_CYCLE_TIME] * 7)
        connection.close.assert_called_once()

    async def test_unexpected_individual_delivery_error_does_not_stop_later_delivery(self) -> None:
        connection = SimpleNamespace(in_transaction=False, close=Mock())
        first_claim = build_claim("first")
        later_claim = build_claim("later")
        deliveries: list[str] = []

        async def deliver_individual(_, *, claimed_reminder, **__):
            deliveries.append(claimed_reminder.reminder_id)
            if claimed_reminder.reminder_id == "first":
                raise RuntimeError("unexpected")
            return TelegramReminderDeliveryResult(
                reminder_id=claimed_reminder.reminder_id,
                outcome=TelegramReminderDeliveryOutcome.SENT,
            )

        with (
            patch.object(runner, "connect", return_value=connection),
            patch.object(runner, "recover_abandoned_processing_reminders", return_value=()),
            patch.object(runner, "claim_due_reminders", return_value=(first_claim, later_claim)),
            patch.object(
                runner,
                "apply_reminder_downtime_recovery",
                return_value=(
                    RecoveredReminderRecord(first_claim, ReminderRecoveryDeliveryKind.NORMAL),
                    RecoveredReminderRecord(later_claim, ReminderRecoveryDeliveryKind.NORMAL),
                ),
            ),
            patch.object(runner, "deliver_claimed_reminder", side_effect=deliver_individual),
            patch.object(runner, "deliver_missed_reminders", return_value=()),
            patch.object(runner, "claim_due_reminder_retries", return_value=()),
        ):
            await runner.process_reminder_cycle(
                db_path=Path("secretary.sqlite3"),
                allowed_telegram_user_ids=(1001,),
                send_message=AsyncMock(),
                cycle_time=CYCLE_TIME,
            )

        self.assertEqual(deliveries, ["first", "later"])
        connection.close.assert_called_once()

    async def test_cancellation_closes_the_cycle_connection(self) -> None:
        connection = SimpleNamespace(in_transaction=False, close=Mock())
        normal_claim = build_claim("normal")

        async def cancelled_delivery(*_, **__):
            raise asyncio.CancelledError()

        with (
            patch.object(runner, "connect", return_value=connection),
            patch.object(runner, "recover_abandoned_processing_reminders", return_value=()),
            patch.object(runner, "claim_due_reminders", return_value=(normal_claim,)),
            patch.object(
                runner,
                "apply_reminder_downtime_recovery",
                return_value=(
                    RecoveredReminderRecord(normal_claim, ReminderRecoveryDeliveryKind.NORMAL),
                ),
            ),
            patch.object(runner, "deliver_claimed_reminder", side_effect=cancelled_delivery),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await runner.process_reminder_cycle(
                    db_path=Path("secretary.sqlite3"),
                    allowed_telegram_user_ids=(1001,),
                    send_message=AsyncMock(),
                    cycle_time=CYCLE_TIME,
                )

        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
