"""Telegram owner identity services."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from sqlite3 import Connection
from uuid import uuid4

from tele_secretary.time_utils import utc_now_iso


LOGGER = logging.getLogger(__name__)
LEGACY_SINGLE_OWNER_USER_ID = "single-owner"


@dataclass(frozen=True)
class TelegramUserRecord:
    """The persisted application user mapped to one Telegram identity."""

    user_id: str
    telegram_user_id: int
    timezone: str


def get_or_create_telegram_user(
    conn: Connection,
    *,
    telegram_user_id: int,
    default_timezone: str,
) -> TelegramUserRecord:
    """Return the stable persisted user record for one authorized Telegram ID."""
    if conn.in_transaction:
        raise RuntimeError(
            "get_or_create_telegram_user requires a connection without an active transaction."
        )

    row = _get_telegram_user_row(conn, telegram_user_id=telegram_user_id)
    if row is None:
        now = utc_now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO users (
                    id, telegram_user_id, timezone, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO NOTHING
                """,
                (str(uuid4()), telegram_user_id, default_timezone, now, now),
            )
        row = _get_telegram_user_row(conn, telegram_user_id=telegram_user_id)
        if row is None:
            raise RuntimeError("Telegram user creation did not produce a persisted user row.")

    return _telegram_user_from_row(row)


def bind_unassigned_legacy_single_owner(
    conn: Connection,
    *,
    allowed_telegram_user_ids: tuple[int, ...],
) -> TelegramUserRecord | None:
    """Bind an unassigned legacy owner to the first configured allowlist entry once."""
    # TSEC-39: Remove this compatibility path after the legacy upgrade window closes.
    if conn.in_transaction:
        raise RuntimeError(
            "bind_unassigned_legacy_single_owner requires a connection without an active transaction."
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, telegram_user_id, timezone
            FROM users
            WHERE id = ?
            """,
            (LEGACY_SINGLE_OWNER_USER_ID,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        if row["telegram_user_id"] is not None:
            conn.commit()
            return _telegram_user_from_row(row)
        if not allowed_telegram_user_ids:
            conn.commit()
            return None

        telegram_user_id = allowed_telegram_user_ids[0]
        conflict_row = _get_telegram_user_row(conn, telegram_user_id=telegram_user_id)
        if conflict_row is not None:
            LOGGER.error(
                "Legacy owner binding conflict legacy_user_id=%s telegram_user_id=%s",
                LEGACY_SINGLE_OWNER_USER_ID,
                telegram_user_id,
            )
            raise RuntimeError(
                "Cannot bind the legacy single-owner user because the first allowed "
                "Telegram ID already belongs to another user."
            )

        now = utc_now_iso()
        cursor = conn.execute(
            """
            UPDATE users
            SET telegram_user_id = ?, updated_at = ?
            WHERE id = ? AND telegram_user_id IS NULL
            """,
            (telegram_user_id, now, LEGACY_SINGLE_OWNER_USER_ID),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Legacy single-owner binding did not update exactly one user row.")
        row = conn.execute(
            """
            SELECT id, telegram_user_id, timezone
            FROM users
            WHERE id = ?
            """,
            (LEGACY_SINGLE_OWNER_USER_ID,),
        ).fetchone()
        if row is None or row["telegram_user_id"] is None:
            raise RuntimeError("Legacy single-owner binding did not produce a Telegram user row.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    user = _telegram_user_from_row(row)
    LOGGER.info(
        "Bound legacy owner user_id=%s telegram_user_id=%s",
        user.user_id,
        user.telegram_user_id,
    )
    return user


def get_or_create_telegram_user_id(
    conn: Connection,
    *,
    telegram_user_id: int,
    timezone: str,
) -> str:
    """Return only the internal ID for callers not yet migrated to the record API."""
    return get_or_create_telegram_user(
        conn,
        telegram_user_id=telegram_user_id,
        default_timezone=timezone,
    ).user_id


def _get_telegram_user_row(conn: Connection, *, telegram_user_id: int):
    """Fetch the persisted user row mapped to one Telegram ID."""
    return conn.execute(
        """
        SELECT id, telegram_user_id, timezone
        FROM users
        WHERE telegram_user_id = ?
        """,
        (telegram_user_id,),
    ).fetchone()


def _telegram_user_from_row(row) -> TelegramUserRecord:
    """Build a typed Telegram user record from a selected database row."""
    return TelegramUserRecord(
        user_id=row["id"],
        telegram_user_id=row["telegram_user_id"],
        timezone=row["timezone"],
    )
