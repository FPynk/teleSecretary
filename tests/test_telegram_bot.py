from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import _path  # noqa: F401
from tele_secretary.config import AppConfig
from tele_secretary.telegram.bot import SCHEDULER_BOT_DATA_KEY, build_application


NOW = datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc)


class TelegramBotSchedulerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_starts_and_stops_one_scheduler_through_lifecycle_callbacks(self) -> None:
        class FakeCommandHandler:
            def __init__(self, command, callback) -> None:
                self.command = command
                self.callback = callback

        class FakeApplication:
            def __init__(self) -> None:
                self.handlers = []
                self.bot = SimpleNamespace(send_message=AsyncMock())
                self.bot_data = {}

            @classmethod
            def builder(cls):
                return FakeApplicationBuilder()

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

        telegram_module = types.ModuleType("telegram")
        telegram_ext_module = types.ModuleType("telegram.ext")
        telegram_ext_module.Application = FakeApplication
        telegram_ext_module.CommandHandler = FakeCommandHandler
        telegram_module.ext = telegram_ext_module
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
                patch.dict(
                    sys.modules,
                    {"telegram": telegram_module, "telegram.ext": telegram_ext_module},
                ),
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
        self.assertIn("remind", [handler.command for handler in application.handlers])
        self.assertIn("unremind", [handler.command for handler in application.handlers])


if __name__ == "__main__":
    unittest.main()
