"""Tests for the bounded, stateless LLM tool-calling agent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from sqlite3 import Connection
from typing import Any, Mapping, Sequence
import unittest
from unittest.mock import Mock, patch

import _path  # noqa: F401
from tele_secretary.app.tasks import CategoryRecord, VocabularyRecord
from tele_secretary.llm.agent import (
    TOOL_CALLS_NOT_YET_SUPPORTED_TEXT,
    run_agent,
)
from tele_secretary.llm.client import LLMResponse, LLMToolCall
from tele_secretary.llm.prompts import load_system_prompt
from tele_secretary.llm.tools import get_tool_definitions


@dataclass
class FakeLLMClient:
    """Record agent requests and return one predetermined model response."""

    response: LLMResponse

    def __post_init__(self) -> None:
        """Initialize the request history used by assertions."""
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
        return self.response


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
            result = run_agent(
                conn,
                client=client,
                user_id="trusted-owner-id",
                user_message="Plan groceries",
                current_time=datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc),
                user_timezone="America/Chicago",
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

    def test_omits_categories_when_the_owner_has_more_than_the_context_cap(self) -> None:
        """Large category lists cannot consume an unbounded model context."""
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
        ), self.assertLogs("tele_secretary.llm.agent", level="WARNING") as logged:
            run_agent(
                Mock(spec=Connection),
                client=client,
                user_id="trusted-owner-id",
                user_message="Plan groceries",
                current_time=datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc),
                user_timezone="America/Chicago",
            )

        input_items = client.requests[0]["input_items"]
        self.assertIn("No category names are available for this request.", input_items)
        self.assertNotIn("Category 0", input_items)
        self.assertEqual(
            logged.output,
            [
                "WARNING:tele_secretary.llm.agent:LLM category context omitted "
                "category_count=51 category_limit=50"
            ],
        )

    def test_does_not_repeat_model_text_that_claims_an_unexecuted_tool_write(self) -> None:
        """Tool proposals stay unexecuted until the second TSEC-43 TODO is complete."""
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
            )
        )

        with patch(
            "tele_secretary.llm.agent.list_categories_and_tags",
            return_value=VocabularyRecord(categories=(), tags=()),
        ), self.assertLogs("tele_secretary.llm.agent", level="INFO") as logged:
            result = run_agent(
                Mock(spec=Connection),
                client=client,
                user_id="trusted-owner-id",
                user_message="Buy milk",
                current_time=datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc),
                user_timezone="America/Chicago",
            )

        self.assertEqual(result, TOOL_CALLS_NOT_YET_SUPPORTED_TEXT)
        self.assertEqual(
            logged.output,
            ["INFO:tele_secretary.llm.agent:LLM tool calls deferred count=1"],
        )

    def test_uses_fresh_input_for_each_message(self) -> None:
        """One request cannot add a previous message to the next request's context."""
        client = FakeLLMClient(LLMResponse(response_id="resp_1", text="Done.", tool_calls=()))

        with patch(
            "tele_secretary.llm.agent.list_categories_and_tags",
            return_value=VocabularyRecord(categories=(), tags=()),
        ):
            for user_message in ("First private message", "Second private message"):
                run_agent(
                    Mock(spec=Connection),
                    client=client,
                    user_id="trusted-owner-id",
                    user_message=user_message,
                    current_time=datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc),
                    user_timezone="America/Chicago",
                )

        self.assertIn("First private message", client.requests[0]["input_items"])
        self.assertIn("Second private message", client.requests[1]["input_items"])
        self.assertNotIn("First private message", client.requests[1]["input_items"])

    def category(self, *, name: str) -> CategoryRecord:
        """Return one active category owned by the test user."""
        return CategoryRecord(
            id=f"category-{name}",
            user_id="trusted-owner-id",
            name=name,
            created_at="2026-09-13T00:00:00+00:00",
            archived_at=None,
        )


# TODO(TSEC-43): Add fake-client tests for one or more tool-call rounds
# followed by final text, tool failures, malformed and unknown calls, provider
# failure, round-limit exhaustion, and fresh state for each message. These
# tests must not make live provider calls or import Telegram.
