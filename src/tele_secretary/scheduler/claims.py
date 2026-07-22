"""Atomic worker claims for due reminders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection
from typing import Any

from tele_secretary.time_utils import ensure_utc, to_storage_text, utc_now


DEFAULT_CLAIM_BATCH_SIZE = 50
MAX_CLAIM_BATCH_SIZE = 100


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


def claim_due_reminders(
    conn: Connection,
    *,
    now: datetime | None = None,
    batch_size: int = DEFAULT_CLAIM_BATCH_SIZE,
) -> tuple[ClaimedReminderRecord, ...]:
    _validate_batch_size(batch_size)
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

        claimed_reminders = tuple(_claimed_reminder_from_row(row) for row in claimed_rows)
        conn.commit()
        return claimed_reminders
    except Exception:
        conn.rollback()
        raise


def _validate_batch_size(batch_size: int) -> None:
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= MAX_CLAIM_BATCH_SIZE
    ):
        raise ValueError(
            f"batch_size must be an integer from 1 through {MAX_CLAIM_BATCH_SIZE}."
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
