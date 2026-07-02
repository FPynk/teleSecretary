"""Telegram user services."""

from __future__ import annotations

from sqlite3 import Connection

from tele_secretary.time_utils import utc_now_iso


SINGLE_OWNER_USER_ID = "single-owner"


# TeleSecretary is currently a single-user app. The users table remains because
# task records have an explicit owner, but this helper always maps the configured
# Telegram owner to one deterministic internal user row.
def get_or_create_telegram_user_id(
    conn: Connection,
    *,
    telegram_user_id: int,
    timezone: str,
) -> str:
    row = conn.execute(
        "SELECT id FROM users WHERE telegram_user_id = ?",
        (telegram_user_id,),
    ).fetchone()
    if row is not None:
        return row["id"]

    now = utc_now_iso()
    row = conn.execute(
        "SELECT id FROM users WHERE id = ?",
        (SINGLE_OWNER_USER_ID,),
    ).fetchone()
    if row is not None:
        with conn:
            conn.execute(
                """
                UPDATE users
                SET telegram_user_id = ?, timezone = ?, updated_at = ?
                WHERE id = ?
                """,
                (telegram_user_id, timezone, now, SINGLE_OWNER_USER_ID),
            )
        return SINGLE_OWNER_USER_ID

    with conn:
        conn.execute(
            """
            INSERT INTO users (
                id, telegram_user_id, timezone, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (SINGLE_OWNER_USER_ID, telegram_user_id, timezone, now, now),
        )
    return SINGLE_OWNER_USER_ID
