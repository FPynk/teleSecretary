from __future__ import annotations

from contextlib import contextmanager
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import _path  # noqa: F401
from db_helpers import open_test_database
from telegram_helpers import FakeCommandHandler, FakeMessageHandler, fake_telegram_modules
from tele_secretary.llm.client import LLMResponse
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.config import AppConfig
from tele_secretary.telegram.bot import SCHEDULER_BOT_DATA_KEY, build_application


NOW = datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc)


class TelegramBotSchedulerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_starts_and_stops_one_scheduler_through_lifecycle_callbacks(self) -> None:
        class FakeScheduler:
            instances = []

            def __init__(self, *, run_cycle) -> None:
                self.run_cycle = run_cycle
                self.started = False
                self.stopped = False
                self.instances.append(self)

            def start(self) -> None:
                self.started = True

            async def stop(self) -> None:
                self.stopped = True

        with tempfile.TemporaryDirectory() as temp_dir:
            config = AppConfig(
                telegram_bot_token="test-token",
                telegram_allowed_user_ids=(1001, 2002),
                data_dir=Path(temp_dir) / "data",
                log_dir=Path(temp_dir) / "logs",
                db_path=Path(temp_dir) / "data" / "secretary.sqlite3",
                user_timezone="America/Chicago",
                log_level="INFO",
            )
            process_cycle = AsyncMock()
            with (
                fake_telegram_modules(),
                patch("tele_secretary.telegram.bot.Scheduler", FakeScheduler),
                patch("tele_secretary.telegram.bot.process_reminder_cycle", process_cycle),
            ):
                application = build_application(config)
                await application.post_init(application)
                scheduler = application.bot_data[SCHEDULER_BOT_DATA_KEY]
                await scheduler.run_cycle(NOW)
                await application.post_stop(application)

        self.assertEqual(len(FakeScheduler.instances), 1)
        self.assertTrue(scheduler.started)
        self.assertTrue(scheduler.stopped)
        self.assertEqual(
            process_cycle.await_args.kwargs,
            {
                "db_path": config.db_path,
                "allowed_telegram_user_ids": (1001, 2002),
                "send_message": application.bot.send_message,
                "cycle_time": NOW,
            },
        )
        self.assertIn("remind", [handler.command for handler in application.handlers if isinstance(handler, FakeCommandHandler)])
        self.assertIn("unremind", [handler.command for handler in application.handlers if isinstance(handler, FakeCommandHandler)])


    @contextmanager
    def application_for_test(self, *, enabled=False):
        """Build an application with shared fake dependencies and isolated paths."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = AppConfig(
                telegram_bot_token="test-token", telegram_allowed_user_ids=(1001, 2002),
                data_dir=root, log_dir=root, db_path=root / "secretary.sqlite3",
                user_timezone="America/Chicago", log_level="INFO",
                openai_api_key="test-key" if enabled else None,
                openai_model="test-model" if enabled else None,
            )
            client = SimpleNamespace(
                create_response=Mock(return_value=LLMResponse("resp_1", "All set.", ())),
                close=Mock(),
            )
            with fake_telegram_modules(), patch(
                "tele_secretary.telegram.bot.OpenAIResponsesClient", return_value=client
            ) as client_factory:
                yield build_application(config), config, client, client_factory

    async def test_registers_ordinary_text_after_commands_and_returns_fresh_replies(self):
        """The registered adapter uses the configured client for fresh messages."""
        with self.application_for_test(enabled=True) as (application, config, client, client_factory):
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
            text_handler = application.handlers[-1]
            self.assertIsInstance(text_handler, FakeMessageHandler)
            self.assertEqual(text_handler.filters.expression, "((MESSAGE & TEXT) & ~COMMAND)")
            self.assertEqual([h.command for h in application.handlers[:-1]], [
                "ping", "help", "list", "addtask", "show", "edit", "done", "reopen",
                "delete", "today", "due", "urgent", "remind", "unremind",
            ])
            for text in ("First private message", "Second private message"):
                update = SimpleNamespace(
                    effective_user=SimpleNamespace(id=2002),
                    message=SimpleNamespace(text=text, reply_text=AsyncMock()),
                )
                await text_handler.callback(update, SimpleNamespace())
                update.message.reply_text.assert_awaited_once_with("All set.")
            command = SimpleNamespace(
                effective_user=SimpleNamespace(id=2002),
                message=SimpleNamespace(text="/unknown", reply_text=AsyncMock()),
            )
            await text_handler.callback(command, SimpleNamespace())
            client_factory.assert_called_once_with(
                api_key="test-key", model="test-model", log_transcripts=False,
                secrets_to_redact=("test-token",),
            )
            self.assertEqual(client.create_response.call_count, 2)
            second_input = client.create_response.call_args.kwargs["input_items"]
            self.assertIn("Second private message", second_input)
            self.assertNotIn("First private message", second_input)
            command.message.reply_text.assert_not_awaited()
            await application.post_shutdown(application)

    async def test_disabled_application_replies_without_constructing_provider(self):
        """Disabled text guidance works while existing commands remain registered."""
        with self.application_for_test() as (application, config, client, client_factory):
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1001),
                message=SimpleNamespace(text="Buy milk", reply_text=AsyncMock()),
            )
            await application.handlers[-1].callback(update, SimpleNamespace())
            await application.post_shutdown(application)
            client_factory.assert_not_called()
            client.create_response.assert_not_called()
            client.close.assert_not_called()
            self.assertIn("Natural-language support is disabled", update.message.reply_text.await_args.args[0])
            self.assertFalse(config.db_path.exists())

    async def test_shutdown_closes_the_configured_client_once(self):
        """Shutdown releases the shared provider resources without duplicate closes."""
        with self.application_for_test(enabled=True) as (application, config, client, client_factory):
            await application.post_shutdown(application)
            await application.post_shutdown(application)
            client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
