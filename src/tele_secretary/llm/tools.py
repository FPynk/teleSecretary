"""Bounded LLM tool adapters that call public application services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from sqlite3 import Connection
from typing import Any, Mapping

from tele_secretary.app.tasks import (
    TaskRecord,
    TaskValidationError,
    create_task,
    list_categories_and_tags,
)


CREATE_TASK_TOOL_NAME = "create_task_tool"


@dataclass(frozen=True)
class ToolDispatchResult:
    """One safe, structured result for an LLM-proposed tool call."""

    call_id: str
    output: Mapping[str, object]


def get_tool_definitions() -> tuple[Mapping[str, object], ...]:
    """Return the currently allowlisted function definitions for the LLM client."""
    return (
        {
            "type": "function",
            "name": CREATE_TASK_TOOL_NAME,
            "description": "Create one task for the authenticated Telegram user.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short task title.",
                    },
                    "description": {
                        "type": ["string", "null"],
                        "description": "Optional task details.",
                    },
                    "deadline_at": {
                        "type": ["string", "null"],
                        "description": "Optional ISO-8601 timestamp with a timezone offset.",
                    },
                    "deadline_type": {
                        "type": ["string", "null"],
                        "enum": ["hard", "soft", None],
                        "description": "Whether the deadline is hard or soft.",
                    },
                    "estimated_minutes": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "description": "Optional positive estimate in minutes.",
                    },
                    "urgency": {
                        "type": ["string", "null"],
                        "enum": ["low", "medium", "high", "top_priority", None],
                        "description": "Optional task urgency.",
                    },
                    "category_name": {
                        "type": ["string", "null"],
                        "description": "Optional exact category name supplied in the user context.",
                    },
                    "parse_confidence": {
                        "type": ["number", "null"],
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "Optional confidence in the parsed task details.",
                    },
                },
                "required": [
                    "title",
                    "description",
                    "deadline_at",
                    "deadline_type",
                    "estimated_minutes",
                    "urgency",
                    "category_name",
                    "parse_confidence",
                ],
                "additionalProperties": False,
            },
        },
    )


def dispatch_tool_call(
    conn: Connection,
    *,
    user_id: str,
    call_id: str,
    name: str,
    arguments_json: str,
) -> ToolDispatchResult:
    """Dispatch one allowlisted call with the trusted owner injected by the app."""
    if name != CREATE_TASK_TOOL_NAME:
        return _tool_error(call_id, "unknown_tool", "The requested tool is not available.")

    arguments_or_error = _parse_create_task_tool_arguments(arguments_json)
    if isinstance(arguments_or_error, ToolDispatchResult):
        return ToolDispatchResult(call_id=call_id, output=arguments_or_error.output)

    try:
        task = create_task_tool(conn, user_id=user_id, **arguments_or_error)
    except TaskValidationError as error:
        return _tool_error(call_id, error.code, "The task fields are invalid.")
    except Exception:
        return _tool_error(call_id, "tool_execution_failed", "The task could not be created.")

    return ToolDispatchResult(
        call_id=call_id,
        output={
            "ok": True,
            "task": {
                "ref": task.ref,
                "title": task.title,
            },
        },
    )


def _parse_create_task_tool_arguments(
    arguments_json: str,
) -> dict[str, Any] | ToolDispatchResult:
    """Load only arguments accepted by ``create_task_tool`` from model JSON."""
    if not isinstance(arguments_json, str):
        return _tool_error("", "malformed_arguments", "Tool arguments must be JSON text.")

    try:
        arguments = json.loads(arguments_json)
    except json.JSONDecodeError:
        return _tool_error("", "malformed_arguments", "Tool arguments must be valid JSON.")

    if not isinstance(arguments, dict):
        return _tool_error("", "malformed_arguments", "Tool arguments must be a JSON object.")

    allowed_argument_names = {
        "title",
        "description",
        "deadline_at",
        "deadline_type",
        "estimated_minutes",
        "urgency",
        "category_name",
        "parse_confidence",
    }
    unexpected_argument_names = set(arguments) - allowed_argument_names
    if unexpected_argument_names:
        return _tool_error(
            "",
            "unexpected_argument",
            "Tool arguments include an unsupported field.",
        )
    if "title" not in arguments:
        return _tool_error("", "missing_title", "A task title is required.")

    return arguments


def _tool_error(call_id: str, code: str, message: str) -> ToolDispatchResult:
    """Build a provider-neutral error result without exposing exception details."""
    return ToolDispatchResult(
        call_id=call_id,
        output={
            "ok": False,
            "error": {
                "code": code,
                "message": message,
            },
        },
    )


def create_task_tool(
    conn: Connection,
    *,
    user_id: str,
    title: str,
    description: str | None = None,
    deadline_at: str | None = None,
    deadline_type: str | None = None,
    estimated_minutes: int | None = None,
    urgency: str | None = None,
    category_name: str | None = None,
    parse_confidence: float | None = None,
) -> TaskRecord:
    """Create a task using only the fields the LLM may propose."""
    _validate_model_required_text_field(title, "title")
    _validate_model_optional_text_field(description, "description")
    _validate_model_optional_text_field(deadline_type, "deadline_type")
    _validate_model_optional_text_field(urgency, "urgency")
    _validate_model_estimated_minutes(estimated_minutes)
    deadline = _parse_deadline_at(deadline_at)
    confidence = _validate_model_parse_confidence(parse_confidence)
    category = _resolve_owner_category_id(
        conn,
        user_id=user_id,
        category_name=category_name,
    )

    return create_task(
        conn,
        user_id=user_id,
        title=title,
        source="telegram_nl",
        description=description,
        deadline_at=deadline,
        deadline_type=deadline_type,
        estimated_minutes=estimated_minutes,
        urgency=urgency,
        category_id=category,
        parse_status="parsed",
        parse_confidence=confidence,
    )


def _parse_deadline_at(deadline_at: str | None) -> datetime | None:
    """Parse an LLM deadline only when it includes an explicit timezone."""
    if deadline_at is None:
        return None
    if not isinstance(deadline_at, str):
        raise TaskValidationError(
            "invalid_deadline_at",
            "deadline_at must be an ISO-8601/RFC 3339 timestamp with a timezone offset.",
        )

    try:
        parsed_deadline_at = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise TaskValidationError(
            "invalid_deadline_at",
            "deadline_at must be an ISO-8601/RFC 3339 timestamp with a timezone offset.",
        ) from error

    if parsed_deadline_at.tzinfo is None or parsed_deadline_at.utcoffset() is None:
        raise TaskValidationError(
            "invalid_deadline_at",
            "deadline_at must be an ISO-8601/RFC 3339 timestamp with a timezone offset.",
        )

    return parsed_deadline_at


def _validate_model_required_text_field(value: str, field_name: str) -> None:
    """Reject missing or non-string required model fields."""
    if not isinstance(value, str):
        raise TaskValidationError(f"invalid_{field_name}", f"{field_name} must be a string.")


def _validate_model_optional_text_field(value: str | None, field_name: str) -> None:
    """Reject non-string model values before they reach application services."""
    if not isinstance(value, str) and value is not None:
        raise TaskValidationError(f"invalid_{field_name}", f"{field_name} must be a string.")


def _validate_model_estimated_minutes(estimated_minutes: int | None) -> None:
    """Reject non-integer model estimates before service-level validation."""
    if isinstance(estimated_minutes, bool) or (
        estimated_minutes is not None and not isinstance(estimated_minutes, int)
    ):
        raise TaskValidationError(
            "invalid_estimated_minutes",
            "estimated_minutes must be a positive integer.",
        )


def _validate_model_parse_confidence(parse_confidence: float | None) -> float | None:
    """Reject non-numeric confidence values before service-level validation."""
    if parse_confidence is None:
        return None
    if isinstance(parse_confidence, bool) or not isinstance(
        parse_confidence, (int, float)
    ):
        raise TaskValidationError(
            "invalid_parse_confidence",
            "parse_confidence must be a number between 0.0 and 1.0.",
        )
    return float(parse_confidence)


def _resolve_owner_category_id(
    conn: Connection,
    *,
    user_id: str,
    category_name: str | None,
) -> str | None:
    """Resolve an exact active category name belonging to the trusted owner."""
    _validate_model_optional_text_field(category_name, "category_name")
    if category_name is None:
        return None

    categories = list_categories_and_tags(conn, user_id=user_id).categories
    for category in categories:
        if category.name == category_name:
            return category.id

    raise TaskValidationError(
        "invalid_category",
        "Category does not exist for this user.",
    )
