"""Telegram long-polling bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import re
import sqlite3
from typing import Any

from tele_secretary.config import AppConfig
from tele_secretary.app.reminder_time_parser import (
    ParsedReminderTime,
    ReminderTimeParseError,
    parse_reminder_time_expression,
)
from tele_secretary.app.reminders import (
    ReminderNotFoundError,
    ReminderServiceError,
    ReminderValidationError,
    cancel_pending_reminder,
    create_reminder,
    list_pending_reminders_for_task,
)
from tele_secretary.app.tasks import (
    TaskNotFoundError,
    TaskValidationError,
    complete_task,
    create_task,
    edit_task_by_ref,
    get_task_details_by_ref,
    get_focus_today,
    list_active_tasks,
    list_urgent_tasks,
    reopen_task,
    soft_delete_task,
)
from tele_secretary.app.users import (
    bind_unassigned_legacy_single_owner,
    get_or_create_telegram_user,
)
from tele_secretary.persistence.connection import connect
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.scheduler.runner import Scheduler, process_reminder_cycle
from tele_secretary.time_utils import local_to_utc
from tele_secretary.telegram.edit_command import (
    EditTaskCommandParseError,
    parse_edit_task_command_text,
)
from tele_secretary.telegram.responses import (
    build_addtask_usage_response,
    build_done_error_response,
    build_done_usage_response,
    build_delete_error_response,
    build_delete_usage_response,
    build_edit_error_response,
    build_edit_usage_response,
    build_help_response,
    build_ping_response,
    build_remind_error_response,
    build_remind_missing_time_response,
    build_remind_persistence_error_response,
    build_remind_usage_response,
    build_reminder_created_response,
    build_reopen_error_response,
    build_reopen_usage_response,
    build_task_reopened_response,
    build_show_usage_response,
    build_task_created_response,
    build_task_completed_response,
    build_task_deleted_response,
    build_task_details_response,
    build_task_not_found_response,
    build_task_updated_response,
    build_task_owner_not_configured_response,
    build_task_list_response,
    build_today_focus_response,
    build_unauthorized_response,
    build_unremind_cancelled_response,
    build_unremind_multiple_pending_response,
    build_unremind_no_pending_response,
    build_unremind_persistence_error_response,
    build_unremind_stale_response,
    build_unremind_usage_response,
    build_urgent_tasks_response,
    build_urgent_usage_response,
)

LOGGER = logging.getLogger(__name__)
ADD_TASK_DUE_DATE_PATTERN = re.compile(r"^\d{2}/\d{2}/\d{4}$")
ADD_TASK_DUE_FLAG_PATTERN = re.compile(r"(?<!\S)-due(?!\S)")
ADD_TASK_DUE_LIKE_PATTERN = re.compile(r"(?<!\S)-{1,2}due")
TASK_REF_PATTERN = re.compile(r"T[1-9]\d*", re.IGNORECASE)
REMIND_COMMAND_TOKEN_PATTERN = re.compile(r"/remind(?:@[A-Za-z0-9_]+)?", re.IGNORECASE)
UNREMIND_COMMAND_TOKEN_PATTERN = re.compile(r"/unremind(?:@[A-Za-z0-9_]+)?", re.IGNORECASE)
URGENT_COMMAND_TOKEN_PATTERN = re.compile(r"/urgent(?:@[A-Za-z0-9_]+)?", re.IGNORECASE)
DELETE_COMMAND_TOKEN_PATTERN = re.compile(r"/delete(?:@[A-Za-z0-9_]+)?", re.IGNORECASE)
SCHEDULER_BOT_DATA_KEY = "_tele_secretary_reminder_scheduler"


@dataclass(frozen=True)
class ParsedAddTaskCommand:
    title: str
    deadline_at: datetime | None
    due_date_text: str | None


class AddTaskCommandParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedRemindCommand:
    """Telegram command envelope fields for a reminder request."""

    task_ref: str
    time_expression: str | None


class RemindCommandParseError(ValueError):
    """The `/remind` command envelope is malformed."""


def run_bot(config: AppConfig) -> None:
    """Apply migrations and start the configured Telegram bot."""
    if not config.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to start the bot.")

    conn = connect(config.db_path)
    try:
        apply_migrations(conn)
        # TSEC-39: Remove this one-time legacy binding after the upgrade window closes.
        bind_unassigned_legacy_single_owner(
            conn,
            allowed_telegram_user_ids=config.telegram_allowed_user_ids,
        )
    finally:
        conn.close()

    application = build_application(config)
    LOGGER.info("Starting Telegram long-polling bot.")
    application.run_polling()


def build_application(config: AppConfig) -> Any:
    """Build the configured Telegram application and register its commands."""
    try:
        from telegram.ext import Application, CommandHandler
    except ImportError as exc:
        raise RuntimeError(
            "python-telegram-bot is not installed. "
            "Install project dependencies before starting the bot."
        ) from exc

    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .post_init(_scheduler_post_init(config))
        .post_stop(_scheduler_post_stop)
        .build()
    )
    application.add_handler(CommandHandler("ping", _ping_handler(config)))
    application.add_handler(CommandHandler("help", _help_handler(config)))
    application.add_handler(CommandHandler("list", _list_handler(config)))
    application.add_handler(CommandHandler("addtask", _addtask_handler(config)))
    application.add_handler(CommandHandler("show", _show_handler(config)))
    application.add_handler(CommandHandler("edit", _edit_handler(config)))
    application.add_handler(CommandHandler("done", _done_handler(config)))
    application.add_handler(CommandHandler("reopen", _reopen_handler(config)))
    application.add_handler(CommandHandler("delete", _delete_handler(config)))
    application.add_handler(CommandHandler("today", _today_handler(config)))
    application.add_handler(CommandHandler("urgent", _urgent_handler(config)))
    application.add_handler(CommandHandler("remind", _remind_handler(config)))
    application.add_handler(CommandHandler("unremind", _unremind_handler(config)))
    return application


def _scheduler_post_init(config: AppConfig) -> Any:
    """Build the Telegram lifecycle callback that starts reminder processing."""

    async def post_init(application: Any) -> None:
        """Start one scheduler after Telegram and the migrated database are ready."""

        async def run_cycle(cycle_time: datetime) -> None:
            await process_reminder_cycle(
                db_path=config.db_path,
                allowed_telegram_user_ids=config.telegram_allowed_user_ids,
                send_message=application.bot.send_message,
                cycle_time=cycle_time,
            )

        scheduler = Scheduler(run_cycle=run_cycle)
        application.bot_data[SCHEDULER_BOT_DATA_KEY] = scheduler
        scheduler.start()

    return post_init


async def _scheduler_post_stop(application: Any) -> None:
    """Stop the scheduler before the Telegram application shuts down."""

    scheduler = application.bot_data.get(SCHEDULER_BOT_DATA_KEY)
    if scheduler is not None:
        await scheduler.stop()


def _ping_handler(config: AppConfig) -> Any:
    """Build the authorized `/ping` command handler."""
    async def handler(update: Any, context: Any) -> None:
        """Reply with the service-health response for an authorized sender."""
        del context
        if await _get_authorized_telegram_user_id(update, config) is None:
            return
        if update.message is None:
            return
        await update.message.reply_text(build_ping_response())

    return handler


def _help_handler(config: AppConfig) -> Any:
    """Build the authorized `/help` command handler."""
    async def handler(update: Any, context: Any) -> None:
        """Reply with general or topic-specific help for an authorized sender."""
        del context
        if await _get_authorized_telegram_user_id(update, config) is None:
            return
        if update.message is None:
            return
        command_parts = (update.message.text or "").strip().split()
        topic = None if len(command_parts) == 1 else " ".join(command_parts[1:])
        await update.message.reply_text(build_help_response(topic))

    return handler


def _list_handler(config: AppConfig) -> Any:
    """Build the owner-scoped `/list` command handler."""
    async def handler(update: Any, context: Any) -> None:
        """List active tasks belonging to the authorized sender."""
        del context
        telegram_user_id = await _get_authorized_telegram_user_id(update, config)
        if telegram_user_id is None:
            return
        if update.message is None:
            return

        conn = connect(config.db_path)
        try:
            user = get_or_create_telegram_user(
                conn,
                telegram_user_id=telegram_user_id,
                default_timezone=config.user_timezone,
            )
            tasks = list_active_tasks(conn, user_id=user.user_id)
        finally:
            conn.close()

        await update.message.reply_text(build_task_list_response(tasks))

    return handler


def _addtask_handler(config: AppConfig) -> Any:
    """Build the owner-scoped `/addtask` command handler."""
    async def handler(update: Any, context: Any) -> None:
        """Create a task using the authorized sender's persisted timezone."""
        del context
        telegram_user_id = await _get_authorized_telegram_user_id(update, config)
        if telegram_user_id is None:
            return
        if update.message is None:
            return

        command_text = update.message.text or ""
        conn = connect(config.db_path)
        try:
            user = get_or_create_telegram_user(
                conn,
                telegram_user_id=telegram_user_id,
                default_timezone=config.user_timezone,
            )
            try:
                parsed_command = parse_addtask_command_text(command_text, user.timezone)
            except AddTaskCommandParseError:
                task = None
            else:
                task = create_task(
                    conn,
                    user_id=user.user_id,
                    title=parsed_command.title,
                    source="telegram_command",
                    deadline_at=parsed_command.deadline_at,
                    deadline_type="hard" if parsed_command.deadline_at is not None else None,
                    raw_input_text=command_text,
                )
        finally:
            conn.close()

        if task is None:
            await update.message.reply_text(build_addtask_usage_response())
            return
        await update.message.reply_text(
            build_task_created_response(task, parsed_command.due_date_text)
        )

    return handler


