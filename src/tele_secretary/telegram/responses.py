"""Pure response builders for Telegram commands."""

from __future__ import annotations

from tele_secretary.app.help import get_help_text


def build_ping_response() -> str:
    return "pong"


def build_help_response() -> str:
    return get_help_text()


def build_unauthorized_response() -> str:
    return "This Telegram account is not authorized to use TeleSecretary."
