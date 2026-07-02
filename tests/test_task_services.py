from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.tasks import (
    TaskNotFoundError,
    TaskValidationError,
    complete_task,
    create_note,
    create_task,
    get_task_details,
    list_active_tasks,
    list_categories_and_tags,
    reopen_task,
    soft_delete_task,
    update_task_fields,
)
from tele_secretary.persistence.migrations import apply_migrations


FIXED_NOW = "2026-07-02T01:00:00+00:00"


class TaskServiceTests(unittest.TestCase):
    def test_create_get_and_list_minimal_task(self) -> None:
        with self.open_seeded_database() as conn:
            task = create_task(
                conn,
                user_id="user-a",
                title="  Email professor  ",
                source="manual_entry",
            )

            fetched_task = get_task_details(conn, user_id="user-a", task_id=task.id)
            active_tasks = list_active_tasks(conn, user_id="user-a")

        self.assertEqual(task.title, "Email professor")
        self.assertEqual(task.status, "active")
        self.assertEqual(task.parse_status, "not_applicable")
        self.assertIsNone(task.completed_at)
        self.assertEqual(fetched_task, task)
        self.assertEqual(active_tasks, (task,))
        self.assertTrue(task.created_at.endswith("+00:00"))
        self.assertTrue(task.updated_at.endswith("+00:00"))

    def test_create_task_with_optional_fields_category_and_tags(self) -> None:
        deadline = datetime(2026, 7, 10, 22, 0, tzinfo=timezone.utc)
        planned_start = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)
        planned_end = datetime(2026, 7, 8, 17, 0, tzinfo=timezone.utc)

        with self.open_seeded_database() as conn:
            self.insert_category(conn, "cat-school", "user-a", "school")
            self.insert_tag(conn, "tag-email", "user-a", "email")
            self.insert_tag(conn, "tag-admin", "user-a", "admin")

            task = create_task(
                conn,
                user_id="user-a",
                title="Submit assignment",
                source="telegram_nl",
                description="Finish and upload the final PDF.",
                category_id="cat-school",
                deadline_at=deadline,
                deadline_type="hard",
                planned_start_at=planned_start,
                planned_end_at=planned_end,
                estimated_minutes=90,
                urgency="high",
                raw_input_text="Need to submit assignment by Friday night",
                parse_status="parsed",
                parse_confidence=0.9,
                tag_ids=("tag-email", "tag-admin", "tag-email"),
            )

        self.assertEqual(task.category_name, "school")
        self.assertEqual(task.deadline_at, "2026-07-10T22:00:00+00:00")
        self.assertEqual(task.planned_start_at, "2026-07-08T15:00:00+00:00")
        self.assertEqual(task.planned_end_at, "2026-07-08T17:00:00+00:00")
        self.assertEqual(task.estimated_minutes, 90)
        self.assertEqual(task.urgency, "high")
        self.assertEqual(task.parse_confidence, 0.9)
        self.assertEqual([tag.name for tag in task.tags], ["admin", "email"])

    def test_list_categories_and_tags_excludes_archived_categories_by_default(self) -> None:
        with self.open_seeded_database() as conn:
            self.insert_category(conn, "cat-work", "user-a", "work")
            self.insert_category(conn, "cat-old", "user-a", "old", archived_at=FIXED_NOW)
            self.insert_tag(conn, "tag-email", "user-a", "email")
            self.insert_tag(conn, "tag-admin", "user-a", "admin")

            active_vocabulary = list_categories_and_tags(conn, user_id="user-a")
            full_vocabulary = list_categories_and_tags(
                conn,
                user_id="user-a",
                include_archived_categories=True,
            )

        self.assertEqual([category.name for category in active_vocabulary.categories], ["work"])
        self.assertEqual([category.name for category in full_vocabulary.categories], ["old", "work"])
        self.assertEqual([tag.name for tag in active_vocabulary.tags], ["admin", "email"])

    def test_update_task_fields_and_tags(self) -> None:
        planned_start = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)
        planned_end = datetime(2026, 7, 8, 16, 0, tzinfo=timezone.utc)

        with self.open_seeded_database() as conn:
            self.insert_category(conn, "cat-work", "user-a", "work")
            self.insert_tag(conn, "tag-admin", "user-a", "admin")
            task = create_task(
                conn,
                user_id="user-a",
                title="Draft memo",
                source="manual_entry",
            )

            updated_task = update_task_fields(
                conn,
                user_id="user-a",
                task_id=task.id,
                source="manual_entry",
                title="Draft board memo",
                description="Send to Alex for review.",
                category_id="cat-work",
                planned_start_at=planned_start,
                planned_end_at=planned_end,
                estimated_minutes=45,
                urgency="medium",
                tag_ids=("tag-admin",),
            )

        self.assertEqual(updated_task.title, "Draft board memo")
        self.assertEqual(updated_task.description, "Send to Alex for review.")
        self.assertEqual(updated_task.category_name, "work")
        self.assertEqual(updated_task.planned_start_at, "2026-07-08T15:00:00+00:00")
        self.assertEqual(updated_task.planned_end_at, "2026-07-08T16:00:00+00:00")
        self.assertEqual(updated_task.estimated_minutes, 45)
        self.assertEqual(updated_task.urgency, "medium")
        self.assertEqual([tag.id for tag in updated_task.tags], ["tag-admin"])

    def test_update_validates_complete_resulting_task_state(self) -> None:
        with self.open_seeded_database() as conn:
            task = create_task(
                conn,
                user_id="user-a",
                title="Plan study block",
                source="manual_entry",
                planned_start_at=datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc),
                planned_end_at=datetime(2026, 7, 8, 17, 0, tzinfo=timezone.utc),
            )

            with self.assertRaises(TaskValidationError) as error:
                update_task_fields(
                    conn,
                    user_id="user-a",
                    task_id=task.id,
                    source="manual_entry",
                    planned_end_at=datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc),
                )

        self.assertEqual(error.exception.code, "invalid_planned_window")

    def test_complete_and_reopen_task_write_completion_logs(self) -> None:
        completed_at = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        reopened_at = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

        with self.open_seeded_database() as conn:
            task = create_task(
                conn,
                user_id="user-a",
                title="Pay rent",
                source="manual_entry",
            )
            completed_task = complete_task(
                conn,
                user_id="user-a",
                task_id=task.id,
                source="manual_entry",
                completed_at=completed_at,
            )
            reopened_task = reopen_task(
                conn,
                user_id="user-a",
                task_id=task.id,
                source="manual_entry",
                reopened_at=reopened_at,
            )
            completion_logs = conn.execute(
                """
                SELECT event_type, occurred_at
                FROM completion_logs
                WHERE item_id = ?
                ORDER BY occurred_at
                """,
                (task.id,),
            ).fetchall()

        self.assertEqual(completed_task.status, "completed")
        self.assertEqual(completed_task.completed_at, "2026-07-03T12:00:00+00:00")
        self.assertEqual(reopened_task.status, "active")
        self.assertIsNone(reopened_task.completed_at)
        self.assertEqual(
            [(row["event_type"], row["occurred_at"]) for row in completion_logs],
            [
                ("completed", "2026-07-03T12:00:00+00:00"),
                ("reopened", "2026-07-04T12:00:00+00:00"),
            ],
        )

    def test_completion_and_reopen_reject_invalid_transitions(self) -> None:
        with self.open_seeded_database() as conn:
            task = create_task(
                conn,
                user_id="user-a",
                title="One-shot task",
                source="manual_entry",
            )

            with self.assertRaises(TaskValidationError) as reopen_error:
                reopen_task(
                    conn,
                    user_id="user-a",
                    task_id=task.id,
                    source="manual_entry",
                )

            complete_task(
                conn,
                user_id="user-a",
                task_id=task.id,
                source="manual_entry",
            )

            with self.assertRaises(TaskValidationError) as complete_error:
                complete_task(
                    conn,
                    user_id="user-a",
                    task_id=task.id,
                    source="manual_entry",
                )

        self.assertEqual(reopen_error.exception.code, "invalid_reopen_transition")
        self.assertEqual(complete_error.exception.code, "invalid_completion_transition")

    def test_soft_delete_hides_task_but_keeps_details_available_when_requested(self) -> None:
        with self.open_seeded_database() as conn:
            task = create_task(
                conn,
                user_id="user-a",
                title="Archive me",
                source="manual_entry",
            )
            delete_result = soft_delete_task(
                conn,
                user_id="user-a",
                task_id=task.id,
                source="manual_entry",
                deleted_at=datetime(2026, 7, 5, 9, 0, tzinfo=timezone.utc),
            )
            active_tasks = list_active_tasks(conn, user_id="user-a")
            deleted_task = get_task_details(
                conn,
                user_id="user-a",
                task_id=task.id,
                include_deleted=True,
            )

            with self.assertRaises(TaskNotFoundError):
                get_task_details(conn, user_id="user-a", task_id=task.id)

        self.assertEqual(delete_result.deleted_at, "2026-07-05T09:00:00+00:00")
        self.assertEqual(active_tasks, ())
        self.assertEqual(deleted_task.status, "deleted")
        self.assertEqual(deleted_task.deleted_at, "2026-07-05T09:00:00+00:00")

    def test_create_note_plumbing(self) -> None:
        with self.open_seeded_database() as conn:
            note = create_note(
                conn,
                user_id="user-a",
                title="Possible project idea",
                body="Could turn this into a note workflow later.",
                source="manual_entry",
                raw_input_text="random thought",
                parse_status="fallback",
            )

        self.assertEqual(note.title, "Possible project idea")
        self.assertEqual(note.body, "Could turn this into a note workflow later.")
        self.assertEqual(note.parse_status, "fallback")

    def test_validation_edges(self) -> None:
        with self.open_seeded_database() as conn:
            self.insert_tag(conn, "tag-other", "user-b", "other")
            invalid_cases = (
                (
                    "invalid_title",
                    lambda: create_task(
                        conn,
                        user_id="user-a",
                        title="   ",
                        source="manual_entry",
                    ),
                ),
                (
                    "invalid_source",
                    lambda: create_task(
                        conn,
                        user_id="user-a",
                        title="Bad source",
                        source="llm_parse",
                    ),
                ),
                (
                    "deadline_type_without_deadline",
                    lambda: create_task(
                        conn,
                        user_id="user-a",
                        title="Bad deadline",
                        source="manual_entry",
                        deadline_type="hard",
                    ),
                ),
                (
                    "invalid_estimated_minutes",
                    lambda: create_task(
                        conn,
                        user_id="user-a",
                        title="Bad estimate",
                        source="manual_entry",
                        estimated_minutes=0,
                    ),
                ),
                (
                    "invalid_tags",
                    lambda: create_task(
                        conn,
                        user_id="user-a",
                        title="Bad tag",
                        source="manual_entry",
                        tag_ids=("tag-other",),
                    ),
                ),
            )

            for expected_code, call in invalid_cases:
                with self.subTest(expected_code=expected_code):
                    with self.assertRaises(TaskValidationError) as error:
                        call()
                    self.assertEqual(error.exception.code, expected_code)

    def open_seeded_database(self):
        return seeded_database()

    def insert_category(
        self,
        conn,
        category_id: str,
        user_id: str,
        name: str,
        archived_at: str | None = None,
    ) -> None:
        with conn:
            conn.execute(
                """
                INSERT INTO categories (id, user_id, name, created_at, archived_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (category_id, user_id, name, FIXED_NOW, archived_at),
            )

    def insert_tag(self, conn, tag_id: str, user_id: str, name: str) -> None:
        with conn:
            conn.execute(
                """
                INSERT INTO tags (id, user_id, name, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (tag_id, user_id, name, FIXED_NOW),
            )


@contextmanager
def seeded_database():
    temp_dir = tempfile.TemporaryDirectory()
    db_path = Path(temp_dir.name) / "secretary.sqlite3"
    database_context = open_test_database(db_path)
    conn = database_context.__enter__()
    try:
        apply_migrations(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO users (id, telegram_user_id, timezone)
                VALUES (?, ?, ?), (?, ?, ?)
                """,
                (
                    "user-a",
                    1001,
                    "America/Chicago",
                    "user-b",
                    1002,
                    "America/Chicago",
                ),
            )
        yield conn
    finally:
        database_context.__exit__(None, None, None)
        temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