def _show_handler(config: AppConfig) -> Any:
    """Build the owner-scoped `/show` command handler."""
    async def handler(update: Any, context: Any) -> None:
        """Show a task only when it belongs to the authorized sender."""
        del context
        telegram_user_id = await _get_authorized_telegram_user_id(update, config)
        if telegram_user_id is None:
            return
        if update.message is None:
            return

        conn = connect(config.db_path)
        try:
            user = get_or_create_telegram_user(
                conn,
                telegram_user_id=telegram_user_id,
                default_timezone=config.user_timezone,
            )
            task_ref = parse_show_command_text(update.message.text or "")
            if task_ref is None:
                response = build_show_usage_response()
            else:
                try:
                    task = get_task_details_by_ref(
                        conn,
                        user_id=user.user_id,
                        task_ref=task_ref,
                    )
                except TaskNotFoundError:
                    response = build_task_not_found_response(task_ref)
                else:
                    response = build_task_details_response(task, user.timezone)
        finally:
            conn.close()

        await update.message.reply_text(response)

    return handler


def _edit_handler(config: AppConfig) -> Any:
    """Build the owner-scoped `/edit` command handler."""
    async def handler(update: Any, context: Any) -> None:
        """Edit a task using the authorized sender's persisted timezone."""
        del context
        telegram_user_id = await _get_authorized_telegram_user_id(update, config)
        if telegram_user_id is None:
            return
        if update.message is None:
            return

        command_parts = (update.message.text or "").strip().split()
        if len(command_parts) == 2 and command_parts[1].lower() == "-help":
            await update.message.reply_text(build_help_response("edit"))
            return

        conn = connect(config.db_path)
        try:
            user = get_or_create_telegram_user(
                conn,
                telegram_user_id=telegram_user_id,
                default_timezone=config.user_timezone,
            )
            try:
                parsed_command = parse_edit_task_command_text(
                    update.message.text or "",
                    user.timezone,
                )
            except EditTaskCommandParseError as exc:
                response = build_edit_usage_response(str(exc))
            else:
                try:
                    task = edit_task_by_ref(
                        conn,
                        user_id=user.user_id,
                        task_ref=parsed_command.task_ref,
                        source="telegram_command",
                        task_field_updates=parsed_command.task_field_updates,
                        category_was_provided=parsed_command.category_was_provided,
                        category_name=parsed_command.category_name,
                        add_tag_names=parsed_command.add_tag_names,
                        remove_tag_names=parsed_command.remove_tag_names,
                        clear_tags=parsed_command.clear_tags,
                    )
                except TaskNotFoundError:
                    response = build_task_not_found_response(parsed_command.task_ref)
                except TaskValidationError as exc:
                    response = build_edit_error_response(exc.message)
                else:
                    response = build_task_updated_response(
                        task,
                        user.timezone,
                        parsed_command.changed_fields,
                    )
        finally:
            conn.close()

        await update.message.reply_text(response)

    return handler


