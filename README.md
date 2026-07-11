# teleSecretary

Telegram-first personal task and reminder assistant.

## Current Status

This repository currently contains the Phase 0 foundation plus the Phase 1 task
data model and the first direct Telegram task commands:

- Python package under `src/tele_secretary`
- environment-driven configuration
- SQLite connection and SQL migration runner
- foundation tables for users, reference sequences, and health checks
- Phase 1 task/note/category/tag tables and task services
- application-level health and help actions
- persistent per-user task refs such as `T1`
- Telegram long-polling bootstrap with `/ping`, `/help`, `/list`, `/addtask`, `/show`, and `/edit`
- Docker and Docker Compose setup
- unit tests for the foundation pieces

Reminders, natural-language parsing, and LLM integration are later phases.

## Telegram Commands

- `/ping` - check that TeleSecretary is awake
- `/help` - show the command list
- `/help edit` - show verbose `/edit` instructions (`/edit -help` is an alias)
- `/list` - show active tasks
- `/addtask <title> [-due DD/MM/YYYY]` - add a task
- `/show T<number>` - show full task details
- `/edit T<number> -field value [...]` - edit one or more task fields

## Local Setup

Target runtime is Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Fill in `TELEGRAM_BOT_TOKEN` in `.env` before starting the bot. Set
`TELEGRAM_ALLOWED_USER_IDS` to a comma-separated list of Telegram user IDs to
restrict access. If it is empty, Telegram commands are disabled.

Task commands are currently single-user. Use your Telegram ID in
`TELEGRAM_ALLOWED_USER_IDS`; the first ID in that list is treated as the task
owner.

The bot applies pending migrations once during startup. Individual Telegram
command handlers assume the database schema is already ready.

## Commands

```bash
python -m tele_secretary migrate
python -m tele_secretary healthcheck
python -m tele_secretary bot
```

## Tests

```bash
pytest
```

The tests are also compatible with stdlib `unittest` for environments where
pytest is not installed:

```bash
python -m unittest discover -s tests
```

SQLite-backed tests should explicitly close database connections before their
temporary directories are cleaned up. This keeps test cleanup reliable on both
Unix-like systems and Windows, especially while SQLite WAL files are present.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

Docker Compose mounts:

- `./data` to `/data`
- `./logs` to `/logs`

The container stores SQLite at `/data/secretary.sqlite3` and logs at
`/logs/secretary.log`.
