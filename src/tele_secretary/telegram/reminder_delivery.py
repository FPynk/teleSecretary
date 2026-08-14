"""Telegram delivery adapter for previously claimed reminders."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from sqlite3 import Connection

from tele_secretary.app.reminders import (
    ClaimedReminderRecord,
    ReminderDeliveryPreparationOutcome,
    ReminderRecord,
    prepare_claimed_reminder_delivery,
    record_claimed_reminder_failure,
    record_claimed_reminder_sent,
)
from tele_secretary.time_utils import utc_now


LOGGER = logging.getLogger(__name__)
MAX_TELEGRAM_MESSAGE_LENGTH = 4_096
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 30


class TelegramReminderDeliveryOutcome(Enum):
    """Describes the durable result of one Telegram reminder delivery attempt."""

    SENT = "sent"
    RETRY_SCHEDULED = "retry_scheduled"
    TERMINAL_FAILURE = "terminal_failure"
    CANCELLED_DISALLOWED_RECIPIENT = "cancelled_disallowed_recipient"
    SKIPPED_INELIGIBLE = "skipped_ineligible"
    PREFLIGHT_PERSISTENCE_ERROR = "preflight_persistence_error"
    RESULT_PERSISTENCE_ERROR = "result_persistence_error"


@dataclass(frozen=True)
class TelegramReminderDeliveryResult:
    """Returns an orchestration-safe outcome for one claimed reminder."""

    reminder_id: str
    outcome: TelegramReminderDeliveryOutcome
    retry_count: int | None = None


def build_reminder_delivery_message(claimed_reminder: ClaimedReminderRecord) -> str:
    """Build the bounded plain-text Telegram message for one reminder."""

    prefix = "Reminder: "
    suffix = f"\nTask: {claimed_reminder.task_ref}"
    title_length_limit = MAX_TELEGRAM_MESSAGE_LENGTH - len(prefix) - len(suffix)
    if len(claimed_reminder.task_title) <= title_length_limit:
        return f"{prefix}{claimed_reminder.task_title}{suffix}"
    return f"{prefix}{claimed_reminder.task_title[:title_length_limit - 1]}…{suffix}"


async def deliver_claimed_reminder(
    conn: Connection,
    *,
    claimed_reminder: ClaimedReminderRecord,
    allowed_telegram_user_ids: tuple[int, ...],
    send_message: Callable[..., Awaitable[object]],
    clock: Callable[[], datetime] = utc_now,
) -> TelegramReminderDeliveryResult:
    """Send a claimed reminder once and persist the resulting lifecycle transition."""

    try:
        preparation = prepare_claimed_reminder_delivery(
            conn,
            claimed_reminder=claimed_reminder,
            allowed_telegram_user_ids=allowed_telegram_user_ids,
            evaluated_at=clock(),
        )
    except Exception:
        _log_delivery_event(
            claimed_reminder,
            outcome=TelegramReminderDeliveryOutcome.PREFLIGHT_PERSISTENCE_ERROR,
        )
        return TelegramReminderDeliveryResult(
            reminder_id=claimed_reminder.reminder_id,
            outcome=TelegramReminderDeliveryOutcome.PREFLIGHT_PERSISTENCE_ERROR,
        )

    if preparation.outcome is ReminderDeliveryPreparationOutcome.INELIGIBLE:
        _log_delivery_event(
            claimed_reminder,
            outcome=TelegramReminderDeliveryOutcome.SKIPPED_INELIGIBLE,
        )
        return TelegramReminderDeliveryResult(
            reminder_id=claimed_reminder.reminder_id,
            outcome=TelegramReminderDeliveryOutcome.SKIPPED_INELIGIBLE,
        )
    if (
        preparation.outcome
        is ReminderDeliveryPreparationOutcome.CANCELLED_DISALLOWED_RECIPIENT
    ):
        _log_delivery_event(
            claimed_reminder,
            outcome=TelegramReminderDeliveryOutcome.CANCELLED_DISALLOWED_RECIPIENT,
        )
        return TelegramReminderDeliveryResult(
            reminder_id=claimed_reminder.reminder_id,
            outcome=TelegramReminderDeliveryOutcome.CANCELLED_DISALLOWED_RECIPIENT,
        )

    if conn.in_transaction:
        raise RuntimeError("Telegram reminder delivery cannot hold a database transaction.")

    try:
        await send_message(
            chat_id=claimed_reminder.telegram_user_id,
            text=build_reminder_delivery_message(claimed_reminder),
            parse_mode=None,
            connect_timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            read_timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            write_timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            pool_timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as error:
        failure_code = _telegram_failure_code(error)
        try:
            recorded_reminder = record_claimed_reminder_failure(
                conn,
                claimed_reminder=claimed_reminder,
                failure_reason=failure_code,
                attempted_at=clock(),
            )
        except Exception:
            _log_delivery_event(
                claimed_reminder,
                outcome=TelegramReminderDeliveryOutcome.RESULT_PERSISTENCE_ERROR,
                failure_code=failure_code,
            )
            return TelegramReminderDeliveryResult(
                reminder_id=claimed_reminder.reminder_id,
                outcome=TelegramReminderDeliveryOutcome.RESULT_PERSISTENCE_ERROR,
            )
        return _delivery_result_for_failed_attempt(
            claimed_reminder,
            recorded_reminder,
            failure_code,
        )

    try:
        recorded_reminder = record_claimed_reminder_sent(
            conn,
            claimed_reminder=claimed_reminder,
            sent_at=clock(),
        )
    except Exception:
        _log_delivery_event(
            claimed_reminder,
            outcome=TelegramReminderDeliveryOutcome.RESULT_PERSISTENCE_ERROR,
        )
        return TelegramReminderDeliveryResult(
            reminder_id=claimed_reminder.reminder_id,
            outcome=TelegramReminderDeliveryOutcome.RESULT_PERSISTENCE_ERROR,
        )

    _log_delivery_event(
        claimed_reminder,
        outcome=TelegramReminderDeliveryOutcome.SENT,
        retry_count=recorded_reminder.retry_count,
    )
    return TelegramReminderDeliveryResult(
        reminder_id=claimed_reminder.reminder_id,
        outcome=TelegramReminderDeliveryOutcome.SENT,
        retry_count=recorded_reminder.retry_count,
    )


def _delivery_result_for_failed_attempt(
    claimed_reminder: ClaimedReminderRecord,
    recorded_reminder: ReminderRecord,
    failure_code: str,
) -> TelegramReminderDeliveryResult:
    outcome = (
        TelegramReminderDeliveryOutcome.RETRY_SCHEDULED
        if recorded_reminder.status == "pending"
        else TelegramReminderDeliveryOutcome.TERMINAL_FAILURE
    )
    _log_delivery_event(
        claimed_reminder,
        outcome=outcome,
        retry_count=recorded_reminder.retry_count,
        failure_code=failure_code,
    )
    return TelegramReminderDeliveryResult(
        reminder_id=claimed_reminder.reminder_id,
        outcome=outcome,
        retry_count=recorded_reminder.retry_count,
    )


def _telegram_failure_code(error: Exception) -> str:
    exception_name = type(error).__name__
    if exception_name == "TimedOut":
        return "telegram_timeout"
    if exception_name == "RetryAfter":
        return "telegram_retry_after"
    if exception_name == "Forbidden":
        return "telegram_forbidden"
    if exception_name == "BadRequest":
        return "telegram_bad_request"
    if exception_name == "NetworkError":
        return "telegram_network_error"
    return "telegram_delivery_error"


def _log_delivery_event(
    claimed_reminder: ClaimedReminderRecord,
    *,
    outcome: TelegramReminderDeliveryOutcome,
    retry_count: int | None = None,
    failure_code: str | None = None,
) -> None:
    """Log only delivery metadata that cannot expose message or exception content."""

    LOGGER.info(
        "Telegram reminder delivery reminder_id=%s outcome=%s retry_count=%s failure_code=%s",
        claimed_reminder.reminder_id,
        outcome.value,
        retry_count,
        failure_code,
    )