def _reopen_handler(config: AppConfig) -> Any:
    """Build the owner-scoped `/reopen` command handler."""
    async def handler(update: Any, context: Any) -> None:
        """Reopen a task only when it belongs to the authorized sender."""
        del context
        telegram_user_id = await _get_authorized_telegram_user_id(update, config)
        if telegram_user_id is None:
            return
        if update.message is None:
            return

        conn = connect(config.db_path)
        try:
            user = get_or_create_telegram_user(
                conn,
                telegram_user_id=telegram_user_id,
                default_timezone=config.user_timezone,
            )
            task_ref = parse_reopen_command_text(update.message.text or "")
            if task_ref is None:
                response = build_reopen_usage_response()
            else:
                try:
                    task = get_task_details_by_ref(
                        conn,
                        user_id=user.user_id,
                        task_ref=task_ref,
                    )
                    task = reopen_task(
                        conn,
                        user_id=user.user_id,
                        task_id=task.id,
                        source="telegram_command",
                    )
                except TaskNotFoundError:
                    response = build_task_not_found_response(task_ref)
                except TaskValidationError as exc:
                    response = build_reopen_error_response(exc.message)
                else:
                    response = build_task_reopened_response(task)
        finally:
            conn.close()

        await update.message.reply_text(response)

    return handler


