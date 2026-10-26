"""Bounded, stateless orchestration for one natural-language LLM request."""

from __future__ import annotations

from datetime import datetime
import json
import logging
from sqlite3 import Connection
from time import monotonic
from typing import Any, Mapping

from tele_secretary.app.tasks import list_categories_and_tags
from tele_secretary.llm.client import LLMClient, LLMClientError, LLMResponse
from tele_secretary.llm.diagnostics import (
    LLM_LOG_CONTEXT, llm_operation, log_llm_event, safe_error_code, safe_provider_id,
)
from tele_secretary.llm.prompts import load_system_prompt
from tele_secretary.llm.tools import dispatch_tool_call, get_tool_definitions


MAXIMUM_TOOL_CALL_ROUNDS = 3
MAXIMUM_TOOL_CALLS = 5
MAXIMUM_TOOL_RESULT_BYTES = 12_000
LLM_REQUEST_FAILED_TEXT = "I'm having trouble responding right now. Please try again."
TOOL_CALL_LIMIT_TEXT = "I couldn't complete that request because it requested too many actions."
TOOL_CALL_ROUND_LIMIT_TEXT = "I couldn't complete that request within the allowed number of steps."
TOOL_RESULT_LIMIT_TEXT = "I couldn't complete that request because it returned too much data."
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
    with llm_operation():
        return _run_agent(
            conn, client=client, user_id=user_id, user_message=user_message,
            current_time=current_time, user_timezone=user_timezone,
        )


def _run_agent(
    conn: Connection, *, client: LLMClient, user_id: str, user_message: str,
    current_time: datetime, user_timezone: str,
) -> str:
    """Run the bounded loop inside the operation's diagnostic context."""
    started = monotonic()
    category_names = _load_category_names_for_model(conn, user_id=user_id)
    initial_input = _build_initial_input(
        user_message=user_message,
        current_time=current_time,
        user_timezone=user_timezone,
        category_names=category_names,
    )
    tool_definitions = get_tool_definitions()
    system_prompt = load_system_prompt()
    input_items: str | list[Mapping[str, Any]] = initial_input
    conversation_items: list[Mapping[str, Any]] = [
        {"role": "user", "content": initial_input},
    ]
    tool_call_rounds = 0
    total_tool_calls = 0
    total_tool_result_bytes = 0
    saved_task_refs: list[str] = []

    def finish(reason: str, text: str) -> str:
        log_llm_event(
            LOGGER, logging.INFO, "llm_operation_finished", reason=reason,
            saved_task_count=len(saved_task_refs), total_tool_calls=total_tool_calls,
            duration_ms=round((monotonic() - started) * 1000, 2),
        )
        return text

    while True:
        operation_id, _ = LLM_LOG_CONTEXT.get()
        LLM_LOG_CONTEXT.set((operation_id, tool_call_rounds + 1))
        round_started = monotonic()
        log_llm_event(LOGGER, logging.INFO, "llm_round_started")
        try:
            response = client.create_response(
                instructions=system_prompt,
                input_items=input_items,
                tool_definitions=tool_definitions,
            )
        except LLMClientError:
            log_llm_event(
                LOGGER, logging.WARNING, "llm_round_failed",
                saved_task_count=len(saved_task_refs),
                duration_ms=round((monotonic() - round_started) * 1000, 2),
            )
            if saved_task_refs:
                return finish("provider_failure", (
                    f"Saved tasks: {', '.join(saved_task_refs)}. "
                    "I couldn't finish the response, but these tasks are saved. "
                    "Please don't repeat their creation."
                ))
            return finish("provider_failure", LLM_REQUEST_FAILED_TEXT)

        log_llm_event(
            LOGGER, logging.INFO, "llm_round_completed",
            response_id=safe_provider_id(response.response_id),
            tool_call_count=len(response.tool_calls),
            duration_ms=round((monotonic() - round_started) * 1000, 2),
        )

        if not response.tool_calls:
            return finish("final_text" if response.text.strip() else "empty_text", response.text)
        if tool_call_rounds >= MAXIMUM_TOOL_CALL_ROUNDS:
            return finish("round_limit", TOOL_CALL_ROUND_LIMIT_TEXT)
        if total_tool_calls + len(response.tool_calls) > MAXIMUM_TOOL_CALLS:
            return finish("tool_call_limit", TOOL_CALL_LIMIT_TEXT)

        tool_call_rounds += 1
        total_tool_calls += len(response.tool_calls)
        conversation_items.extend(_continuation_items(response))
        for tool_call in response.tool_calls:
            tool_started = monotonic()
            tool_result = dispatch_tool_call(
                conn,
                user_id=user_id,
                call_id=tool_call.call_id,
                name=tool_call.name,
                arguments_json=tool_call.arguments_json,
            )
            tool_error = tool_result.output.get("error")
            log_llm_event(
                LOGGER, logging.INFO, "llm_tool_completed",
                tool_name=(tool_call.name if tool_call.name == "create_task_tool" else "unknown"),
                call_id=safe_provider_id(tool_call.call_id),
                ok=tool_result.output.get("ok") is True,
                error_code=safe_error_code(tool_error.get("code") if isinstance(tool_error, Mapping) else None),
                duration_ms=round((monotonic() - tool_started) * 1000, 2),
            )
            saved_task = tool_result.output.get("task")
            if tool_result.output.get("ok") is True and isinstance(saved_task, Mapping):
                saved_ref = saved_task.get("ref")
                if isinstance(saved_ref, str) and saved_ref:
                    saved_task_refs.append(saved_ref)
            output = json.dumps(tool_result.output, ensure_ascii=False, separators=(",", ":"))
            total_tool_result_bytes += len(output.encode("utf-8"))
            if total_tool_result_bytes > MAXIMUM_TOOL_RESULT_BYTES:
                return finish("tool_result_limit", TOOL_RESULT_LIMIT_TEXT)
            conversation_items.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call.call_id,
                    "output": output,
                }
            )
        input_items = conversation_items


def _load_category_names_for_model(conn: Connection, *, user_id: str) -> tuple[str, ...]:
    """Return the active category names belonging to the trusted owner."""
    return tuple(
        sorted(
            (category.name for category in list_categories_and_tags(conn, user_id=user_id).categories),
            key=str.casefold,
        )
    )


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


def _continuation_items(response: LLMResponse) -> tuple[Mapping[str, Any], ...]:
    """Return prior model output in the format required by the next request."""
    if response.continuation_items:
        return response.continuation_items
    return tuple(
        {
            "type": "function_call",
            "call_id": tool_call.call_id,
            "name": tool_call.name,
            "arguments": tool_call.arguments_json,
        }
        for tool_call in response.tool_calls
    )
