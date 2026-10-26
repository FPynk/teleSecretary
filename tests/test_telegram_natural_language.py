"""Tests for the natural-language Telegram adapter and its authorisation gate."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from sqlite3 import Connection, Row, connect
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.tasks import list_active_tasks
from tele_secretary.app.users import get_or_create_telegram_user
from tele_secretary.config import AppConfig
from tele_secretary.llm.client import LLMClient, LLMClientError, LLMResponse, LLMToolCall
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.authorisation import wrap_authorised_telegram_handler
from tele_secretary.telegram.natural_language import build_natural_language_handler


class NaturalLanguageHandlerTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the adapter with real SQLite services and a fake provider."""

    def setUp(self) -> None:
        """Create a migrated database and common provider-free test inputs."""
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        self.config = AppConfig(
            telegram_bot_token="test-token",
            telegram_allowed_user_ids=(1001, 2002),
            data_dir=root,
            log_dir=root,
            db_path=root / "test.sqlite3",
            user_timezone="America/Chicago",
            log_level="INFO",
            openai_api_key="test-key",
            openai_model="test-model",
        )
        with open_test_database(self.config.db_path) as conn:
            apply_migrations(conn)
        self.client = Mock(spec=LLMClient)
        self.client.create_response.return_value = LLMResponse("resp_1", "All set.", ())

    def update(self, text: str | None = "Buy milk", sender_id: int | None = 1001):
        """Build a new-message update with a recorded reply method."""
        return SimpleNamespace(
            effective_user=None if sender_id is None else SimpleNamespace(id=sender_id),
            message=SimpleNamespace(text=text, reply_text=AsyncMock()),
            edited_message=None,
        )

    async def test_shared_gate_checks_sender_before_invoking_callback(self) -> None:
        """Only admitted senders reach the wrapped handler with trusted identity."""
        for allowed_ids, sender_id, expected_reply in (
            ((1001,), 3003, "This Telegram account is not authorised to use TeleSecretary."),
            ((1001,), None, "This Telegram account is not authorised to use TeleSecretary."),
            ((), 1001, "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using TeleSecretary."),
            ((1001,), 1001, None),
        ):
            with self.subTest(allowed_ids=allowed_ids, sender_id=sender_id):
                callback = AsyncMock()
                update = self.update(sender_id=sender_id)
                context = SimpleNamespace()
                handler = wrap_authorised_telegram_handler(
                    replace(self.config, telegram_allowed_user_ids=allowed_ids), callback
                )
                await handler(update, context)
                if expected_reply is None:
                    callback.assert_awaited_once_with(update, context, telegram_user_id=1001)
                    update.message.reply_text.assert_not_awaited()
                else:
                    callback.assert_not_awaited()
                    update.message.reply_text.assert_awaited_once_with(expected_reply)

    async def test_unauthorised_message_never_opens_database_or_calls_agent(self) -> None:
        """Authorisation rejects an update before the natural-language callback."""
        update = self.update(sender_id=3003)
        handler = build_natural_language_handler(self.config, client=self.client)
        with patch("tele_secretary.telegram.natural_language.connect") as connect_database:
            await handler(update, SimpleNamespace())
        connect_database.assert_not_called()
        self.client.create_response.assert_not_called()
        update.message.reply_text.assert_awaited_once_with(
            "This Telegram account is not authorised to use TeleSecretary."
        )

    async def test_uses_actual_sender_and_persisted_timezone_for_task_creation(self) -> None:
        """The second allowlisted user owns the task and supplies their saved zone."""
        with open_test_database(self.config.db_path) as conn:
            first = get_or_create_telegram_user(
                conn, telegram_user_id=1001, default_timezone="America/Chicago"
            )
            second = get_or_create_telegram_user(
                conn, telegram_user_id=2002, default_timezone="Asia/Singapore"
            )
        self.client.create_response.side_effect = (
            LLMResponse("resp_1", "", (
                LLMToolCall("call_1", "create_task_tool", '{"title":"Buy milk"}'),
            )),
            LLMResponse("resp_2", "Created T1: Buy milk.", ()),
        )
        update = self.update(sender_id=2002)
        handler = build_natural_language_handler(self.config, client=self.client)
        await handler(update, SimpleNamespace())

        with open_test_database(self.config.db_path) as conn:
            self.assertEqual(list_active_tasks(conn, user_id=first.user_id), ())
            self.assertEqual(
                [task.title for task in list_active_tasks(conn, user_id=second.user_id)],
                ["Buy milk"],
            )
        first_input = self.client.create_response.call_args_list[0].kwargs["input_items"]
        self.assertIn("User timezone: Asia/Singapore", first_input)
        self.assertIn("Buy milk", first_input)
        update.message.reply_text.assert_awaited_once_with("Created T1: Buy milk.")

    async def test_ignores_commands_media_and_edited_messages(self) -> None:
        """Only original ordinary text reaches model or database work."""
        updates = [self.update(text) for text in ("/list", "  /unknown", "", "   ", None)]
        edited_update = self.update()
        edited_update.edited_message = edited_update.message
        edited_update.message = None
        updates.append(edited_update)
        handler = build_natural_language_handler(self.config, client=self.client)
        with patch("tele_secretary.telegram.natural_language.connect") as connect_database:
            for update in updates:
                await handler(update, SimpleNamespace())
                if update.message is not None:
                    update.message.reply_text.assert_not_awaited()
        connect_database.assert_not_called()
        self.client.create_response.assert_not_called()

    async def test_disabled_support_replies_without_database_or_provider_access(self) -> None:
        """Missing configuration or client gives clear slash-command guidance."""
        for config, client in (
            (replace(self.config, openai_api_key=None, openai_model=None), self.client),
            (self.config, None),
        ):
            with self.subTest(client=client):
                update = self.update()
                handler = build_natural_language_handler(config, client=client)
                with patch("tele_secretary.telegram.natural_language.connect") as connect_database:
                    await handler(update, SimpleNamespace())
                connect_database.assert_not_called()
                self.client.create_response.assert_not_called()
                update.message.reply_text.assert_awaited_once_with(
                    "Natural-language support is disabled. Please use slash commands such as /addtask and /list."
                )

    async def test_returns_safe_provider_failure_to_original_message(self) -> None:
        """Provider details cannot escape through a Telegram reply."""
        self.client.create_response.side_effect = LLMClientError("private provider details")
        update = self.update()
        await build_natural_language_handler(self.config, client=self.client)(update, SimpleNamespace())
        update.message.reply_text.assert_awaited_once_with(
            "I'm having trouble responding right now. Please try again."
        )

    async def test_worker_closes_connection_and_passes_aware_current_time(self) -> None:
        """Connection creation, agent use, and cleanup happen off the event loop."""
        event_loop_thread = threading.get_ident()
        observed = {}

        class TrackedConnection(Connection):
            def close(self):
                observed["closed_thread"] = threading.get_ident()
                super().close()

        def connect_in_worker(path):
            observed["worker_thread"] = threading.get_ident()
            observed["connection"] = connect(path, factory=TrackedConnection)
            observed["connection"].row_factory = Row
            return observed["connection"]

        def agent_in_worker(conn, **kwargs):
            self.assertNotEqual(threading.get_ident(), event_loop_thread)
            self.assertIs(conn, observed["connection"])
            self.assertIsNotNone(kwargs["current_time"].utcoffset())
            raise RuntimeError("secret database details")

        update = self.update()
        with patch("tele_secretary.telegram.natural_language.connect", connect_in_worker), patch(
            "tele_secretary.telegram.natural_language.run_agent", agent_in_worker
        ):
            await build_natural_language_handler(self.config, client=self.client)(update, SimpleNamespace())
        update.message.reply_text.assert_awaited_once_with(
            "I couldn't finish that request. Check /list before trying again."
        )
        self.assertNotEqual(observed["worker_thread"], event_loop_thread)
        self.assertEqual(observed["closed_thread"], observed["worker_thread"])

    async def test_long_reply_is_split_without_repeating_task_execution(self) -> None:
        """An answer beyond Telegram's size limit is delivered without losing text."""
        update = self.update()
        delivered = []

        async def send_with_telegram_limit(text):
            if len(text) > 4096:
                raise ValueError("Message is too long")
            delivered.append(text)

        update.message.reply_text.side_effect = send_with_telegram_limit
        response = "Saved T1. " + "x" * 5000
        with patch("tele_secretary.telegram.natural_language.run_agent", return_value=response) as agent:
            await build_natural_language_handler(self.config, client=self.client)(update, SimpleNamespace())
        self.assertEqual("".join(delivered), response)
        self.assertTrue(all(len(part) <= 4096 for part in delivered))
        agent.assert_called_once()

    async def test_blank_output_gets_safe_reply_and_send_failure_does_not_rerun_agent(self) -> None:
        """Empty model text is usable, and Telegram failure never retries a write."""
        update = self.update()
        handler = build_natural_language_handler(self.config, client=self.client)
        with patch("tele_secretary.telegram.natural_language.run_agent", return_value=" "):
            await handler(update, SimpleNamespace())
        self.assertIn("Check /list", update.message.reply_text.await_args.args[0])

        failing_update = self.update()
        failing_update.message.reply_text.side_effect = RuntimeError("Telegram unavailable")
        with patch("tele_secretary.telegram.natural_language.run_agent", return_value="Saved T1") as agent:
            with self.assertRaises(RuntimeError):
                await handler(failing_update, SimpleNamespace())
        agent.assert_called_once()