def _done_handler(config: AppConfig) -> Any:
    """Build the owner-scoped `/done` command handler."""
    async def handler(update: Any, context: Any) -> None:
        """Complete a task only when it belongs to the authorized sender."""
        del context
        telegram_user_id = await _get_authorized_telegram_user_id(update, config)
        if telegram_user_id is None:
            return
        if update.message is None:
            return

        conn = connect(config.db_path)
        try:
            user = get_or_create_telegram_user(
                conn,
                telegram_user_id=telegram_user_id,
                default_timezone=config.user_timezone,
            )
            task_ref = parse_done_command_text(update.message.text or "")
            if task_ref is None:
                response = build_done_usage_response()
            else:
                try:
                    task = get_task_details_by_ref(
                        conn,
                        user_id=user.user_id,
                        task_ref=task_ref,
                    )
                    task = complete_task(
                        conn,
                        user_id=user.user_id,
                        task_id=task.id,
                        source="telegram_command",
                    )
                except TaskNotFoundError:
                    response = build_task_not_found_response(task_ref)
                except TaskValidationError as exc:
                    response = build_done_error_response(exc.message)
                else:
                    response = build_task_completed_response(task)
        finally:
            conn.close()

        await update.message.reply_text(response)

    return handler


def _delete_handler(config: AppConfig) -> Any:
    """Build the owner-scoped `/delete` command handler."""
    async def handler(update: Any, context: Any) -> None:
        """Soft-delete one owned task through the application service."""
        del context
        telegram_user_id = await _get_authorized_telegram_user_id(update, config)
        if telegram_user_id is None:
            return
        if update.message is None:
            return

        task_ref = parse_delete_command_text(update.message.text or "")
        if task_ref is None:
            await update.message.reply_text(build_delete_usage_response())
            return

        conn = connect(config.db_path)
        try:
            user = get_or_create_telegram_user(
                conn,
                telegram_user_id=telegram_user_id,
                default_timezone=config.user_timezone,
            )
            try:
                task = get_task_details_by_ref(
                    conn,
                    user_id=user.user_id,
                    task_ref=task_ref,
                )
                soft_delete_task(
                    conn,
                    user_id=user.user_id,
                    task_id=task.id,
                    source="telegram_command",
                )
            except TaskNotFoundError:
                response = build_task_not_found_response(task_ref)
            except TaskValidationError as exc:
                response = build_delete_error_response(exc.message)
            else:
                response = build_task_deleted_response(task)
        finally:
            conn.close()

        await update.message.reply_text(response)

    return handler


