# TeleSecretary Coding Guidance

Read [docs/architecture.md](docs/architecture.md) before changing package
boundaries or introducing a new adapter.

## Responsibilities

- Keep Telegram parsing, handlers, responses, and Telegram delivery in
  `src/tele_secretary/telegram/`.
- Keep application operations, validation, owner scoping, lifecycle rules, and
  task/reminder/user SQL in `src/tele_secretary/app/`.
- Keep shared SQLite connections, migrations, and public-reference allocation
  in `src/tele_secretary/persistence/`.
- Keep background timing and service orchestration in
  `src/tele_secretary/scheduler/`.
- Keep future provider adapters, bounded model orchestration, tool dispatch,
  and runtime prompts in `src/tele_secretary/llm/`.

## Dependencies and APIs

- Telegram and LLM adapters call descriptive public functions in `app.tasks`,
  `app.reminders`, and `app.users`; leading-underscore helpers are private.
- Adapters may open and close a connection before passing it to an application
  operation, but must not execute task, reminder, or user SQL.
- Application modules intentionally combine domain rules with their SQLite
  persistence. Do not introduce repository/DAO classes or dependency injection
  without an accepted concrete need.
- `persistence/` must not import application, scheduler, Telegram, or LLM code.
- `scheduler/` composes public application operations and delivery adapters; it
  does not duplicate domain policy or SQL.

## Simplicity, identity, and verification

- Keep changes small, descriptive, and covered by focused tests. Preserve
  existing slash-command behavior unless the ticket changes it.
- Do not add generic tool frameworks, message buses, LangChain, or LangGraph
  without an accepted requirement.
- Trusted owner identity comes from Telegram. A model never chooses an internal
  `user_id`; tool dispatch injects it separately and application operations stay
  owner-scoped.
- Never log API keys, bot tokens, token-bearing URLs, or private task contents.
- Run `python -m pytest` before completion. Also run the relevant compilation
  or configuration check when source, packaging, Docker, or workflow files
  change.
