"""Deterministic V1 reminder time parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from tele_secretary.time_utils import utc_now


class ReminderTimeWarning(Enum):
    NONEXISTENT_TIME_MOVED_FORWARD = "nonexistent_time_moved_forward"
    AMBIGUOUS_TIME_FIRST_OCCURRENCE = "ambiguous_time_first_occurrence"


@dataclass(frozen=True)
class ParsedReminderTime:
    scheduled_at: datetime
    warning: ReminderTimeWarning | None


class ReminderTimeParseError(ValueError):
    """A reminder expression cannot be resolved to a schedulable time."""


class InvalidReminderTimeError(ReminderTimeParseError):
    """The expression does not match the supported V1 reminder grammar."""


class PastReminderTimeError(ReminderTimeParseError):
    """The expression resolves to a time that is not strictly in the future."""


_INVALID_EXPRESSION_MESSAGE = (
    "Use tomorrow, tomorrow 2pm, a weekday with an optional time, or "
    "DD/MM/YYYY with an optional time."
)
_PAST_TIME_MESSAGE = "That reminder time has already passed. Choose a future time."
_DEFAULT_REMINDER_TIME = time(9)
_TWELVE_HOUR_TIME = r"(?:1[0-2]|[1-9])(?::[0-5][0-9])?\s*(?:am|pm)"
_TWENTY_FOUR_HOUR_TIME = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
_WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}
_WEEKDAY_NAMES = "|".join(_WEEKDAYS)


def parse_reminder_time_expression(
    expression: str,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> ParsedReminderTime:
    """Resolve a V1 reminder expression to a future UTC time."""
    normalized_expression = _normalize_expression(expression)
    zone = ZoneInfo(timezone_name)
    now_utc = _normalize_now(now)
    local_now = now_utc.astimezone(zone)
    local_candidate, is_weekday_expression = _parse_expression_to_local_datetime(
        normalized_expression,
        local_now,
    )

    parsed_time = _resolve_local_datetime(local_candidate, zone)
    if parsed_time.scheduled_at > now_utc:
        return parsed_time

    if is_weekday_expression:
        return _resolve_future_weekday(local_candidate, zone, now_utc)

    raise PastReminderTimeError(_PAST_TIME_MESSAGE)


def _normalize_expression(expression: str) -> str:
    if not isinstance(expression, str):
        raise InvalidReminderTimeError(_INVALID_EXPRESSION_MESSAGE)
    normalized_expression = re.sub(r"\s+", " ", expression.strip()).casefold()
    if not normalized_expression:
        raise InvalidReminderTimeError(_INVALID_EXPRESSION_MESSAGE)
    return normalized_expression


def _normalize_now(now: datetime | None) -> datetime:
    value = utc_now() if now is None else now
    if value.tzinfo is None:
        raise ValueError("The injected reference clock must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _parse_expression_to_local_datetime(
    expression: str,
    local_now: datetime,
) -> tuple[datetime, bool]:
    tomorrow_match = re.fullmatch(rf"tomorrow(?: (?P<time>{_TWELVE_HOUR_TIME}))?", expression)
    if tomorrow_match:
        requested_time = _parse_clock_time(
            tomorrow_match.group("time"),
            allow_twenty_four_hour=False,
        )
        return datetime.combine(local_now.date() + timedelta(days=1), requested_time), False

    weekday_match = re.fullmatch(
        rf"(?P<weekday>{_WEEKDAY_NAMES})(?: (?P<time>{_TWELVE_HOUR_TIME}|{_TWENTY_FOUR_HOUR_TIME}))?",
        expression,
    )
    if weekday_match:
        requested_time = _parse_clock_time(
            weekday_match.group("time"),
            allow_twenty_four_hour=True,
        )
        requested_date = _resolve_weekday_date(
            _WEEKDAYS[weekday_match.group("weekday")],
            local_now.date(),
        )
        return datetime.combine(requested_date, requested_time), True

    absolute_date_match = re.fullmatch(
        rf"(?P<date>\d{{2}}/\d{{2}}/\d{{4}})(?: (?P<time>{_TWELVE_HOUR_TIME}|{_TWENTY_FOUR_HOUR_TIME}))?",
        expression,
    )
    if absolute_date_match:
        requested_date = _parse_absolute_date(absolute_date_match.group("date"))
        requested_time = _parse_clock_time(
            absolute_date_match.group("time"),
            allow_twenty_four_hour=True,
        )
        return datetime.combine(requested_date, requested_time), False

    raise InvalidReminderTimeError(_INVALID_EXPRESSION_MESSAGE)


def _parse_clock_time(value: str | None, *, allow_twenty_four_hour: bool) -> time:
    if value is None:
        return _DEFAULT_REMINDER_TIME

    twelve_hour_match = re.fullmatch(
        r"(?P<hour>1[0-2]|[1-9])(?::(?P<minute>[0-5][0-9]))?\s*(?P<meridiem>am|pm)",
        value,
    )
    if twelve_hour_match:
        hour = int(twelve_hour_match.group("hour"))
        minute = int(twelve_hour_match.group("minute") or 0)
        if twelve_hour_match.group("meridiem") == "am":
            return time(0 if hour == 12 else hour, minute)
        return time(12 if hour == 12 else hour + 12, minute)

    if allow_twenty_four_hour and re.fullmatch(_TWENTY_FOUR_HOUR_TIME, value):
        hour, minute = value.split(":")
        return time(int(hour), int(minute))

    raise InvalidReminderTimeError(_INVALID_EXPRESSION_MESSAGE)


def _resolve_weekday_date(target_weekday: int, local_date: date) -> date:
    return local_date + timedelta(days=(target_weekday - local_date.weekday()) % 7)


def _parse_absolute_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError as error:
        raise InvalidReminderTimeError(_INVALID_EXPRESSION_MESSAGE) from error


def _resolve_future_weekday(
    local_candidate: datetime,
    zone: ZoneInfo,
    now_utc: datetime,
) -> ParsedReminderTime:
    next_week_candidate = local_candidate + timedelta(days=7)
    parsed_time = _resolve_local_datetime(next_week_candidate, zone)
    if parsed_time.scheduled_at <= now_utc:
        raise PastReminderTimeError(_PAST_TIME_MESSAGE)
    return parsed_time


def _resolve_local_datetime(local_candidate: datetime, zone: ZoneInfo) -> ParsedReminderTime:
    utc_candidates = _find_valid_utc_candidates(local_candidate, zone)
    if len(utc_candidates) == 1:
        return ParsedReminderTime(utc_candidates[0], None)
    if len(utc_candidates) == 2:
        return ParsedReminderTime(
            utc_candidates[0],
            ReminderTimeWarning.AMBIGUOUS_TIME_FIRST_OCCURRENCE,
        )
    if not utc_candidates:
        return _resolve_nonexistent_local_datetime(local_candidate, zone)
    raise InvalidReminderTimeError(_INVALID_EXPRESSION_MESSAGE)


def _find_valid_utc_candidates(
    local_candidate: datetime,
    zone: ZoneInfo,
) -> tuple[datetime, ...]:
    valid_candidates = set()
    for fold in (0, 1):
        utc_candidate = local_candidate.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        round_tripped_local_candidate = utc_candidate.astimezone(zone).replace(tzinfo=None)
        if round_tripped_local_candidate == local_candidate:
            valid_candidates.add(utc_candidate)
    return tuple(sorted(valid_candidates))


def _resolve_nonexistent_local_datetime(
    local_candidate: datetime,
    zone: ZoneInfo,
) -> ParsedReminderTime:
    for minute_offset in range(1, 24 * 60 + 1):
        next_local_candidate = local_candidate + timedelta(minutes=minute_offset)
        utc_candidates = _find_valid_utc_candidates(next_local_candidate, zone)
        if utc_candidates:
            return ParsedReminderTime(
                utc_candidates[0],
                ReminderTimeWarning.NONEXISTENT_TIME_MOVED_FORWARD,
            )
    raise InvalidReminderTimeError(_INVALID_EXPRESSION_MESSAGE)