def _today_handler(config: AppConfig) -> Any:
    """Build the owner-scoped `/today` command handler."""
    async def handler(update: Any, context: Any) -> None:
        """List today's tasks using the authorized sender's persisted timezone."""
        del context
        telegram_user_id = await _get_authorized_telegram_user_id(update, config)
        if telegram_user_id is None:
            return
        if update.message is None:
            return

        conn = connect(config.db_path)
        try:
            user = get_or_create_telegram_user(
                conn,
                telegram_user_id=telegram_user_id,
                default_timezone=config.user_timezone,
            )
            focus_tasks = get_focus_today(
                conn,
                user_id=user.user_id,
                timezone_name=user.timezone,
            )
        finally:
            conn.close()

        await update.message.reply_text(build_today_focus_response(focus_tasks))

    return handler


def _urgent_handler(config: AppConfig) -> Any:
    """Build the owner-scoped `/urgent` command handler."""
    async def handler(update: Any, context: Any) -> None:
        """List the authenticated owner's high-priority tasks."""
        del context
        telegram_user_id = await _get_authorized_telegram_user_id(update, config)
        if telegram_user_id is None:
            return
        if update.message is None:
            return
        if not parse_urgent_command_text(update.message.text or ""):
            await update.message.reply_text(build_urgent_usage_response())
            return

        conn = connect(config.db_path)
        try:
            user = get_or_create_telegram_user(
                conn,
                telegram_user_id=telegram_user_id,
                default_timezone=config.user_timezone,
            )
            urgent_tasks = list_urgent_tasks(conn, user_id=user.user_id)
        finally:
            conn.close()

        await update.message.reply_text(build_urgent_tasks_response(urgent_tasks))

    return handler


def _remind_handler(config: AppConfig) -> Any:
    """Build the `/remind` handler for each authorized Telegram owner."""
    async def handler(update: Any, context: Any) -> None:
        """Create an owner-scoped reminder using the sender's persisted timezone."""
        del context
        telegram_user_id = await _get_authorized_telegram_user_id(update, config)
        if telegram_user_id is None:
            return
        if update.message is None:
            return

        conn = connect(config.db_path)
        try:
            user = get_or_create_telegram_user(
                conn,
                telegram_user_id=telegram_user_id,
                default_timezone=config.user_timezone,
            )
            try:
                command = parse_remind_command_text(update.message.text or "")
            except RemindCommandParseError:
                response = build_remind_usage_response()
            else:
                try:
                    task = get_task_details_by_ref(
                        conn,
                        user_id=user.user_id,
                        task_ref=command.task_ref,
                    )
                    if command.time_expression is None:
                        response = build_remind_missing_time_response(task)
                    else:
                        parsed_time: ParsedReminderTime = parse_reminder_time_expression(
                            command.time_expression,
                            user.timezone,
                        )
                        reminder = create_reminder(
                            conn,
                            user_id=user.user_id,
                            task_id=task.id,
                            scheduled_at=parsed_time.scheduled_at,
                        )
                        response = build_reminder_created_response(
                            task,
                            reminder.scheduled_at,
                            user.timezone,
                            parsed_time.warning,
                        )
                except TaskNotFoundError:
                    response = build_task_not_found_response(command.task_ref)
                except ReminderTimeParseError as error:
                    response = build_remind_error_response(str(error))
                except ReminderNotFoundError:
                    response = build_task_not_found_response(command.task_ref)
                except ReminderServiceError as error:
                    response = build_remind_error_response(error.message)
                except sqlite3.Error:
                    LOGGER.exception("Could not persist reminder for %s", command.task_ref)
                    response = build_remind_persistence_error_response()
        finally:
            conn.close()

        await update.message.reply_text(response)

    return handler


