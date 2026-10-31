"""Regression coverage for SDK output replay at the HTTP request boundary."""

from contextlib import closing
from datetime import datetime, timezone
import json
from sqlite3 import Row, connect
import unittest

import httpx
from openai import OpenAI
from openai.types.responses import ResponseReasoningItem

import _path  # noqa: F401
from tele_secretary.app.tasks import list_active_tasks
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.llm.agent import run_agent
from tele_secretary.llm.client import OpenAIResponsesClient

from tele_secretary.persistence.migrations import apply_migrations


def response_body(response_id: str, output: list[dict]) -> dict:
    """Provide a minimal wire response parsed by the real OpenAI SDK."""
    return {
        "id": response_id, "object": "response", "created_at": 1793316957,
        "model": "gpt-5.6-luna", "status": "completed", "output": output,
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
    }


class LLMReplayTests(unittest.TestCase):
    """Verify real SDK defaults cannot break a task's follow-up confirmation."""

    def test_reasoning_replay_omits_null_status_and_confirms_one_saved_task(self):
        """Both SDK-defaulted and explicit null statuses reproduce the live failure."""
        for status_fields in ({}, {"status": None}):
            with self.subTest(status_fields=status_fields), closing(connect(":memory:")) as conn:
                conn.row_factory = Row
                conn.execute("PRAGMA foreign_keys = ON")
                apply_migrations(conn)
                owner = get_or_create_telegram_user_id(
                    conn, telegram_user_id=1001, timezone="America/Chicago",
                )
                reasoning = {
                    "id": "rs_test", "type": "reasoning", "summary": [], "content": [],
                    "encrypted_content": "opaque-test-context", **status_fields,
                }
                function_call = {
                    "id": "fc_test", "type": "function_call", "call_id": "call_test",
                    "name": "create_task_tool", "status": "completed",
                    "arguments": '{"title":"Clean computer screen"}',
                }
                requests = []

                def respond(request: httpx.Request) -> httpx.Response:
                    """Reject the observed invalid field without making network calls."""
                    body = json.loads(request.content)
                    requests.append(body)
                    if len(requests) == 1:
                        return httpx.Response(200, json=response_body("resp_first", [reasoning, function_call]))
                    if "status" in body["input"][1]:
                        return httpx.Response(400, json={"error": {
                            "message": "Unknown parameter: 'input[1].status'.",
                            "type": "invalid_request_error", "param": "input[1].status",
                            "code": "unknown_parameter",
                        }})
                    return httpx.Response(200, json=response_body("resp_final", [{
                        "id": "msg_test", "type": "message", "role": "assistant",
                        "status": "completed", "content": [{
                            "type": "output_text", "text": "Saved T1.", "annotations": [],
                        }],
                    }]))

                sdk = OpenAI(
                    api_key="test-key", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(respond)),
                )
                client = OpenAIResponsesClient(api_key="test-key", model="gpt-5.6-luna", sdk_client=sdk)
                try:
                    text = run_agent(
                        conn, client=client, user_id=owner,
                        user_message="I need to clean my computer screen",
                        current_time=datetime(2026, 10, 29, tzinfo=timezone.utc),
                        user_timezone="America/Chicago",
                    )
                finally:
                    client.close()

                self.assertEqual(text, "Saved T1.")
                self.assertEqual(len(requests), 2)
                self.assertEqual([request["store"] for request in requests], [False, False])
                replay = requests[1]["input"]
                self.assertEqual([item.get("type", "message") for item in replay], [
                    "message", "reasoning", "function_call", "function_call_output",
                ])
                self.assertEqual(replay[1], {
                    "id": "rs_test", "type": "reasoning", "summary": [], "content": [],
                    "encrypted_content": "opaque-test-context",
                })
                self.assertEqual(replay[2], function_call)
                self.assertEqual(replay[3]["call_id"], "call_test")
                self.assertEqual(json.loads(replay[3]["output"]), {
                    "ok": True, "task": {"ref": "T1", "title": "Clean computer screen"},
                })
                self.assertEqual(len(list_active_tasks(conn, user_id=owner)), 1)

    def test_replay_preserves_explicit_nullable_fields_and_non_null_status(self):
        """Serialization must not indiscriminately remove nulls or valid statuses."""
        reasoning = ResponseReasoningItem(
            id="rs_preserve", type="reasoning", summary=[],
            encrypted_content=None, status="completed",
        )
        from tele_secretary.llm.client import _sdk_to_json_continuation_item

        self.assertEqual(_sdk_to_json_continuation_item(reasoning), {
            "id": "rs_preserve", "type": "reasoning", "summary": [],
            "encrypted_content": None, "status": "completed",
        })
