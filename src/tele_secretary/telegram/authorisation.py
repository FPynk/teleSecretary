"""Shared authorisation boundary for Telegram handler callbacks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from tele_secretary.config import AppConfig
from tele_secretary.telegram.responses import (
    build_task_owner_not_configured_response,
    build_unauthorized_response,
)


def wrap_authorised_telegram_handler(
    config: AppConfig,
    callback: Callable[..., Awaitable[None]],
) -> Callable[..., Awaitable[None]]:
    """Invoke a callback only after admitting the actual sender from the allowlist."""
    async def handler(update: Any, context: Any) -> None:
        """Reject disallowed updates before passing trusted identity to the callback."""
        if update.message is None:
            return
        if not config.telegram_allowed_user_ids:
            await update.message.reply_text(build_task_owner_not_configured_response())
            return
        sender = update.effective_user
        if sender is None or sender.id not in config.telegram_allowed_user_ids:
            await update.message.reply_text(build_unauthorized_response())
            return
        await callback(update, context, telegram_user_id=sender.id)

    return handler
