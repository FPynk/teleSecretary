"""Bounded, stateless orchestration for one natural-language LLM request."""

# TODO(TSEC-43): Add the public agent entry point used by the future Telegram
# natural-language handler. For each incoming message, it must build fresh
# provider input from the system prompt, message, trusted owner context, and
# bounded category context. Do not retain conversation history between calls.
#
# TODO(TSEC-43): Implement the bounded tool-calling loop here. Send only the
# allowlisted definitions from llm.tools, dispatch each proposed call through
# llm.tools with the trusted user ID and database connection injected outside
# model arguments, append correlated structured tool results, and stop after a
# small fixed number of rounds. Return typed, safe outcomes for provider,
# malformed-call, unknown-tool, tool-failure, and round-limit cases.
