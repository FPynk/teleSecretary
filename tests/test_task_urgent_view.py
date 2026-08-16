from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.tasks import create_task, list_urgent_tasks
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.responses import (
    build_urgent_tasks_response,
    build_urgent_usage_response,
)


class UrgentTaskViewTests(unittest.TestCase):
    @contextmanager
    def open_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with open_test_database(Path(temp_dir) / "secretary.sqlite3") as conn:
                apply_migrations(conn)
                yield conn

    def test_urgent_tasks_are_owner_scoped_and_ordered_by_urgency_then_numeric_ref(self) -> None:
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
            create_task(
                conn,
                user_id=user_a,
                title="Medium task",
                source="manual_entry",
                urgency="medium",
            )
            early_high = create_task(
                conn,
                user_id=user_a,
                title="Early high task",
                source="manual_entry",
                urgency="high",
            )
            for index in range(3, 10):
                create_task(
                    conn,
                    user_id=user_a,
                    title=f"Low task {index}",
                    source="manual_entry",
                    urgency="low",
                )
            top_priority = create_task(
                conn,
                user_id=user_a,
                title="Top priority task",
                source="manual_entry",
                urgency="top_priority",
            )
            later_high = create_task(
                conn,
                user_id=user_a,
                title="Later high task",
                source="manual_entry",
                urgency="high",
            )
            completed = create_task(
                conn,
                user_id=user_a,
                title="Completed high task",
                source="manual_entry",
                urgency="high",
            )
            archived = create_task(
                conn,
                user_id=user_a,
                title="Archived high task",
                source="manual_entry",
                urgency="high",
            )
            deleted = create_task(
                conn,
                user_id=user_a,
                title="Deleted high task",
                source="manual_entry",
                urgency="high",
            )
            foreign = create_task(
                conn,
                user_id=user_b,
                title="Foreign high task",
                source="manual_entry",
                urgency="high",
            )
            with conn:
                conn.execute("UPDATE items SET status = 'completed' WHERE id = ?", (completed.id,))
                conn.execute("UPDATE items SET status = 'archived' WHERE id = ?", (archived.id,))
                conn.execute(
                    "UPDATE items SET status = 'deleted', deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (deleted.id,),
                )

            urgent_tasks = list_urgent_tasks(conn, user_id=user_a)

        self.assertEqual(
            [(task.ref, task.urgency) for task in urgent_tasks],
            [
                (top_priority.ref, "top_priority"),
                (early_high.ref, "high"),
                (later_high.ref, "high"),
            ],
        )
        self.assertEqual((top_priority.ref, early_high.ref, later_high.ref), ("T10", "T2", "T11"))
        self.assertEqual(foreign.ref, "T1")

    def test_urgent_responses_are_deterministic_and_have_an_empty_state(self) -> None:
        with self.open_database() as conn:
            user_id = get_or_create_telegram_user_id(
                conn,
                telegram_user_id=1001,
                timezone="America/Chicago",
            )
            task = create_task(
                conn,
                user_id=user_id,
                title="Call bank",
                source="manual_entry",
                urgency="high",
            )
            urgent_tasks = list_urgent_tasks(conn, user_id=user_id)

        self.assertEqual(build_urgent_usage_response(), "Usage: /urgent")
        self.assertEqual(
            build_urgent_tasks_response(urgent_tasks),
            f"Urgent tasks:\n{task.ref} â€” Call bank â€” high",
        )
        self.assertEqual(build_urgent_tasks_response(()), "No urgent tasks.")


if __name__ == "__main__":
    unittest.main()
