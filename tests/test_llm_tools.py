"""Tests for bounded LLM tool adapters."""

from __future__ import annotations

from contextlib import contextmanager
import inspect
import tempfile
import unittest
from pathlib import Path
from sqlite3 import Connection
from typing import Iterator

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.tasks import TaskValidationError, list_active_tasks
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.llm.tools import (
    CREATE_TASK_TOOL_NAME,
    create_task_tool,
    dispatch_tool_call,
    get_tool_definitions,
)
from tele_secretary.persistence.migrations import apply_migrations

class CreateTaskToolTests(unittest.TestCase):
    """Verify that LLM task proposals remain safely owner-scoped."""

    def test_creates_task_with_allowed_llm_fields(self) -> None:
        """The tool passes permitted fields to the task service."""
        with self.open_seeded_database() as (conn, owner_user_id, _):
            self.insert_category(
                conn,
                category_id="category-owner-work",
                user_id=owner_user_id,
                name="Work",
            )
            task = create_task_tool(
                conn,
                user_id=owner_user_id,
                title="Submit expense report",
                description="Include the client dinner receipt.",
                deadline_at="2026-09-10T14:30:00-05:00",
                deadline_type="hard",
                estimated_minutes=30,
                urgency="high",
                category_name="Work",
                parse_confidence=0.5,
            )

            self.assertEqual(task.title, "Submit expense report")
            self.assertEqual(task.description, "Include the client dinner receipt.")
            self.assertEqual(task.source, "telegram_nl")
            self.assertEqual(task.deadline_at, "2026-09-10T19:30:00+00:00")
            self.assertEqual(task.deadline_type, "hard")
            self.assertEqual(task.estimated_minutes, 30)
            self.assertEqual(task.urgency, "high")
            self.assertEqual(task.category_id, "category-owner-work")
            self.assertEqual(task.category_name, "Work")
            self.assertEqual(task.parse_status, "parsed")
            self.assertEqual(task.parse_confidence, 0.5)

    def test_rejects_blank_title_using_task_service_validation(self) -> None:
        """Blank titles remain subject to the canonical task validation."""
        with self.open_seeded_database() as (conn, owner_user_id, _):
            with self.assertRaises(TaskValidationError) as raised:
                create_task_tool(
                    conn,
                    user_id=owner_user_id,
                    title="   ",
                    parse_confidence=1.0,
                )

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
                    parse_confidence=0.5,
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
                    parse_confidence=0.5,
                )
            with self.assertRaises(TaskValidationError) as urgency_error:
                create_task_tool(
                    conn,
                    user_id=owner_user_id,
                    title="Prepare slides",
                    urgency="critical",
                    parse_confidence=0.5,
                )

            self.assertEqual(estimate_error.exception.code, "invalid_estimated_minutes")
            self.assertEqual(urgency_error.exception.code, "invalid_urgency")

    def test_accepts_valid_parse_confidence_values(self) -> None:
        """Boundary and representative confidence values persist unchanged."""
        with self.open_seeded_database() as (conn, owner_user_id, _):
            for parse_confidence in (0.0, 0.5, 1.0):
                with self.subTest(parse_confidence=parse_confidence):
                    task = create_task_tool(
                        conn,
                        user_id=owner_user_id,
                        title=f"Confidence {parse_confidence}",
                        parse_confidence=parse_confidence,
                    )

                    self.assertEqual(task.parse_status, "parsed")
                    self.assertEqual(task.parse_confidence, parse_confidence)

    def test_accepts_missing_parse_confidence(self) -> None:
        """A successful LLM proposal may omit diagnostic confidence."""
        with self.open_seeded_database() as (conn, owner_user_id, _):
            task = create_task_tool(
                conn,
                user_id=owner_user_id,
                title="Task without confidence",
            )

            self.assertEqual(task.parse_status, "parsed")
            self.assertIsNone(task.parse_confidence)

    def test_rejects_invalid_parse_confidence_values(self) -> None:
        """Invalid confidence cannot make a task write succeed."""
        with self.open_seeded_database() as (conn, owner_user_id, _):
            for parse_confidence in (-0.1, 1.1, True, "0.5"):
                with self.subTest(parse_confidence=parse_confidence):
                    with self.assertRaises(TaskValidationError) as raised:
                        create_task_tool(
                            conn,
                            user_id=owner_user_id,
                            title="Invalid confidence",
                            parse_confidence=parse_confidence,  # type: ignore[arg-type]
                        )

                    self.assertEqual(raised.exception.code, "invalid_parse_confidence")
                    self.assertEqual(list_active_tasks(conn, user_id=owner_user_id), ())

    def test_rejects_an_unknown_category_name(self) -> None:
        """The model cannot create a category by proposing an unknown name."""
        with self.open_seeded_database() as (conn, owner_user_id, _):
            self.insert_category(
                conn,
                category_id="category-owner-work",
                user_id=owner_user_id,
                name="Work",
            )

            with self.assertRaises(TaskValidationError) as raised:
                create_task_tool(
                    conn,
                    user_id=owner_user_id,
                    title="Send expense report",
                    category_name="Finance",
                    parse_confidence=0.5,
                )

            self.assertEqual(raised.exception.code, "invalid_category")
            self.assertEqual(list_active_tasks(conn, user_id=owner_user_id), ())

    def test_rejects_a_category_owned_by_another_user(self) -> None:
        """The model cannot attach another owner's category to this task."""
        with self.open_seeded_database() as (conn, owner_user_id, other_user_id):
            self.insert_category(
                conn,
                category_id="category-other-work",
                user_id=other_user_id,
                name="Work",
            )

            with self.assertRaises(TaskValidationError) as raised:
                create_task_tool(
                    conn,
                    user_id=owner_user_id,
                    title="Prepare quarterly plan",
                    category_name="Work",
                    parse_confidence=0.5,
                )

            self.assertEqual(raised.exception.code, "invalid_category")
            self.assertEqual(list_active_tasks(conn, user_id=owner_user_id), ())

    def test_creates_tasks_only_for_the_trusted_owner_argument(self) -> None:
        """The tool does not expose any model-controlled owner field."""
        with self.open_seeded_database() as (conn, owner_user_id, other_user_id):
            owner_task = create_task_tool(
                conn,
                user_id=owner_user_id,
                title="Owner task",
                parse_confidence=0.5,
            )
            other_task = create_task_tool(
                conn,
                user_id=other_user_id,
                title="Other task",
                parse_confidence=0.5,
            )

            self.assertEqual(
                [task.id for task in list_active_tasks(conn, user_id=owner_user_id)],
                [owner_task.id],
            )
            self.assertEqual(
                [task.id for task in list_active_tasks(conn, user_id=other_user_id)],
                [other_task.id],
            )

    def test_does_not_accept_model_selected_internal_fields(self) -> None:
        """The model cannot select source or bypass category-name resolution."""
        with self.open_seeded_database() as (conn, owner_user_id, other_user_id):
            self.insert_category(
                conn,
                category_id="category-other-work",
                user_id=other_user_id,
                name="Work",
            )
            for field_name, value in (
                ("source", "manual_entry"),
                ("category_id", "category-other-work"),
            ):
                with self.subTest(field_name=field_name):
                    with self.assertRaises(TypeError):
                        create_task_tool(
                            conn,
                            user_id=owner_user_id,
                            title="Buy printer paper",
                            parse_confidence=0.5,
                            **{field_name: value},
                        )

    def test_definition_matches_create_task_tool_model_arguments(self) -> None:
        """The provider schema exposes every and only model-controlled fields."""
        (definition,) = get_tool_definitions()
        parameters = definition["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        tool_parameters = inspect.signature(create_task_tool).parameters

        self.assertEqual(definition["type"], "function")
        self.assertEqual(definition["name"], CREATE_TASK_TOOL_NAME)
        self.assertTrue(definition["strict"])
        self.assertEqual(parameters["additionalProperties"], False)
        self.assertEqual(set(parameters["required"]), set(properties))
        self.assertEqual(
            set(properties),
            set(tool_parameters) - {"conn", "user_id"},
        )

    def test_dispatches_only_the_create_task_tool_for_the_trusted_owner(self) -> None:
        """Dispatch injects the owner instead of accepting it from model JSON."""
        with self.open_seeded_database() as (conn, owner_user_id, other_user_id):
            result = dispatch_tool_call(
                conn,
                user_id=owner_user_id,
                call_id="call_create_task",
                name=CREATE_TASK_TOOL_NAME,
                arguments_json='{"title":"Owner-only task"}',
            )

            self.assertEqual(result.call_id, "call_create_task")
            self.assertEqual(result.output["ok"], True)
            self.assertEqual(result.output["task"], {"ref": "T1", "title": "Owner-only task"})
            self.assertEqual(len(list_active_tasks(conn, user_id=owner_user_id)), 1)
            self.assertEqual(list_active_tasks(conn, user_id=other_user_id), ())

    def test_dispatch_rejects_unknown_and_model_controlled_arguments(self) -> None:
        """Only the explicit allowlist may reach an application service."""
        with self.open_seeded_database() as (conn, owner_user_id, other_user_id):
            for name, arguments_json, expected_code in (
                ("delete_everything", '{"title":"Ignore this"}', "unknown_tool"),
                (
                    CREATE_TASK_TOOL_NAME,
                    f'{{"title":"Ignore this","user_id":"{other_user_id}"}}',
                    "unexpected_argument",
                ),
            ):
                with self.subTest(name=name, expected_code=expected_code):
                    result = dispatch_tool_call(
                        conn,
                        user_id=owner_user_id,
                        call_id="call_rejected",
                        name=name,
                        arguments_json=arguments_json,
                    )

                    self.assertEqual(result.output["ok"], False)
                    self.assertEqual(result.output["error"]["code"], expected_code)
                    self.assertEqual(list_active_tasks(conn, user_id=owner_user_id), ())
                    self.assertEqual(list_active_tasks(conn, user_id=other_user_id), ())

    def test_dispatch_returns_safe_errors_for_malformed_and_invalid_arguments(self) -> None:
        """Model errors do not raise or reveal internal application exceptions."""
        with self.open_seeded_database() as (conn, owner_user_id, _):
            for arguments_json, expected_code in (
                ("not JSON", "malformed_arguments"),
                ('{"title":"   "}', "invalid_title"),
            ):
                with self.subTest(arguments_json=arguments_json):
                    result = dispatch_tool_call(
                        conn,
                        user_id=owner_user_id,
                        call_id="call_invalid",
                        name=CREATE_TASK_TOOL_NAME,
                        arguments_json=arguments_json,
                    )

                    self.assertEqual(result.call_id, "call_invalid")
                    self.assertEqual(result.output["ok"], False)
                    self.assertEqual(result.output["error"]["code"], expected_code)
                    self.assertEqual(list_active_tasks(conn, user_id=owner_user_id), ())

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

    def insert_category(
        self,
        conn: Connection,
        *,
        category_id: str,
        user_id: str,
        name: str,
    ) -> None:
        """Seed an owned category for LLM tool integration tests."""
        with conn:
            conn.execute(
                """
                INSERT INTO categories (id, user_id, name, created_at, archived_at)
                VALUES (?, ?, ?, '2026-09-10T00:00:00+00:00', NULL)
                """,
                (category_id, user_id, name),
            )
