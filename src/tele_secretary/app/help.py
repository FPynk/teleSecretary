"""Application-level help text."""

from __future__ import annotations


def get_help_text() -> str:
    return "\n".join(
        [
            "Commands:",
            "- /ping - check that TeleSecretary is awake",
            "- /help - show this command list",
            "",
            "Task commands are coming in Phase 2 after the core task model is built.",
        ]
    )
