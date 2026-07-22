from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.reminders import (
    DuplicateReminderError,
    ReminderNotFoundError,
    ReminderValidationError,
    create_reminder,
    get_reminder_by_id,
    list_pending_reminders_for_task,
)
from tele_secretary.persistence.migrations import apply_migrations


NOW = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)


class ReminderServiceTests(unittest.TestCase):
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with open_test_database(Path(temp_dir) / "secretary.sqlite3") as conn:
                apply_migrations(conn)
                self._insert_user(conn, "user-a", 1001)
                self._insert_user(conn, "user-b", 1002)
                yield conn

    def test_create_normalizes_utc_and_maps_every_persisted_field(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a")
            reminder = create_reminder(
                conn,
                user_id="user-a",
                task_id="task-a",
                scheduled_at=datetime(2026, 7, 21, 12, 30, 45, 800000, tzinfo=timezone(timedelta(hours=-5))),
                now=NOW,
            )

        UUID(reminder.id)
        self.assertEqual(reminder.task_id, "task-a")
        self.assertEqual(reminder.scheduled_at, "2026-07-21T17:30:45+00:00")
        self.assertEqual((reminder.status, reminder.delivery_channel, reminder.retry_count), ("pending", "telegram", 0))
        self.assertEqual(
            (reminder.last_attempted_at, reminder.sent_at, reminder.failure_reason, reminder.cancelled_at, reminder.expired_at),
            (None, None, None, None, None),
        )
        self.assertEqual((reminder.created_at, reminder.updated_at), ("2026-07-21T15:00:00+00:00",) * 2)

    def test_get_reminder_is_owner_scoped_and_includes_terminal_history(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a")
            reminder = self._create_reminder(conn, "task-a")
            with conn:
                conn.execute(
                    "UPDATE reminders SET status = 'sent', sent_at = ? WHERE id = ?",
                    ("2026-07-21T16:00:00+00:00", reminder.id),
                )
                conn.execute(
                    "UPDATE items SET status = 'deleted', deleted_at = ? WHERE id = 'task-a'",
                    ("2026-07-21T16:01:00+00:00",),
                )

            fetched = get_reminder_by_id(conn, user_id="user-a", reminder_id=reminder.id)
            with self.assertRaises(ReminderNotFoundError) as cross_owner_error:
                get_reminder_by_id(conn, user_id="user-b", reminder_id=reminder.id)

        self.assertEqual(fetched.status, "sent")
        self.assertEqual(cross_owner_error.exception.code, "reminder_not_found")

    def test_timestamp_validation_rejects_non_future_and_naive_values_without_inserting(self) -> None:
        cases = (
            (datetime(2026, 7, 21, 16, 0), "invalid_scheduled_at"),
            (NOW, "reminder_time_not_future"),
            (NOW - timedelta(seconds=1), "reminder_time_not_future"),
            (NOW + timedelta(microseconds=1), "reminder_time_not_future"),
        )
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a")
            for scheduled_at, code in cases:
                with self.subTest(scheduled_at=scheduled_at):
                    with self.assertRaises(ReminderValidationError) as error:
                        create_reminder(conn, user_id="user-a", task_id="task-a", scheduled_at=scheduled_at, now=NOW)
                    self.assertEqual(error.exception.code, code)
            count = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]

        self.assertEqual(count, 0)

    def test_task_eligibility_hides_missing_notes_and_cross_owner_tasks(self) -> None:
        invalid_tasks = (
            ("missing", "user-a", "task_not_found"),
            ("note-a", "user-a", "task_not_found"),
            ("task-b", "user-a", "task_not_found"),
            ("completed", "user-a", "task_not_active"),
            ("archived", "user-a", "task_not_active"),
            ("deleted", "user-a", "task_not_active"),
        )
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a")
            self._insert_task(conn, "task-b", "user-b")
            self._insert_note(conn, "note-a", "user-a")
            for task_id, status in (("completed", "completed"), ("archived", "archived"), ("deleted", "deleted")):
                self._insert_task(conn, task_id, "user-a", status=status, deleted_at="2026-07-21T14:00:00+00:00" if task_id == "deleted" else None)

            for task_id, user_id, code in invalid_tasks:
                with self.subTest(task_id=task_id):
                    with self.assertRaises((ReminderNotFoundError, ReminderValidationError)) as error:
                        self._create_reminder(conn, task_id, user_id=user_id)
                    self.assertEqual(error.exception.code, code)
            count = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]

        self.assertEqual(count, 0)

    def test_pending_list_is_owner_scoped_and_deterministically_sorted(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a")
            self._insert_task(conn, "task-other", "user-a")
            self._insert_task(conn, "task-empty", "user-a")
            self._insert_task(conn, "task-b", "user-b")
            self._insert_reminder(conn, "z-last", "task-a", "2026-07-21T18:00:00+00:00")
            self._insert_reminder(conn, "b-second", "task-a", "2026-07-21T17:00:00+00:00")
            self._insert_reminder(conn, "a-first", "task-a", "2026-07-21T17:00:00+00:00", status="sent", sent_at="2026-07-21T17:00:00+00:00")
            self._insert_reminder(conn, "other-task", "task-other", "2026-07-21T17:00:00+00:00")
            self._insert_reminder(conn, "other-user", "task-b", "2026-07-21T17:00:00+00:00")

            reminders = list_pending_reminders_for_task(conn, user_id="user-a", task_id="task-a")
            self.assertEqual(list_pending_reminders_for_task(conn, user_id="user-a", task_id="task-empty"), ())
            with self.assertRaises(ReminderNotFoundError):
                list_pending_reminders_for_task(conn, user_id="user-a", task_id="task-b")

        self.assertEqual([reminder.id for reminder in reminders], ["b-second", "z-last"])

    def test_pending_list_allows_owned_inactive_tasks_for_later_cleanup(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a", status="completed")
            self._insert_reminder(conn, "pending", "task-a", "2026-07-21T18:00:00+00:00")

            reminders = list_pending_reminders_for_task(conn, user_id="user-a", task_id="task-a")

        self.assertEqual([reminder.id for reminder in reminders], ["pending"])

    def test_active_duplicates_are_translated_but_terminal_replacements_are_allowed(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a")
            first = self._create_reminder(conn, "task-a")
            with self.assertRaises(DuplicateReminderError) as duplicate_error:
                self._create_reminder(conn, "task-a")
            with conn:
                conn.execute("UPDATE reminders SET status = 'processing' WHERE id = ?", (first.id,))
            with self.assertRaises(DuplicateReminderError):
                self._create_reminder(conn, "task-a")
            with conn:
                conn.execute(
                    "UPDATE reminders SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
                    ("2026-07-21T15:30:00+00:00", first.id),
                )
            replacement = self._create_reminder(conn, "task-a")

        self.assertEqual(duplicate_error.exception.code, "duplicate_active_reminder")
        self.assertNotEqual(replacement.id, first.id)

    def test_unrelated_integrity_errors_are_not_mislabeled_as_duplicates(self) -> None:
        with self.open_database() as conn:
            self._insert_task(conn, "task-a", "user-a")
            with self.assertRaises(sqlite3.IntegrityError):
                with conn:
                    conn.execute(
                        "INSERT INTO reminders (id, item_id, scheduled_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        ("invalid", "task-a", "2026-07-21T18:00:00+00:00", "", "2026-07-21T15:00:00+00:00"),
                    )

    def _create_reminder(self, conn, task_id, *, user_id="user-a"):
        return create_reminder(
            conn,
            user_id=user_id,
            task_id=task_id,
            scheduled_at=NOW + timedelta(hours=1),
            now=NOW,
        )

    def _insert_user(self, conn, user_id, telegram_user_id):
        with conn:
            conn.execute(
                "INSERT INTO users (id, telegram_user_id, timezone) VALUES (?, ?, 'America/Chicago')",
                (user_id, telegram_user_id),
            )

    def _insert_task(self, conn, task_id, user_id, *, status="active", deleted_at=None):
        self._insert_item(conn, task_id, user_id, "task", status=status, deleted_at=deleted_at)
        with conn:
            conn.execute("INSERT INTO task_items (item_id) VALUES (?)", (task_id,))

    def _insert_note(self, conn, note_id, user_id):
        self._insert_item(conn, note_id, user_id, "note")
        with conn:
            conn.execute("INSERT INTO note_items (item_id) VALUES (?)", (note_id,))

    def _insert_item(self, conn, item_id, user_id, item_type, *, status="active", deleted_at=None):
        reference_prefix = "T" if item_type == "task" else "N"
        public_reference = f"{reference_prefix}{sum(ord(character) for character in item_id)}"
        with conn:
            conn.execute(
                """
                INSERT INTO items (
                    id, user_id, item_type, pub_ref, title, status, source,
                    parse_status, created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'manual_entry', 'not_applicable', ?, ?, ?)
                """,
                (item_id, user_id, item_type, public_reference, item_id, status, NOW.isoformat(), NOW.isoformat(), deleted_at),
            )

    def _insert_reminder(self, conn, reminder_id, task_id, scheduled_at, *, status="pending", sent_at=None):
        with conn:
            conn.execute(
                """
                INSERT INTO reminders (
                    id, item_id, scheduled_at, status, delivery_channel, retry_count,
                    sent_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'telegram', 0, ?, ?, ?)
                """,
                (reminder_id, task_id, scheduled_at, status, sent_at, NOW.isoformat(), NOW.isoformat()),
            )


if __name__ == "__main__":
    unittest.main()
