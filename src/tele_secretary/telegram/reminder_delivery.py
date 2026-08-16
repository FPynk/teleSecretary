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
    RecoveredReminderRecord,
    ReminderDeliveryPreparationOutcome,
    ReminderRecoveryDeliveryKind,
    ReminderRecord,
    prepare_claimed_reminder_delivery,
    record_claimed_reminder_failure,
    record_claimed_reminder_sent,
    record_claimed_reminder_summary_failure,
    record_claimed_reminder_summary_sent,
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


class MissedReminderDeliveryMode(Enum):
    """Identifies whether missed reminders were sent separately or as one summary."""

    INDIVIDUAL = "individual"
    SUMMARY = "summary"


@dataclass(frozen=True)
class MissedReminderDeliveryResult:
    """Returns the durable outcome of one missed-reminder delivery route."""

    reminder_ids: tuple[str, ...]
    mode: MissedReminderDeliveryMode
    outcome: TelegramReminderDeliveryOutcome


def build_reminder_delivery_message(claimed_reminder: ClaimedReminderRecord) -> str:
    """Build the bounded plain-text Telegram message for one reminder."""

    prefix = "Reminder: "
    suffix = f"\nTask: {claimed_reminder.task_ref}"
    title_length_limit = MAX_TELEGRAM_MESSAGE_LENGTH - len(prefix) - len(suffix)
    if len(claimed_reminder.task_title) <= title_length_limit:
        return f"{prefix}{claimed_reminder.task_title}{suffix}"
    return f"{prefix}{claimed_reminder.task_title[:title_length_limit - 1]}…{suffix}"


def build_missed_reminder_delivery_message(claimed_reminder: ClaimedReminderRecord) -> str:
    """Build the bounded plain-text Telegram message for one missed reminder."""

    prefix = "Missed reminder from earlier: "
    suffix = f"\nTask: {claimed_reminder.task_ref}"
    title_length_limit = MAX_TELEGRAM_MESSAGE_LENGTH - len(prefix) - len(suffix)
    if len(claimed_reminder.task_title) <= title_length_limit:
        return f"{prefix}{claimed_reminder.task_title}{suffix}"
    return f"{prefix}{claimed_reminder.task_title[:title_length_limit - 1]}…{suffix}"


def build_missed_reminder_summary_message(
    claimed_reminders: tuple[ClaimedReminderRecord, ...],
) -> str:
    """Build one deterministic, bounded plain-text summary for one owner's misses."""

    if len(claimed_reminders) < 4:
        raise ValueError("Missed reminder summaries require at least four reminders.")
    if len({claimed_reminder.user_id for claimed_reminder in claimed_reminders}) != 1:
        raise ValueError("Missed reminder summaries must contain one owner.")

    ordered_reminders = tuple(
        sorted(
            claimed_reminders,
            key=lambda claimed_reminder: (
                claimed_reminder.scheduled_at,
                claimed_reminder.reminder_id,
            ),
        )
    )
    header = f"You had {len(ordered_reminders)} reminders while I was offline:"
    line_prefixes = tuple(
        f"- {claimed_reminder.task_ref}: " for claimed_reminder in ordered_reminders
    )
    fixed_length = len(header) + sum(len(line_prefix) for line_prefix in line_prefixes)
    fixed_length += len(ordered_reminders)
    if fixed_length > MAX_TELEGRAM_MESSAGE_LENGTH:
        raise ValueError("Missed reminder references exceed Telegram's message limit.")

    title_budget = MAX_TELEGRAM_MESSAGE_LENGTH - fixed_length
    lines: list[str] = []
    for claimed_reminder, line_prefix in zip(ordered_reminders, line_prefixes, strict=True):
        title = claimed_reminder.task_title
        if len(title) <= title_budget:
            rendered_title = title
        elif title_budget == 0:
            rendered_title = ""
        elif title_budget == 1:
            rendered_title = "…"
        else:
            rendered_title = f"{title[:title_budget - 1]}…"
        title_budget -= len(rendered_title)
        lines.append(f"{line_prefix}{rendered_title}")
    return "\n".join((header, *lines))


