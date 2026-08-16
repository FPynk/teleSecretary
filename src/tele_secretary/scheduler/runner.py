"""Bounded reminder-processing loop and its production cycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from tele_secretary.app.reminders import (
    ReminderRecoveryDeliveryKind,
    apply_reminder_downtime_recovery,
    claim_due_reminder_retries,
    claim_due_reminders,
    recover_abandoned_processing_reminders,
)
from tele_secretary.persistence.connection import connect
from tele_secretary.telegram.reminder_delivery import (
    MissedReminderDeliveryResult,
    TelegramReminderDeliveryOutcome,
    TelegramReminderDeliveryResult,
    deliver_claimed_reminder,
    deliver_missed_reminders,
)
from tele_secretary.time_utils import ensure_utc, utc_now


LOGGER = logging.getLogger(__name__)
REMINDER_POLL_INTERVAL_SECONDS = 30


class Scheduler:
    """Run one reminder cycle at a time until the Telegram app stops."""

    def __init__(
        self,
        *,
        run_cycle: Callable[[datetime], Awaitable[None]],
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._run_cycle = run_cycle
        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the non-overlapping loop unless it is already running."""

        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(),
            name="tele-secretary-reminder-scheduler",
        )

    async def stop(self) -> None:
        """Cancel and await the active loop, if one exists."""

        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if self._task is task:
                self._task = None

    async def _run(self) -> None:
        while True:
            try:
                cycle_time = ensure_utc(self._clock()).replace(microsecond=0)
                await self._run_cycle(cycle_time)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.error("Reminder processing cycle failed.")
            await self._sleep(REMINDER_POLL_INTERVAL_SECONDS)


async def process_reminder_cycle(
    *,
    db_path: Path,
    allowed_telegram_user_ids: tuple[int, ...],
    send_message: Callable[..., Awaitable[object]],
    cycle_time: datetime,
) -> None:
    """Process one bounded first-attempt and retry batch using one UTC clock."""

    normalized_cycle_time = ensure_utc(cycle_time).replace(microsecond=0)
    conn = connect(db_path)
    try:
        recovery_results = recover_abandoned_processing_reminders(
            conn,
            now=normalized_cycle_time,
        )
        first_attempts = claim_due_reminders(conn, now=normalized_cycle_time)
        recovered_first_attempts = apply_reminder_downtime_recovery(
            conn,
            claimed_reminders=first_attempts,
            now=normalized_cycle_time,
        )
        normal_reminders = tuple(
            recovered_reminder.reminder
            for recovered_reminder in recovered_first_attempts
            if recovered_reminder.delivery_kind is ReminderRecoveryDeliveryKind.NORMAL
        )
        missed_reminders = tuple(
            recovered_reminder
            for recovered_reminder in recovered_first_attempts
            if recovered_reminder.delivery_kind is ReminderRecoveryDeliveryKind.MISSED
        )
        outcome_counts = _new_outcome_counts(
            recovered_count=len(recovery_results),
            first_attempt_count=len(first_attempts),
            normal_candidate_count=len(normal_reminders),
            missed_candidate_count=len(missed_reminders),
        )

        for claimed_reminder in normal_reminders:
            try:
                result = await deliver_claimed_reminder(
                    conn,
                    claimed_reminder=claimed_reminder,
                    allowed_telegram_user_ids=allowed_telegram_user_ids,
                    send_message=send_message,
                    clock=lambda: normalized_cycle_time,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.error(
                    "Unexpected reminder delivery failure reminder_id=%s",
                    claimed_reminder.reminder_id,
                )
                continue
            _record_delivery_outcome(outcome_counts, result)

        missed_results = await deliver_missed_reminders(
            conn,
            recovered_reminders=missed_reminders,
            allowed_telegram_user_ids=allowed_telegram_user_ids,
            send_message=send_message,
            clock=lambda: normalized_cycle_time,
        )
        for result in missed_results:
            _record_delivery_outcome(outcome_counts, result)

        retry_attempts = claim_due_reminder_retries(conn, now=normalized_cycle_time)
        outcome_counts["retry_attempts_claimed"] = len(retry_attempts)
        for claimed_reminder in retry_attempts:
            try:
                result = await deliver_claimed_reminder(
                    conn,
                    claimed_reminder=claimed_reminder,
                    allowed_telegram_user_ids=allowed_telegram_user_ids,
                    send_message=send_message,
                    clock=lambda: normalized_cycle_time,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.error(
                    "Unexpected reminder retry delivery failure reminder_id=%s",
                    claimed_reminder.reminder_id,
                )
                continue
            _record_delivery_outcome(outcome_counts, result)

        LOGGER.info(
            "Reminder processing cycle complete recovered=%s first_attempts_claimed=%s "
            "normal_candidates=%s missed_candidates=%s retry_attempts_claimed=%s "
            "sent=%s retry_scheduled=%s terminal_failed=%s cancelled_or_skipped=%s "
            "persistence_errors=%s",
            outcome_counts["recovered"],
            outcome_counts["first_attempts_claimed"],
            outcome_counts["normal_candidates"],
            outcome_counts["missed_candidates"],
            outcome_counts["retry_attempts_claimed"],
            outcome_counts["sent"],
            outcome_counts["retry_scheduled"],
            outcome_counts["terminal_failed"],
            outcome_counts["cancelled_or_skipped"],
            outcome_counts["persistence_errors"],
        )
    finally:
        conn.close()


def _new_outcome_counts(
    *,
    recovered_count: int,
    first_attempt_count: int,
    normal_candidate_count: int,
    missed_candidate_count: int,
) -> dict[str, int]:
    return {
        "recovered": recovered_count,
        "first_attempts_claimed": first_attempt_count,
        "normal_candidates": normal_candidate_count,
        "missed_candidates": missed_candidate_count,
        "retry_attempts_claimed": 0,
        "sent": 0,
        "retry_scheduled": 0,
        "terminal_failed": 0,
        "cancelled_or_skipped": 0,
        "persistence_errors": 0,
    }


def _record_delivery_outcome(
    outcome_counts: dict[str, int],
    result: TelegramReminderDeliveryResult | MissedReminderDeliveryResult,
) -> None:
    affected_reminder_count = (
        len(result.reminder_ids)
        if isinstance(result, MissedReminderDeliveryResult)
        else 1
    )
    if result.outcome is TelegramReminderDeliveryOutcome.SENT:
        outcome_counts["sent"] += affected_reminder_count
    elif result.outcome is TelegramReminderDeliveryOutcome.RETRY_SCHEDULED:
        outcome_counts["retry_scheduled"] += affected_reminder_count
    elif result.outcome is TelegramReminderDeliveryOutcome.TERMINAL_FAILURE:
        outcome_counts["terminal_failed"] += affected_reminder_count
    elif result.outcome in (
        TelegramReminderDeliveryOutcome.CANCELLED_DISALLOWED_RECIPIENT,
        TelegramReminderDeliveryOutcome.SKIPPED_INELIGIBLE,
    ):
        outcome_counts["cancelled_or_skipped"] += affected_reminder_count
    elif result.outcome in (
        TelegramReminderDeliveryOutcome.PREFLIGHT_PERSISTENCE_ERROR,
        TelegramReminderDeliveryOutcome.RESULT_PERSISTENCE_ERROR,
    ):
        outcome_counts["persistence_errors"] += affected_reminder_count
