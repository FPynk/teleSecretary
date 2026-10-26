"""Telegram adapter for authorised ordinary-text LLM requests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import logging
from typing import Any

from tele_secretary.app.users import get_or_create_telegram_user
from tele_secretary.config import AppConfig
from tele_secretary.llm.agent import run_agent
from tele_secretary.llm.client import LLMClient
from tele_secretary.persistence.connection import connect
from tele_secretary.telegram.authorisation import wrap_authorised_telegram_handler
from tele_secretary.telegram.responses import (
    build_natural_language_disabled_response,
    build_natural_language_failure_response,
)


LOGGER = logging.getLogger(__name__)
MAXIMUM_TELEGRAM_TEXT_LENGTH = 4096


def build_natural_language_handler(
    config: AppConfig,
    *,
    client: LLMClient | None,
) -> Callable[..., Awaitable[None]]:
    """Build an authorised text callback using a caller-owned LLM client."""
    async def authorised_handler(
        update: Any, context: Any, *, telegram_user_id: int
    ) -> None:
        """Run one fresh agent request off the event loop and reply to its message."""
        del context
        if client is None or not config.openai_api_key or not config.openai_model:
            await update.message.reply_text(build_natural_language_disabled_response())
            return
        try:
            response = await asyncio.to_thread(
                _run_natural_language_request,
                config,
                client,
                telegram_user_id,
                update.message.text,
            )
        except Exception:
            LOGGER.warning("Natural-language request failed.")
            response = build_natural_language_failure_response()
        if not isinstance(response, str) or not response.strip():
            response = build_natural_language_failure_response()
        for start in range(0, len(response), MAXIMUM_TELEGRAM_TEXT_LENGTH):
            await update.message.reply_text(response[start : start + MAXIMUM_TELEGRAM_TEXT_LENGTH])

    authorised_callback = wrap_authorised_telegram_handler(config, authorised_handler)

    async def handler(update: Any, context: Any) -> None:
        """Ignore non-message updates, empty text, and slash-command envelopes."""
        message = update.message
        if message is None or not isinstance(message.text, str):
            return
        text = message.text.lstrip()
        if not text or text.startswith("/"):
            return
        await authorised_callback(update, context)

    return handler


def _run_natural_language_request(
    config: AppConfig,
    client: LLMClient,
    telegram_user_id: int,
    user_message: str,
) -> str:
    """Open, use, and close the request's SQLite connection in the same worker."""
    conn = connect(config.db_path)
    try:
        user = get_or_create_telegram_user(
            conn,
            telegram_user_id=telegram_user_id,
            default_timezone=config.user_timezone,
        )
        return run_agent(
            conn,
            client=client,
            user_id=user.user_id,
            user_message=user_message,
            current_time=datetime.now(timezone.utc),
            user_timezone=user.timezone,
        )
    finally:
        conn.close()
