from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.reminder_time_parser import ParsedReminderTime, ReminderTimeWarning
from tele_secretary.app.reminders import ReminderNotFoundError
from tele_secretary.app.tasks import TaskRecord, complete_task, create_task
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.config import AppConfig
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.bot import (
    RemindCommandParseError,
    _remind_handler,
    build_application,
    parse_remind_command_text,
)
from tele_secretary.telegram.responses import (
    build_remind_persistence_error_response,
    build_remind_usage_response,
    build_reminder_created_response,
)


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class RemindCommandParserTests(unittest.TestCase):
    def test_parser_accepts_mentions_normalizes_refs_and_preserves_expression(self) -> None:
        parsed_command = parse_remind_command_text(
            "/remind@TeleSecretaryBot t12   Fri   2 pM"
        )

        self.assertEqual(parsed_command.task_ref, "T12")
        self.assertEqual(parsed_command.time_expression, "Fri   2 pM")

    def test_parser_returns_no_expression_for_valid_task_ref(self) -> None:
        parsed_command = parse_remind_command_text("  /REMIND T2  ")

        self.assertEqual(parsed_command.task_ref, "T2")
        self.assertIsNone(parsed_command.time_expression)

    def test_parser_rejects_invalid_command_envelopes(self) -> None:
        invalid_commands = (
            "/remind",
            "/remind 12 tomorrow",
            "/remind T0 tomorrow",
            "/remind T01 tomorrow",
            "/remind T1tomorrow",
            "/show T1 tomorrow",
        )

        for command_text in invalid_commands:
            with self.subTest(command_text=command_text):
                with self.assertRaises(RemindCommandParseError):
                    parse_remind_command_text(command_text)


class TelegramRemindCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_envelope_and_unauthorized_callers_do_not_open_a_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid_config = self.build_config(root / "invalid")
            invalid_update = self.build_update("/remind 1 tomorrow")
            await _remind_handler(invalid_config)(invalid_update, SimpleNamespace())

            unconfigured_config = self.build_config(root / "unconfigured", allowed_user_ids=())
            unconfigured_update = self.build_update("/remind T1 tomorrow")
            await _remind_handler(unconfigured_config)(unconfigured_update, SimpleNamespace())

            unauthorized_config = self.build_config(root / "unauthorized", allowed_user_ids=(2002,))
            unauthorized_update = self.build_update("/remind T1 tomorrow")
            await _remind_handler(unauthorized_config)(unauthorized_update, SimpleNamespace())

        self.assertEqual(invalid_update.message.replies, [build_remind_usage_response()])
        self.assertEqual(
            unconfigured_update.message.replies,
            [
                "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using TeleSecretary."
            ],
        )
        self.assertEqual(
            unauthorized_update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )

    async def test_missing_expression_names_the_owned_task_without_creating_a_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                self.create_owner_task(conn, config, "Email professor")

            update = self.build_update("/remind T1")
            with patch("tele_secretary.telegram.bot.parse_reminder_time_expression") as parse_time:
                await _remind_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                reminder_count = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]

        parse_time.assert_not_called()
        self.assertEqual(reminder_count, 0)
        self.assertEqual(
            update.message.replies,
            ['When should I remind you about "Email professor"?'],
        )

    async def test_handler_creates_reminder_and_confirms_persisted_local_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_owner_task(conn, config, "Email professor")

            update = self.build_update("/remind@TeleSecretaryBot T1 01/01/2099 2pm")
            await _remind_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                reminder = conn.execute(
                    "SELECT item_id, scheduled_at, status, delivery_channel, retry_count FROM reminders"
                ).fetchone()

        self.assertEqual(reminder["item_id"], task.id)
        self.assertEqual(reminder["scheduled_at"], "2099-01-01T20:00:00+00:00")
        self.assertEqual(
            (reminder["status"], reminder["delivery_channel"], reminder["retry_count"]),
            ("pending", "telegram", 0),
        )
        self.assertEqual(
            update.message.replies,
            ['Reminder set for "Email professor" on Thu Jan 1, 2099 at 2:00 PM.'],
        )

    async def test_handler_delegates_the_unmodified_expression_to_the_shared_parser(self) -> None:
        parsed_time = ParsedReminderTime(
            scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
            warning=None,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                self.create_owner_task(conn, config, "Email professor")

            update = self.build_update("/remind t1   Fri   2 pM")
            with patch(
                "tele_secretary.telegram.bot.parse_reminder_time_expression",
                return_value=parsed_time,
            ) as parse_time:
                await _remind_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                scheduled_at = conn.execute("SELECT scheduled_at FROM reminders").fetchone()[0]

        parse_time.assert_called_once_with("Fri   2 pM", "America/Chicago")
        self.assertEqual(scheduled_at, "2099-01-01T20:00:00+00:00")

    async def test_parser_and_service_failures_never_send_a_success_or_dst_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                task = create_task(
                    conn,
                    user_id=user_id,
                    title="Email professor",
                    source="test_fixture",
                )

            unsupported_update = self.build_update("/remind T1 next week")
            past_update = self.build_update("/remind T1 01/01/2020")
            await _remind_handler(config)(unsupported_update, SimpleNamespace())
            await _remind_handler(config)(past_update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                complete_task(
                    conn,
                    user_id=user_id,
                    task_id=task.id,
                    source="test_fixture",
                )

            inactive_update = self.build_update("/remind T1 01/01/2099 2pm")
            await _remind_handler(config)(inactive_update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                reminder_count = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]

        self.assertIn("Use tomorrow", unsupported_update.message.replies[0])
        self.assertIn("already passed", past_update.message.replies[0])
        self.assertEqual(
            inactive_update.message.replies,
            ["Could not set reminder: Reminders can only be created for active tasks."],
        )
        self.assertEqual(reminder_count, 0)
        for update in (unsupported_update, past_update, inactive_update):
            self.assertNotIn("Reminder set", update.message.replies[0])
            self.assertNotIn("Note:", update.message.replies[0])

    async def test_duplicate_and_race_errors_do_not_report_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                self.create_owner_task(conn, config, "Email professor")

            first_update = self.build_update("/remind T1 01/01/2099 2pm")
            duplicate_update = self.build_update("/remind T1 01/01/2099 2pm")
            await _remind_handler(config)(first_update, SimpleNamespace())
            await _remind_handler(config)(duplicate_update, SimpleNamespace())

            race_update = self.build_update("/remind T1 01/01/2099 3pm")
            with patch(
                "tele_secretary.telegram.bot.create_reminder",
                side_effect=ReminderNotFoundError("task_not_found", "Task was not found."),
            ):
                await _remind_handler(config)(race_update, SimpleNamespace())

        self.assertEqual(
            duplicate_update.message.replies,
            [
                "Could not set reminder: An active reminder already exists for this task at that time."
            ],
        )
        self.assertEqual(
            race_update.message.replies,
            ["Task T1 was not found. Use /list to see active task refs."],
        )
        for update in (duplicate_update, race_update):
            self.assertNotIn("Reminder set", update.message.replies[0])
            self.assertNotIn("Note:", update.message.replies[0])

    async def test_persistence_error_returns_generic_reply_without_a_dst_warning(self) -> None:
        parsed_time = ParsedReminderTime(
            scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
            warning=ReminderTimeWarning.NONEXISTENT_TIME_MOVED_FORWARD,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                self.create_owner_task(conn, config, "Email professor")

            update = self.build_update("/remind T1 tomorrow")
            with (
                patch(
                    "tele_secretary.telegram.bot.parse_reminder_time_expression",
                    return_value=parsed_time,
                ),
                patch(
                    "tele_secretary.telegram.bot.create_reminder",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ),
                patch("tele_secretary.telegram.bot.LOGGER.exception") as log_exception,
            ):
                await _remind_handler(config)(update, SimpleNamespace())

        log_exception.assert_called_once()
        self.assertEqual(update.message.replies, [build_remind_persistence_error_response()])

    async def test_dst_warning_is_sent_with_the_persisted_confirmation(self) -> None:
        warning_cases = (
            (
                ReminderTimeWarning.NONEXISTENT_TIME_MOVED_FORWARD,
                "Note: That local time does not exist because the clocks change, so I used the next available time.",
            ),
            (
                ReminderTimeWarning.AMBIGUOUS_TIME_FIRST_OCCURRENCE,
                "Note: That local time occurs twice because the clocks change, so I used the first occurrence.",
            ),
        )
        for warning, expected_note in warning_cases:
            with self.subTest(warning=warning):
                parsed_time = ParsedReminderTime(
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                    warning=warning,
                )
                with tempfile.TemporaryDirectory() as temp_dir:
                    config = self.build_config(Path(temp_dir))
                    with open_test_database(config.db_path) as conn:
                        apply_migrations(conn)
                        self.create_owner_task(conn, config, "Email professor")

                    update = self.build_update("/remind T1 tomorrow")
                    with patch(
                        "tele_secretary.telegram.bot.parse_reminder_time_expression",
                        return_value=parsed_time,
                    ):
                        await _remind_handler(config)(update, SimpleNamespace())

                    with open_test_database(config.db_path) as conn:
                        reminder_count = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]

                self.assertEqual(reminder_count, 1)
                self.assertEqual(len(update.message.replies), 1)
                self.assertIn("Reminder set for", update.message.replies[0])
                self.assertIn(expected_note, update.message.replies[0])

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

    def create_owner_task(self, conn, config: AppConfig, title: str) -> TaskRecord:
        user_id = get_or_create_telegram_user_id(
            conn,
            telegram_user_id=1001,
            timezone=config.user_timezone,
        )
        return create_task(
            conn,
            user_id=user_id,
            title=title,
            source="test_fixture",
        )


class TelegramRemindResponseTests(unittest.TestCase):
    def test_response_formats_persisted_schedule_and_warning(self) -> None:
        task = TaskRecord(
            id="task-id",
            ref="T1",
            user_id="user-id",
            title="Email professor",
            status="active",
            source="test_fixture",
            raw_input_text=None,
            parse_status="not_applicable",
            parse_confidence=None,
            created_at="2026-07-01T00:00:00+00:00",
            updated_at="2026-07-01T00:00:00+00:00",
            deleted_at=None,
            description=None,
            category_id=None,
            category_name=None,
            deadline_at=None,
            deadline_type=None,
            planned_start_at=None,
            planned_end_at=None,
            estimated_minutes=None,
            urgency=None,
            completed_at=None,
            tags=(),
        )

        response = build_reminder_created_response(
            task,
            "2099-01-01T20:00:00+00:00",
            "America/Chicago",
            ReminderTimeWarning.AMBIGUOUS_TIME_FIRST_OCCURRENCE,
        )

        self.assertEqual(
            response,
            "Reminder set for \"Email professor\" on Thu Jan 1, 2099 at 2:00 PM.\n"
            "Note: That local time occurs twice because the clocks change, so I used the first occurrence.",
        )
        self.assertLessEqual(len(response), 4096)


class TelegramRemindRegistrationTests(unittest.TestCase):
    def test_application_registers_remind_command(self) -> None:
        class FakeCommandHandler:
            def __init__(self, command, callback) -> None:
                self.command = command
                self.callback = callback

        class FakeApplication:
            def __init__(self) -> None:
                self.handlers = []

            @classmethod
            def builder(cls):
                return FakeApplicationBuilder()

            def add_handler(self, handler) -> None:
                self.handlers.append(handler)

        class FakeApplicationBuilder:
            def __init__(self) -> None:
                self.application = FakeApplication()

            def token(self, token):
                return self

            def build(self):
                return self.application

        telegram_module = types.ModuleType("telegram")
        telegram_ext_module = types.ModuleType("telegram.ext")
        telegram_ext_module.Application = FakeApplication
        telegram_ext_module.CommandHandler = FakeCommandHandler
        telegram_module.ext = telegram_ext_module
        with tempfile.TemporaryDirectory() as temp_dir:
            config = TelegramRemindCommandTests().build_config(Path(temp_dir))
            with patch.dict(
                sys.modules,
                {"telegram": telegram_module, "telegram.ext": telegram_ext_module},
            ):
                application = build_application(config)

        self.assertIn("remind", [handler.command for handler in application.handlers])


if __name__ == "__main__":
    unittest.main()
