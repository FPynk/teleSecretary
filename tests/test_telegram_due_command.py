from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.tasks import create_task
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.config import AppConfig
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.bot import (
    _due_handler,
    build_application,
    parse_due_command_text,
)


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class TelegramDueCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_due_command_reads_only_the_authenticated_owners_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(1001, 2002))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                owner_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone="America/Chicago",
                )
                other_owner_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=2002,
                    timezone="America/Chicago",
                )
                due_time = datetime.now(timezone.utc) + timedelta(hours=1)
                create_task(
                    conn,
                    user_id=owner_id,
                    title="Owner deadline",
                    source="manual_entry",
                    deadline_at=due_time,
                    deadline_type="hard",
                )
                create_task(
                    conn,
                    user_id=other_owner_id,
                    title="Private deadline",
                    source="manual_entry",
                    deadline_at=due_time,
                    deadline_type="hard",
                )

            updates = (
                self.build_update(telegram_user_id=1001, text="/due"),
                self.build_update(telegram_user_id=1001, text="/DUE"),
                self.build_update(telegram_user_id=1001, text="/due@TeleSecretaryBot"),
            )
            for update in updates:
                await _due_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            [update.message.replies for update in updates],
            [[unittest.mock.ANY], [unittest.mock.ANY], [unittest.mock.ANY]],
        )
        for update in updates:
            self.assertIn("Owner deadline", update.message.replies[0])
            self.assertNotIn("Private deadline", update.message.replies[0])

    async def test_due_rejects_arguments_and_unauthorized_callers_before_database_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            invalid_update = self.build_update(telegram_user_id=1001, text="/due today")
            unauthorized_update = self.build_update(telegram_user_id=2002, text="/due")

            await _due_handler(config)(invalid_update, SimpleNamespace())
            await _due_handler(config)(unauthorized_update, SimpleNamespace())

        self.assertEqual(invalid_update.message.replies, ["Usage: /due"])
        self.assertEqual(
            unauthorized_update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )
        self.assertFalse(config.db_path.exists())

    async def test_due_uses_the_authenticated_users_persisted_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone="America/Los_Angeles",
                )

            update = self.build_update(telegram_user_id=1001, text="/due")
            with patch("tele_secretary.telegram.bot.list_due_tasks", return_value=()) as list_due:
                await _due_handler(config)(update, SimpleNamespace())

        self.assertEqual(list_due.call_args.kwargs["timezone_name"], "America/Los_Angeles")
        self.assertEqual(update.message.replies, ["No overdue or upcoming tasks."])

    def test_due_parser_and_application_registration(self) -> None:
        self.assertTrue(parse_due_command_text("/due"))
        self.assertTrue(parse_due_command_text("/DUE@TeleSecretaryBot"))
        self.assertFalse(parse_due_command_text("/due today"))
        self.assertFalse(parse_due_command_text("/due@"))

        class FakeCommandHandler:
            def __init__(self, command, callback) -> None:
                self.command = command
                self.callback = callback

        class FakeApplication:
            @classmethod
            def builder(cls):
                return FakeApplicationBuilder()

            def __init__(self) -> None:
                self.handlers = []

            def add_handler(self, handler) -> None:
                self.handlers.append(handler)

        class FakeApplicationBuilder:
            def __init__(self) -> None:
                self.application = FakeApplication()

            def token(self, _):
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
            with patch.dict(
                sys.modules,
                {"telegram": telegram_module, "telegram.ext": telegram_ext_module},
            ):
                application = build_application(self.build_config(Path(temp_dir)))

        self.assertIn("due", [handler.command for handler in application.handlers])

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

    def build_update(self, *, telegram_user_id: int, text: str) -> SimpleNamespace:
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=telegram_user_id),
            message=FakeMessage(text),
        )


if __name__ == "__main__":
    unittest.main()
