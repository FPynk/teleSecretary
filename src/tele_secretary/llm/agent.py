"""Bounded, stateless orchestration for one natural-language LLM request."""

from __future__ import annotations

from datetime import datetime
import json
import logging
from sqlite3 import Connection

from tele_secretary.app.tasks import list_categories_and_tags
from tele_secretary.llm.client import LLMClient
from tele_secretary.llm.prompts import load_system_prompt
from tele_secretary.llm.tools import get_tool_definitions


MAXIMUM_CATEGORY_CONTEXT_NAMES = 50
TOOL_CALLS_NOT_YET_SUPPORTED_TEXT = "I can't complete saved task changes yet."
LOGGER = logging.getLogger(__name__)


def run_agent(
    conn: Connection,
    *,
    client: LLMClient,
    user_id: str,
    user_message: str,
    current_time: datetime,
    user_timezone: str,
) -> str:
    """Return one stateless model reply for the trusted owner's message."""
    category_names = _load_category_names_for_model(conn, user_id=user_id)
    response = client.create_response(
        instructions=load_system_prompt(),
        input_items=_build_initial_input(
            user_message=user_message,
            current_time=current_time,
            user_timezone=user_timezone,
            category_names=category_names,
        ),
        tool_definitions=get_tool_definitions(),
    )
    if response.tool_calls:
        LOGGER.info("LLM tool calls deferred count=%s", len(response.tool_calls))
        return TOOL_CALLS_NOT_YET_SUPPORTED_TEXT
    return response.text


def _load_category_names_for_model(conn: Connection, *, user_id: str) -> tuple[str, ...]:
    """Return an owner-scoped category list only when it fits the context bound."""
    category_names = tuple(
        sorted(
            (category.name for category in list_categories_and_tags(conn, user_id=user_id).categories),
            key=str.casefold,
        )
    )
    if len(category_names) > MAXIMUM_CATEGORY_CONTEXT_NAMES:
        LOGGER.warning(
            "LLM category context omitted category_count=%s category_limit=%s",
            len(category_names),
            MAXIMUM_CATEGORY_CONTEXT_NAMES,
        )
        return ()
    return category_names


def _build_initial_input(
    *,
    user_message: str,
    current_time: datetime,
    user_timezone: str,
    category_names: tuple[str, ...],
) -> str:
    """Build the complete, per-message context without retaining prior input."""
    if category_names:
        category_context = (
            "Available category names; use an exact name or omit the category: "
            f"{json.dumps(category_names, ensure_ascii=False)}"
        )
    else:
        category_context = "No category names are available for this request."

    return "\n".join(
        (
            f"Current time: {current_time.isoformat()}",
            f"User timezone: {user_timezone}",
            category_context,
            "User message:",
            user_message,
        )
    )


# TODO(TSEC-43): Implement the bounded tool-calling loop here. Send only the
# allowlisted definitions from llm.tools, dispatch each proposed call through
# llm.tools with the trusted user ID and database connection injected outside
# model arguments, append correlated structured tool results, and stop after a
# small fixed number of rounds. Return typed, safe outcomes for provider,
# malformed-call, unknown-tool, tool-failure, and round-limit cases.
