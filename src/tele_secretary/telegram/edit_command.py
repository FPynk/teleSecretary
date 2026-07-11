"""Deterministic parsing for the Telegram edit command."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import shlex
from typing import Any

from tele_secretary.time_utils import local_to_utc


TASK_REF_PATTERN = re.compile(r"T[1-9]\d*", re.IGNORECASE)
VALUE_FLAGS = {
    "-title",
    "-description",
    "-category",
    "-deadline",
    "-deadline-type",
    "-planned-start",
    "-planned-end",
    "-estimate",
    "-urgency",
    "-add-tag",
    "-remove-tag",
}
CLEAR_FLAGS = {
    "-clear-description",
    "-clear-category",
    "-clear-deadline",
    "-clear-planned-start",
    "-clear-planned-end",
    "-clear-planned-window",
    "-clear-estimate",
    "-clear-urgency",
    "-clear-tags",
}
REPEATABLE_FLAGS = {"-add-tag", "-remove-tag"}


class EditTaskCommandParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedEditTaskCommand:
    task_ref: str
    task_field_updates: dict[str, Any]
    category_was_provided: bool
    category_name: str | None
    add_tag_names: tuple[str, ...]
    remove_tag_names: tuple[str, ...]
    clear_tags: bool
    changed_fields: tuple[str, ...]


def parse_edit_task_command_text(
    command_text: str,
    timezone_name: str,
) -> ParsedEditTaskCommand:
    command_parts = _split_command_text(command_text)
    if len(command_parts) < 3:
        raise EditTaskCommandParseError(
            "Provide a task ref and at least one edit flag."
        )

    task_ref = command_parts[1].upper()
    if TASK_REF_PATTERN.fullmatch(task_ref) is None:
        raise EditTaskCommandParseError("Task ref must look like T12.")

    task_field_updates: dict[str, Any] = {}
    category_was_provided = False
    category_name: str | None = None
    add_tag_names: list[str] = []
    remove_tag_names: list[str] = []
    clear_tags = False
    seen_flags: set[str] = set()
    changed_fields: list[str] = []

    index = 2
    while index < len(command_parts):
        flag = command_parts[index]
        if flag not in VALUE_FLAGS and flag not in CLEAR_FLAGS:
            raise EditTaskCommandParseError(f"Unknown edit flag: {flag}")
        if flag not in REPEATABLE_FLAGS and flag in seen_flags:
            raise EditTaskCommandParseError(f"Flag may only be used once: {flag}")
        seen_flags.add(flag)

        if flag in CLEAR_FLAGS:
            (
                category_was_provided,
                category_name,
                clear_tags,
            ) = _apply_clear_flag(
                flag,
                task_field_updates,
                category_was_provided,
                category_name,
                clear_tags,
            )
            _append_once(changed_fields, _changed_field_for_flag(flag))
            index += 1
            continue

        if index + 1 >= len(command_parts):
            raise EditTaskCommandParseError(f"Missing value for {flag}.")
        value = command_parts[index + 1]
        if not value and flag != "-description":
            raise EditTaskCommandParseError(f"Value for {flag} must not be blank.")

        if flag == "-title":
            task_field_updates["title"] = value
        elif flag == "-description":
            task_field_updates["description"] = value
        elif flag == "-category":
            category_was_provided = True
            category_name = value
        elif flag == "-deadline":
            task_field_updates["deadline_at"] = _parse_local_date_or_datetime(
                value,
                timezone_name,
                allow_date_only=True,
                field_name="deadline",
            )
        elif flag == "-deadline-type":
            if value not in {"hard", "soft"}:
                raise EditTaskCommandParseError(
                    "Deadline type must be hard or soft."
                )
            task_field_updates["deadline_type"] = value
        elif flag == "-planned-start":
            task_field_updates["planned_start_at"] = _parse_local_date_or_datetime(
                value,
                timezone_name,
                allow_date_only=False,
                field_name="planned start",
            )
        elif flag == "-planned-end":
            task_field_updates["planned_end_at"] = _parse_local_date_or_datetime(
                value,
                timezone_name,
                allow_date_only=False,
                field_name="planned end",
            )
        elif flag == "-estimate":
            task_field_updates["estimated_minutes"] = _parse_estimate(value)
        elif flag == "-urgency":
            if value not in {"low", "medium", "high", "top_priority"}:
                raise EditTaskCommandParseError(
                    "Urgency must be low, medium, high, or top_priority."
                )
            task_field_updates["urgency"] = value
        elif flag == "-add-tag":
            add_tag_names.append(value)
        elif flag == "-remove-tag":
            remove_tag_names.append(value)

        _append_once(changed_fields, _changed_field_for_flag(flag))
        index += 2

    _validate_conflicting_flags(seen_flags, add_tag_names, remove_tag_names)
    return ParsedEditTaskCommand(
        task_ref=task_ref,
        task_field_updates=task_field_updates,
        category_was_provided=category_was_provided,
        category_name=category_name,
        add_tag_names=tuple(add_tag_names),
        remove_tag_names=tuple(remove_tag_names),
        clear_tags=clear_tags,
        changed_fields=tuple(changed_fields),
    )


def _split_command_text(command_text: str) -> list[str]:
    normalized_command_text = command_text.translate(
        str.maketrans({"“": '"', "”": '"'})
    )
    try:
        return shlex.split(normalized_command_text, posix=True)
    except ValueError as exc:
        raise EditTaskCommandParseError("Quotes are not balanced.") from exc


def _apply_clear_flag(
    flag: str,
    task_field_updates: dict[str, Any],
    category_was_provided: bool,
    category_name: str | None,
    clear_tags: bool,
) -> tuple[bool, str | None, bool]:
    if flag == "-clear-description":
        task_field_updates["description"] = None
    elif flag == "-clear-category":
        category_was_provided = True
        category_name = None
    elif flag == "-clear-deadline":
        task_field_updates["deadline_at"] = None
        task_field_updates["deadline_type"] = None
    elif flag == "-clear-planned-start":
        task_field_updates["planned_start_at"] = None
    elif flag == "-clear-planned-end":
        task_field_updates["planned_end_at"] = None
    elif flag == "-clear-planned-window":
        task_field_updates["planned_start_at"] = None
        task_field_updates["planned_end_at"] = None
    elif flag == "-clear-estimate":
        task_field_updates["estimated_minutes"] = None
    elif flag == "-clear-urgency":
        task_field_updates["urgency"] = None
    elif flag == "-clear-tags":
        clear_tags = True
    return category_was_provided, category_name, clear_tags


def _parse_local_date_or_datetime(
    value: str,
    timezone_name: str,
    *,
    allow_date_only: bool,
    field_name: str,
) -> datetime:
    accepted_formats = ["%d/%m/%Y %H:%M"]
    if allow_date_only:
        accepted_formats.append("%d/%m/%Y")

    for date_format in accepted_formats:
        try:
            parsed_value = datetime.strptime(value, date_format)
        except ValueError:
            continue
        if date_format == "%d/%m/%Y":
            parsed_value = parsed_value.replace(hour=23, minute=59)
        return local_to_utc(parsed_value, timezone_name)

    expected_format = "DD/MM/YYYY or DD/MM/YYYY HH:MM"
    if not allow_date_only:
        expected_format = "DD/MM/YYYY HH:MM"
    raise EditTaskCommandParseError(
        f"Invalid {field_name}; use {expected_format}."
    )


def _parse_estimate(value: str) -> int:
    try:
        estimate = int(value)
    except ValueError as exc:
        raise EditTaskCommandParseError(
            "Estimate must be a positive number of minutes."
        ) from exc
    if estimate <= 0:
        raise EditTaskCommandParseError(
            "Estimate must be a positive number of minutes."
        )
    return estimate


def _validate_conflicting_flags(
    seen_flags: set[str],
    add_tag_names: list[str],
    remove_tag_names: list[str],
) -> None:
    conflicting_pairs = (
        ("-description", "-clear-description"),
        ("-category", "-clear-category"),
        ("-deadline", "-clear-deadline"),
        ("-deadline-type", "-clear-deadline"),
        ("-planned-start", "-clear-planned-start"),
        ("-planned-end", "-clear-planned-end"),
        ("-planned-start", "-clear-planned-window"),
        ("-planned-end", "-clear-planned-window"),
        ("-estimate", "-clear-estimate"),
        ("-urgency", "-clear-urgency"),
    )
    for set_flag, clear_flag in conflicting_pairs:
        if set_flag in seen_flags and clear_flag in seen_flags:
            raise EditTaskCommandParseError(
                f"Conflicting edit flags: {set_flag} and {clear_flag}"
            )
    if "-clear-tags" in seen_flags and (add_tag_names or remove_tag_names):
        raise EditTaskCommandParseError(
            "-clear-tags cannot be combined with tag additions or removals."
        )
    tag_conflicts = set(add_tag_names) & set(remove_tag_names)
    if tag_conflicts:
        conflicting_tag = sorted(tag_conflicts)[0]
        raise EditTaskCommandParseError(
            f'Tag cannot be added and removed together: "{conflicting_tag}"'
        )


def _changed_field_for_flag(flag: str) -> str:
    if flag in {"-add-tag", "-remove-tag", "-clear-tags"}:
        return "tags"
    if flag in {"-deadline", "-deadline-type", "-clear-deadline"}:
        return "deadline"
    if flag in {
        "-planned-start",
        "-planned-end",
        "-clear-planned-start",
        "-clear-planned-end",
        "-clear-planned-window",
    }:
        return "planned_window"
    return flag.removeprefix("-clear-").removeprefix("-")


def _append_once(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
