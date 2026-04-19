# teleSecretary

Telegram-first personal task and reminder assistant.

## Phase 0 Status

This repository currently contains the Phase 0 foundation:

- Python package under `src/tele_secretary`
- environment-driven configuration
- SQLite connection and SQL migration runner
- foundation tables for users, reference sequences, and health checks
- application-level health and help actions
- Telegram long-polling bootstrap with `/ping` and `/help`
- Docker and Docker Compose setup
- unit tests for the foundation pieces

Task CRUD, reminders, natural-language parsing, and LLM integration are later phases.

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
restrict access. If it is empty, the Phase 0 bot allows any Telegram user who can
message it.

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
