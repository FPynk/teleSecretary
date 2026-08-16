"""Owner-scoped reminder application services."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from sqlite3 import Connection
from typing import Any
from uuid import uuid4

from tele_secretary.time_utils import ensure_utc, to_storage_text, utc_now


LOGGER = logging.getLogger(__name__)


class ReminderServiceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ReminderValidationError(ReminderServiceError):
    pass


class ReminderNotFoundError(ReminderServiceError):
    pass


class DuplicateReminderError(ReminderServiceError):
    pass


class ReminderSelectionError(ReminderServiceError):
    pass


class ReminderDeliveryStateError(ReminderServiceError):
    pass


@dataclass(frozen=True)
class ReminderRecord:
    id: str
    task_id: str
    scheduled_at: str
    status: str
    delivery_channel: str
    retry_count: int
    last_attempted_at: str | None
    sent_at: str | None
    failure_reason: str | None
    cancelled_at: str | None
    expired_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReminderCancellationResult:
    reminder: ReminderRecord
    was_cancelled: bool


@dataclass(frozen=True)
class ClaimedReminderRecord:
    reminder_id: str
    task_id: str
    user_id: str
    telegram_user_id: int | None
    user_timezone: str
    task_ref: str
    task_title: str
    scheduled_at: str
    status: str
    delivery_channel: str
    retry_count: int
    claimed_at: str


class ReminderRecoveryDeliveryKind(Enum):
    """Routes a recovered reminder to its normal or missed delivery path."""

    NORMAL = "normal"
    MISSED = "missed"


@dataclass(frozen=True)
class RecoveredReminderRecord:
    """Pairs one claimed reminder with its post-downtime delivery route."""

    reminder: ClaimedReminderRecord
    delivery_kind: ReminderRecoveryDeliveryKind


class ReminderDeliveryPreparationOutcome(Enum):
    """Describes whether a claimed reminder is ready for Telegram delivery."""

    READY = "ready"
    CANCELLED_DISALLOWED_RECIPIENT = "cancelled_disallowed_recipient"
    INELIGIBLE = "ineligible"


@dataclass(frozen=True)
class ReminderDeliveryPreparationResult:
    """Returns the final persisted eligibility decision for a claimed reminder."""

    outcome: ReminderDeliveryPreparationOutcome


_REMINDER_COLUMNS = """
    reminders.id,
    reminders.item_id AS task_id,
    reminders.scheduled_at,
    reminders.status,
    reminders.delivery_channel,
    reminders.retry_count,
    reminders.last_attempted_at,
    reminders.sent_at,
    reminders.failure_reason,
    reminders.cancelled_at,
    reminders.expired_at,
    reminders.created_at,
    reminders.updated_at
"""

DEFAULT_CLAIM_BATCH_SIZE = 50
MAX_CLAIM_BATCH_SIZE = 100
MAX_DELIVERY_FAILURE_CODE_LENGTH = 64
REMINDER_RETRY_DELAYS = {
    1: timedelta(minutes=1),
    2: timedelta(minutes=5),
    3: timedelta(minutes=15),
}


def create_reminder(
    conn: Connection,
    *,
    user_id: str,
    task_id: str,
    scheduled_at: datetime,
    now: datetime | None = None,
) -> ReminderRecord:
    normalized_scheduled_at = _normalize_scheduled_at(scheduled_at)
    now_utc = ensure_utc(now or utc_now())
    if normalized_scheduled_at <= now_utc:
        raise ReminderValidationError(
            "reminder_time_not_future",
            "Reminder time must be in the future.",
        )

    reminder_id = str(uuid4())
    scheduled_at_text = to_storage_text(normalized_scheduled_at)
    now_text = to_storage_text(now_utc)
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO reminders (
                    id,
                    item_id,
                    scheduled_at,
                    status,
                    delivery_channel,
                    retry_count,
                    created_at,
                    updated_at
                )
                SELECT
                    ?,
                    task_items.item_id,
                    ?,
                    'pending',
                    'telegram',
                    0,
                    ?,
                    ?
                FROM task_items
                JOIN items ON items.id = task_items.item_id
                WHERE task_items.item_id = ?
                    AND items.user_id = ?
                    AND items.item_type = 'task'
                    AND items.status = 'active'
                    AND items.deleted_at IS NULL
                """,
                (reminder_id, scheduled_at_text, now_text, now_text, task_id, user_id),
            )
            if cursor.rowcount == 0:
                _require_owned_task(conn, user_id=user_id, task_id=task_id, require_active=True)
    except sqlite3.IntegrityError as error:
        if _is_active_reminder_duplicate(error):
            raise DuplicateReminderError(
                "duplicate_active_reminder",
                "An active reminder already exists for this task at that time.",
            ) from error
        raise

    return get_reminder_by_id(conn, user_id=user_id, reminder_id=reminder_id)


