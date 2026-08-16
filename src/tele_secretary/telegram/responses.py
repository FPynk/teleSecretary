"""Pure response builders for Telegram commands."""

from __future__ import annotations

from datetime import datetime

from tele_secretary.app.help import get_help_text
from tele_secretary.app.reminder_time_parser import ReminderTimeWarning
from tele_secretary.app.tasks import DueTaskRecord, FocusTaskRecord, TaskRecord
from tele_secretary.time_utils import utc_to_local


def build_ping_response() -> str:
    return "pong"


def build_help_response(topic: str | None = None) -> str:
    return get_help_text(topic)


def build_task_list_response(tasks: tuple[TaskRecord, ...]) -> str:
    if not tasks:
        return "No active tasks."

    lines = ["Active tasks:"]
    for task in tasks:
        lines.append(f"{task.ref} — {task.title}")
    return "\n".join(lines)


def build_today_focus_response(focus_tasks: tuple[FocusTaskRecord, ...]) -> str:
    if not focus_tasks:
        return "No tasks need your focus today."

    lines = ["Focus today:"]
    for index, focus_task in enumerate(focus_tasks, start=1):
        lines.append(
            f"{index}. {focus_task.task.ref} — {focus_task.task.title} — {focus_task.reason}"
        )
    return "\n".join(lines)


def build_due_usage_response() -> str:
    return "Usage: /due"


def build_due_tasks_response(due_tasks: tuple[DueTaskRecord, ...], timezone_name: str) -> str:
    if not due_tasks:
        return "No overdue or upcoming tasks."

    lines = ["Due tasks:"]
    for due_task in due_tasks:
        deadline = _format_optional_datetime(due_task.task.deadline_at, timezone_name)
        lines.append(
            f"{due_task.task.ref} â€” {due_task.task.title} â€” {due_task.timing} â€” {deadline}"
        )
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


def build_done_usage_response() -> str:
    return "Usage: /done T<number>"


def build_done_error_response(error_message: str) -> str:
    return f"Could not complete task: {error_message}"


def build_task_completed_response(task: TaskRecord) -> str:
    return f'Marked "{task.title}" as done.'


def build_reopen_usage_response() -> str:
    return "Usage: /reopen T<number>"


def build_reopen_error_response(error_message: str) -> str:
    return f"Could not reopen task: {error_message}"


def build_task_reopened_response(task: TaskRecord) -> str:
    return f'Reopened "{task.title}".'


def build_edit_usage_response(error_message: str | None = None) -> str:
    lines = []
    if error_message is not None:
        lines.append(f"Could not edit task: {error_message}")
    lines.extend(
        [
            "Usage: /edit T<number> -field value [-field value ...]",
            'Example: /edit T1 -title "New title" -urgency high',
            "Detailed help: /help edit",
        ]
    )
    return "\n".join(lines)


def build_edit_error_response(error_message: str) -> str:
    return f"Could not edit task: {error_message}"


def build_remind_usage_response() -> str:
    """Build guidance for a malformed `/remind` command."""
    return "\n".join(
        [
            "Usage: /remind T<number> <time>",
            "Example: /remind T12 tomorrow 2pm",
        ]
    )


def build_remind_missing_time_response(task: TaskRecord) -> str:
    """Ask for the missing reminder time while naming the resolved task."""
    return f'When should I remind you about "{task.title}"?'


def build_remind_error_response(error_message: str) -> str:
    """Build a safe, user-facing reminder validation failure."""
    return f"Could not set reminder: {error_message}"


def build_remind_persistence_error_response() -> str:
    """Build the generic reminder storage failure response."""
    return "Could not save the reminder. Please try again."


def build_reminder_created_response(
    task: TaskRecord,
    scheduled_at: str,
    timezone_name: str,
    warning: ReminderTimeWarning | None,
) -> str:
    """Confirm a persisted reminder and include any DST adjustment guidance."""
    localized_time = _format_optional_datetime(scheduled_at, timezone_name)
    lines = [f'Reminder set for "{task.title}" on {localized_time}.']
    if warning is ReminderTimeWarning.NONEXISTENT_TIME_MOVED_FORWARD:
        lines.append(
            "Note: That local time does not exist because the clocks change, "
            "so I used the next available time."
        )
    elif warning is ReminderTimeWarning.AMBIGUOUS_TIME_FIRST_OCCURRENCE:
        lines.append(
            "Note: That local time occurs twice because the clocks change, "
            "so I used the first occurrence."
        )
    return "\n".join(lines)


def build_unremind_usage_response() -> str:
    """Build guidance for an invalid `/unremind` command."""
    return "Usage: /unremind T<number>"


def build_unremind_no_pending_response(task: TaskRecord) -> str:
    """Explain that an owned task has no pending reminders to cancel."""
    return f'No pending reminders for "{task.title}".'


def build_unremind_cancelled_response(task: TaskRecord) -> str:
    """Confirm cancellation of an owned task's sole pending reminder."""
    return f'Cancelled the reminder for "{task.title}".'


def build_unremind_multiple_pending_response(task: TaskRecord) -> str:
    """Avoid guessing which of several pending reminders the user meant."""
    return f'"{task.title}" has multiple pending reminders. None were cancelled.'


def build_unremind_stale_response(task_ref: str) -> str:
    """Ask the user to reload a reminder that changed before cancellation."""
    return f"That reminder is no longer pending. Run /unremind {task_ref} again."


def build_unremind_persistence_error_response() -> str:
    """Build the generic reply for a reminder-cancellation storage failure."""
    return "Could not cancel the reminder. Please try again."


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


def build_task_updated_response(
    task: TaskRecord,
    timezone_name: str,
    changed_fields: tuple[str, ...],
) -> str:
    lines = [f"Updated {task.ref} — {task.title}"]
    for field_name in changed_fields:
        if field_name == "title":
            lines.append(f"Title: {task.title}")
        elif field_name == "description":
            lines.append(f"Description: {task.description or 'None'}")
        elif field_name == "category":
            lines.append(f"Category: {task.category_name or 'None'}")
        elif field_name == "deadline":
            deadline = _format_optional_datetime(task.deadline_at, timezone_name)
            if deadline is not None and task.deadline_type is not None:
                deadline = f"{deadline} ({task.deadline_type})"
            lines.append(f"Deadline: {deadline or 'None'}")
        elif field_name == "planned_window":
            planned_start = _format_optional_datetime(
                task.planned_start_at,
                timezone_name,
            )
            planned_end = _format_optional_datetime(
                task.planned_end_at,
                timezone_name,
            )
            if planned_start is not None and planned_end is not None:
                planned_window = f"{planned_start} — {planned_end}"
            elif planned_start is not None:
                planned_window = f"Starts {planned_start}"
            elif planned_end is not None:
                planned_window = f"Ends {planned_end}"
            else:
                planned_window = "None"
            lines.append(f"Planned window: {planned_window}")
        elif field_name == "estimate":
            estimate = (
                f"{task.estimated_minutes} minutes"
                if task.estimated_minutes is not None
                else "None"
            )
            lines.append(f"Estimate: {estimate}")
        elif field_name == "urgency":
            lines.append(f"Urgency: {task.urgency or 'None'}")
        elif field_name == "tags":
            lines.append(f"Tags: {', '.join(tag.name for tag in task.tags) or 'None'}")
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