def _unremind_handler(config: AppConfig) -> Any:
    """Build the owner-scoped `/unremind` handler."""
    async def handler(update: Any, context: Any) -> None:
        """Cancel a sole pending reminder without guessing among multiple reminders."""
        del context
        telegram_user_id = await _get_authorized_telegram_user_id(update, config)
        if telegram_user_id is None:
            return
        if update.message is None:
            return

        task_ref = parse_unremind_command_text(update.message.text or "")
        if task_ref is None:
            await update.message.reply_text(build_unremind_usage_response())
            return

        conn = connect(config.db_path)
        try:
            try:
                user = get_or_create_telegram_user(
                    conn,
                    telegram_user_id=telegram_user_id,
                    default_timezone=config.user_timezone,
                )
                task = get_task_details_by_ref(
                    conn,
                    user_id=user.user_id,
                    task_ref=task_ref,
                )
                pending_reminders = list_pending_reminders_for_task(
                    conn,
                    user_id=user.user_id,
                    task_id=task.id,
                )
                if not pending_reminders:
                    response = build_unremind_no_pending_response(task)
                elif len(pending_reminders) == 1:
                    cancellation = cancel_pending_reminder(
                        conn,
                        user_id=user.user_id,
                        reminder_id=pending_reminders[0].id,
                    )
                    response = (
                        build_unremind_cancelled_response(task)
                        if cancellation.was_cancelled
                        else build_unremind_stale_response(task_ref)
                    )
                else:
                    response = build_unremind_multiple_pending_response(task)
            except (TaskNotFoundError, ReminderNotFoundError):
                response = build_task_not_found_response(task_ref)
            except ReminderValidationError:
                response = build_unremind_stale_response(task_ref)
            except sqlite3.Error:
                LOGGER.exception("Could not cancel reminder for %s", task_ref)
                response = build_unremind_persistence_error_response()
        finally:
            conn.close()

        await update.message.reply_text(response)

    return handler


def parse_show_command_text(command_text: str) -> str | None:
    """Parse a task reference from a `/show` command."""
    command_parts = command_text.strip().split()
    if len(command_parts) != 2 or TASK_REF_PATTERN.fullmatch(command_parts[1]) is None:
        return None
    return command_parts[1].upper()


def parse_reopen_command_text(command_text: str) -> str | None:
    """Parse a task reference from a `/reopen` command."""
    return parse_show_command_text(command_text)


def parse_done_command_text(command_text: str) -> str | None:
    """Parse a task reference from a `/done` command."""
    return parse_show_command_text(command_text)


def parse_urgent_command_text(command_text: str) -> bool:
    """Return whether text is an argument-free `/urgent` command envelope."""
    command_parts = command_text.strip().split()
    return (
        len(command_parts) == 1
        and URGENT_COMMAND_TOKEN_PATTERN.fullmatch(command_parts[0]) is not None
    )


def parse_delete_command_text(command_text: str) -> str | None:
    """Parse the sole task reference accepted by `/delete`."""
    command_parts = command_text.strip().split()
    if (
        len(command_parts) != 2
        or DELETE_COMMAND_TOKEN_PATTERN.fullmatch(command_parts[0]) is None
        or TASK_REF_PATTERN.fullmatch(command_parts[1]) is None
    ):
        return None
    return command_parts[1].upper()


