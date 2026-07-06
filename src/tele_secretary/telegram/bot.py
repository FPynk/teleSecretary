"""Telegram long-polling bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import re
from typing import Any

from tele_secretary.config import AppConfig
from tele_secretary.app.tasks import create_task, list_active_tasks
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.persistence.connection import connect
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.time_utils import local_to_utc
from tele_secretary.telegram.responses import (
    build_addtask_usage_response,
    build_help_response,
    build_ping_response,
    build_task_created_response,
    build_task_owner_not_configured_response,
    build_task_list_response,
    build_unauthorized_response,
)

LOGGER = logging.getLogger(__name__)
ADD_TASK_DUE_DATE_PATTERN = re.compile(r"^\d{2}/\d{2}/\d{4}$")
ADD_TASK_DUE_FLAG_PATTERN = re.compile(r"(?<!\S)--due(?!\S)")


@dataclass(frozen=True)
class ParsedAddTaskCommand:
    title: str
    deadline_at: datetime | None
    due_date_text: str | None


class AddTaskCommandParseError(ValueError):
    pass


def run_bot(config: AppConfig) -> None:
    if not config.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to start the bot.")

    conn = connect(config.db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()

    application = build_application(config)
    LOGGER.info("Starting Telegram long-polling bot.")
    application.run_polling()


def build_application(config: AppConfig) -> Any:
    try:
        from telegram.ext import Application, CommandHandler
    except ImportError as exc:
        raise RuntimeError(
            "python-telegram-bot is not installed. "
            "Install project dependencies before starting the bot."
        ) from exc

    application = Application.builder().token(config.telegram_bot_token).build()
    application.add_handler(CommandHandler("ping", _ping_handler(config)))
    application.add_handler(CommandHandler("help", _help_handler(config)))
    application.add_handler(CommandHandler("list", _list_handler(config)))
    application.add_handler(CommandHandler("addtask", _addtask_handler(config)))
    return application


def _ping_handler(config: AppConfig) -> Any:
    async def handler(update: Any, context: Any) -> None:
        del context
        if not await _ensure_authorized(update, config):
            return
        await update.message.reply_text(build_ping_response())

    return handler


def _help_handler(config: AppConfig) -> Any:
    async def handler(update: Any, context: Any) -> None:
        del context
        if not await _ensure_authorized(update, config):
            return
        await update.message.reply_text(build_help_response())

    return handler


def _list_handler(config: AppConfig) -> Any:
    async def handler(update: Any, context: Any) -> None:
        del context
        if not await _ensure_authorized(update, config):
            return
        if update.message is None or update.effective_user is None:
            return

        conn = connect(config.db_path)
        try:
            user_id = get_or_create_telegram_user_id(
                conn,
                telegram_user_id=config.telegram_allowed_user_ids[0],
                timezone=config.user_timezone,
            )
            tasks = list_active_tasks(conn, user_id=user_id)
        finally:
            conn.close()

        await update.message.reply_text(build_task_list_response(tasks))

    return handler


def _addtask_handler(config: AppConfig) -> Any:
    async def handler(update: Any, context: Any) -> None:
        del context
        if not await _ensure_authorized(update, config):
            return
        if update.message is None or update.effective_user is None:
            return

        command_text = update.message.text or ""
        try:
            parsed_command = parse_addtask_command_text(command_text, config.user_timezone)
        except AddTaskCommandParseError:
            await update.message.reply_text(build_addtask_usage_response())
            return

        conn = connect(config.db_path)
        try:
            user_id = get_or_create_telegram_user_id(
                conn,
                telegram_user_id=config.telegram_allowed_user_ids[0],
                timezone=config.user_timezone,
            )
            task = create_task(
                conn,
                user_id=user_id,
                title=parsed_command.title,
                source="telegram_command",
                deadline_at=parsed_command.deadline_at,
                deadline_type="hard" if parsed_command.deadline_at is not None else None,
                raw_input_text=command_text,
            )
        finally:
            conn.close()

        await update.message.reply_text(
            build_task_created_response(task, parsed_command.due_date_text)
        )

    return handler


def parse_addtask_command_text(command_text: str, timezone_name: str) -> ParsedAddTaskCommand:
    command_parts = command_text.strip().split(maxsplit=1)
    if len(command_parts) != 2:
        raise AddTaskCommandParseError("Add task command requires a title.")

    task_text = command_parts[1].strip()
    if not task_text:
        raise AddTaskCommandParseError("Add task command requires a title.")

    due_flag_matches = list(ADD_TASK_DUE_FLAG_PATTERN.finditer(task_text))
    if not due_flag_matches:
        if "--due" in task_text:
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


async def _ensure_authorized(update: Any, config: AppConfig) -> bool:
    allowed_ids = config.telegram_allowed_user_ids
    if not allowed_ids:
        if update.message is not None:
            await update.message.reply_text(build_task_owner_not_configured_response())
        return False

    user = update.effective_user
    if user is not None and user.id in allowed_ids:
        return True

    if update.message is not None:
        await update.message.reply_text(build_unauthorized_response())
    return False
