"""Application-level help text."""

from __future__ import annotations


def get_help_text() -> str:
    return "\n".join(
        [
            "Commands:",
            "- /ping - check that TeleSecretary is awake",
            "- /list - show active tasks",
            "- /help - show this command list",
            "",
            "More task commands are coming in Phase 2.",
        ]
    )
