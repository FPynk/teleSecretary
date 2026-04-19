"""Human-friendly reference generation."""

from __future__ import annotations

from sqlite3 import Connection


REF_PREFIXES = {
    "task": "T",
}


def allocate_ref(conn: Connection, *, user_id: str, ref_type: str) -> str:
    if ref_type not in REF_PREFIXES:
        raise ValueError(f"Unsupported ref type: {ref_type}")

    with conn:
        row = conn.execute(
            """
            SELECT next_value
            FROM ref_sequences
            WHERE user_id = ? AND ref_type = ?
            """,
            (user_id, ref_type),
        ).fetchone()

        if row is None:
            value = 1
            conn.execute(
                """
                INSERT INTO ref_sequences (user_id, ref_type, next_value)
                VALUES (?, ?, ?)
                """,
                (user_id, ref_type, 2),
            )
        else:
            value = int(row["next_value"])
            conn.execute(
                """
                UPDATE ref_sequences
                SET next_value = ?
                WHERE user_id = ? AND ref_type = ?
                """,
                (value + 1, user_id, ref_type),
            )

    return f"{REF_PREFIXES[ref_type]}{value}"
