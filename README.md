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
- task-linked reminder persistence with lifecycle constraints and indexes
- application-level health and help actions
- persistent per-user public refs on shared items (`T1` for tasks and `N1` for notes)
- Telegram long-polling bootstrap with `/ping`, `/help`, `/list`, `/addtask`, `/show`, `/edit`, `/done`, `/reopen`, `/delete`, `/today`, `/due`, `/urgent`, `/remind`, and `/unremind`
- Docker and Docker Compose setup
- unit tests for the foundation pieces

Reminder creation is available through `/remind`. `/unremind` cancels a sole
pending reminder and safely asks for clarification when several exist. The bot
also runs one reminder-processing cycle at startup and then every 30 seconds:
it recovers abandoned leases, delivers due reminders, summarizes four or more
moderately stale reminders for one owner, and processes eligible retries.
Authorised ordinary text can create tasks through the stateless LLM tool loop
when OpenAI support is configured. Slash commands remain deterministic.

## Natural Language

Set both `OPENAI_API_KEY` and `OPENAI_MODEL` in `.env` to enable natural-language
requests. Leave both empty to disable them; ordinary text then receives guidance
to use slash commands. The existing Telegram allowlist applies before database
or model work, and each request uses the sender's persisted timezone.

For example, send `Add a task to submit my expense report tomorrow at 5pm`.
The model proposes task details, the application validates and saves them for
your account, and the bot replies in the same chat. Only task creation is
currently exposed to the model; use slash commands to view, edit, or remind.
Each message is independent, so include all necessary details rather than
referring to an earlier conversation. Replies use British English spelling.

Commands, media, captions, and edited messages do not enter the LLM path. Long
replies are split into Telegram-sized messages. If a failure occurs, follow the
reply's guidance and check `/list` before repeating a creation that may have
already succeeded. The shared provider client is closed when the bot shuts down.

## Architecture

The repository's package boundaries, internal application API, and planned LLM
integration rules are documented in [docs/architecture.md](docs/architecture.md).

## Telegram Commands

- `/ping` - check that TeleSecretary is awake
- `/help` - show the command list
- `/help edit` - show verbose `/edit` instructions (`/edit -help` is an alias)
- `/list` - show active tasks
- `/addtask <title> [-due DD/MM/YYYY]` - add a task
- `/show T<number>` - show full task details
- `/edit T<number> -field value [...]` - edit one or more task fields
- `/done T<number>` - mark an active task complete
- `/reopen T<number>` - reopen a completed task
- `/delete T<number>` - soft-delete an active or completed task
- `/today` - show your deterministic task focus list
- `/due` - show overdue and upcoming tasks
- `/remind T<number> <time>` - set a task reminder
- `/unremind T<number>` - cancel a task's sole pending reminder
- `/urgent` - show high-priority tasks

`/remind` accepts deterministic V1 times such as `tomorrow 2pm`, `fri 14:30`,
and `25/07/2026 2:30 PM`. Expressions and confirmation times use each owner's
persisted IANA timezone; reminders are stored in UTC.

`/unremind` cancels a task's sole pending reminder. If there are none, it says
so; if several are pending, it makes no change and asks for a selection. The
numbered selection flow is the next reminder-command step.

`/delete` immediately soft-deletes one active or completed task and cancels
only that task's strictly future pending reminders. Deleted tasks are hidden
from normal task views while their reminder history remains available for audit.

## Local Setup

Target runtime is Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Fill in `TELEGRAM_BOT_TOKEN` in `.env` before starting the bot. Set
`TELEGRAM_ALLOWED_USER_IDS` to one or more comma-separated Telegram user IDs.
If it is empty, Telegram commands are disabled. Each configured ID is an
independent owner with isolated tasks, references, reminders, and persisted
timezone; allowlist order has no runtime ownership meaning.

`SECRETARY_USER_TIMEZONE` is the default for newly created users. An existing
user's persisted timezone is used for parsing and rendering and is not changed
by later environment updates.

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

### Troubleshooting natural-language requests

At the default `INFO` log level, LLM diagnostics are written to the same
`logs/secretary.log` on the machine running the app. On the homelab:

```bash
cd /srv/apps/teleSecretary
tail -f logs/secretary.log | grep --line-buffered '"event":"llm_'
```

Each message has an `operation_id`; filter by that value to follow one request.
`round:1` is the initial model request, and later rounds include tool results.
`llm_tool_completed` records `ok` and the application `error_code`.
`llm_client_failed` distinguishes the `request` and `normalization` stages and
includes HTTP status, provider request ID, known error code, and structural
parameter path when available. `llm_operation_finished` gives the exit reason
and number of confirmed saved tasks. Request/response item types and the count
of reasoning items with encrypted content help diagnose continuation failures.

These are metadata logs, not conversation dumps: prompts, message text, task
contents, tool arguments/results, raw provider error text, and credentials are
not logged. Unknown error codes are reported as `other`; unknown parameter paths
are omitted. Existing historical entries cannot gain these details retroactively.
