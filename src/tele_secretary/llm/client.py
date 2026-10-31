"""OpenAI Responses API adapter for the LLM orchestration layer."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from time import monotonic
from typing import Any, Mapping, Protocol, Sequence

from tele_secretary.llm.diagnostics import (
    input_structure, log_llm_event, safe_error_code, safe_error_param, safe_model_name,
    safe_provider_id, redact_transcript,
)


LOGGER = logging.getLogger(__name__)

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
    """Normalized provider result returned to the LLM agent."""

    response_id: str
    text: str
    tool_calls: tuple[LLMToolCall, ...]
    continuation_items: tuple[Mapping[str, Any], ...] = ()


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
        log_transcripts: bool = False,
        secrets_to_redact: Sequence[str] = (),
    ) -> None:
        """Create a client with injected SDK support for deterministic tests."""
        self._api_key = _require_nonempty_value(api_key, "api_key")
        self._model = _require_nonempty_value(model, "model")
        self._log_transcripts = log_transcripts
        self._secrets_to_redact = (self._api_key, *secrets_to_redact)
        self._sdk_client = sdk_client or _build_openai_sdk_client(self._api_key)

    def close(self) -> None:
        """Release the SDK's HTTP resources when the owning application shuts down."""
        self._sdk_client.close()

    def create_response(
        self,
        *,
        instructions: str,
        input_items: str | Sequence[Mapping[str, Any]],
        tool_definitions: Sequence[Mapping[str, Any]] = (),
    ) -> LLMResponse:
        """Request one stateless OpenAI response and normalize its output."""
        started = monotonic()
        log_llm_event(
            LOGGER, logging.INFO, "llm_client_started", model=safe_model_name(self._model),
            **input_structure(input_items),
        )
        self._log_transcript("llm_transcript_request", {
            "model": self._model, "instructions": instructions,
            "input": input_items, "tools": tool_definitions,
        })
        try:
            response = self._sdk_client.responses.create(
                model=self._model,
                instructions=instructions,
                input=input_items if isinstance(input_items, str) else list(input_items),
                tools=list(tool_definitions),
                store=False,
            )
        except Exception as error:
            self._log_transcript("llm_transcript_error", {
                "message": str(error), "body": getattr(error, "body", None),
            })
            _log_client_failure(error, stage="request", started=started)
            raise LLMClientError("OpenAI request failed.") from error

        self._log_transcript("llm_transcript_response", response)
        output_items = getattr(response, "output", None)
        output_structure = input_structure([
            {"type": getattr(item, "type", None),
             "encrypted_content": bool(getattr(item, "encrypted_content", None))}
            for item in (output_items if isinstance(output_items, (list, tuple)) else ())
        ])
        log_llm_event(
            LOGGER, logging.INFO, "llm_client_received",
            provider_request_id=safe_provider_id(getattr(response, "_request_id", None)),
            response_id=safe_provider_id(getattr(response, "id", None)),
            status=(getattr(response, "status", None)
                    if getattr(response, "status", None) in ("completed", "incomplete", "failed")
                    else "other"),
            output_types=output_structure["input_types"],
            encrypted_reasoning_count=output_structure["encrypted_reasoning_count"],
        )
        try:
            normalized = _to_llm_response(response)
        except Exception as error:
            _log_client_failure(
                error, stage="normalization", started=started,
                provider_request_id=getattr(response, "_request_id", None),
            )
            if isinstance(error, LLMClientError):
                raise
            raise LLMClientError("OpenAI returned an unusable response.") from error
        log_llm_event(
            LOGGER, logging.INFO, "llm_client_completed",
            duration_ms=round((monotonic() - started) * 1000, 2),
            tool_call_count=len(normalized.tool_calls),
        )
        return normalized

    def _log_transcript(self, event: str, payload: object) -> None:
        """Dump request-local content only when explicitly enabled by configuration."""
        if not self._log_transcripts:
            return
        try:
            transcript = redact_transcript(payload, self._secrets_to_redact)
        except Exception:
            log_llm_event(LOGGER, logging.WARNING, "llm_transcript_unavailable")
            return
        log_llm_event(LOGGER, logging.INFO, event, transcript=transcript)


def _log_client_failure(
    error: Exception, *, stage: str, started: float, provider_request_id: object = None,
) -> None:
    """Report machine error fields without raw exception text, bodies, or tracebacks."""
    status_code = getattr(error, "status_code", None)
    validation_reason = None
    if stage == "normalization":
        validation_reason = {
            "OpenAI returned a response without an ID.": "missing_response_id",
            "OpenAI returned malformed response text.": "malformed_text",
            "OpenAI returned a malformed function call.": "malformed_function_call",
        }.get(str(error), "unusable_response")
    log_llm_event(
        LOGGER, logging.WARNING, "llm_client_failed", stage=stage,
        exception_type=type(error).__name__,
        status_code=status_code if type(status_code) is int else None,
        provider_request_id=safe_provider_id(
            provider_request_id or getattr(error, "request_id", None)
        ),
        error_code=safe_error_code(getattr(error, "code", None)),
        error_param=safe_error_param(getattr(error, "param", None)),
        validation_reason=validation_reason,
        duration_ms=round((monotonic() - started) * 1000, 2),
    )


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
    continuation_items: list[Mapping[str, Any]] = []
    for output_item in getattr(response, "output", ()):
        continuation_item = _sdk_to_json_continuation_item(output_item)
        if continuation_item is not None:
            continuation_items.append(continuation_item)
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
        continuation_items=tuple(continuation_items),
    )


def _sdk_to_json_continuation_item(output_item: Any) -> Mapping[str, Any] | None:
    """Convert one SDK output item into a JSON-compatible continuation mapping."""
    to_dict = getattr(output_item, "to_dict", None)
    model_dump = getattr(output_item, "model_dump", None)
    if callable(to_dict):
        dumped_item = to_dict(mode="json")
    elif callable(model_dump):
        dumped_item = model_dump(mode="json", by_alias=True, exclude_unset=True)
    else:
        dumped_item = None
    if isinstance(dumped_item, Mapping):
        # The deployed API rejects a reasoning item's status when it is null.
        if dumped_item.get("type") == "reasoning" and dumped_item.get("status") is None:
            dumped_item = {key: value for key, value in dumped_item.items() if key != "status"}
        return dumped_item

    if getattr(output_item, "type", None) == "function_call":
        call_id = getattr(output_item, "call_id", None)
        name = getattr(output_item, "name", None)
        arguments = getattr(output_item, "arguments", None)
        if all(isinstance(value, str) and value for value in (call_id, name, arguments)):
            return {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
    return None
