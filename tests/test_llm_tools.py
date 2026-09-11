"""Tests for bounded LLM tool adapters."""

from __future__ import annotations

from contextlib import contextmanager
import tempfile
import unittest
from pathlib import Path
from sqlite3 import Connection
from typing import Iterator

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.tasks import TaskValidationError, list_active_tasks
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.llm.tools import create_task_tool
from tele_secretary.persistence.migrations import apply_migrations

# TODO: Update the tests to take into account the new category function, for all of the pre-existing ones just have goldflow
# Add 2 more failing ones with invalid category inputs
class CreateTaskToolTests(unittest.TestCase):
    """Verify that LLM task proposals remain safely owner-scoped."""

    def test_creates_task_with_allowed_llm_fields(self) -> None:
        """The tool passes permitted fields to the task service."""
        with self.open_seeded_database() as (conn, owner_user_id, _):
            task = create_task_tool(
                conn,
                user_id=owner_user_id,
                title="Submit expense report",
                description="Include the client dinner receipt.",
                deadline_at="2026-09-10T14:30:00-05:00",
                deadline_type="hard",
                estimated_minutes=30,
                urgency="high",
            )

            self.assertEqual(task.title, "Submit expense report")
            self.assertEqual(task.description, "Include the client dinner receipt.")
            self.assertEqual(task.source, "telegram_nl")
            self.assertEqual(task.deadline_at, "2026-09-10T19:30:00+00:00")
            self.assertEqual(task.deadline_type, "hard")
            self.assertEqual(task.estimated_minutes, 30)
            self.assertEqual(task.urgency, "high")

    def test_rejects_blank_title_using_task_service_validation(self) -> None:
        """Blank titles remain subject to the canonical task validation."""
        with self.open_seeded_database() as (conn, owner_user_id, _):
            with self.assertRaises(TaskValidationError) as raised:
                create_task_tool(conn, user_id=owner_user_id, title="   ")

            self.assertEqual(raised.exception.code, "invalid_title")
            self.assertEqual(list_active_tasks(conn, user_id=owner_user_id), ())

    def test_rejects_deadline_without_timezone_offset(self) -> None:
        """The LLM must supply a deadline that is unambiguous in time."""
        with self.open_seeded_database() as (conn, owner_user_id, _):
            with self.assertRaises(TaskValidationError) as raised:
                create_task_tool(
                    conn,
                    user_id=owner_user_id,
                    title="Schedule dentist",
                    deadline_at="2026-09-10T14:30:00",
                )

            self.assertEqual(raised.exception.code, "invalid_deadline_at")
            self.assertEqual(list_active_tasks(conn, user_id=owner_user_id), ())

    def test_delegates_estimate_and_urgency_rules_to_task_service(self) -> None:
        """Application task rules remain the sole authority for task metadata."""
        with self.open_seeded_database() as (conn, owner_user_id, _):
            with self.assertRaises(TaskValidationError) as estimate_error:
                create_task_tool(
                    conn,
                    user_id=owner_user_id,
                    title="Prepare slides",
                    estimated_minutes=0,
                )
            with self.assertRaises(TaskValidationError) as urgency_error:
                create_task_tool(
                    conn,
                    user_id=owner_user_id,
                    title="Prepare slides",
                    urgency="critical",
                )

            self.assertEqual(estimate_error.exception.code, "invalid_estimated_minutes")
            self.assertEqual(urgency_error.exception.code, "invalid_urgency")

    def test_creates_tasks_only_for_the_trusted_owner_argument(self) -> None:
        """The tool does not expose any model-controlled owner field."""
        with self.open_seeded_database() as (conn, owner_user_id, other_user_id):
            owner_task = create_task_tool(
                conn,
                user_id=owner_user_id,
                title="Owner task",
            )
            other_task = create_task_tool(
                conn,
                user_id=other_user_id,
                title="Other task",
            )

            self.assertEqual(
                [task.id for task in list_active_tasks(conn, user_id=owner_user_id)],
                [owner_task.id],
            )
            self.assertEqual(
                [task.id for task in list_active_tasks(conn, user_id=other_user_id)],
                [other_task.id],
            )

    def test_does_not_accept_model_selected_task_source(self) -> None:
        """The tool fixes source to the Telegram natural-language flow."""
        with self.open_seeded_database() as (conn, owner_user_id, _):
            with self.assertRaises(TypeError):
                create_task_tool(
                    conn,
                    user_id=owner_user_id,
                    title="Buy printer paper",
                    source="manual_entry",  # type: ignore[call-arg]
                )

    @contextmanager
    def open_seeded_database(self) -> Iterator[tuple[Connection, str, str]]:
        """Yield a migrated database and two independent Telegram users."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "tele_secretary.db"
            with open_test_database(database_path) as conn:
                apply_migrations(conn)
                owner_user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone="America/Chicago",
                )
                other_user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1002,
                    timezone="America/Chicago",
                )
                yield conn, owner_user_id, other_user_id
