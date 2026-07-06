"""Pure response builders for Telegram commands."""

from __future__ import annotations

from tele_secretary.app.help import get_help_text
from tele_secretary.app.tasks import TaskRecord


def build_ping_response() -> str:
    return "pong"


def build_help_response() -> str:
    return get_help_text()


def build_task_list_response(tasks: tuple[TaskRecord, ...]) -> str:
    if not tasks:
        return "No active tasks."

    lines = ["Active tasks:"]
    for index, task in enumerate(tasks, start=1):
        lines.append(f"{index}. {task.title}")
    return "\n".join(lines)


def build_task_created_response(task: TaskRecord, due_date_text: str | None = None) -> str:
    lines = [f"Task added: {task.title}"]
    if due_date_text is not None:
        lines.append(f"Due: {due_date_text}")
    return "\n".join(lines)


def build_addtask_usage_response() -> str:
    return "Usage: /addtask <title> [--due DD/MM/YYYY]"


def build_task_owner_not_configured_response() -> str:
    return "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using TeleSecretary."


def build_unauthorized_response() -> str:
    return "This Telegram account is not authorized to use TeleSecretary."
