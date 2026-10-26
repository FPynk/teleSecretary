"""Content-free diagnostic events correlated within one agent operation."""

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator, Mapping, Sequence
import json
import logging
import re
from uuid import uuid4


LLM_LOG_CONTEXT: ContextVar[tuple[str, int]] = ContextVar("llm_log_context", default=("-", 0))
SAFE_ERROR_CODES = frozenset({
    "invalid_request_error", "invalid_api_key", "insufficient_quota", "rate_limit_exceeded",
    "rate_limit_error", "server_error", "invalid_value", "invalid_type", "invalid_parameter",
    "missing_required_parameter", "unknown_parameter", "unsupported_parameter",
    "unsupported_value", "model_not_found", "context_length_exceeded", "item_not_found",
    "invalid_encrypted_content", "unknown_tool", "malformed_arguments", "unexpected_argument",
    "missing_title", "tool_execution_failed", "unknown_user", "invalid_title",
    "invalid_description", "invalid_deadline_at", "invalid_deadline_type", "invalid_category",
    "invalid_estimated_minutes", "invalid_urgency", "invalid_parse_confidence",
    "invalid_source", "invalid_tags", "invalid_parse_status", "deadline_type_without_deadline",
})


@contextmanager
def llm_operation() -> Iterator[None]:
    """Give concurrent messages separate IDs and restore context on every exit."""
    token = LLM_LOG_CONTEXT.set((uuid4().hex, 0))
    try:
        yield
    finally:
        LLM_LOG_CONTEXT.reset(token)


def log_llm_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """Write JSON metadata supplied by call sites, never request or response bodies."""
    operation_id, round_number = LLM_LOG_CONTEXT.get()
    logger.log(level, json.dumps({
        "event": event, "operation_id": operation_id, "round": round_number, **fields,
    }, separators=(",", ":")))


def safe_provider_id(value: object) -> str | None:
    """Accept only bounded provider-generated request, response, or call IDs."""
    if isinstance(value, str) and re.fullmatch(r"(?:req|resp|call)_[A-Za-z0-9_-]{1,128}", value):
        return value
    return None


def safe_error_code(value: object) -> str | None:
    """Unknown provider codes must not smuggle content into diagnostic logs."""
    if value is None:
        return None
    return value if isinstance(value, str) and value in SAFE_ERROR_CODES else "other"


def safe_error_param(value: object) -> str | None:
    """Keep structural API paths while rejecting arbitrary provider error text."""
    if not isinstance(value, str) or len(value) > 160:
        return None
    root = r"(?:input|tools|model|instructions|store|previous_response_id|include|reasoning)"
    child = r"(?:id|type|call_id|name|arguments|content|summary|encrypted_content|parameters|properties|items|required|additionalProperties)"
    return value if re.fullmatch(root + r"(?:\[\d+\]|\." + child + r")*", value) else None


def safe_model_name(value: str) -> str:
    """Log conventional model identifiers, never an accidentally configured key."""
    return value if re.fullmatch(r"(?:gpt-|o[134](?:-|$))[A-Za-z0-9_.:-]{0,100}", value) else "other"


def input_structure(input_items: str | Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Describe continuation envelopes without their messages, arguments, or results."""
    if isinstance(input_items, str):
        return {"input_types": ["text"], "encrypted_reasoning_count": 0}
    known_types = {"message", "reasoning", "function_call", "function_call_output"}
    types = []
    encrypted_reasoning_count = 0
    for item in input_items:
        if not isinstance(item, Mapping):
            types.append("other")
            continue
        item_type = item.get("type", "message" if "role" in item else "other")
        types.append(item_type if isinstance(item_type, str) and item_type in known_types else "other")
        if item_type == "reasoning" and item.get("encrypted_content"):
            encrypted_reasoning_count += 1
    return {"input_types": types, "encrypted_reasoning_count": encrypted_reasoning_count}
