"""Bounded LLM tool adapters that call public application services."""

from __future__ import annotations

from datetime import datetime
from sqlite3 import Connection

from tele_secretary.app.tasks import TaskRecord, TaskValidationError, create_task


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
) -> TaskRecord:
    """Create a task using only the fields the LLM may propose."""
    _validate_model_required_text_field(title, "title")
    _validate_model_optional_text_field(description, "description")
    _validate_model_optional_text_field(deadline_type, "deadline_type")
    _validate_model_optional_text_field(urgency, "urgency")
    _validate_model_estimated_minutes(estimated_minutes)

    return create_task(
        conn,
        user_id=user_id,
        title=title,
        source="telegram_nl",
        description=description,
        deadline_at=_parse_deadline_at(deadline_at),
        deadline_type=deadline_type,
        estimated_minutes=estimated_minutes,
        urgency=urgency,
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
