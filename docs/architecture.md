# TeleSecretary Architecture

TeleSecretary uses a small application-service architecture. The current
application modules deliberately combine domain rules and their SQLite
statements; a separate repository layer is not missing architecture.

## Package map

| Package | Responsibility | Must not own |
| --- | --- | --- |
| `telegram` | Telegram parsing, handlers, responses, and delivery | Domain SQL or owner policy |
| `llm` | Planned provider adapter, bounded stateless loop, tool dispatch, and prompt | Trusted identity choice or domain SQL |
| `scheduler` | Background timing and service orchestration | Reminder policy or duplicated SQL |
| `app` | Public internal API, validation, ownership, lifecycle, and domain SQL | Telegram or provider protocol details |
| `persistence` | Connections, migrations, and shared SQLite primitives | Task or reminder business operations |

The normal responsibility flow is:

```text
Telegram adapter ---------+
                          |
LLM tool adapter ----------+--> public application services --> SQLite/shared persistence
                          |
Scheduler orchestration ---+--> Telegram delivery adapter
```

This is a responsibility map, not a strict one-way import graph: the scheduler
legitimately coordinates application services and Telegram delivery.

## Public application API

Descriptive public functions in these modules are the supported internal API
for adapters:

```text
tele_secretary.app.tasks
tele_secretary.app.reminders
tele_secretary.app.users
```

Telegram, LLM, and other adapters may create a shared connection and pass it to
an application operation, but they must not issue task, reminder, or user SQL.
When an adapter needs behavior that does not exist, add the smallest
owner-scoped application operation instead of accessing tables directly.

Application modules may use `persistence` primitives and stdlib `sqlite3`.
`persistence` must not import application, scheduler, Telegram, or LLM code.
Leading-underscore application helpers are private.

## Planned LLM boundary

The LLM MVP keeps deterministic slash commands unchanged. Authenticated
non-command Telegram text will follow this planned path:

```text
telegram/natural_language.py
        |
        v
llm/agent.py bounded stateless tool-call loop
        |
        v
llm/tools.py allowlisted dispatch plus trusted owner injection
        |
        v
public application service
```

`llm/client.py` will convert provider request and response data. `llm/agent.py`
will bound the stateless loop, and `llm/tools.py` will own schemas and
allowlisted dispatch. Tool calls are proposals, not proof of a successful write:
only a confirmed application-service result may be reported as successful.
The model never supplies an internal owner ID; dispatch injects the trusted
Telegram identity.

## Contributor instructions and runtime prompt

| Artifact | Audience | Purpose |
| --- | --- | --- |
| `AGENTS.md` | Coding agents changing this repository | Modification and verification rules |
| `docs/architecture.md` | Human and agent contributors | Package responsibilities and rationale |
| `llm/prompts/system.md` | Deployed runtime model | Telegram assistant behavior and allowed tools |

Repository coding rules do not belong in the runtime system prompt, and a
runtime prompt is not contributor guidance.

## Change examples

- New Telegram wording belongs in `telegram/responses.py`.
- A task mutation belongs in an owner-scoped `app/tasks.py` function.
- LLM exposure for that mutation belongs in `llm/tools.py`, which calls the
  application function.
- A migration belongs in the migrations package and is applied through shared
  persistence infrastructure.
- A recurring background sequence belongs in `scheduler/`, which composes
  existing services rather than duplicating their policy.
