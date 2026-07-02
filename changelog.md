# Changelog

## 2026-07-02

### Phase 1 Core Task Foundation
- Added the Phase 1 item/task/note schema migration with `items`, `task_items`, `note_items`, `categories`, `tags`, `item_tags`, and `completion_logs`.
- Added database constraints, indexes, and subtype triggers for core task/note integrity.
- Added reusable task and note application services for create, details, active listing, vocabulary listing, update, complete, reopen, soft delete, and note creation.
- Added Telegram `/list` to show active tasks for the configured single owner.
- Standardized Phase 1 domain timestamps on application-generated UTC ISO text.
- Added explicit-close SQLite test helpers so persistence tests clean up reliably on Windows and Unix-like systems.
- Added Phase 1 schema and service tests for gold flows and validation edges.

### Verification
- `python -m unittest discover -s tests` passed.
- `python -m compileall src tests` passed when bytecode cache output was redirected with `PYTHONPYCACHEPREFIX`.
- `python -m pytest` could not run because `pytest` is not installed in this environment.

## 2026-04-19

### Phase 0 Foundation
- Added the initial Python project skeleton under `teleSecretary/src/tele_secretary`.
- Added environment-driven configuration with `.env.example`.
- Added SQLite persistence foundation with connection helpers, packaged SQL migrations, and migration tracking.
- Added foundation database tables: `schema_migrations`, `users`, `ref_sequences`, and `health_checks`.
- Added CLI commands for migrations, health checks, and Telegram bot startup.
- Added application-level health/help actions and pure Telegram response builders.
- Added Telegram long-polling bootstrap with `/ping` and `/help`.
- Added scheduler package boundary for later reminder work.
- Added UTC/IANA timezone utilities.
- Added per-user task reference sequence support for future refs such as `T12`.
- Added Dockerfile and Docker Compose setup with persistent data/log mounts.
- Expanded `teleSecretary/README.md` with setup, commands, tests, and Docker notes.
- Added unit tests covering config, timezone utilities, migrations, health checks, ref generation, and Telegram responses.
- Created `agent_notes.md` for persistent implementation notes.

### Verification
- `python3 -m unittest discover -s tests` passed.
- `PYTHONPATH=src python3 -m tele_secretary migrate` passed and reran cleanly.
- `PYTHONPATH=src python3 -m tele_secretary healthcheck` passed.
- `python3 -m compileall src tests` passed.
- Docker Compose and real Telegram bot startup remain unverified in the current WSL environment.
