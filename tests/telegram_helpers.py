"""Shared Telegram registration doubles for provider-free application tests."""

from contextlib import contextmanager
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


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

    def post_shutdown(self, callback):
        self.application.post_shutdown = callback
        return self

    def build(self):
        return self.application


class FakeFilter:
    """Record the expression passed to the Telegram handler registration."""

    def __init__(self, expression):
        self.expression = expression

    def __and__(self, other):
        return FakeFilter(f"({self.expression} & {other.expression})")

    def __invert__(self):
        return FakeFilter(f"~{self.expression}")


class FakeMessageHandler:
    def __init__(self, filters, callback):
        self.filters = filters
        self.callback = callback


@contextmanager
def fake_telegram_modules():
    """Provide the Telegram registration API without installing or contacting it."""
    telegram_module = types.ModuleType("telegram")
    telegram_ext_module = types.ModuleType("telegram.ext")
    telegram_ext_module.Application = FakeApplication
    telegram_ext_module.CommandHandler = FakeCommandHandler
    telegram_ext_module.MessageHandler = FakeMessageHandler
    telegram_ext_module.filters = SimpleNamespace(
        TEXT=FakeFilter("TEXT"), COMMAND=FakeFilter("COMMAND"),
        UpdateType=SimpleNamespace(MESSAGE=FakeFilter("MESSAGE")),
    )
    telegram_module.ext = telegram_ext_module
    with patch.dict(sys.modules, {"telegram": telegram_module, "telegram.ext": telegram_ext_module}):
        yield