def get_reminder_by_id(
    conn: Connection,
    *,
    user_id: str,
    reminder_id: str,
) -> ReminderRecord:
    row = conn.execute(
        f"""
        SELECT {_REMINDER_COLUMNS}
        FROM reminders
        JOIN items ON items.id = reminders.item_id
        WHERE items.user_id = ?
            AND reminders.id = ?
        """,
        (user_id, reminder_id),
    ).fetchone()
    if row is None:
        raise ReminderNotFoundError("reminder_not_found", "Reminder was not found.")
    return _reminder_from_row(row)


def list_pending_reminders_for_task(
    conn: Connection,
    *,
    user_id: str,
    task_id: str,
) -> tuple[ReminderRecord, ...]:
    _require_owned_task(conn, user_id=user_id, task_id=task_id, require_active=False)
    rows = conn.execute(
        f"""
        SELECT {_REMINDER_COLUMNS}
        FROM reminders
        JOIN items ON items.id = reminders.item_id
        WHERE items.user_id = ?
            AND reminders.item_id = ?
            AND reminders.status = 'pending'
        ORDER BY reminders.scheduled_at ASC, reminders.id ASC
        """,
        (user_id, task_id),
    ).fetchall()
    return tuple(_reminder_from_row(row) for row in rows)


def cancel_pending_reminder(
    conn: Connection,
    *,
    user_id: str,
    reminder_id: str,
    cancelled_at: datetime | None = None,
) -> ReminderCancellationResult:
    """Cancel one owned pending reminder while keeping repeated calls idempotent."""
    cancelled_at_text = _normalize_cancellation_time(cancelled_at or utc_now())
    _require_no_active_transaction(conn, "cancel_pending_reminder")

    try:
        conn.execute("BEGIN IMMEDIATE")
        reminder = _get_owned_reminder(conn, user_id=user_id, reminder_id=reminder_id)
        if reminder.status == "cancelled":
            conn.commit()
            return ReminderCancellationResult(reminder=reminder, was_cancelled=False)
        if reminder.status != "pending":
            raise ReminderValidationError(
                "reminder_not_cancellable",
                "Only pending reminders can be cancelled.",
            )

        cursor = conn.execute(
            """
            UPDATE reminders
            SET status = 'cancelled', cancelled_at = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (cancelled_at_text, cancelled_at_text, reminder_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Reminder cancellation count did not match the selected reminder.")
        cancelled_reminder = _get_owned_reminder(
            conn,
            user_id=user_id,
            reminder_id=reminder_id,
        )
        conn.commit()
        return ReminderCancellationResult(
            reminder=cancelled_reminder,
            was_cancelled=True,
        )
    except Exception:
        conn.rollback()
        raise


def cancel_selected_pending_reminders(
    conn: Connection,
    *,
    user_id: str,
    task_id: str,
    reminder_ids: tuple[str, ...],
    cancelled_at: datetime | None = None,
) -> tuple[ReminderRecord, ...]:
    """Atomically cancel the exact pending reminders selected for an owned task."""
    _validate_selected_reminder_ids(reminder_ids)
    cancelled_at_text = _normalize_cancellation_time(cancelled_at or utc_now())
    _require_no_active_transaction(conn, "cancel_selected_pending_reminders")
    placeholders = ", ".join("?" for _ in reminder_ids)

    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"""
            SELECT {_REMINDER_COLUMNS}
            FROM reminders
            JOIN items ON items.id = reminders.item_id
            WHERE items.user_id = ?
                AND reminders.item_id = ?
                AND reminders.id IN ({placeholders})
            """,
            (user_id, task_id, *reminder_ids),
        ).fetchall()
        reminders_by_id = {row["id"]: _reminder_from_row(row) for row in rows}
        if len(reminders_by_id) != len(reminder_ids) or any(
            reminder.status != "pending" for reminder in reminders_by_id.values()
        ):
            raise ReminderSelectionError(
                "reminder_selection_unavailable",
                "Reminder selection is no longer available.",
            )

        cursor = conn.execute(
            f"""
            UPDATE reminders
            SET status = 'cancelled', cancelled_at = ?, updated_at = ?
            WHERE item_id = ?
                AND status = 'pending'
                AND id IN ({placeholders})
            """,
            (cancelled_at_text, cancelled_at_text, task_id, *reminder_ids),
        )
        if cursor.rowcount != len(reminder_ids):
            raise RuntimeError("Reminder cancellation count did not match the selected reminders.")

        cancelled_rows = conn.execute(
            f"""
            SELECT {_REMINDER_COLUMNS}
            FROM reminders
            JOIN items ON items.id = reminders.item_id
            WHERE items.user_id = ?
                AND reminders.item_id = ?
                AND reminders.id IN ({placeholders})
            """,
            (user_id, task_id, *reminder_ids),
        ).fetchall()
        cancelled_by_id = {
            row["id"]: _reminder_from_row(row) for row in cancelled_rows
        }
        if len(cancelled_by_id) != len(reminder_ids):
            raise RuntimeError("Cancelled reminders could not be read back.")
        conn.commit()
        return tuple(cancelled_by_id[reminder_id] for reminder_id in reminder_ids)
    except Exception:
        conn.rollback()
        raise


def cancel_all_future_pending_reminders_for_task(
    conn: Connection,
    *,
    user_id: str,
    task_id: str,
    now: datetime | None = None,
) -> tuple[ReminderRecord, ...]:
    """Cancel an owned task's pending reminders scheduled after the supplied time."""
    cancellation_time = now or utc_now()
    _normalize_cancellation_time(cancellation_time)
    _require_no_active_transaction(conn, "cancel_all_future_pending_reminders_for_task")

    try:
        conn.execute("BEGIN IMMEDIATE")
        cancelled_reminders = cancel_all_future_pending_reminders_for_task_in_transaction(
            conn,
            user_id=user_id,
            task_id=task_id,
            now=cancellation_time,
        )
        conn.commit()
        return cancelled_reminders
    except Exception:
        conn.rollback()
        raise


def cancel_all_future_pending_reminders_for_task_in_transaction(
    conn: Connection,
    *,
    user_id: str,
    task_id: str,
    now: datetime,
) -> tuple[ReminderRecord, ...]:
    """Cancel one owned task's future pending reminders within the caller's transaction."""

    if not conn.in_transaction:
        raise RuntimeError(
            "cancel_all_future_pending_reminders_for_task_in_transaction requires an active transaction."
        )
    cancellation_time_text = _normalize_cancellation_time(now)
    _require_owned_task(conn, user_id=user_id, task_id=task_id, require_active=False)
    rows = conn.execute(
        f"""
        SELECT {_REMINDER_COLUMNS}
        FROM reminders
        JOIN items ON items.id = reminders.item_id
        WHERE items.user_id = ?
            AND reminders.item_id = ?
            AND reminders.status = 'pending'
            AND reminders.scheduled_at > ?
        ORDER BY reminders.scheduled_at ASC, reminders.id ASC
        """,
        (user_id, task_id, cancellation_time_text),
    ).fetchall()
    reminder_ids = tuple(row["id"] for row in rows)
    if not reminder_ids:
        return ()

    placeholders = ", ".join("?" for _ in reminder_ids)
    cursor = conn.execute(
        f"""
        UPDATE reminders
        SET status = 'cancelled', cancelled_at = ?, updated_at = ?
        WHERE item_id = ?
            AND status = 'pending'
            AND id IN ({placeholders})
        """,
        (cancellation_time_text, cancellation_time_text, task_id, *reminder_ids),
    )
    if cursor.rowcount != len(reminder_ids):
        raise RuntimeError("Reminder cancellation count did not match future reminders.")

    cancelled_rows = conn.execute(
        f"""
        SELECT {_REMINDER_COLUMNS}
        FROM reminders
        JOIN items ON items.id = reminders.item_id
        WHERE items.user_id = ?
            AND reminders.item_id = ?
            AND reminders.id IN ({placeholders})
        """,
        (user_id, task_id, *reminder_ids),
    ).fetchall()
    cancelled_by_id = {row["id"]: _reminder_from_row(row) for row in cancelled_rows}
    if len(cancelled_by_id) != len(reminder_ids):
        raise RuntimeError("Cancelled reminders could not be read back.")
    return tuple(cancelled_by_id[reminder_id] for reminder_id in reminder_ids)


def claim_due_reminders(
    conn: Connection,
    *,
    now: datetime | None = None,
    batch_size: int = DEFAULT_CLAIM_BATCH_SIZE,
) -> tuple[ClaimedReminderRecord, ...]:
    """Claim the next bounded batch of due reminders that have not been attempted."""

    _validate_claim_batch_size(batch_size)
    _require_no_active_transaction(conn, "claim_due_reminders")

    claim_time = _normalize_claim_time(now or utc_now())
    claim_time_text = to_storage_text(claim_time)
    try:
        conn.execute("BEGIN IMMEDIATE")
        candidate_ids = tuple(
            row["id"]
            for row in conn.execute(
                """
                SELECT reminders.id
                FROM reminders
                JOIN items ON items.id = reminders.item_id
                WHERE reminders.status = 'pending'
                    AND reminders.scheduled_at <= ?
                    AND reminders.retry_count = 0
                    AND reminders.last_attempted_at IS NULL
                    AND items.status = 'active'
                    AND items.deleted_at IS NULL
                ORDER BY reminders.scheduled_at ASC, reminders.id ASC
                LIMIT ?
                """,
                (claim_time_text, batch_size),
            ).fetchall()
        )
        if not candidate_ids:
            conn.commit()
            return ()

        claimed_reminders = _claim_selected_reminders(
            conn,
            candidate_ids=candidate_ids,
            claim_time_text=claim_time_text,
        )
        conn.commit()
        return claimed_reminders
    except Exception:
        conn.rollback()
        raise


def claim_due_reminder_retries(
    conn: Connection,
    *,
    now: datetime | None = None,
    batch_size: int = DEFAULT_CLAIM_BATCH_SIZE,
) -> tuple[ClaimedReminderRecord, ...]:
    """Claim the next bounded batch of pending reminders whose retry delay has elapsed."""

    _validate_claim_batch_size(batch_size)
    _require_no_active_transaction(conn, "claim_due_reminder_retries")

    claim_time = _normalize_claim_time(now or utc_now())
    claim_time_text = to_storage_text(claim_time)
    retry_cutoffs = tuple(
        to_storage_text(claim_time - REMINDER_RETRY_DELAYS[retry_count])
        for retry_count in (1, 2, 3)
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        candidate_ids = tuple(
            row["id"]
            for row in conn.execute(
                """
                SELECT reminders.id
                FROM reminders
                JOIN items ON items.id = reminders.item_id
                WHERE reminders.status = 'pending'
                    AND reminders.retry_count BETWEEN 1 AND 3
                    AND reminders.last_attempted_at IS NOT NULL
                    AND strftime('%Y-%m-%dT%H:%M:%S+00:00', reminders.last_attempted_at)
                        = reminders.last_attempted_at
                    AND (
                        (reminders.retry_count = 1 AND reminders.last_attempted_at <= ?)
                        OR (reminders.retry_count = 2 AND reminders.last_attempted_at <= ?)
                        OR (reminders.retry_count = 3 AND reminders.last_attempted_at <= ?)
                    )
                    AND items.status = 'active'
                    AND items.deleted_at IS NULL
                ORDER BY
                    CASE reminders.retry_count
                        WHEN 1 THEN strftime(
                            '%Y-%m-%dT%H:%M:%S+00:00',
                            datetime(reminders.last_attempted_at, '+1 minute')
                        )
                        WHEN 2 THEN strftime(
                            '%Y-%m-%dT%H:%M:%S+00:00',
                            datetime(reminders.last_attempted_at, '+5 minutes')
                        )
                        WHEN 3 THEN strftime(
                            '%Y-%m-%dT%H:%M:%S+00:00',
                            datetime(reminders.last_attempted_at, '+15 minutes')
                        )
                    END ASC,
                    reminders.id ASC
                LIMIT ?
                """,
                (*retry_cutoffs, batch_size),
            ).fetchall()
        )
        if not candidate_ids:
            conn.commit()
            LOGGER.info("Claimed due reminder retries count=%s", 0)
            return ()

        claimed_reminders = _claim_selected_reminders(
            conn,
            candidate_ids=candidate_ids,
            claim_time_text=claim_time_text,
        )
        conn.commit()
        LOGGER.info("Claimed due reminder retries count=%s", len(claimed_reminders))
        return claimed_reminders
    except Exception:
        conn.rollback()
        raise


def apply_reminder_downtime_recovery(
    conn: Connection,
    *,
    claimed_reminders: tuple[ClaimedReminderRecord, ...],
    now: datetime | None = None,
) -> tuple[RecoveredReminderRecord, ...]:
    """Classify fresh claims and expire reminders that are at least twelve hours late."""

    if conn.in_transaction:
        raise RuntimeError(
            "apply_reminder_downtime_recovery requires a connection without an active transaction."
        )

    evaluation_time = _normalize_recovery_evaluation_time(now or utc_now())
    claimed_schedules = _validate_claimed_reminders(
        claimed_reminders,
        evaluation_time=evaluation_time,
    )
    if not claimed_reminders:
        return ()

    evaluation_time_text = to_storage_text(evaluation_time)
    recovered_reminders: list[RecoveredReminderRecord] = []
    decisions: list[tuple[str, str, datetime | None, int | None, str | None]] = []
    expiration_ids: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        placeholders = ", ".join("?" for _ in claimed_reminders)
        rows_by_id = {
            row["id"]: row
            for row in conn.execute(
                f"""
                SELECT
                    reminders.id,
                    reminders.scheduled_at,
                    reminders.status,
                    reminders.retry_count,
                    reminders.last_attempted_at,
                    items.status AS task_status,
                    items.deleted_at
                FROM reminders
                JOIN items ON items.id = reminders.item_id
                WHERE reminders.id IN ({placeholders})
                """,
                tuple(reminder.reminder_id for reminder in claimed_reminders),
            ).fetchall()
        }

        for claimed_reminder in claimed_reminders:
            row = rows_by_id.get(claimed_reminder.reminder_id)
            if row is None:
                decisions.append(
                    (claimed_reminder.reminder_id, "unchanged_missing", None, None, None)
                )
                continue

            scheduled_at = _parse_recovery_scheduled_at(row["scheduled_at"])
            if scheduled_at != claimed_schedules[claimed_reminder.reminder_id]:
                raise RuntimeError("Claimed reminder schedule did not match the persisted schedule.")

            if row["status"] != "processing":
                decisions.append(
                    (
                        claimed_reminder.reminder_id,
                        "unchanged",
                        None,
                        None,
                        row["status"],
                    )
                )
                continue

            if row["retry_count"] != 0 or row["last_attempted_at"] is not None:
                decisions.append(
                    (
                        claimed_reminder.reminder_id,
                        "unchanged_retry",
                        None,
                        None,
                        row["status"],
                    )
                )
                continue

            if row["task_status"] != "active" or row["deleted_at"] is not None:
                decisions.append(
                    (
                        claimed_reminder.reminder_id,
                        "unchanged_inactive",
                        None,
                        None,
                        row["status"],
                    )
                )
                continue

            lateness = evaluation_time - scheduled_at
            lateness_seconds = int(lateness.total_seconds())
            if lateness <= timedelta(minutes=60):
                recovered_reminders.append(
                    RecoveredReminderRecord(
                        reminder=claimed_reminder,
                        delivery_kind=ReminderRecoveryDeliveryKind.NORMAL,
                    )
                )
                decisions.append(
                    (
                        claimed_reminder.reminder_id,
                        "normal",
                        scheduled_at,
                        lateness_seconds,
                        None,
                    )
                )
            elif lateness < timedelta(hours=12):
                recovered_reminders.append(
                    RecoveredReminderRecord(
                        reminder=claimed_reminder,
                        delivery_kind=ReminderRecoveryDeliveryKind.MISSED,
                    )
                )
                decisions.append(
                    (
                        claimed_reminder.reminder_id,
                        "missed",
                        scheduled_at,
                        lateness_seconds,
                        None,
                    )
                )
            else:
                expiration_ids.append(claimed_reminder.reminder_id)
                decisions.append(
                    (
                        claimed_reminder.reminder_id,
                        "expired",
                        scheduled_at,
                        lateness_seconds,
                        None,
                    )
                )

        if expiration_ids:
            expiration_placeholders = ", ".join("?" for _ in expiration_ids)
            update_cursor = conn.execute(
                f"""
                UPDATE reminders
                SET status = 'expired', expired_at = ?, updated_at = ?
                WHERE status = 'processing'
                    AND retry_count = 0
                    AND last_attempted_at IS NULL
                    AND id IN ({expiration_placeholders})
                """,
                (evaluation_time_text, evaluation_time_text, *expiration_ids),
            )
            if update_cursor.rowcount != len(expiration_ids):
                raise RuntimeError("Expired reminder count did not match the recovery decision.")

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    for reminder_id, action, scheduled_at, lateness_seconds, current_status in decisions:
        if scheduled_at is None:
            status_fragment = f" current_status={current_status}" if current_status else ""
            LOGGER.info(
                "Reminder recovery decision reminder_id=%s action=%s%s",
                reminder_id,
                action,
                status_fragment,
            )
            continue
        LOGGER.info(
            "Reminder recovery decision reminder_id=%s scheduled_at=%s evaluated_at=%s "
            "lateness_seconds=%s action=%s",
            reminder_id,
            to_storage_text(scheduled_at),
            evaluation_time_text,
            lateness_seconds,
            action,
        )
    return tuple(recovered_reminders)


def prepare_claimed_reminder_delivery(
    conn: Connection,
    *,
    claimed_reminder: ClaimedReminderRecord,
    allowed_telegram_user_ids: tuple[int, ...],
    evaluated_at: datetime | None = None,
) -> ReminderDeliveryPreparationResult:
    """Confirm a claim is still eligible and cancel a removed recipient before sending."""

    _require_no_active_transaction(conn, "prepare_claimed_reminder_delivery")
    evaluated_at_text = _normalize_delivery_timestamp(evaluated_at or utc_now())
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT
                reminders.item_id AS task_id,
                reminders.status,
                reminders.delivery_channel,
                reminders.retry_count,
                reminders.updated_at,
                items.user_id,
                items.status AS task_status,
                items.deleted_at,
                users.telegram_user_id
            FROM reminders
            JOIN items ON items.id = reminders.item_id
            JOIN users ON users.id = items.user_id
            WHERE reminders.id = ?
            """,
            (claimed_reminder.reminder_id,),
        ).fetchone()
        if row is None or any(
            (
                claimed_reminder.status != "processing",
                row["task_id"] != claimed_reminder.task_id,
                row["user_id"] != claimed_reminder.user_id,
                row["status"] != "processing",
                row["delivery_channel"] != claimed_reminder.delivery_channel,
                row["retry_count"] != claimed_reminder.retry_count,
                row["updated_at"] != claimed_reminder.claimed_at,
            )
        ):
            conn.commit()
            return ReminderDeliveryPreparationResult(
                ReminderDeliveryPreparationOutcome.INELIGIBLE
            )

        if row["task_status"] != "active" or row["deleted_at"] is not None:
            conn.commit()
            return ReminderDeliveryPreparationResult(
                ReminderDeliveryPreparationOutcome.INELIGIBLE
            )

        if row["telegram_user_id"] != claimed_reminder.telegram_user_id:
            conn.commit()
            return ReminderDeliveryPreparationResult(
                ReminderDeliveryPreparationOutcome.INELIGIBLE
            )

        if row["telegram_user_id"] not in allowed_telegram_user_ids:
            update_cursor = conn.execute(
                """
                UPDATE reminders
                SET status = 'cancelled', cancelled_at = ?, updated_at = ?
                WHERE id = ?
                    AND item_id = ?
                    AND status = 'processing'
                    AND retry_count = ?
                    AND updated_at = ?
                """,
                (
                    evaluated_at_text,
                    evaluated_at_text,
                    claimed_reminder.reminder_id,
                    claimed_reminder.task_id,
                    claimed_reminder.retry_count,
                    claimed_reminder.claimed_at,
                ),
            )
            if update_cursor.rowcount != 1:
                raise ReminderDeliveryStateError(
                    "reminder_delivery_state_changed",
                    "Reminder delivery state changed before recipient cancellation.",
                )
            conn.commit()
            return ReminderDeliveryPreparationResult(
                ReminderDeliveryPreparationOutcome.CANCELLED_DISALLOWED_RECIPIENT
            )

        conn.commit()
        return ReminderDeliveryPreparationResult(ReminderDeliveryPreparationOutcome.READY)
    except Exception:
        conn.rollback()
        raise


def record_claimed_reminder_sent(
    conn: Connection,
    *,
    claimed_reminder: ClaimedReminderRecord,
    sent_at: datetime | None = None,
) -> ReminderRecord:
    """Persist one successful Telegram send for the exact claim lease."""

    _require_no_active_transaction(conn, "record_claimed_reminder_sent")
    sent_at_text = _normalize_delivery_timestamp(sent_at or utc_now())
    try:
        conn.execute("BEGIN IMMEDIATE")
        update_cursor = conn.execute(
            """
            UPDATE reminders
            SET status = 'sent', sent_at = ?, updated_at = ?, failure_reason = NULL
            WHERE id = ?
                AND item_id = ?
                AND status = 'processing'
                AND retry_count = ?
                AND updated_at = ?
            """,
            (
                sent_at_text,
                sent_at_text,
                claimed_reminder.reminder_id,
                claimed_reminder.task_id,
                claimed_reminder.retry_count,
                claimed_reminder.claimed_at,
            ),
        )
        if update_cursor.rowcount != 1:
            raise ReminderDeliveryStateError(
                "reminder_delivery_state_changed",
                "Reminder delivery state changed before recording success.",
            )
        reminder = _get_reminder_by_id_for_delivery(
            conn,
            reminder_id=claimed_reminder.reminder_id,
        )
        conn.commit()
        return reminder
    except Exception:
        conn.rollback()
        raise


def record_claimed_reminder_failure(
    conn: Connection,
    *,
    claimed_reminder: ClaimedReminderRecord,
    failure_reason: str,
    attempted_at: datetime | None = None,
) -> ReminderRecord:
    """Persist one failed Telegram send and return it to pending or terminal failure."""

    _require_no_active_transaction(conn, "record_claimed_reminder_failure")
    normalized_failure_reason = _normalize_delivery_failure_reason(failure_reason)
    attempted_at_text = _normalize_delivery_timestamp(attempted_at or utc_now())
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT retry_count
            FROM reminders
            WHERE id = ?
                AND item_id = ?
                AND status = 'processing'
                AND retry_count = ?
                AND updated_at = ?
            """,
            (
                claimed_reminder.reminder_id,
                claimed_reminder.task_id,
                claimed_reminder.retry_count,
                claimed_reminder.claimed_at,
            ),
        ).fetchone()
        if row is None:
            raise ReminderDeliveryStateError(
                "reminder_delivery_state_changed",
                "Reminder delivery state changed before recording failure.",
            )
        if row["retry_count"] not in range(4):
            raise ReminderDeliveryStateError(
                "invalid_reminder_delivery_state",
                "Reminder retry count cannot be recorded as another delivery failure.",
            )

        next_retry_count = row["retry_count"] + 1
        next_status = "pending" if next_retry_count < 4 else "failed"
        update_cursor = conn.execute(
            """
            UPDATE reminders
            SET
                status = ?,
                retry_count = ?,
                last_attempted_at = ?,
                updated_at = ?,
                failure_reason = ?
            WHERE id = ?
                AND item_id = ?
                AND status = 'processing'
                AND retry_count = ?
                AND updated_at = ?
            """,
            (
                next_status,
                next_retry_count,
                attempted_at_text,
                attempted_at_text,
                normalized_failure_reason,
                claimed_reminder.reminder_id,
                claimed_reminder.task_id,
                claimed_reminder.retry_count,
                claimed_reminder.claimed_at,
            ),
        )
        if update_cursor.rowcount != 1:
            raise ReminderDeliveryStateError(
                "reminder_delivery_state_changed",
                "Reminder delivery state changed while recording failure.",
            )
        reminder = _get_reminder_by_id_for_delivery(
            conn,
            reminder_id=claimed_reminder.reminder_id,
        )
        conn.commit()
        return reminder
    except Exception:
        conn.rollback()
        raise


def _normalize_scheduled_at(scheduled_at: datetime) -> datetime:
    try:
        return ensure_utc(scheduled_at).replace(microsecond=0)
    except ValueError as error:
        raise ReminderValidationError(
            "invalid_scheduled_at",
            "scheduled_at must be timezone-aware.",
        ) from error


def _normalize_cancellation_time(cancelled_at: datetime) -> str:
    try:
        return to_storage_text(ensure_utc(cancelled_at).replace(microsecond=0))
    except ValueError as error:
        raise ReminderValidationError(
            "invalid_cancelled_at",
            "cancelled_at must be timezone-aware.",
        ) from error


def _normalize_delivery_timestamp(value: datetime) -> str:
    try:
        return to_storage_text(ensure_utc(value).replace(microsecond=0))
    except ValueError as error:
        raise ReminderValidationError(
            "invalid_delivery_timestamp",
            "Delivery timestamps must be timezone-aware.",
        ) from error


def _normalize_delivery_failure_reason(failure_reason: str) -> str:
    if (
        not isinstance(failure_reason, str)
        or not failure_reason
        or len(failure_reason) > MAX_DELIVERY_FAILURE_CODE_LENGTH
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in failure_reason)
    ):
        raise ReminderValidationError(
            "invalid_delivery_failure_reason",
            "failure_reason must be a short lowercase delivery failure code.",
        )
    return failure_reason