def parse_remind_command_text(command_text: str) -> ParsedRemindCommand:
    """Separate a `/remind` envelope from its untouched time expression."""
    normalized_command_text = command_text.strip()
    command_parts = normalized_command_text.split(maxsplit=1)
    if len(command_parts) != 2 or REMIND_COMMAND_TOKEN_PATTERN.fullmatch(command_parts[0]) is None:
        raise RemindCommandParseError("Remind command requires a task reference.")

    remaining_text = command_parts[1]
    task_ref_match = re.match(r"T[1-9]\d*(?=$|\s)", remaining_text, re.IGNORECASE)
    if task_ref_match is None:
        raise RemindCommandParseError("Remind command has an invalid task reference.")

    time_expression = remaining_text[task_ref_match.end() :].lstrip()
    return ParsedRemindCommand(
        task_ref=task_ref_match.group().upper(),
        time_expression=time_expression or None,
    )


def parse_unremind_command_text(command_text: str) -> str | None:
    """Parse the sole task reference accepted by the initial `/unremind` command."""
    command_parts = command_text.strip().split()
    if (
        len(command_parts) != 2
        or UNREMIND_COMMAND_TOKEN_PATTERN.fullmatch(command_parts[0]) is None
        or TASK_REF_PATTERN.fullmatch(command_parts[1]) is None
    ):
        return None
    return command_parts[1].upper()


def parse_addtask_command_text(command_text: str, timezone_name: str) -> ParsedAddTaskCommand:
    """Parse an `/addtask` command and convert its optional due date to UTC."""
    command_parts = command_text.strip().split(maxsplit=1)
    if len(command_parts) != 2:
        raise AddTaskCommandParseError("Add task command requires a title.")

    task_text = command_parts[1].strip()
    if not task_text:
        raise AddTaskCommandParseError("Add task command requires a title.")

    due_flag_matches = list(ADD_TASK_DUE_FLAG_PATTERN.finditer(task_text))
    if not due_flag_matches:
        if ADD_TASK_DUE_LIKE_PATTERN.search(task_text) is not None:
            raise AddTaskCommandParseError("Add task command has an invalid due flag.")
        return ParsedAddTaskCommand(title=task_text, deadline_at=None, due_date_text=None)
    if len(due_flag_matches) != 1:
        raise AddTaskCommandParseError("Add task command supports one due date.")

    due_flag_match = due_flag_matches[0]
    title = task_text[: due_flag_match.start()].strip()
    due_date_text = task_text[due_flag_match.end() :].strip()
    if (
        " " in due_date_text
        or not title
        or not ADD_TASK_DUE_DATE_PATTERN.fullmatch(due_date_text)
    ):
        raise AddTaskCommandParseError("Add task command has an invalid due date.")

    try:
        due_date = datetime.strptime(due_date_text, "%d/%m/%Y").date()
    except ValueError as exc:
        raise AddTaskCommandParseError("Add task command has an invalid due date.") from exc

    local_deadline_at = datetime(
        due_date.year,
        due_date.month,
        due_date.day,
        23,
        59,
    )
    return ParsedAddTaskCommand(
        title=title,
        deadline_at=local_to_utc(local_deadline_at, timezone_name),
        due_date_text=due_date_text,
    )


async def _get_authorized_telegram_user_id(
    update: Any,
    config: AppConfig,
) -> int | None:
    """Return the authorized sender ID without touching persistent identity data."""
    allowed_ids = config.telegram_allowed_user_ids
    if not allowed_ids:
        if update.message is not None:
            await update.message.reply_text(build_task_owner_not_configured_response())
        return None

    user = update.effective_user
    if user is not None and user.id in allowed_ids:
        return user.id

    if update.message is not None:
        await update.message.reply_text(build_unauthorized_response())
    return None