async def deliver_claimed_reminder(
    conn: Connection,
    *,
    claimed_reminder: ClaimedReminderRecord,
    allowed_telegram_user_ids: tuple[int, ...],
    send_message: Callable[..., Awaitable[object]],
    clock: Callable[[], datetime] = utc_now,
    message_builder: Callable[[ClaimedReminderRecord], str] = build_reminder_delivery_message,
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
            text=message_builder(claimed_reminder),
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


async def deliver_missed_reminders(
    conn: Connection,
    *,
    recovered_reminders: tuple[RecoveredReminderRecord, ...],
    allowed_telegram_user_ids: tuple[int, ...],
    send_message: Callable[..., Awaitable[object]],
    clock: Callable[[], datetime] = utc_now,
) -> tuple[MissedReminderDeliveryResult, ...]:
    """Deliver owner-scoped missed reminders individually or in bounded summaries."""

    missed_reminders_by_owner: dict[str, list[ClaimedReminderRecord]] = {}
    for recovered_reminder in recovered_reminders:
        if recovered_reminder.delivery_kind is not ReminderRecoveryDeliveryKind.MISSED:
            continue
        missed_reminders_by_owner.setdefault(recovered_reminder.reminder.user_id, []).append(
            recovered_reminder.reminder
        )

    results: list[MissedReminderDeliveryResult] = []
    for owner_reminders in missed_reminders_by_owner.values():
        ordered_reminders = tuple(
            sorted(
                owner_reminders,
                key=lambda claimed_reminder: (
                    claimed_reminder.scheduled_at,
                    claimed_reminder.reminder_id,
                ),
            )
        )
        ready_reminders: list[ClaimedReminderRecord] = []
        preflight_results: list[MissedReminderDeliveryResult] = []
        for claimed_reminder in ordered_reminders:
            try:
                preparation = prepare_claimed_reminder_delivery(
                    conn,
                    claimed_reminder=claimed_reminder,
                    allowed_telegram_user_ids=allowed_telegram_user_ids,
                    evaluated_at=clock(),
                )
            except Exception:
                preflight_results.append(
                    MissedReminderDeliveryResult(
                        reminder_ids=(claimed_reminder.reminder_id,),
                        mode=MissedReminderDeliveryMode.INDIVIDUAL,
                        outcome=TelegramReminderDeliveryOutcome.PREFLIGHT_PERSISTENCE_ERROR,
                    )
                )
                continue
            if preparation.outcome is ReminderDeliveryPreparationOutcome.READY:
                ready_reminders.append(claimed_reminder)
                continue
            preflight_results.append(
                MissedReminderDeliveryResult(
                    reminder_ids=(claimed_reminder.reminder_id,),
                    mode=MissedReminderDeliveryMode.INDIVIDUAL,
                    outcome=(
                        TelegramReminderDeliveryOutcome.CANCELLED_DISALLOWED_RECIPIENT
                        if preparation.outcome
                        is ReminderDeliveryPreparationOutcome.CANCELLED_DISALLOWED_RECIPIENT
                        else TelegramReminderDeliveryOutcome.SKIPPED_INELIGIBLE
                    ),
                )
            )

        results.extend(preflight_results)
        if not ready_reminders:
            continue
        if len(ready_reminders) <= 3:
            for claimed_reminder in ready_reminders:
                individual_result = await deliver_claimed_reminder(
                    conn,
                    claimed_reminder=claimed_reminder,
                    allowed_telegram_user_ids=allowed_telegram_user_ids,
                    send_message=send_message,
                    clock=clock,
                    message_builder=build_missed_reminder_delivery_message,
                )
                results.append(
                    MissedReminderDeliveryResult(
                        reminder_ids=(claimed_reminder.reminder_id,),
                        mode=MissedReminderDeliveryMode.INDIVIDUAL,
                        outcome=individual_result.outcome,
                    )
                )
            continue

        results.append(
            await _deliver_missed_reminder_summary(
                conn,
                claimed_reminders=tuple(ready_reminders),
                send_message=send_message,
                clock=clock,
            )
        )
    return tuple(results)


async def _deliver_missed_reminder_summary(
    conn: Connection,
    *,
    claimed_reminders: tuple[ClaimedReminderRecord, ...],
    send_message: Callable[..., Awaitable[object]],
    clock: Callable[[], datetime],
) -> MissedReminderDeliveryResult:
    reminder_ids = tuple(claimed_reminder.reminder_id for claimed_reminder in claimed_reminders)
    if conn.in_transaction:
        raise RuntimeError("Telegram reminder delivery cannot hold a database transaction.")

    try:
        await send_message(
            chat_id=claimed_reminders[0].telegram_user_id,
            text=build_missed_reminder_summary_message(claimed_reminders),
            parse_mode=None,
            connect_timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            read_timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            write_timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            pool_timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as error:
        failure_code = _telegram_failure_code(error)
        try:
            record_claimed_reminder_summary_failure(
                conn,
                claimed_reminders=claimed_reminders,
                failure_reason=failure_code,
                attempted_at=clock(),
            )
        except Exception:
            _log_summary_delivery_event(
                reminder_ids,
                outcome=TelegramReminderDeliveryOutcome.RESULT_PERSISTENCE_ERROR,
                failure_code=failure_code,
            )
            return MissedReminderDeliveryResult(
                reminder_ids=reminder_ids,
                mode=MissedReminderDeliveryMode.SUMMARY,
                outcome=TelegramReminderDeliveryOutcome.RESULT_PERSISTENCE_ERROR,
            )
        _log_summary_delivery_event(
            reminder_ids,
            outcome=TelegramReminderDeliveryOutcome.RETRY_SCHEDULED,
            failure_code=failure_code,
        )
        return MissedReminderDeliveryResult(
            reminder_ids=reminder_ids,
            mode=MissedReminderDeliveryMode.SUMMARY,
            outcome=TelegramReminderDeliveryOutcome.RETRY_SCHEDULED,
        )

    try:
        record_claimed_reminder_summary_sent(
            conn,
            claimed_reminders=claimed_reminders,
            sent_at=clock(),
        )
    except Exception:
        _log_summary_delivery_event(
            reminder_ids,
            outcome=TelegramReminderDeliveryOutcome.RESULT_PERSISTENCE_ERROR,
        )
        return MissedReminderDeliveryResult(
            reminder_ids=reminder_ids,
            mode=MissedReminderDeliveryMode.SUMMARY,
            outcome=TelegramReminderDeliveryOutcome.RESULT_PERSISTENCE_ERROR,
        )

    _log_summary_delivery_event(
        reminder_ids,
        outcome=TelegramReminderDeliveryOutcome.SENT,
    )
    return MissedReminderDeliveryResult(
        reminder_ids=reminder_ids,
        mode=MissedReminderDeliveryMode.SUMMARY,
        outcome=TelegramReminderDeliveryOutcome.SENT,
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


def _log_summary_delivery_event(
    reminder_ids: tuple[str, ...],
    *,
    outcome: TelegramReminderDeliveryOutcome,
    failure_code: str | None = None,
) -> None:
    """Log summary delivery metadata without Telegram or reminder content."""

    LOGGER.info(
        "Telegram missed reminder summary reminder_count=%s outcome=%s failure_code=%s",
        len(reminder_ids),
        outcome.value,
        failure_code,
    )
