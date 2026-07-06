from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.tasks import complete_task, create_task, list_active_tasks
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.config import AppConfig
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.bot import (
    AddTaskCommandParseError,
    _addtask_handler,
    _help_handler,
    _list_handler,
    _ping_handler,
    parse_addtask_command_text,
)


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class TelegramListCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_command_requires_configured_owner_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=())

            update = self.build_update(telegram_user_id=1001, text="/ping")
            await _ping_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            [
                "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using TeleSecretary."
            ],
        )

    async def test_help_command_requires_configured_owner_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=())

            update = self.build_update(telegram_user_id=1001, text="/help")
            await _help_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            [
                "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using TeleSecretary."
            ],
        )

    async def test_addtask_command_creates_task_with_title_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            update = self.build_update(
                telegram_user_id=1001,
                text="/addtask Buy milk",
            )
            await _addtask_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                tasks = list_active_tasks(conn, user_id=user_id)

        self.assertEqual(update.message.replies, ["Task added: Buy milk"])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "Buy milk")
        self.assertIsNone(tasks[0].deadline_at)
        self.assertIsNone(tasks[0].deadline_type)
        self.assertEqual(tasks[0].source, "telegram_command")
        self.assertEqual(tasks[0].raw_input_text, "/addtask Buy milk")

    async def test_addtask_command_creates_task_with_due_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            update = self.build_update(
                telegram_user_id=1001,
                text="/addtask Pay electricity bill --due 12/07/2026",
            )
            await _addtask_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                tasks = list_active_tasks(conn, user_id=user_id)

        self.assertEqual(
            update.message.replies,
            ["Task added: Pay electricity bill\nDue: 12/07/2026"],
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "Pay electricity bill")
        self.assertEqual(tasks[0].deadline_at, "2026-07-13T04:59:00+00:00")
        self.assertEqual(tasks[0].deadline_type, "hard")

    async def test_addtask_command_rejects_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            update = self.build_update(telegram_user_id=1001, text="/addtask")
            await _addtask_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                tasks = list_active_tasks(conn, user_id=user_id)

        self.assertEqual(
            update.message.replies,
            ["Usage: /addtask <title> [--due DD/MM/YYYY]"],
        )
        self.assertEqual(tasks, ())

    async def test_addtask_command_requires_configured_owner_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=())

            update = self.build_update(telegram_user_id=1001, text="/addtask Buy milk")
            await _addtask_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            [
                "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using TeleSecretary."
            ],
        )

    async def test_addtask_command_respects_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(2002,))

            update = self.build_update(telegram_user_id=1001, text="/addtask Buy milk")
            await _addtask_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )

    def test_addtask_parser_accepts_bot_username_suffix(self) -> None:
        parsed_command = parse_addtask_command_text(
            "/addtask@TeleSecretaryBot Renew passport --due 31/08/2026",
            "America/Chicago",
        )

        self.assertEqual(parsed_command.title, "Renew passport")
        self.assertEqual(
            parsed_command.deadline_at.isoformat(),
            "2026-09-01T04:59:00+00:00",
        )
        self.assertEqual(parsed_command.due_date_text, "31/08/2026")

    def test_addtask_parser_rejects_malformed_due_flag(self) -> None:
        invalid_commands = (
            "/addtask --due 12/07/2026",
            "/addtask Pay bill --due",
            "/addtask Pay bill --due 2026-07-12",
            "/addtask Pay bill --due 31/02/2026",
            "/addtask Pay bill --due 12/07/2026 --due 13/07/2026",
            "/addtask Pay bill --dueish 12/07/2026",
        )

        for invalid_command in invalid_commands:
            with self.subTest(invalid_command=invalid_command):
                with self.assertRaises(AddTaskCommandParseError):
                    parse_addtask_command_text(invalid_command, "America/Chicago")

    async def test_list_command_shows_active_tasks_for_telegram_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                create_task(
                    conn,
                    user_id=user_id,
                    title="Email professor",
                    source="manual_entry",
                )
                completed_task = create_task(
                    conn,
                    user_id=user_id,
                    title="Already done",
                    source="manual_entry",
                )
                complete_task(
                    conn,
                    user_id=user_id,
                    task_id=completed_task.id,
                    source="manual_entry",
                )

            update = self.build_update(telegram_user_id=1001)
            await _list_handler(config)(update, SimpleNamespace())

        self.assertEqual(update.message.replies, ["Active tasks:\n1. Email professor"])

    async def test_list_command_has_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            update = self.build_update(telegram_user_id=1001)
            await _list_handler(config)(update, SimpleNamespace())

        self.assertEqual(update.message.replies, ["No active tasks."])

    async def test_list_command_requires_configured_owner_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=())

            update = self.build_update(telegram_user_id=1001)
            await _list_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            [
                "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using TeleSecretary."
            ],
        )

    async def test_list_command_respects_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(2002,))

            update = self.build_update(telegram_user_id=1001)
            await _list_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
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

    def build_update(self, *, telegram_user_id: int, text: str = "/list") -> SimpleNamespace:
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=telegram_user_id),
            message=FakeMessage(text),
        )


if __name__ == "__main__":
    unittest.main()