def _require_no_active_transaction(conn: Connection, operation_name: str) -> None:
    if conn.in_transaction:
        raise RuntimeError(f"{operation_name} requires a connection without an active transaction.")


def _validate_selected_reminder_ids(reminder_ids: tuple[str, ...]) -> None:
    if not reminder_ids or len(set(reminder_ids)) != len(reminder_ids):
        raise ReminderValidationError(
            "invalid_reminder_selection",
            "Reminder selection must include one or more unique reminder IDs.",
        )


def _normalize_recovery_evaluation_time(evaluation_time: datetime) -> datetime:
    """Return the evaluation clock as a second-precision UTC datetime."""

    return ensure_utc(evaluation_time).replace(microsecond=0)


def _normalize_claim_time(claim_time: datetime) -> datetime:
    """Return the claim clock as a second-precision UTC datetime."""

    return ensure_utc(claim_time).replace(microsecond=0)


def _claim_selected_reminders(
    conn: Connection,
    *,
    candidate_ids: tuple[str, ...],
    claim_time_text: str,
) -> tuple[ClaimedReminderRecord, ...]:
    """Transition selected pending reminders and return their delivery context in candidate order."""

    placeholders = ", ".join("?" for _ in candidate_ids)
    update_cursor = conn.execute(
        f"""
        UPDATE reminders
        SET status = 'processing', updated_at = ?
        WHERE status = 'pending' AND id IN ({placeholders})
        """,
        (claim_time_text, *candidate_ids),
    )
    if update_cursor.rowcount != len(candidate_ids):
        raise RuntimeError("Claimed reminder count did not match the selected batch.")

    claimed_rows = conn.execute(
        f"""
        SELECT
            reminders.id AS reminder_id,
            reminders.item_id AS task_id,
            items.user_id,
            users.telegram_user_id,
            users.timezone AS user_timezone,
            items.pub_ref AS task_ref,
            items.title AS task_title,
            reminders.scheduled_at,
            reminders.status,
            reminders.delivery_channel,
            reminders.retry_count,
            reminders.updated_at AS claimed_at
        FROM reminders
        JOIN items ON items.id = reminders.item_id
        JOIN users ON users.id = items.user_id
        WHERE reminders.id IN ({placeholders})
        """,
        candidate_ids,
    ).fetchall()
    claimed_rows_by_id = {row["reminder_id"]: row for row in claimed_rows}
    if (
        len(claimed_rows_by_id) != len(candidate_ids)
        or any(
            reminder_id not in claimed_rows_by_id
            or claimed_rows_by_id[reminder_id]["status"] != "processing"
            or claimed_rows_by_id[reminder_id]["claimed_at"] != claim_time_text
            for reminder_id in candidate_ids
        )
    ):
        raise RuntimeError("Claimed reminder context did not match the selected batch.")
    return tuple(
        _claimed_reminder_from_row(claimed_rows_by_id[reminder_id])
        for reminder_id in candidate_ids
    )


