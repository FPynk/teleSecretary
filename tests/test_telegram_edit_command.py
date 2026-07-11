from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.tasks import create_task, get_task_details_by_ref
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.config import AppConfig
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.bot import _edit_handler
from tele_secretary.telegram.edit_command import (
    EditTaskCommandParseError,
    parse_edit_task_command_text,
)


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class EditTaskCommandParserTests(unittest.TestCase):
    def test_parser_accepts_multiple_flags_curly_quotes_and_repeated_tags(self) -> None:
        parsed_command = parse_edit_task_command_text(
            "/edit T12 -title “Email Professor Smith” "
            "-deadline “18/07/2026 17:00” -deadline-type soft "
            "-estimate 90 -urgency high "
            "-add-tag school -add-tag “email follow-up”",
            "America/Chicago",
        )

        self.assertEqual(parsed_command.task_ref, "T12")
        self.assertEqual(
            parsed_command.task_field_updates["title"],
            "Email Professor Smith",
        )
        self.assertEqual(
            parsed_command.task_field_updates["deadline_at"].isoformat(),
            "2026-07-18T22:00:00+00:00",
        )
        self.assertEqual(parsed_command.task_field_updates["deadline_type"], "soft")
        self.assertEqual(parsed_command.task_field_updates["estimated_minutes"], 90)
        self.assertEqual(parsed_command.task_field_updates["urgency"], "high")
        self.assertEqual(
            parsed_command.add_tag_names,
            ("school", "email follow-up"),
        )
        self.assertEqual(
            parsed_command.changed_fields,
            ("title", "deadline", "estimate", "urgency", "tags"),
        )

    def test_parser_converts_date_only_deadline_to_local_end_of_day(self) -> None:
        parsed_command = parse_edit_task_command_text(
            "/edit t7 -deadline 18/07/2026",
            "America/Chicago",
        )

        self.assertEqual(parsed_command.task_ref, "T7")
        self.assertEqual(
            parsed_command.task_field_updates["deadline_at"].isoformat(),
            "2026-07-19T04:59:00+00:00",
        )

    def test_parser_rejects_conflicts_unknown_flags_and_invalid_values(self) -> None:
        invalid_commands = (
            "/edit T1",
            "/edit 1 -title New",
            "/edit T1 -unknown value",
            "/edit T1 -urgency extreme",
            "/edit T1 -estimate 0",
            "/edit T1 -deadline 2026-07-18",
            "/edit T1 -title New -title Again",
            "/edit T1 -deadline 18/07/2026 -clear-deadline",
            "/edit T1 -clear-tags -add-tag school",
            "/edit T1 -add-tag school -remove-tag school",
            '/edit T1 -title "Unclosed',
        )

        for command_text in invalid_commands:
            with self.subTest(command_text=command_text):
                with self.assertRaises(EditTaskCommandParseError):
                    parse_edit_task_command_text(command_text, "America/Chicago")


class TelegramEditCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_command_updates_multiple_fields_and_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                user_id = self.seed_user_vocabulary_and_task(conn, config)

            update = self.build_update(
                "/edit T1 -title “Email Professor Smith” "
                "-category “School Work” "
                "-deadline “18/07/2026 17:00” -deadline-type soft "
                "-urgency high -remove-tag school -add-tag “email follow-up”"
            )
            await _edit_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                task = get_task_details_by_ref(conn, user_id=user_id, task_ref="T1")

        self.assertEqual(task.title, "Email Professor Smith")
        self.assertEqual(task.category_name, "School Work")
        self.assertEqual(task.deadline_at, "2026-07-18T22:00:00+00:00")
        self.assertEqual(task.deadline_type, "soft")
        self.assertEqual(task.urgency, "high")
        self.assertEqual([tag.name for tag in task.tags], ["email follow-up"])
        self.assertEqual(
            update.message.replies,
            [
                "Updated T1 — Email Professor Smith\n"
                "Title: Email Professor Smith\n"
                "Category: School Work\n"
                "Deadline: Sat Jul 18, 2026 at 5:00 PM (soft)\n"
                "Urgency: high\n"
                "Tags: email follow-up"
            ],
        )

    async def test_edit_command_clears_optional_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                user_id = self.seed_user_vocabulary_and_task(conn, config)

            update = self.build_update(
                "/edit T1 -clear-description -clear-category "
                "-clear-deadline -clear-planned-window -clear-estimate "
                "-clear-urgency -clear-tags"
            )
            await _edit_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                task = get_task_details_by_ref(conn, user_id=user_id, task_ref="T1")

        self.assertIsNone(task.description)
        self.assertIsNone(task.category_id)
        self.assertIsNone(task.deadline_at)
        self.assertIsNone(task.deadline_type)
        self.assertIsNone(task.planned_start_at)
        self.assertIsNone(task.planned_end_at)
        self.assertIsNone(task.estimated_minutes)
        self.assertIsNone(task.urgency)
        self.assertEqual(task.tags, ())
        self.assertIn("Deadline: None", update.message.replies[0])
        self.assertIn("Tags: None", update.message.replies[0])

    async def test_edit_command_is_atomic_when_one_value_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                user_id = self.seed_user_vocabulary_and_task(conn, config)

            update = self.build_update(
                '/edit T1 -title "Changed title" -urgency extreme'
            )
            await _edit_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                task = get_task_details_by_ref(conn, user_id=user_id, task_ref="T1")

        self.assertEqual(task.title, "Email professor")
        self.assertEqual(task.urgency, "medium")
        self.assertIn("Urgency must be", update.message.replies[0])

    async def test_edit_command_rejects_unknown_tag_without_writing_other_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                user_id = self.seed_user_vocabulary_and_task(conn, config)

            update = self.build_update(
                '/edit T1 -title "Changed title" -add-tag "does not exist"'
            )
            await _edit_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                task = get_task_details_by_ref(conn, user_id=user_id, task_ref="T1")

        self.assertEqual(task.title, "Email professor")
        self.assertEqual([tag.name for tag in task.tags], ["school"])
        self.assertEqual(
            update.message.replies,
            ['Could not edit task: Tag "does not exist" does not exist.'],
        )

    async def test_edit_command_handles_missing_task_and_unauthorized_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            missing_update = self.build_update("/edit T99 -title Missing")
            await _edit_handler(config)(missing_update, SimpleNamespace())

            unauthorized_config = self.build_config(
                Path(temp_dir),
                allowed_user_ids=(2002,),
            )
            unauthorized_update = self.build_update("/edit T1 -title Nope")
            await _edit_handler(unauthorized_config)(
                unauthorized_update,
                SimpleNamespace(),
            )

        self.assertEqual(
            missing_update.message.replies,
            ["Task T99 was not found. Use /list to see active task refs."],
        )
        self.assertEqual(
            unauthorized_update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )

    def seed_user_vocabulary_and_task(self, conn, config: AppConfig) -> str:
        user_id = get_or_create_telegram_user_id(
            conn,
            telegram_user_id=1001,
            timezone=config.user_timezone,
        )
        with conn:
            conn.execute(
                """
                INSERT INTO categories (id, user_id, name, created_at)
                VALUES ('cat-school', ?, 'School Work', '2026-07-01T00:00:00+00:00')
                """,
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO tags (id, user_id, name, created_at)
                VALUES
                    ('tag-school', ?, 'school', '2026-07-01T00:00:00+00:00'),
                    ('tag-email', ?, 'email follow-up', '2026-07-01T00:00:00+00:00')
                """,
                (user_id, user_id),
            )
        create_task(
            conn,
            user_id=user_id,
            title="Email professor",
            source="manual_entry",
            description="Ask about the syllabus.",
            category_id="cat-school",
            deadline_at=datetime(2026, 7, 10, 22, 0, tzinfo=timezone.utc),
            deadline_type="hard",
            planned_start_at=datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc),
            planned_end_at=datetime(2026, 7, 8, 16, 0, tzinfo=timezone.utc),
            estimated_minutes=30,
            urgency="medium",
            tag_ids=("tag-school",),
        )
        return user_id

    def build_config(
        self,
        root: Path,
        *,
        allowed_user_ids: tuple[int, ...] = (1001,),
    ) -> AppConfig:
        return AppConfig(
            telegram_bot_token="test-token",
            telegram_allowed_user_ids=allowed_user_ids,
            data_dir=root / "data",
            log_dir=root / "logs",
            db_path=root / "data" / "secretary.sqlite3",
            user_timezone="America/Chicago",
            log_level="INFO",
        )

    def build_update(self, text: str) -> SimpleNamespace:
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=1001),
            message=FakeMessage(text),
        )


if __name__ == "__main__":
    unittest.main()
