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
from tele_secretary.app.reminders import create_reminder
from tele_secretary.app.tasks import (
    TaskNotFoundError,
    TaskRecord,
    complete_task,
    create_task,
    get_task_details_by_ref,
    list_active_tasks,
)
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.config import AppConfig
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.bot import (
    _delete_handler,
    build_application,
    parse_delete_command_text,
)
from tele_secretary.telegram.responses import (
    build_delete_error_response,
    build_delete_usage_response,
    build_task_deleted_response,
    build_task_not_found_response,
)


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class DeleteCommandParserTests(unittest.TestCase):
    def test_parser_accepts_mentions_and_normalizes_task_refs(self) -> None:
        self.assertEqual(parse_delete_command_text("/delete T12"), "T12")
        self.assertEqual(
            parse_delete_command_text(" /DELETE@TeleSecretaryBot t7 "),
            "T7",
        )

    def test_parser_rejects_malformed_commands(self) -> None:
        invalid_commands = (
            "/delete",
            "/delete 12",
            "/delete T0",
            "/delete T01",
            "/delete T1 now",
            "/done T1",
        )

        for command_text in invalid_commands:
            with self.subTest(command_text=command_text):
                self.assertIsNone(parse_delete_command_text(command_text))


class TelegramDeleteCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_or_unauthorized_commands_do_not_open_a_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid_config = self.build_config(root / "invalid")
            invalid_update = self.build_update("/delete T1 now")
            unauthorized_config = self.build_config(
                root / "unauthorized",
                allowed_user_ids=(2002,),
            )
            unauthorized_update = self.build_update("/delete T1")

            with patch("tele_secretary.telegram.bot.connect") as connect:
                await _delete_handler(invalid_config)(invalid_update, SimpleNamespace())
                await _delete_handler(unauthorized_config)(
                    unauthorized_update,
                    SimpleNamespace(),
                )

        connect.assert_not_called()
        self.assertEqual(invalid_update.message.replies, [build_delete_usage_response()])
        self.assertEqual(
            unauthorized_update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )

    async def test_active_task_is_deleted_and_future_reminders_are_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                future_reminder = create_reminder(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                )

            update = self.build_update("/delete t1")
            await _delete_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                reminder_statuses = {
                    row["id"]: row["status"]
                    for row in conn.execute(
                        "SELECT id, status FROM reminders WHERE item_id = ?",
                        (task.id,),
                    ).fetchall()
                }
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                active_tasks = list_active_tasks(conn, user_id=user_id)
                with self.assertRaises(TaskNotFoundError):
                    get_task_details_by_ref(conn, user_id=user_id, task_ref="T1")

        self.assertEqual(update.message.replies, [build_task_deleted_response(task)])
        self.assertEqual(reminder_statuses[future_reminder.id], "cancelled")
        self.assertEqual(active_tasks, ())

    async def test_completed_task_can_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "File taxes")
                complete_task(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    source="test_fixture",
                )

            update = self.build_update("/delete T1")
            await _delete_handler(config)(update, SimpleNamespace())

        self.assertEqual(update.message.replies, [build_task_deleted_response(task)])

    async def test_missing_cross_owner_and_deleted_refs_are_indistinguishable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(1001, 2002))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Private task")
                reminder = create_reminder(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                )

            missing_update = self.build_update("/delete T2")
            cross_owner_update = self.build_update("/delete T1", telegram_user_id=2002)
            await _delete_handler(config)(missing_update, SimpleNamespace())
            await _delete_handler(config)(cross_owner_update, SimpleNamespace())

            deleted_update = self.build_update("/delete T1")
            await _delete_handler(config)(deleted_update, SimpleNamespace())
            repeated_update = self.build_update("/delete T1")
            await _delete_handler(config)(repeated_update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                reminder_status = conn.execute(
                    "SELECT status FROM reminders WHERE id = ?",
                    (reminder.id,),
                ).fetchone()["status"]

        self.assertEqual(
            missing_update.message.replies,
            [build_task_not_found_response("T2")],
        )
        self.assertEqual(
            cross_owner_update.message.replies,
            [build_task_not_found_response("T1")],
        )
        self.assertEqual(deleted_update.message.replies, [build_task_deleted_response(task)])
        self.assertEqual(
            repeated_update.message.replies,
            [build_task_not_found_response("T1")],
        )
        self.assertEqual(reminder_status, "cancelled")

    async def test_invalid_lifecycle_returns_the_safe_service_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Archived task")
                conn.execute("UPDATE items SET status = 'archived' WHERE id = ?", (task.id,))
                conn.commit()

            update = self.build_update("/delete T1")
            await _delete_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            [
                build_delete_error_response(
                    "Only active or completed tasks can be deleted."
                )
            ],
        )

    async def test_service_failure_does_not_send_a_success_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                self.create_task_for_user(conn, config, 1001, "Email professor")

            update = self.build_update("/delete T1")
            with (
                patch(
                    "tele_secretary.telegram.bot.soft_delete_task",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ),
                self.assertRaises(sqlite3.OperationalError),
            ):
                await _delete_handler(config)(update, SimpleNamespace())

        self.assertEqual(update.message.replies, [])

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


class TelegramDeleteRegistrationAndHelpTests(unittest.TestCase):
    def test_application_registers_delete_and_help_lists_it(self) -> None:
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
            config = TelegramDeleteCommandTests().build_config(Path(temp_dir))
            with patch.dict(
                sys.modules,
                {"telegram": telegram_module, "telegram.ext": telegram_ext_module},
            ):
                application = build_application(config)

        self.assertIn("delete", [handler.command for handler in application.handlers])
        from tele_secretary.app.help import get_help_text

        self.assertIn("- /delete T<number> - remove a task", get_help_text())


if __name__ == "__main__":
    unittest.main()