def _validate_claimed_reminders(
    claimed_reminders: tuple[ClaimedReminderRecord, ...],
    *,
    evaluation_time: datetime,
) -> dict[str, datetime]:
    """Reject duplicate, malformed, or future claim records before a transaction begins."""

    schedules_by_id: dict[str, datetime] = {}
    for claimed_reminder in claimed_reminders:
        if claimed_reminder.reminder_id in schedules_by_id:
            raise ValueError("claimed_reminders must not contain duplicate reminder IDs.")
        scheduled_at = _parse_recovery_scheduled_at(claimed_reminder.scheduled_at)
        if scheduled_at > evaluation_time:
            raise ValueError("Claimed reminder schedule must not be in the future.")
        schedules_by_id[claimed_reminder.reminder_id] = scheduled_at
    return schedules_by_id


def _parse_recovery_scheduled_at(scheduled_at: str) -> datetime:
    """Parse one persisted reminder schedule as an aware UTC instant."""

    try:
        return ensure_utc(datetime.fromisoformat(scheduled_at))
    except (TypeError, ValueError) as error:
        raise ValueError(
            "Reminder scheduled_at must be an ISO 8601 timezone-aware timestamp."
        ) from error


def _validate_claim_batch_size(batch_size: int) -> None:
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= MAX_CLAIM_BATCH_SIZE
    ):
        raise ValueError(
            f"batch_size must be an integer from 1 through {MAX_CLAIM_BATCH_SIZE}."
        )


