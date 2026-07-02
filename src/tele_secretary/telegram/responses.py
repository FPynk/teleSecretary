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


def build_task_owner_not_configured_response() -> str:
    return "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using task commands."


def build_unauthorized_response() -> str:
    return "This Telegram account is not authorized to use TeleSecretary."
