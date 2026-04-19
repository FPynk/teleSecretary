"""Command-line entrypoint for TeleSecretary."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from tele_secretary.config import ConfigError, load_config
from tele_secretary.logging_config import configure_logging
from tele_secretary.app.health import perform_healthcheck
from tele_secretary.persistence.connection import connect
from tele_secretary.persistence.migrations import apply_migrations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tele_secretary")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional env file to load before reading environment variables.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="Apply pending SQLite migrations.")
    subparsers.add_parser("healthcheck", help="Run DB startup and write-path checks.")
    subparsers.add_parser("bot", help="Start the Telegram long-polling bot.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    require_bot_token = args.command == "bot"
    try:
        config = load_config(
            env_file=Path(args.env_file),
            require_bot_token=require_bot_token,
        )
    except ConfigError as exc:
        parser.error(str(exc))

    configure_logging(config.log_dir, config.log_level)

    if args.command == "migrate":
        with connect(config.db_path) as conn:
            result = apply_migrations(conn)
        print(
            "Migrations applied: "
            f"{len(result.applied)} applied, {len(result.skipped)} already present."
        )
        return 0

    if args.command == "healthcheck":
        result = perform_healthcheck(config)
        print(f"Healthcheck {result.status}: {result.details}")
        return 0

    if args.command == "bot":
        from tele_secretary.telegram.bot import run_bot

        run_bot(config)
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