def _require_owned_task(
    conn: Connection,
    *,
    user_id: str,
    task_id: str,
    require_active: bool,
) -> None:
    row = conn.execute(
        """
        SELECT items.status, items.deleted_at
        FROM items
        JOIN task_items ON task_items.item_id = items.id
        WHERE items.id = ?
            AND items.user_id = ?
            AND items.item_type = 'task'
        """,
        (task_id, user_id),
    ).fetchone()
    if row is None:
        raise ReminderNotFoundError("task_not_found", "Task was not found.")
    if require_active and (row["status"] != "active" or row["deleted_at"] is not None):
        raise ReminderValidationError(
            "task_not_active",
            "Reminders can only be created for active tasks.",
        )


def _get_owned_reminder(
    conn: Connection,
    *,
    user_id: str,
    reminder_id: str,
) -> ReminderRecord:
    row = conn.execute(
        f"""
        SELECT {_REMINDER_COLUMNS}
        FROM reminders
        JOIN items ON items.id = reminders.item_id
        WHERE items.user_id = ?
            AND reminders.id = ?
        """,
        (user_id, reminder_id),
    ).fetchone()
    if row is None:
        raise ReminderNotFoundError("reminder_not_found", "Reminder was not found.")
    return _reminder_from_row(row)


def _get_reminder_by_id_for_delivery(
    conn: Connection,
    *,
    reminder_id: str,
) -> ReminderRecord:
    row = conn.execute(
        f"""
        SELECT {_REMINDER_COLUMNS}
        FROM reminders
        WHERE reminders.id = ?
        """,
        (reminder_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Reminder delivery result could not be read back.")
    return _reminder_from_row(row)


def _is_active_reminder_duplicate(error: sqlite3.IntegrityError) -> bool:
    error_code = getattr(error, "sqlite_errorcode", None)
    message = str(error)
    return (
        error_code == sqlite3.SQLITE_CONSTRAINT_UNIQUE
        and "reminders.item_id" in message
        and "reminders.scheduled_at" in message
        and "reminders.delivery_channel" in message
    )


def _reminder_from_row(row: Any) -> ReminderRecord:
    return ReminderRecord(
        id=row["id"],
        task_id=row["task_id"],
        scheduled_at=row["scheduled_at"],
        status=row["status"],
        delivery_channel=row["delivery_channel"],
        retry_count=row["retry_count"],
        last_attempted_at=row["last_attempted_at"],
        sent_at=row["sent_at"],
        failure_reason=row["failure_reason"],
        cancelled_at=row["cancelled_at"],
        expired_at=row["expired_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _claimed_reminder_from_row(row: Any) -> ClaimedReminderRecord:
    return ClaimedReminderRecord(
        reminder_id=row["reminder_id"],
        task_id=row["task_id"],
        user_id=row["user_id"],
        telegram_user_id=row["telegram_user_id"],
        user_timezone=row["user_timezone"],
        task_ref=row["task_ref"],
        task_title=row["task_title"],
        scheduled_at=row["scheduled_at"],
        status=row["status"],
        delivery_channel=row["delivery_channel"],
        retry_count=row["retry_count"],
        claimed_at=row["claimed_at"],
    )
