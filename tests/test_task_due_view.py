from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.tasks import (
    create_task,
    list_due_tasks,
)
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.responses import (
    build_due_tasks_response,
    build_due_usage_response,
)


NOW = datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc)


class DueTaskViewTests(unittest.TestCase):
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with open_test_database(Path(temp_dir) / "secretary.sqlite3") as conn:
                apply_migrations(conn)
                yield conn

    def test_due_tasks_classify_deadlines_sort_by_time_and_exclude_ineligible_rows(self) -> None:
        with self.open_database() as conn:
            user_a = get_or_create_telegram_user_id(
                conn,
                telegram_user_id=1001,
                timezone="America/Chicago",
            )
            user_b = get_or_create_telegram_user_id(
                conn,
                telegram_user_id=2002,
                timezone="America/Chicago",
            )
            overdue = create_task(
                conn,
                user_id=user_a,
                title="Overdue hard deadline",
                source="manual_entry",
                deadline_at=datetime(2026, 8, 15, 18, 0, tzinfo=timezone.utc),
                deadline_type="hard",
            )
            due_now = create_task(
                conn,
                user_id=user_a,
                title="Due exactly now",
                source="manual_entry",
                deadline_at=NOW,
                deadline_type="soft",
            )
            for index in range(3, 10):
                create_task(
                    conn,
                    user_id=user_a,
                    title=f"Undated {index}",
                    source="manual_entry",
                )
            same_time = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)
            second_ref = create_task(
                conn,
                user_id=user_a,
                title="Earlier reference",
                source="manual_entry",
                deadline_at=same_time,
                deadline_type="hard",
            )
            tenth_ref = create_task(
                conn,
                user_id=user_a,
                title="Later reference",
                source="manual_entry",
                deadline_at=same_time,
                deadline_type="soft",
            )
            excluded_completed = create_task(
                conn,
                user_id=user_a,
                title="Completed",
                source="manual_entry",
                deadline_at=datetime(2026, 8, 17, 18, 0, tzinfo=timezone.utc),
                deadline_type="hard",
            )
            excluded_archived = create_task(
                conn,
                user_id=user_a,
                title="Archived",
                source="manual_entry",
                deadline_at=datetime(2026, 8, 17, 18, 0, tzinfo=timezone.utc),
                deadline_type="hard",
            )
            excluded_deleted = create_task(
                conn,
                user_id=user_a,
                title="Deleted",
                source="manual_entry",
                deadline_at=datetime(2026, 8, 17, 18, 0, tzinfo=timezone.utc),
                deadline_type="hard",
            )
            foreign_task = create_task(
                conn,
                user_id=user_b,
                title="Other owner",
                source="manual_entry",
                deadline_at=datetime(2026, 8, 17, 18, 0, tzinfo=timezone.utc),
                deadline_type="hard",
            )
            with conn:
                conn.execute("UPDATE items SET status = 'completed' WHERE id = ?", (excluded_completed.id,))
                conn.execute("UPDATE items SET status = 'archived' WHERE id = ?", (excluded_archived.id,))
                conn.execute(
                    "UPDATE items SET status = 'deleted', deleted_at = ? WHERE id = ?",
                    (NOW.isoformat(), excluded_deleted.id),
                )

            due_tasks = list_due_tasks(
                conn,
                user_id=user_a,
                timezone_name="America/Chicago",
                now=NOW,
            )

        self.assertEqual(
            [(due_task.task.ref, due_task.timing) for due_task in due_tasks],
            [
                (overdue.ref, "overdue"),
                (due_now.ref, "due today"),
                (second_ref.ref, "upcoming"),
                (tenth_ref.ref, "upcoming"),
            ],
        )
        self.assertEqual((second_ref.ref, tenth_ref.ref), ("T10", "T11"))
        self.assertEqual(foreign_task.ref, "T1")

    def test_due_window_uses_local_calendar_boundaries_across_daylight_saving_time(self) -> None:
        now = datetime(2026, 3, 7, 18, 0, tzinfo=timezone.utc)
        with self.open_database() as conn:
            user_id = get_or_create_telegram_user_id(
                conn,
                telegram_user_id=1001,
                timezone="America/Chicago",
            )
            included = create_task(
                conn,
                user_id=user_id,
                title="Sunday before the boundary",
                source="manual_entry",
                deadline_at=datetime(2026, 3, 15, 4, 59, tzinfo=timezone.utc),
                deadline_type="hard",
            )
            excluded = create_task(
                conn,
                user_id=user_id,
                title="Exact window boundary",
                source="manual_entry",
                deadline_at=datetime(2026, 3, 15, 5, 0, tzinfo=timezone.utc),
                deadline_type="hard",
            )

            due_tasks = list_due_tasks(
                conn,
                user_id=user_id,
                timezone_name="America/Chicago",
                now=now,
            )

        self.assertEqual([due_task.task.ref for due_task in due_tasks], [included.ref])
        self.assertNotEqual(included.ref, excluded.ref)

    def test_due_responses_are_localized_and_empty_results_are_explicit(self) -> None:
        with self.open_database() as conn:
            user_id = get_or_create_telegram_user_id(
                conn,
                telegram_user_id=1001,
                timezone="America/Chicago",
            )
            create_task(
                conn,
                user_id=user_id,
                title="Submit assignment",
                source="manual_entry",
                deadline_at=datetime(2026, 8, 16, 14, 0, tzinfo=timezone.utc),
                deadline_type="hard",
            )
            due_tasks = list_due_tasks(
                conn,
                user_id=user_id,
                timezone_name="America/Chicago",
                now=NOW,
            )

        self.assertEqual(build_due_usage_response(), "Usage: /due")
        self.assertEqual(
            build_due_tasks_response(due_tasks, "America/Chicago"),
            "Due tasks:\nT1 â€” Submit assignment â€” overdue â€” Sun Aug 16, 2026 at 9:00 AM",
        )
        self.assertEqual(
            build_due_tasks_response((), "America/Chicago"),
            "No overdue or upcoming tasks.",
        )


if __name__ == "__main__":
    unittest.main()
