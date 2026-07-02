from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.tasks import complete_task, create_task
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.config import AppConfig
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.bot import _list_handler


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class TelegramListCommandTests(unittest.IsolatedAsyncioTestCase):
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
                "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using task commands."
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

    def build_update(self, *, telegram_user_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=telegram_user_id),
            message=FakeMessage(),
        )


if __name__ == "__main__":
    unittest.main()
