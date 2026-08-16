# Changelog

## 2026-08-16

- Wired the live reminder worker into the Telegram application lifecycle. It
  runs immediately at startup and then sequentially every 30 seconds, recovering
  abandoned leases, processing first attempts and missed summaries, then
  processing retry attempts with one shared UTC timestamp per cycle.
- Added cross-service verification for the reminder retry lifecycle, covering
  failure, the exact retry boundary, successful retry, terminal exhaustion,
  lifecycle cancellation, and abandoned-lease recovery.
- Added repository-local coding guidance and a canonical architecture document
  covering package boundaries, owner-scoped application APIs, and the planned
  LLM integration boundary.

## 2026-08-16

- Added owner-scoped missed-reminder delivery after downtime recovery. One to
  three eligible stale reminders use an explicit missed-message format; four or
  more use one deterministic, bounded plain-text summary.
- Summary success marks every included first-attempt reminder sent in one
  transaction. A Telegram failure makes the full group retryable together with
  one sanitized failure code; the existing retry path later sends retries
  individually.
- Split due-reminder claims into mutually exclusive first-attempt and retry
  paths. First attempts require no recorded delivery attempt.
- Added bounded, atomic retry claiming after fixed 1-, 5-, and 15-minute
  delays, ordered by the next eligible retry time without changing attempt
  metadata.
- Added `/urgent` for owner-scoped active `top_priority` and `high` tasks,
  ordered deterministically by urgency and numeric task reference.
- Added atomic recovery for abandoned five-minute `processing` leases. Stale
  active-task reminders return to pending; stale inactive-task reminders are
  cancelled while retaining their attempt history.

## 2026-08-14

- Completing or soft-deleting a task now atomically cancels that task's pending
  reminders scheduled strictly after the same transition timestamp.
- Reopening a task preserves cancelled reminder history; it does not recreate
  or reactivate reminders.
- Added `/unremind T<number>` for owner-scoped cancellation of a task's sole
  pending reminder, with clear empty, stale, and persistence-failure responses.
- Multiple pending reminders are never guessed or changed; the command asks for
  a selection, which is added separately.

## 2026-08-13

- Added one-attempt Telegram delivery for reminders already claimed by the
  worker, using deterministic plain-text messages and the claimed owner's ID.
- Delivery rechecks the exact claim lease, task activity, persisted owner, and
  current allowlist before Telegram I/O; removed recipients are cancelled.
- Successful sends are recorded as sent. Failed sends store only a sanitized
  code and return to pending through count 3, then become terminal at count 4.

## 2026-07-23

- Added independent multi-user Telegram ownership: every authorized sender now
  resolves to their own stable user row, owner-scoped data, and persisted
  timezone.
- Added an idempotent startup binding for legacy `single-owner` data without
  rewriting its linked records.
- Added owner-scoped reminder cancellation services for one pending reminder,
  a strict selected set, and all future pending reminders on a task.
- Cancellation is atomic against due-reminder claims, records a canonical UTC
  cancellation timestamp, and keeps terminal reminder history unchanged.
- Added deterministic first-attempt downtime recovery for claimed reminders.
- Routes reminders up to 60 minutes late normally, marks reminders more than
  60 minutes but under 12 hours late as missed delivery candidates, and
  atomically expires reminders at least 12 hours late without network I/O.

## 2026-07-22

- Added `/remind T<number> <time>` for owner-scoped, deterministic reminder
  creation through Telegram.
- Confirmations display the persisted local schedule and give one creation-time
  daylight-saving note when a requested local time is adjusted or ambiguous.

- Added atomic, bounded claiming of due reminders for the worker boundary.
- Claims transition eligible rows from `pending` to `processing` and return
  owner/task delivery context without performing network I/O.

## 2026-07-21

- Added owner-scoped reminder creation, retrieval, and pending-listing services.
- Enforced active-task ownership, future UTC schedule validation, and typed
  duplicate active-reminder errors.

## 2026-07-20

- Added deterministic V1 reminder-time parsing for `tomorrow`, weekdays, and
  `DD/MM/YYYY` expressions with 12- or 24-hour times.
- Resolves local calendar values to future UTC instants, including typed
  best-effort handling for daylight-saving gaps and overlaps.
- Added the task-linked reminder persistence schema with lifecycle validation,
  Telegram-only delivery, retry metadata, due-polling indexes, and active
  duplicate prevention. Reminder services and delivery remain future work.

## 2026-07-17

- Added `/today` for a deterministic, timezone-aware task focus list covering
  overdue tasks, due-today tasks, hard deadlines due tomorrow, planned-today
  tasks, and urgent undated tasks.

## 2026-07-17

- Added `/done T<number>` for completing an owner-scoped active task, recording
  its UTC completion timestamp and completion event.

## 2026-07-17

- Added `/reopen T<number>` for reopening an owner-scoped completed task. It
  clears the task's current completion timestamp without recreating reminders.

## 2026-07-17

- Moved stable public references onto the shared `items.pub_ref` field and
  removed the separate `task_refs` table through a forward migration.
- Added owner-scoped `N<number>` references for notes and a shared lookup model
  that future item types can extend with their own prefix.
- Enforced owner-scoped uniqueness, item-type prefixes, and immutable public
  references in SQLite.
- Aligned task/note creation and lookup services, planning documents, schema
  docs, and tests on the shared public-reference model and canonical ref format.
- Added a complete table-and-column data dictionary to the database diagram.

## 2026-07-11

### Telegram Task Details
- Added persistent per-user task refs and backfilled refs for existing tasks.
- Updated `/list` and `/addtask` confirmations to expose stable refs such as `T1`.
- Changed the optional `/addtask` deadline flag from `--due` to phone-friendly `-due`.
- Added `/show <task_ref>` with localized task details and clear invalid/not-found responses.
- Added migration, reference ownership, parser, handler, and detail-formatting coverage.
- Added atomic `/edit` support for task fields, categories, tags, clearing optional values, localized dates, quoted whitespace, and mobile curly quotes.
- Fixed flaky CI logging cleanup by detaching temporary handlers before their directories are removed.
- Added topic-based `/help edit` plus `/edit -help` for a shared verbose edit guide.

### Verification
- `python -B -m unittest discover -s tests` passed all 60 tests.

## 2026-07-06

### Telegram Task Commands
- Added Telegram `/addtask <title> [--due DD/MM/YYYY]` for direct task creation.
- Stored date-only due dates as local end-of-day deadlines converted to UTC.
- Required `TELEGRAM_ALLOWED_USER_IDS` before any Telegram command can run.
- Kept database migrations as a startup/setup concern instead of running them inside each Telegram command handler.
- Updated README and homelab setup notes for the new command and authorization behavior.

### Verification
- `python -B -m unittest discover -s tests` passed.
- `python -m compileall src tests` passed when bytecode cache output was redirected with `PYTHONPYCACHEPREFIX`.
- `python -m pytest` could not run because `pytest` is not installed in this environment.

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
