"""Timezone and UTC timestamp helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return to_storage_text(utc_now())


def to_storage_text(value: datetime) -> str:
    return ensure_utc(value).isoformat(timespec="seconds")


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Naive datetimes cannot be stored; attach a timezone first.")
    return value.astimezone(timezone.utc)


def local_to_utc(value: datetime, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value.astimezone(timezone.utc)


def utc_to_local(value: datetime, timezone_name: str) -> datetime:
    return ensure_utc(value).astimezone(ZoneInfo(timezone_name))
