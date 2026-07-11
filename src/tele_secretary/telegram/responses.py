"""Pure response builders for Telegram commands."""

from __future__ import annotations

from datetime import datetime

from tele_secretary.app.help import get_help_text
from tele_secretary.app.tasks import TaskRecord
from tele_secretary.time_utils import utc_to_local


def build_ping_response() -> str:
    return "pong"


def build_help_response() -> str:
    return get_help_text()


def build_task_list_response(tasks: tuple[TaskRecord, ...]) -> str:
    if not tasks:
        return "No active tasks."

    lines = ["Active tasks:"]
    for task in tasks:
        lines.append(f"{task.ref} — {task.title}")
    return "\n".join(lines)


def build_task_created_response(task: TaskRecord, due_date_text: str | None = None) -> str:
    lines = [f"Task added: {task.ref} — {task.title}"]
    if due_date_text is not None:
        lines.append(f"Due: {due_date_text}")
    return "\n".join(lines)


def build_addtask_usage_response() -> str:
    return "Usage: /addtask <title> [-due DD/MM/YYYY]"


def build_show_usage_response() -> str:
    return "Usage: /show T<number>"


def build_task_not_found_response(task_ref: str) -> str:
    return f"Task {task_ref} was not found. Use /list to see active task refs."


def build_task_details_response(task: TaskRecord, timezone_name: str) -> str:
    deadline = _format_optional_datetime(task.deadline_at, timezone_name)
    if deadline is not None and task.deadline_type is not None:
        deadline = f"{deadline} ({task.deadline_type})"

    planned_start = _format_optional_datetime(task.planned_start_at, timezone_name)
    planned_end = _format_optional_datetime(task.planned_end_at, timezone_name)
    if planned_start is not None and planned_end is not None:
        planned_window = f"{planned_start} — {planned_end}"
    elif planned_start is not None:
        planned_window = f"Starts {planned_start}"
    elif planned_end is not None:
        planned_window = f"Ends {planned_end}"
    else:
        planned_window = None

    lines = [
        f"{task.ref} — {task.title}",
        f"Status: {task.status}",
        f"Description: {task.description or 'None'}",
        f"Category: {task.category_name or 'None'}",
        f"Deadline: {deadline or 'None'}",
        f"Planned window: {planned_window or 'None'}",
        f"Urgency: {task.urgency or 'None'}",
        f"Estimate: {f'{task.estimated_minutes} minutes' if task.estimated_minutes else 'None'}",
        f"Tags: {', '.join(tag.name for tag in task.tags) or 'None'}",
        "Reminders: None",
    ]
    return "\n".join(lines)


def _format_optional_datetime(value: str | None, timezone_name: str) -> str | None:
    if value is None:
        return None
    local_value = utc_to_local(datetime.fromisoformat(value), timezone_name)
    hour = local_value.strftime("%I").lstrip("0") or "0"
    return (
        f"{local_value.strftime('%a %b')} {local_value.day}, {local_value.year} "
        f"at {hour}:{local_value.strftime('%M %p')}"
    )


def build_task_owner_not_configured_response() -> str:
    return "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using TeleSecretary."


def build_unauthorized_response() -> str:
    return "This Telegram account is not authorized to use TeleSecretary."
