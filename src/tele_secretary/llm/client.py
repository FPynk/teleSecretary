"""OpenAI Responses API adapter for the LLM orchestration layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


class LLMClientError(RuntimeError):
    """Raised when the configured LLM provider cannot produce a usable response."""


@dataclass(frozen=True)
class LLMToolCall:
    """One function call proposed by the provider for the agent to dispatch."""

    call_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class LLMResponse:
    """Provider-neutral result returned to the LLM agent."""

    response_id: str
    text: str
    tool_calls: tuple[LLMToolCall, ...]


class LLMClient(Protocol):
    """The small interface the future LLM agent uses to request a response."""

    def create_response(
        self,
        *,
        instructions: str,
        input_items: str | Sequence[Mapping[str, Any]],
        tool_definitions: Sequence[Mapping[str, Any]] = (),
    ) -> LLMResponse:
        """Return model text and any proposed function calls for one request."""


class OpenAIResponsesClient:
    """Translate TeleSecretary LLM requests to the OpenAI Responses API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        sdk_client: Any | None = None,
    ) -> None:
        """Create a client with injected SDK support for deterministic tests."""
        self._api_key = _require_nonempty_value(api_key, "api_key")
        self._model = _require_nonempty_value(model, "model")
        self._sdk_client = sdk_client or _build_openai_sdk_client(self._api_key)

    def create_response(
        self,
        *,
        instructions: str,
        input_items: str | Sequence[Mapping[str, Any]],
        tool_definitions: Sequence[Mapping[str, Any]] = (),
    ) -> LLMResponse:
        """Request one stateless OpenAI response and normalize its output."""
        try:
            response = self._sdk_client.responses.create(
                model=self._model,
                instructions=instructions,
                input=input_items if isinstance(input_items, str) else list(input_items),
                tools=list(tool_definitions),
                store=False,
            )
        except Exception as error:
            raise LLMClientError("OpenAI request failed.") from error

        return _to_llm_response(response)


def _build_openai_sdk_client(api_key: str) -> Any:
    """Import and construct the official SDK only when a real client is needed."""
    try:
        from openai import OpenAI
    except ImportError as error:
        raise LLMClientError(
            "The OpenAI SDK is not installed. Add the openai package before enabling LLM support."
        ) from error

    return OpenAI(api_key=api_key)


def _require_nonempty_value(value: str, field_name: str) -> str:
    """Return a trimmed required configuration value or raise a safe error."""
    if not isinstance(value, str) or not value.strip():
        raise LLMClientError(f"{field_name} is required.")
    return value.strip()


def _to_llm_response(response: Any) -> LLMResponse:
    """Convert the OpenAI response fields required by the future agent."""
    response_id = getattr(response, "id", None)
    output_text = getattr(response, "output_text", "")
    if not isinstance(response_id, str) or not response_id:
        raise LLMClientError("OpenAI returned a response without an ID.")
    if not isinstance(output_text, str):
        raise LLMClientError("OpenAI returned malformed response text.")

    tool_calls: list[LLMToolCall] = []
    for output_item in getattr(response, "output", ()):
        if getattr(output_item, "type", None) != "function_call":
            continue
        call_id = getattr(output_item, "call_id", None)
        name = getattr(output_item, "name", None)
        arguments_json = getattr(output_item, "arguments", None)
        if not all(isinstance(value, str) and value for value in (call_id, name, arguments_json)):
            raise LLMClientError("OpenAI returned a malformed function call.")
        tool_calls.append(
            LLMToolCall(
                call_id=call_id,
                name=name,
                arguments_json=arguments_json,
            )
        )

    return LLMResponse(
        response_id=response_id,
        text=output_text,
        tool_calls=tuple(tool_calls),
    )
