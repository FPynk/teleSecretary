"""Environment-driven application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """Raised when runtime configuration is invalid."""


@dataclass(frozen=True)
class AppConfig:
    telegram_bot_token: str | None
    telegram_allowed_user_ids: tuple[int, ...]
    data_dir: Path
    log_dir: Path
    db_path: Path
    user_timezone: str
    log_level: str

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        require_bot_token: bool = False,
    ) -> "AppConfig":
        values = env if env is not None else os.environ

        data_dir = Path(values.get("SECRETARY_DATA_DIR", "./data")).expanduser()
        log_dir = Path(values.get("SECRETARY_LOG_DIR", "./logs")).expanduser()
        db_path = Path(
            values.get("SECRETARY_DB_PATH", str(data_dir / "secretary.sqlite3"))
        ).expanduser()

        timezone = values.get("SECRETARY_USER_TIMEZONE", "America/Chicago").strip()
        _validate_timezone(timezone)

        bot_token = values.get("TELEGRAM_BOT_TOKEN", "").strip() or None
        if require_bot_token and not bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required for the bot command.")

        return cls(
            telegram_bot_token=bot_token,
            telegram_allowed_user_ids=_parse_allowed_user_ids(
                values.get("TELEGRAM_ALLOWED_USER_IDS", "")
            ),
            data_dir=data_dir,
            log_dir=log_dir,
            db_path=db_path,
            user_timezone=timezone,
            log_level=values.get("SECRETARY_LOG_LEVEL", "INFO").strip().upper(),
        )


def load_config(
    *,
    env_file: Path | None = None,
    require_bot_token: bool = False,
) -> AppConfig:
    env = dict(os.environ)
    if env_file is not None and env_file.exists():
        env.update(load_env_file(env_file))
    return AppConfig.from_env(env, require_bot_token=require_bot_token)


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ConfigError(f"Invalid env file line: {raw_line!r}")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _parse_allowed_user_ids(raw_value: str) -> tuple[int, ...]:
    raw_value = raw_value.strip()
    if not raw_value:
        return ()

    parsed: list[int] = []
    for piece in raw_value.split(","):
        value = piece.strip()
        if not value:
            continue
        try:
            parsed.append(int(value))
        except ValueError as exc:
            raise ConfigError(
                "TELEGRAM_ALLOWED_USER_IDS must be a comma-separated list of integers."
            ) from exc
    return tuple(parsed)


def _validate_timezone(timezone: str) -> None:
    if not timezone:
        raise ConfigError("SECRETARY_USER_TIMEZONE cannot be empty.")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            "SECRETARY_USER_TIMEZONE must be an IANA timezone, "
            "for example America/Chicago."
        ) from exc
