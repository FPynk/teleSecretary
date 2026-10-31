"""Offline diagnostic traces for real agent, tool, and provider-adapter paths."""

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from sqlite3 import Row, connect
from types import SimpleNamespace
from threading import Barrier
import unittest
from unittest.mock import Mock

import _path  # noqa: F401
from tele_secretary.app.tasks import list_active_tasks
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.llm.agent import LLM_REQUEST_FAILED_TEXT, run_agent
from tele_secretary.llm.client import LLMClientError, OpenAIResponsesClient
from tele_secretary.llm.diagnostics import LLM_LOG_CONTEXT
from tele_secretary.persistence.migrations import apply_migrations


PRIVATE_TEXT = "PRIVATE-message-title-and-provider-body"


class BadRequestError(RuntimeError):
    """Supply SDK-shaped error metadata without a live HTTP request."""

    status_code = 400
    request_id = "req_rejected"
    code = "invalid_request_error"
    param = "input[1].id"


class LLMDiagnosticsTests(unittest.TestCase):
    """Pin the evidence needed to debug failures without exposing content."""

    def test_metadata_reports_status_parameter_without_private_error_body(self):
        """The structural path rejected in production must survive metadata filtering."""
        error = BadRequestError(PRIVATE_TEXT)
        error.param = "input[1].status"
        client = OpenAIResponsesClient(
            api_key="test-key", model="gpt-test",
            sdk_client=SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=error))),
        )
        with self.assertLogs("tele_secretary.llm.client", level="WARNING") as logged:
            with self.assertRaises(LLMClientError):
                client.create_response(instructions=PRIVATE_TEXT, input_items=PRIVATE_TEXT)
        failure = json.loads(logged.records[0].getMessage())
        self.assertEqual(failure["error_param"], "input[1].status")
        self.assertNotIn(PRIVATE_TEXT, "\n".join(logged.output))

    def test_opt_in_dumps_requests_responses_and_errors_with_credentials_redacted(self):
        """A debugging transcript keeps content but removes credentials and opaque reasoning."""
        api_key = "sk-secret-configured-key"
        bot_token = "123456789:telegram-secret-token"
        raw_response = {
            "id": "resp_dump", "output": [{
                "type": "message", "content": [{"type": "output_text", "text": PRIVATE_TEXT}],
            }, {"type": "reasoning", "encrypted_content": "OPAQUE-REASONING"}],
        }
        response = SimpleNamespace(
            id="resp_dump", output_text=PRIVATE_TEXT, output=[],
            model_dump=lambda **kwargs: raw_response,
        )
        error = BadRequestError(f"Rejected {api_key}")
        error.body = {"message": f"Bad argument containing {bot_token}"}
        sdk = SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=[response, error])))
        client = OpenAIResponsesClient(
            api_key=api_key, model="gpt-test", sdk_client=sdk,
            log_transcripts=True, secrets_to_redact=(bot_token,),
        )
        input_items = [
            {"role": "user", "content": f"{PRIVATE_TEXT} {api_key} {bot_token}"},
            {"type": "function_call_output", "call_id": "call_1", "output": '{"ok":true}'},
        ]
        with self.assertLogs("tele_secretary.llm.client", level="INFO") as logged:
            client.create_response(instructions="Static prompt", input_items=input_items)
            with self.assertRaises(LLMClientError):
                client.create_response(instructions="Static prompt", input_items=input_items)
        events = [json.loads(record.getMessage()) for record in logged.records]
        request_dump = next(event["transcript"] for event in events if event["event"] == "llm_transcript_request")
        response_dump = next(event["transcript"] for event in events if event["event"] == "llm_transcript_response")
        error_dump = next(event["transcript"] for event in events if event["event"] == "llm_transcript_error")
        self.assertEqual(request_dump["instructions"], "Static prompt")
        self.assertIn(PRIVATE_TEXT, request_dump["input"][0]["content"])
        self.assertEqual(request_dump["input"][1]["output"], '{"ok":true}')
        self.assertEqual(response_dump["output"][0]["content"][0]["text"], PRIVATE_TEXT)
        self.assertIn("Rejected", error_dump["message"])
        self.assertNotIn(api_key, "\n".join(logged.output))
        self.assertNotIn(bot_token, "\n".join(logged.output))
        self.assertNotIn("OPAQUE-REASONING", "\n".join(logged.output))

    def test_correlates_saved_or_rejected_tool_with_follow_up_failure(self):
        """Tool outcomes and failed provider rounds share a per-message ID."""
        for confidence, expected_code in ((None, None), (2, "invalid_parse_confidence")):
            with self.subTest(confidence=confidence), closing(connect(":memory:")) as conn:
                conn.row_factory = Row
                conn.execute("PRAGMA foreign_keys = ON")
                apply_migrations(conn)
                owner = get_or_create_telegram_user_id(
                    conn, telegram_user_id=1001, timezone="America/Chicago",
                )
                call = SimpleNamespace(
                    type="function_call", call_id="call_create", name="create_task_tool",
                    arguments=json.dumps({"title": PRIVATE_TEXT, "parse_confidence": confidence}),
                )
                reasoning = SimpleNamespace(
                    type="reasoning",
                    model_dump=lambda **kwargs: {
                        "type": "reasoning", "id": "rs_example", "encrypted_content": PRIVATE_TEXT,
                    },
                )
                first = SimpleNamespace(
                    id="resp_first", _request_id="req_first", status="completed",
                    output_text="", output=[reasoning, call],
                )
                final = SimpleNamespace(
                    id="resp_final", _request_id="req_final", status="completed",
                    output_text=PRIVATE_TEXT, output=[],
                )
                sdk = SimpleNamespace(responses=SimpleNamespace(
                    create=Mock(side_effect=[first, BadRequestError(PRIVATE_TEXT), final]),
                ))
                client = OpenAIResponsesClient(api_key="sk-private-test-key", model="gpt-test", sdk_client=sdk)
                with self.assertLogs("tele_secretary.llm", level="INFO") as logged:
                    result = run_agent(
                        conn, client=client, user_id=owner, user_message=PRIVATE_TEXT,
                        current_time=datetime(2026, 10, 26, tzinfo=timezone.utc),
                        user_timezone="America/Chicago",
                    )
                    run_agent(
                        conn, client=client, user_id=owner, user_message=PRIVATE_TEXT,
                        current_time=datetime(2026, 10, 26, tzinfo=timezone.utc),
                        user_timezone="America/Chicago",
                    )

                events = [json.loads(record.getMessage()) for record in logged.records]
                starts = [event for event in events if event["event"] == "llm_round_started"]
                self.assertEqual([event["round"] for event in starts], [1, 2, 1])
                operation = starts[0]["operation_id"]
                self.assertEqual(starts[1]["operation_id"], operation)
                self.assertNotEqual(starts[2]["operation_id"], operation)
                self.assertTrue(all(event["operation_id"] == operation
                                    for event in events[:events.index(starts[2])]))
                tool = next(event for event in events if event["event"] == "llm_tool_completed")
                self.assertEqual(tool["operation_id"], operation)
                self.assertEqual(tool["round"], 1)
                self.assertEqual(tool["ok"], confidence is None)
                self.assertEqual(tool["error_code"], expected_code)
                failure = next(event for event in events if event["event"] == "llm_client_failed")
                self.assertEqual(failure["operation_id"], operation)
                self.assertEqual(failure["round"], 2)
                self.assertEqual(failure["stage"], "request")
                self.assertEqual(failure["exception_type"], "BadRequestError")
                self.assertEqual(failure["status_code"], 400)
                self.assertEqual(failure["provider_request_id"], "req_rejected")
                self.assertEqual(failure["error_code"], "invalid_request_error")
                self.assertEqual(failure["error_param"], "input[1].id")
                self.assertGreaterEqual(failure["duration_ms"], 0)
                follow_up = next(event for event in events
                                 if event["event"] == "llm_client_started" and event["round"] == 2)
                self.assertEqual(follow_up["input_types"], ["message", "reasoning", "function_call", "function_call_output"])
                self.assertEqual(follow_up["encrypted_reasoning_count"], 1)
                ended = next(event for event in events if event["event"] == "llm_operation_finished")
                self.assertEqual(ended["reason"], "provider_failure")
                self.assertEqual(ended["saved_task_count"], int(confidence is None))
                self.assertEqual(len(list_active_tasks(conn, user_id=owner)), int(confidence is None))
                self.assertEqual(result.startswith("Saved tasks:"), confidence is None)
                if confidence is not None:
                    self.assertEqual(result, LLM_REQUEST_FAILED_TEXT)
                self.assertNotIn(PRIVATE_TEXT, "\n".join(logged.output))
                self.assertNotIn("sk-private-test-key", "\n".join(logged.output))
                self.assertEqual(LLM_LOG_CONTEXT.get(), ("-", 0))

    def test_distinguishes_transport_and_response_parsing_failures(self):
        """A received but malformed response differs from a transport failure."""
        for outcome, stage, exception_type in (
            (TimeoutError(PRIVATE_TEXT), "request", "TimeoutError"),
            (SimpleNamespace(id="resp_bad", _request_id="req_bad", status="completed",
                             output_text=123, output=[]), "normalization", "LLMClientError"),
        ):
            with self.subTest(stage=stage):
                sdk = SimpleNamespace(responses=SimpleNamespace(create=Mock()))
                if isinstance(outcome, Exception):
                    sdk.responses.create.side_effect = outcome
                else:
                    sdk.responses.create.return_value = outcome
                client = OpenAIResponsesClient(api_key="test-key", model="gpt-test", sdk_client=sdk)
                with self.assertLogs("tele_secretary.llm.client", level="INFO") as logged:
                    with self.assertRaises(LLMClientError):
                        client.create_response(instructions=PRIVATE_TEXT, input_items=PRIVATE_TEXT)
                failure = next(json.loads(record.getMessage()) for record in logged.records
                               if json.loads(record.getMessage())["event"] == "llm_client_failed")
                self.assertEqual(failure["stage"], stage)
                self.assertEqual(failure["exception_type"], exception_type)
                if stage == "normalization":
                    self.assertEqual(failure["provider_request_id"], "req_bad")
                    self.assertEqual(failure["validation_reason"], "malformed_text")
                self.assertNotIn(PRIVATE_TEXT, "\n".join(logged.output))

    def test_concurrent_messages_keep_separate_operation_ids(self):
        """An overlapping provider call cannot inherit another worker's trace."""
        barrier = Barrier(2)

        def request():
            with closing(connect(":memory:")) as conn:
                conn.row_factory = Row
                apply_migrations(conn)
                owner = get_or_create_telegram_user_id(conn, telegram_user_id=1001, timezone="UTC")

                def respond(**kwargs):
                    barrier.wait(timeout=5)
                    return SimpleNamespace(id="resp_ok", output_text="Done", output=[])

                sdk = SimpleNamespace(responses=SimpleNamespace(create=respond))
                client = OpenAIResponsesClient(api_key="test", model="gpt-test", sdk_client=sdk)
                result = run_agent(
                    conn, client=client, user_id=owner, user_message=PRIVATE_TEXT,
                    current_time=datetime(2026, 10, 26, tzinfo=timezone.utc), user_timezone="UTC",
                )
                self.assertEqual(LLM_LOG_CONTEXT.get(), ("-", 0))
                return result

        with self.assertLogs("tele_secretary.llm", level="INFO") as logged:
            with ThreadPoolExecutor(max_workers=2) as executor:
                self.assertEqual(list(executor.map(lambda _: request(), range(2))), ["Done", "Done"])
        events = [json.loads(record.getMessage()) for record in logged.records]
        operation_ids = {event["operation_id"] for event in events}
        self.assertEqual(len(operation_ids), 2)
        self.assertNotIn("-", operation_ids)
        for operation_id in operation_ids:
            trace = [event for event in events if event["operation_id"] == operation_id]
            self.assertEqual([event["event"] for event in trace], [
                "llm_round_started", "llm_client_started", "llm_client_received",
                "llm_client_completed", "llm_round_completed", "llm_operation_finished",
            ])

    def test_untrusted_error_metadata_cannot_leak_arbitrary_values(self):
        """Only machine fields from known formats may appear in error events."""
        error = BadRequestError(PRIVATE_TEXT)
        error.code = PRIVATE_TEXT
        error.param = PRIVATE_TEXT
        error.request_id = PRIVATE_TEXT
        client = OpenAIResponsesClient(
            api_key="test", model="sk-private-key",
            sdk_client=SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=error))),
        )
        with self.assertLogs("tele_secretary.llm.client", level="INFO") as logged:
            with self.assertRaises(LLMClientError):
                client.create_response(instructions=PRIVATE_TEXT, input_items=PRIVATE_TEXT)
        self.assertNotIn(PRIVATE_TEXT, "\n".join(logged.output))
        self.assertNotIn("sk-private-key", "\n".join(logged.output))
