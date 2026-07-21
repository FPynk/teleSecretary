from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.persistence.migrations import apply_migrations


TIMESTAMP = "2026-07-20T15:00:00+00:00"


class ReminderSchemaTests(unittest.TestCase):
    def test_reminder_defaults_relationship_and_indexes(self) -> None:
        with self.open_database() as conn:
            task_id, note_id = self.insert_task_and_note(conn)
            self.insert_reminder(conn, reminder_id="reminder-a", item_id=task_id)
            self.insert_reminder(
                conn,
                reminder_id="reminder-b",
                item_id=task_id,
                scheduled_at="2026-07-20T16:00:00+00:00",
            )
            reminder = conn.execute(
                """
                SELECT status, delivery_channel, retry_count
                FROM reminders
                WHERE id = 'reminder-a'
                """
            ).fetchone()
            index_sql = {
                row["name"]: row["sql"]
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'reminders'"
                ).fetchall()
            }

            for reminder_id, item_id in (
                ("note-reminder", note_id),
                ("missing-task-reminder", "missing-task"),
            ):
                with self.subTest(reminder_id=reminder_id):
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert_reminder(conn, reminder_id=reminder_id, item_id=item_id)

            with conn:
                conn.execute("DELETE FROM items WHERE id = ?", (task_id,))
            reminder_count = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]

        self.assertEqual(
            (reminder["status"], reminder["delivery_channel"], reminder["retry_count"]),
            ("pending", "telegram", 0),
        )
        self.assertIn("WHERE status = 'pending'", index_sql["idx_reminders_pending_schedule"])
        self.assertIn(
            "WHERE status IN ('pending', 'processing')",
            index_sql["idx_reminders_unique_active_schedule"],
        )
        self.assertEqual(reminder_count, 0)

    def test_reminder_constraints_validate_lifecycle_data(self) -> None:
        with self.open_database() as conn:
            task_id, _ = self.insert_task_and_note(conn)
            valid_terminal_rows = (
                ("sent", {"sent_at": TIMESTAMP}),
                ("failed", {"last_attempted_at": TIMESTAMP, "failure_reason": "network error"}),
                ("cancelled", {"cancelled_at": TIMESTAMP}),
                ("expired", {"expired_at": TIMESTAMP}),
            )
            for index, (status, fields) in enumerate(valid_terminal_rows):
                with self.subTest(status=status):
                    self.insert_reminder(
                        conn,
                        reminder_id=f"valid-{status}",
                        item_id=task_id,
                        scheduled_at=f"2026-07-20T{16 + index}:00:00+00:00",
                        status=status,
                        **fields,
                    )

            invalid_rows = (
                ("invalid-status", {"status": "unknown"}),
                ("invalid-channel", {"delivery_channel": "email"}),
                ("negative-retry", {"retry_count": -1}),
                ("blank-schedule", {"scheduled_at": "  "}),
                ("blank-created", {"created_at": ""}),
                ("blank-updated", {"updated_at": " "}),
                ("blank-failure", {"failure_reason": " "}),
                ("sent-without-timestamp", {"status": "sent"}),
                ("pending-with-sent-timestamp", {"sent_at": TIMESTAMP}),
                ("cancelled-with-sent-timestamp", {"status": "cancelled", "cancelled_at": TIMESTAMP, "sent_at": TIMESTAMP}),
                ("expired-with-cancelled-timestamp", {"status": "expired", "expired_at": TIMESTAMP, "cancelled_at": TIMESTAMP}),
                ("failed-without-attempt", {"status": "failed", "failure_reason": "network error"}),
                ("failed-without-reason", {"status": "failed", "last_attempted_at": TIMESTAMP}),
            )
            for index, (name, fields) in enumerate(invalid_rows):
                with self.subTest(name=name):
                    with self.assertRaises(sqlite3.IntegrityError):
                        reminder_fields = {
                            "scheduled_at": f"2026-07-21T{index:02}:00:00+00:00"
                        }
                        reminder_fields.update(fields)
                        self.insert_reminder(
                            conn,
                            reminder_id=f"invalid-{name}",
                            item_id=task_id,
                            **reminder_fields,
                        )

    def test_reminder_active_duplicates_are_rejected_and_terminal_rows_are_replaceable(self) -> None:
        with self.open_database() as conn:
            task_id, _ = self.insert_task_and_note(conn)
            self.insert_reminder(conn, reminder_id="pending", item_id=task_id)
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_reminder(
                    conn,
                    reminder_id="processing-duplicate",
                    item_id=task_id,
                    status="processing",
                )

            with conn:
                conn.execute(
                    "UPDATE reminders SET status = 'sent', sent_at = ? WHERE id = 'pending'",
                    (TIMESTAMP,),
                )
            self.insert_reminder(conn, reminder_id="replacement", item_id=task_id)

            statuses = conn.execute(
                "SELECT status FROM reminders WHERE item_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()

        self.assertEqual([row["status"] for row in statuses], ["sent", "pending"])

    @contextmanager
    def open_database(self):
        temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(temp_dir.name) / "secretary.sqlite3"
        database_context = open_test_database(db_path)
        conn = database_context.__enter__()
        try:
            apply_migrations(conn)
            yield conn
        finally:
            database_context.__exit__(None, None, None)
            temp_dir.cleanup()

    def insert_task_and_note(self, conn):
        with conn:
            conn.execute(
                """
                INSERT INTO users (id, telegram_user_id, timezone)
                VALUES ('user-a', 1001, 'America/Chicago')
                """
            )
            conn.execute(
                """
                INSERT INTO items (
                    id, user_id, item_type, pub_ref, title, status, source,
                    parse_status, created_at, updated_at
                ) VALUES
                    ('task-a', 'user-a', 'task', 'T1', 'Task', 'active',
                     'manual_entry', 'not_applicable', ?, ?),
                    ('note-a', 'user-a', 'note', 'N1', 'Note', 'active',
                     'manual_entry', 'not_applicable', ?, ?)
                """,
                (TIMESTAMP, TIMESTAMP, TIMESTAMP, TIMESTAMP),
            )
            conn.execute("INSERT INTO task_items (item_id) VALUES ('task-a')")
            conn.execute("INSERT INTO note_items (item_id) VALUES ('note-a')")
        return "task-a", "note-a"

    def insert_reminder(
        self,
        conn,
        *,
        reminder_id,
        item_id,
        scheduled_at=TIMESTAMP,
        status=None,
        delivery_channel=None,
        retry_count=None,
        last_attempted_at=None,
        sent_at=None,
        failure_reason=None,
        cancelled_at=None,
        expired_at=None,
        created_at=TIMESTAMP,
        updated_at=TIMESTAMP,
    ):
        columns = ["id", "item_id", "scheduled_at", "created_at", "updated_at"]
        values = [reminder_id, item_id, scheduled_at, created_at, updated_at]
        for column_name, value in (
            ("status", status),
            ("delivery_channel", delivery_channel),
            ("retry_count", retry_count),
            ("last_attempted_at", last_attempted_at),
            ("sent_at", sent_at),
            ("failure_reason", failure_reason),
            ("cancelled_at", cancelled_at),
            ("expired_at", expired_at),
        ):
            if value is not None:
                columns.append(column_name)
                values.append(value)
        placeholders = ", ".join("?" for _ in columns)
        with conn:
            conn.execute(
                f"INSERT INTO reminders ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )


if __name__ == "__main__":
    unittest.main()
