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
from tele_secretary.app.reminders import ReminderValidationError, create_reminder
from tele_secretary.app.tasks import TaskRecord, create_task
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.config import AppConfig
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.bot import (
    _unremind_handler,
    build_application,
    parse_unremind_command_text,
)
from tele_secretary.telegram.responses import (
    build_unremind_cancelled_response,
    build_unremind_multiple_pending_response,
    build_unremind_no_pending_response,
    build_unremind_persistence_error_response,
    build_unremind_stale_response,
    build_unremind_usage_response,
)


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class UnremindCommandParserTests(unittest.TestCase):
    def test_parser_accepts_mentions_and_normalizes_task_refs(self) -> None:
        self.assertEqual(parse_unremind_command_text("/unremind T12"), "T12")
        self.assertEqual(
            parse_unremind_command_text(" /UNREMIND@TeleSecretaryBot t7 "),
            "T7",
        )

    def test_parser_rejects_missing_malformed_and_extra_arguments(self) -> None:
        invalid_commands = (
            "/unremind",
            "/unremind 12",
            "/unremind T0",
            "/unremind T01",
            "/unremind T1 2",
            "/remind T1",
        )

        for command_text in invalid_commands:
            with self.subTest(command_text=command_text):
                self.assertIsNone(parse_unremind_command_text(command_text))


class TelegramUnremindCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_or_unauthorized_commands_do_not_open_a_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid_config = self.build_config(root / "invalid")
            invalid_update = self.build_update("/unremind T1 2")
            unauthorized_config = self.build_config(
                root / "unauthorized",
                allowed_user_ids=(2002,),
            )
            unauthorized_update = self.build_update("/unremind T1")

            with patch("tele_secretary.telegram.bot.connect") as connect:
                await _unremind_handler(invalid_config)(invalid_update, SimpleNamespace())
                await _unremind_handler(unauthorized_config)(unauthorized_update, SimpleNamespace())

        connect.assert_not_called()
        self.assertEqual(invalid_update.message.replies, [build_unremind_usage_response()])
        self.assertEqual(
            unauthorized_update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )

    async def test_zero_pending_reminders_returns_a_clear_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")

            update = self.build_update("/unremind T1")
            await _unremind_handler(config)(update, SimpleNamespace())

        self.assertEqual(update.message.replies, [build_unremind_no_pending_response(task)])

    async def test_sole_pending_reminder_is_cancelled_and_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminder = create_reminder(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                )

            update = self.build_update("/unremind T1")
            await _unremind_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                row = conn.execute(
                    "SELECT status, cancelled_at, updated_at FROM reminders WHERE id = ?",
                    (reminder.id,),
                ).fetchone()

        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["cancelled_at"])
        self.assertEqual(row["cancelled_at"], row["updated_at"])
        self.assertEqual(update.message.replies, [build_unremind_cancelled_response(task)])

    async def test_multiple_pending_reminders_are_left_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                for hour in (20, 21):
                    create_reminder(
                        conn,
                        user_id=task.user_id,
                        task_id=task.id,
                        scheduled_at=datetime(2099, 1, 1, hour, 0, tzinfo=timezone.utc),
                    )

            update = self.build_update("/unremind T1")
            await _unremind_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                statuses = tuple(
                    row["status"]
                    for row in conn.execute(
                        "SELECT status FROM reminders ORDER BY scheduled_at"
                    ).fetchall()
                )

        self.assertEqual(statuses, ("pending", "pending"))
        self.assertEqual(
            update.message.replies,
            [build_unremind_multiple_pending_response(task)],
        )

    async def test_second_owner_cancels_only_their_own_task_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(1001, 2002))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                first_task = self.create_task_for_user(conn, config, 1001, "First owner's task")
                second_task = self.create_task_for_user(conn, config, 2002, "Second owner's task")
                first_reminder = create_reminder(
                    conn,
                    user_id=first_task.user_id,
                    task_id=first_task.id,
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                )
                second_reminder = create_reminder(
                    conn,
                    user_id=second_task.user_id,
                    task_id=second_task.id,
                    scheduled_at=datetime(2099, 1, 1, 21, 0, tzinfo=timezone.utc),
                )

            update = self.build_update("/unremind T1", telegram_user_id=2002)
            await _unremind_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                statuses_by_id = {
                    row["id"]: row["status"]
                    for row in conn.execute("SELECT id, status FROM reminders").fetchall()
                }

        self.assertEqual(statuses_by_id[first_reminder.id], "pending")
        self.assertEqual(statuses_by_id[second_reminder.id], "cancelled")
        self.assertEqual(
            update.message.replies,
            [build_unremind_cancelled_response(second_task)],
        )

    async def test_missing_or_cross_owner_task_ref_does_not_cancel_a_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(1001, 2002))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminder = create_reminder(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                )

            update = self.build_update("/unremind T1", telegram_user_id=2002)
            await _unremind_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                reminder_status = conn.execute(
                    "SELECT status FROM reminders WHERE id = ?",
                    (reminder.id,),
                ).fetchone()["status"]

        self.assertEqual(reminder_status, "pending")
        self.assertEqual(
            update.message.replies,
            ["Task T1 was not found. Use /list to see active task refs."],
        )

    async def test_changed_reminder_state_returns_stale_response_without_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminder = create_reminder(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                )

            update = self.build_update("/unremind T1")
            with patch(
                "tele_secretary.telegram.bot.cancel_pending_reminder",
                side_effect=ReminderValidationError(
                    "reminder_not_cancellable",
                    "Only pending reminders can be cancelled.",
                ),
            ):
                await _unremind_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                reminder_status = conn.execute(
                    "SELECT status FROM reminders WHERE id = ?",
                    (reminder.id,),
                ).fetchone()["status"]

        self.assertEqual(reminder_status, "pending")
        self.assertEqual(update.message.replies, [build_unremind_stale_response("T1")])

    async def test_persistence_failure_returns_generic_reply_and_logs_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                create_reminder(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                )

            update = self.build_update("/unremind T1")
            with (
                patch(
                    "tele_secretary.telegram.bot.cancel_pending_reminder",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ),
                patch("tele_secretary.telegram.bot.LOGGER.exception") as log_exception,
            ):
                await _unremind_handler(config)(update, SimpleNamespace())

        log_exception.assert_called_once_with("Could not cancel reminder for %s", "T1")
        self.assertEqual(
            update.message.replies,
            [build_unremind_persistence_error_response()],
        )

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

    def build_update(self, text: str, *, telegram_user_id: int = 1001) -> SimpleNamespace:
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=telegram_user_id),
            message=FakeMessage(text),
        )

    def create_task_for_user(
        self,
        conn,
        config: AppConfig,
        telegram_user_id: int,
        title: str,
    ) -> TaskRecord:
        user_id = get_or_create_telegram_user_id(
            conn,
            telegram_user_id=telegram_user_id,
            timezone=config.user_timezone,
        )
        return create_task(
            conn,
            user_id=user_id,
            title=title,
            source="test_fixture",
        )


class TelegramUnremindRegistrationTests(unittest.TestCase):
    def test_application_registers_unremind_command(self) -> None:
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

            def post_init(self, callback):
                self.application.post_init = callback
                return self

            def post_stop(self, callback):
                self.application.post_stop = callback
                return self

            def build(self):
                return self.application

        telegram_module = types.ModuleType("telegram")
        telegram_ext_module = types.ModuleType("telegram.ext")
        telegram_ext_module.Application = FakeApplication
        telegram_ext_module.CommandHandler = FakeCommandHandler
        telegram_module.ext = telegram_ext_module
        with tempfile.TemporaryDirectory() as temp_dir:
            config = TelegramUnremindCommandTests().build_config(Path(temp_dir))
            with patch.dict(
                sys.modules,
                {"telegram": telegram_module, "telegram.ext": telegram_ext_module},
            ):
                application = build_application(config)

        self.assertIn("unremind", [handler.command for handler in application.handlers])


if __name__ == "__main__":
    unittest.main()
