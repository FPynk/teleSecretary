"""Tests for the bounded, stateless LLM tool-calling agent."""

# TODO(TSEC-43): Add fake-client tests for a direct final answer, one or more
# tool-call rounds followed by final text, tool failures, malformed and unknown
# calls, provider failure, round-limit exhaustion, and fresh state for each
# message. These tests must not make live provider calls or import Telegram.
