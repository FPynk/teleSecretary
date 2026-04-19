"""Telegram long-polling bootstrap."""

from __future__ import annotations

import logging
from typing import Any

from tele_secretary.config import AppConfig
from tele_secretary.persistence.connection import connect
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.responses import (
    build_help_response,
    build_ping_response,
    build_unauthorized_response,
)

LOGGER = logging.getLogger(__name__)


def run_bot(config: AppConfig) -> None:
    if not config.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to start the bot.")

    with connect(config.db_path) as conn:
        apply_migrations(conn)

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


async def _ensure_authorized(update: Any, config: AppConfig) -> bool:
    allowed_ids = config.telegram_allowed_user_ids
    if not allowed_ids:
        return True

    user = update.effective_user
    if user is not None and user.id in allowed_ids:
        return True

    if update.message is not None:
        await update.message.reply_text(build_unauthorized_response())
    return False
