"""Owner-scoped reminder application services."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection
from typing import Any
from uuid import uuid4

from tele_secretary.time_utils import ensure_utc, to_storage_text, utc_now


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


def claim_due_reminders(
    conn: Connection,
    *,
    now: datetime | None = None,
    batch_size: int = DEFAULT_CLAIM_BATCH_SIZE,
) -> tuple[ClaimedReminderRecord, ...]:
    _validate_claim_batch_size(batch_size)
    if conn.in_transaction:
        raise RuntimeError("claim_due_reminders requires a connection without an active transaction.")

    claim_time = ensure_utc(now or utc_now())
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
            ORDER BY reminders.scheduled_at ASC, reminders.id ASC
            """,
            candidate_ids,
        ).fetchall()
        if len(claimed_rows) != len(candidate_ids) or any(
            row["status"] != "processing" for row in claimed_rows
        ):
            raise RuntimeError("Claimed reminder context did not match the selected batch.")

        claimed_reminders = tuple(
            _claimed_reminder_from_row(row) for row in claimed_rows
        )
        conn.commit()
        return claimed_reminders
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
