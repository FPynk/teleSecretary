"""Application-level command help text."""

from __future__ import annotations


EDIT_HELP_TEXT = r"""Edit task help

Syntax:
/edit T<number> -field value [-field value ...]
/edit -help

Set fields:
-title <text>
-description <text>
-category <existing category>
-deadline <DD/MM/YYYY or "DD/MM/YYYY HH:MM">
-deadline-type <hard|soft>
-planned-start "DD/MM/YYYY HH:MM"
-planned-end "DD/MM/YYYY HH:MM"
-estimate <positive minutes>
-urgency <low|medium|high|top_priority>
-add-tag <existing tag> (repeatable)
-remove-tag <existing tag> (repeatable)

Clear fields:
-clear-description
-clear-category
-clear-deadline
-clear-planned-start
-clear-planned-end
-clear-planned-window
-clear-estimate
-clear-urgency
-clear-tags

Values with spaces:
Wrap them in straight or mobile curly double quotes.
Example: -title "Email Professor Smith"
Use \" for a literal quote and \\ for a literal backslash.

Dates and times:
Use DD/MM/YYYY or a quoted DD/MM/YYYY HH:MM in 24-hour time.
Dates use your configured timezone. Date-only deadlines become 11:59 PM local.
Planned start/end values require a time.

Categories and tags:
They must already exist. Repeat -add-tag or -remove-tag for multiple tags.
Tag changes are idempotent. -clear-tags cannot be combined with tag changes.

Validation:
Flags may be in any order. Scalar flags may appear once. Unknown, missing,
duplicate, or conflicting flags reject the whole command. No partial edits are saved.

Examples:
/edit T12 -title "Email Professor Smith" -urgency high
/edit T12 -deadline "18/07/2026 17:00" -deadline-type soft
/edit T12 -category "School Work" -estimate 90
/edit T12 -add-tag school -add-tag "email follow-up"
/edit T12 -remove-tag "waiting on reply" -add-tag urgent
/edit T12 -clear-deadline -clear-planned-window"""


def get_help_text(topic: str | None = None) -> str:
    """Return concise command help or the supported detailed topic."""
    if topic is not None:
        normalized_topic = topic.strip().lower()
        if normalized_topic == "edit":
            return EDIT_HELP_TEXT
        if " " in normalized_topic:
            return "\n".join(
                [
                    "Use one help topic at a time.",
                    "Usage: /help [topic]",
                    "Available topics: edit",
                ]
            )
        return "\n".join(
            [
                f"Unknown help topic: {topic.strip() or '(blank)'}",
                "Usage: /help [topic]",
                "Available topics: edit",
            ]
        )

    return "\n".join(
        [
            "Commands:",
            "- /ping - check that TeleSecretary is awake",
            "- /list - show active tasks",
            "- /addtask <title> [-due DD/MM/YYYY] - add a task",
            "- /show T<number> - show full task details",
            "- /edit T<number> -field value [...] - edit task fields",
            "- /done T<number> - mark a task complete",
            "- /reopen T<number> - reopen a completed task",
            "- /delete T<number> - remove a task",
            "- /today - show your deterministic focus list",
            "- /remind T<number> <time> - set a task reminder",
            "- /unremind T<number> - cancel a task reminder",
            "- /help [topic] - show command help",
            "",
            "Use /help edit for detailed edit instructions.",
            "More task commands are coming in Phase 2.",
        ]
    )
