"""Tests for the bounded, stateless LLM tool-calling agent."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from sqlite3 import Connection, Row, connect
from typing import Any, Mapping, Sequence
import unittest
from unittest.mock import ANY, Mock, patch

import _path  # noqa: F401
from tele_secretary.app.tasks import CategoryRecord, VocabularyRecord, list_active_tasks
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.llm.agent import (
    LLM_REQUEST_FAILED_TEXT,
    MAXIMUM_TOOL_CALLS,
    MAXIMUM_TOOL_CALL_ROUNDS,
    MAXIMUM_TOOL_RESULT_BYTES,
    TOOL_CALL_LIMIT_TEXT,
    TOOL_CALL_ROUND_LIMIT_TEXT,
    TOOL_RESULT_LIMIT_TEXT,
    run_agent,
)
from tele_secretary.llm.client import LLMClientError, LLMResponse, LLMToolCall
from tele_secretary.llm.prompts import load_system_prompt
from tele_secretary.llm.tools import ToolDispatchResult, get_tool_definitions
from tele_secretary.persistence.migrations import apply_migrations


class FakeLLMClient:
    """Record agent requests and return a predetermined sequence of outcomes."""

    def __init__(self, *outcomes: LLMResponse | Exception) -> None:
        """Store responses or failures to return in request order."""
        self._outcomes = list(outcomes)
        self.requests: list[dict[str, Any]] = []

    def create_response(
        self,
        *,
        instructions: str,
        input_items: str | Sequence[Mapping[str, Any]],
        tool_definitions: Sequence[Mapping[str, Any]] = (),
    ) -> LLMResponse:
        """Record one provider-neutral request without making a network call."""
        self.requests.append(
            {
                "instructions": instructions,
                "input_items": input_items,
                "tool_definitions": tool_definitions,
            }
        )
        if not self._outcomes:
            raise AssertionError("FakeLLMClient received more requests than configured.")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class AgentEntryPointTests(unittest.TestCase):
    """Verify the initial stateless request sent to the LLM client."""

    def test_builds_fresh_owner_scoped_context_and_returns_direct_text(self) -> None:
        """A direct response receives only current-message owner context."""
        client = FakeLLMClient(LLMResponse(response_id="resp_1", text="Done.", tool_calls=()))
        category_service = Mock(
            return_value=VocabularyRecord(
                categories=(
                    self.category(name="Work"),
                    self.category(name="Home"),
                ),
                tags=(),
            )
        )
        conn = Mock(spec=Connection)

        with patch(
            "tele_secretary.llm.agent.list_categories_and_tags",
            category_service,
        ):
            result = self.run_agent_for_test(
                client,
                user_message="Plan groceries",
                conn=conn,
            )

        self.assertEqual(result, "Done.")
        category_service.assert_called_once_with(conn, user_id="trusted-owner-id")
        self.assertEqual(client.requests[0]["instructions"], load_system_prompt())
        self.assertEqual(client.requests[0]["tool_definitions"], get_tool_definitions())
        self.assertEqual(
            client.requests[0]["input_items"],
            "\n".join(
                (
                    "Current time: 2026-09-13T18:30:00+00:00",
                    "User timezone: America/Chicago",
                    'Available category names; use an exact name or omit the category: ["Home", "Work"]',
                    "User message:",
                    "Plan groceries",
                )
            ),
        )
        self.assertNotIn("trusted-owner-id", client.requests[0]["input_items"])

    def test_includes_all_categories_in_the_owner_context(self) -> None:
        """Category selection remains available regardless of category count."""
        client = FakeLLMClient(LLMResponse(response_id="resp_1", text="Done.", tool_calls=()))
        category_service = Mock(
            return_value=VocabularyRecord(
                categories=tuple(self.category(name=f"Category {index}") for index in range(51)),
                tags=(),
            )
        )

        with patch(
            "tele_secretary.llm.agent.list_categories_and_tags",
            category_service,
        ):
            self.run_agent_for_test(
                client,
                user_message="Plan groceries",
            )

        input_items = client.requests[0]["input_items"]
        self.assertIn("Category 0", input_items)
        self.assertIn("Category 50", input_items)
        (definition,) = client.requests[0]["tool_definitions"]
        self.assertIn("category_name", definition["parameters"]["properties"])

    def test_dispatches_a_tool_call_and_returns_the_follow_up_text(self) -> None:
        """The agent correlates a safe tool result with the model's next request."""
        client = FakeLLMClient(
            LLMResponse(
                response_id="resp_1",
                text="I created the task.",
                tool_calls=(
                    LLMToolCall(
                        call_id="call_1",
                        name="create_task_tool",
                        arguments_json='{"title":"Buy milk"}',
                    ),
                ),
            ),
            LLMResponse(response_id="resp_2", text="Added Buy milk.", tool_calls=()),
        )
        tool_result = ToolDispatchResult(
            call_id="call_1",
            output={"ok": True, "task": {"ref": "T1", "title": "Buy milk"}},
        )
        dispatcher = Mock(return_value=tool_result)

        with patch(
            "tele_secretary.llm.agent.list_categories_and_tags",
            return_value=VocabularyRecord(categories=(self.category(name="Home"),), tags=()),
        ), patch(
            "tele_secretary.llm.agent.dispatch_tool_call",
            dispatcher,
        ), self.assertLogs("tele_secretary.llm.agent", level="INFO") as logged:
            result = self.run_agent_for_test(
                client,
                user_message="Buy milk",
            )

        self.assertEqual(result, "Added Buy milk.")
        dispatcher.assert_called_once_with(
            ANY,
            user_id="trusted-owner-id",
            call_id="call_1",
            name="create_task_tool",
            arguments_json='{"title":"Buy milk"}',
        )
        self.assertEqual(
            logged.output,
            ["INFO:tele_secretary.llm.agent:LLM tool calls requested count=1"],
        )
        self.assertEqual(
            client.requests[1]["input_items"][-2:],
            [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "create_task_tool",
                    "arguments": '{"title":"Buy milk"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": '{"ok":true,"task":{"ref":"T1","title":"Buy milk"}}',
                },
            ],
        )

    def test_passes_structured_tool_errors_back_to_the_model(self) -> None:
        """Malformed or unknown tool requests receive a safe model-visible result."""
        client = FakeLLMClient(
            LLMResponse(
                response_id="resp_1",
                text="",
                tool_calls=(
                    LLMToolCall(
                        call_id="call_1",
                        name="unknown_tool",
                        arguments_json="not JSON",
                    ),
                ),
            ),
            LLMResponse(response_id="resp_2", text="I couldn't do that.", tool_calls=()),
        )
        dispatcher = Mock(
            return_value=ToolDispatchResult(
                call_id="call_1",
                output={
                    "ok": False,
                    "error": {"code": "unknown_tool", "message": "The requested tool is not available."},
                },
            )
        )

        with patch(
            "tele_secretary.llm.agent.list_categories_and_tags",
            return_value=VocabularyRecord(categories=(), tags=()),
        ), patch("tele_secretary.llm.agent.dispatch_tool_call", dispatcher):
            result = self.run_agent_for_test(
                client,
                user_message="Do something unknown",
            )

        self.assertEqual(result, "I couldn't do that.")
        self.assertEqual(
            client.requests[1]["input_items"][-1],
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": (
                    '{"ok":false,"error":{"code":"unknown_tool",'
                    '"message":"The requested tool is not available."}}'
                ),
            },
        )

    def test_allows_multiple_tool_calls_across_multiple_rounds(self) -> None:
        """The loop accumulates correlated calls and results until final text arrives."""
        client = FakeLLMClient(
            self.response_with_tool_calls("resp_1", "call_1"),
            self.response_with_tool_calls("resp_2", "call_2", "call_3"),
            LLMResponse(response_id="resp_3", text="Created all tasks.", tool_calls=()),
        )
        dispatcher = Mock(
            side_effect=[
                ToolDispatchResult(call_id=f"call_{index}", output={"ok": True})
                for index in range(1, 4)
            ]
        )

        with patch(
            "tele_secretary.llm.agent.list_categories_and_tags",
            return_value=VocabularyRecord(categories=(), tags=()),
        ), patch("tele_secretary.llm.agent.dispatch_tool_call", dispatcher):
            result = self.run_agent_for_test(
                client,
                user_message="Create three tasks",
            )

        self.assertEqual(result, "Created all tasks.")
        self.assertEqual(dispatcher.call_count, 3)
        self.assertEqual(len(client.requests), 3)

    def test_returns_safe_text_when_the_provider_fails(self) -> None:
        """Provider failures cannot expose implementation details to Telegram."""
        client = FakeLLMClient(LLMClientError("provider details"))

        with patch(
            "tele_secretary.llm.agent.list_categories_and_tags",
            return_value=VocabularyRecord(categories=(), tags=()),
        ), self.assertLogs("tele_secretary.llm.agent", level="WARNING") as logged:
            result = self.run_agent_for_test(
                client,
                user_message="Hello",
            )

        self.assertEqual(result, LLM_REQUEST_FAILED_TEXT)
        self.assertEqual(
            logged.output,
            ["WARNING:tele_secretary.llm.agent:LLM provider request failed."],
        )

    def test_confirms_persisted_tasks_when_follow_up_provider_request_fails(self) -> None:
        """A failed follow-up must not invite users to repeat committed writes."""
        for titles in (("Buy milk",), ("Buy milk", "Buy bread")):
            with self.subTest(titles=titles), closing(connect(":memory:")) as conn:
                conn.row_factory = Row
                conn.execute("PRAGMA foreign_keys = ON")
                apply_migrations(conn)
                user_id = get_or_create_telegram_user_id(
                    conn, telegram_user_id=1001, timezone="America/Chicago"
                )
                client = FakeLLMClient(
                    LLMResponse(
                        response_id="resp_1",
                        text="",
                        tool_calls=tuple(
                            LLMToolCall(
                                call_id=f"call_{index}",
                                name="create_task_tool",
                                arguments_json=json.dumps({"title": title}),
                            )
                            for index, title in enumerate(titles)
                        ),
                    ),
                    LLMClientError("private provider failure"),
                )

                with self.assertLogs("tele_secretary.llm.agent", level="WARNING"):
                    result = self.run_agent_for_test(
                        client,
                        user_message="Create my shopping tasks",
                        conn=conn,
                        user_id=user_id,
                        current_time=datetime(2026, 9, 13, tzinfo=timezone.utc),
                    )

                tasks = list_active_tasks(conn, user_id=user_id)
                self.assertEqual(sorted(task.title for task in tasks), sorted(titles))
                self.assertFalse(conn.in_transaction)
                self.assertIn("Saved tasks:", result)
                for task in tasks:
                    self.assertIn(task.ref, result)
                self.assertNotIn("Please try again", result)
                self.assertNotIn("private provider failure", result)

    def test_stops_before_executing_tool_calls_after_the_round_limit(self) -> None:
        """Repeated model tool proposals cannot create an unbounded loop."""
        client = FakeLLMClient(
            *(
                self.response_with_tool_calls(f"resp_{index}", f"call_{index}")
                for index in range(MAXIMUM_TOOL_CALL_ROUNDS + 1)
            )
        )
        dispatcher = Mock(return_value=ToolDispatchResult(call_id="call", output={"ok": True}))

        with patch(
            "tele_secretary.llm.agent.list_categories_and_tags",
            return_value=VocabularyRecord(categories=(), tags=()),
        ), patch("tele_secretary.llm.agent.dispatch_tool_call", dispatcher):
            result = self.run_agent_for_test(
                client,
                user_message="Keep going",
            )

        self.assertEqual(result, TOOL_CALL_ROUND_LIMIT_TEXT)
        self.assertEqual(dispatcher.call_count, MAXIMUM_TOOL_CALL_ROUNDS)
        self.assertEqual(len(client.requests), MAXIMUM_TOOL_CALL_ROUNDS + 1)

    def test_stops_before_executing_an_excessive_tool_batch(self) -> None:
        """One response cannot bypass the total tool-call limit with a large batch."""
        client = FakeLLMClient(
            self.response_with_tool_calls(
                "resp_1",
                *(f"call_{index}" for index in range(MAXIMUM_TOOL_CALLS + 1)),
            )
        )
        dispatcher = Mock()

        with patch(
            "tele_secretary.llm.agent.list_categories_and_tags",
            return_value=VocabularyRecord(categories=(), tags=()),
        ), patch("tele_secretary.llm.agent.dispatch_tool_call", dispatcher):
            result = self.run_agent_for_test(
                client,
                user_message="Make many tasks",
            )

        self.assertEqual(result, TOOL_CALL_LIMIT_TEXT)
        dispatcher.assert_not_called()

    def test_stops_when_tool_result_output_exceeds_the_size_limit(self) -> None:
        """Unexpectedly large results cannot grow the follow-up request without bound."""
        client = FakeLLMClient(self.response_with_tool_calls("resp_1", "call_1"))
        dispatcher = Mock(
            return_value=ToolDispatchResult(
                call_id="call_1",
                output={"large_result": "x" * MAXIMUM_TOOL_RESULT_BYTES},
            )
        )

        with patch(
            "tele_secretary.llm.agent.list_categories_and_tags",
            return_value=VocabularyRecord(categories=(), tags=()),
        ), patch("tele_secretary.llm.agent.dispatch_tool_call", dispatcher):
            result = self.run_agent_for_test(
                client,
                user_message="Make a large result",
            )

        self.assertEqual(result, TOOL_RESULT_LIMIT_TEXT)

    def test_uses_fresh_input_for_each_message(self) -> None:
        """One request cannot add a previous message to the next request's context."""
        client = FakeLLMClient(
            LLMResponse(response_id="resp_1", text="Done.", tool_calls=()),
            LLMResponse(response_id="resp_2", text="Done.", tool_calls=()),
        )

        with patch(
            "tele_secretary.llm.agent.list_categories_and_tags",
            return_value=VocabularyRecord(categories=(), tags=()),
        ):
            for user_message in ("First private message", "Second private message"):
                self.run_agent_for_test(
                    client,
                    user_message=user_message,
                )

        self.assertIn("First private message", client.requests[0]["input_items"])
        self.assertIn("Second private message", client.requests[1]["input_items"])
        self.assertNotIn("First private message", client.requests[1]["input_items"])

    def run_agent_for_test(
        self,
        client: FakeLLMClient,
        *,
        user_message: str,
        conn: Connection | None = None,
        user_id: str = "trusted-owner-id",
        current_time: datetime = datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc),
    ) -> str:
        """Run the real agent with common context and an optional real database."""
        return run_agent(
            conn if conn is not None else Mock(spec=Connection),
            client=client,
            user_id=user_id,
            user_message=user_message,
            current_time=current_time,
            user_timezone="America/Chicago",
        )

    def category(self, *, name: str) -> CategoryRecord:
        """Return one active category owned by the test user."""
        return CategoryRecord(
            id=f"category-{name}",
            user_id="trusted-owner-id",
            name=name,
            created_at="2026-09-13T00:00:00+00:00",
            archived_at=None,
        )

    def response_with_tool_calls(self, response_id: str, *call_ids: str) -> LLMResponse:
        """Build one model response that proposes task-tool calls for test coverage."""
        return LLMResponse(
            response_id=response_id,
            text="",
            tool_calls=tuple(
                LLMToolCall(
                    call_id=call_id,
                    name="create_task_tool",
                    arguments_json='{"title":"Task"}',
                )
                for call_id in call_ids
            ),
        )
