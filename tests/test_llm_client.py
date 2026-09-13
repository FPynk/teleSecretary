"""Tests for the OpenAI Responses API adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import unittest

import _path  # noqa: F401
from tele_secretary.llm.client import LLMClientError, OpenAIResponsesClient


# TODO(TSEC-42): Add a build/install test proving system.md is present in the
# distributed package, not only in a source checkout.


@dataclass
class FakeOutputItem:
    """One minimal OpenAI output item used by the fake SDK response."""

    type: str
    call_id: str | None = None
    name: str | None = None
    arguments: str | None = None


@dataclass
class FakeResponse:
    """One minimal OpenAI response used by the fake SDK."""

    id: str
    output_text: str
    output: list[FakeOutputItem]


class FakeResponsesApi:
    """Record calls and return a configured response without network access."""

    def __init__(self, response: FakeResponse | Exception) -> None:
        """Store the configured fake response or failure."""
        self._response = response
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeResponse:
        """Record one OpenAI request and return the configured result."""
        self.requests.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class FakeOpenAIClient:
    """Expose the Responses resource expected by the production adapter."""

    def __init__(self, response: FakeResponse | Exception) -> None:
        """Create a fake SDK client with the configured Responses behavior."""
        self.responses = FakeResponsesApi(response)


class OpenAIResponsesClientTests(unittest.TestCase):
    """Verify OpenAI conversion without making live provider calls."""

    def test_sends_a_stateless_request_and_normalizes_function_calls(self) -> None:
        """The adapter translates provider output into the agent-facing result."""
        sdk_client = FakeOpenAIClient(
            FakeResponse(
                id="resp_123",
                output_text="I will create that task.",
                output=[
                    FakeOutputItem(
                        type="function_call",
                        call_id="call_123",
                        name="create_task_tool",
                        arguments='{"title":"Buy milk"}',
                    )
                ],
            )
        )
        client = OpenAIResponsesClient(
            api_key="test-key",
            model="test-model",
            sdk_client=sdk_client,
        )

        response = client.create_response(
            instructions="Use tools for saved tasks.",
            input_items="Buy milk",
            tool_definitions=[{"type": "function", "name": "create_task_tool"}],
        )

        self.assertEqual(response.response_id, "resp_123")
        self.assertEqual(response.text, "I will create that task.")
        self.assertEqual(response.tool_calls[0].call_id, "call_123")
        self.assertEqual(response.tool_calls[0].name, "create_task_tool")
        self.assertEqual(response.tool_calls[0].arguments_json, '{"title":"Buy milk"}')
        self.assertEqual(
            sdk_client.responses.requests,
            [
                {
                    "model": "test-model",
                    "instructions": "Use tools for saved tasks.",
                    "input": "Buy milk",
                    "tools": [{"type": "function", "name": "create_task_tool"}],
                    "store": False,
                }
            ],
        )

    def test_wraps_provider_failures_without_exposing_provider_details(self) -> None:
        """Network and provider errors become a safe client error."""
        client = OpenAIResponsesClient(
            api_key="test-key",
            model="test-model",
            sdk_client=FakeOpenAIClient(RuntimeError("provider details")),
        )

        with self.assertRaises(LLMClientError) as raised:
            client.create_response(instructions="Instructions", input_items="Hello")

        self.assertEqual(str(raised.exception), "OpenAI request failed.")

    def test_rejects_missing_client_configuration(self) -> None:
        """Empty API credentials and model names fail before a provider call."""
        response = FakeResponse(id="resp_123", output_text="", output=[])

        with self.assertRaises(LLMClientError):
            OpenAIResponsesClient(api_key="", model="test-model", sdk_client=FakeOpenAIClient(response))
        with self.assertRaises(LLMClientError):
            OpenAIResponsesClient(api_key="test-key", model=" ", sdk_client=FakeOpenAIClient(response))
