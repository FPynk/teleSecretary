from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.tasks import (
    complete_task,
    create_task,
    list_active_tasks,
    soft_delete_task,
)
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.config import AppConfig
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.bot import (
    AddTaskCommandParseError,
    _addtask_handler,
    _done_handler,
    _help_handler,
    _list_handler,
    _ping_handler,
    _reopen_handler,
    _show_handler,
    _today_handler,
    parse_addtask_command_text,
    parse_done_command_text,
    parse_reopen_command_text,
    parse_show_command_text,
)
from tele_secretary.telegram.responses import build_help_response


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class TelegramListCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_command_requires_configured_owner_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=())

            update = self.build_update(telegram_user_id=1001, text="/ping")
            await _ping_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            [
                "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using TeleSecretary."
            ],
        )

    async def test_help_command_requires_configured_owner_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=())

            update = self.build_update(telegram_user_id=1001, text="/help edit")
            await _help_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            [
                "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using TeleSecretary."
            ],
        )

    async def test_help_edit_accepts_case_and_bot_username_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            expected_response = build_help_response("edit")

            updates = (
                self.build_update(telegram_user_id=1001, text="/help edit"),
                self.build_update(telegram_user_id=1001, text="/help EDIT"),
                self.build_update(
                    telegram_user_id=1001,
                    text="/help@TeleSecretaryBot edit",
                ),
            )
            for update in updates:
                await _help_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            [update.message.replies for update in updates],
            [[expected_response], [expected_response], [expected_response]],
        )

    async def test_help_command_rejects_unknown_and_multiple_topics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            unknown_update = self.build_update(
                telegram_user_id=1001,
                text="/help done",
            )
            multiple_update = self.build_update(
                telegram_user_id=1001,
                text="/help edit extra",
            )

            await _help_handler(config)(unknown_update, SimpleNamespace())
            await _help_handler(config)(multiple_update, SimpleNamespace())

        self.assertIn("Unknown help topic: done", unknown_update.message.replies[0])
        self.assertIn("Use one help topic at a time", multiple_update.message.replies[0])

    async def test_addtask_command_creates_task_with_title_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            update = self.build_update(
                telegram_user_id=1001,
                text="/addtask Buy milk",
            )
            await _addtask_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                tasks = list_active_tasks(conn, user_id=user_id)

        self.assertEqual(update.message.replies, ["Task added: T1 — Buy milk"])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "Buy milk")
        self.assertIsNone(tasks[0].deadline_at)
        self.assertIsNone(tasks[0].deadline_type)
        self.assertEqual(tasks[0].source, "telegram_command")
        self.assertEqual(tasks[0].raw_input_text, "/addtask Buy milk")

    async def test_addtask_command_creates_task_with_due_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            update = self.build_update(
                telegram_user_id=1001,
                text="/addtask Pay electricity bill -due 12/07/2026",
            )
            await _addtask_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                tasks = list_active_tasks(conn, user_id=user_id)

        self.assertEqual(
            update.message.replies,
            ["Task added: T1 — Pay electricity bill\nDue: 12/07/2026"],
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "Pay electricity bill")
        self.assertEqual(tasks[0].deadline_at, "2026-07-13T04:59:00+00:00")
        self.assertEqual(tasks[0].deadline_type, "hard")

    async def test_addtask_command_rejects_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            update = self.build_update(telegram_user_id=1001, text="/addtask")
            await _addtask_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                tasks = list_active_tasks(conn, user_id=user_id)

        self.assertEqual(
            update.message.replies,
            ["Usage: /addtask <title> [-due DD/MM/YYYY]"],
        )
        self.assertEqual(tasks, ())

    async def test_addtask_command_requires_configured_owner_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=())

            update = self.build_update(telegram_user_id=1001, text="/addtask Buy milk")
            await _addtask_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            [
                "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using TeleSecretary."
            ],
        )

    async def test_addtask_command_respects_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(2002,))

            update = self.build_update(telegram_user_id=1001, text="/addtask Buy milk")
            await _addtask_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )

    def test_addtask_parser_accepts_bot_username_suffix(self) -> None:
        parsed_command = parse_addtask_command_text(
            "/addtask@TeleSecretaryBot Renew passport -due 31/08/2026",
            "America/Chicago",
        )

        self.assertEqual(parsed_command.title, "Renew passport")
        self.assertEqual(
            parsed_command.deadline_at.isoformat(),
            "2026-09-01T04:59:00+00:00",
        )
        self.assertEqual(parsed_command.due_date_text, "31/08/2026")

    def test_addtask_parser_rejects_malformed_due_flag(self) -> None:
        invalid_commands = (
            "/addtask -due 12/07/2026",
            "/addtask Pay bill -due",
            "/addtask Pay bill -due 2026-07-12",
            "/addtask Pay bill -due 31/02/2026",
            "/addtask Pay bill -due 12/07/2026 -due 13/07/2026",
            "/addtask Pay bill -dueish 12/07/2026",
            "/addtask Pay bill --due 12/07/2026",
        )

        for invalid_command in invalid_commands:
            with self.subTest(invalid_command=invalid_command):
                with self.assertRaises(AddTaskCommandParseError):
                    parse_addtask_command_text(invalid_command, "America/Chicago")

    async def test_list_command_shows_active_tasks_for_telegram_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                create_task(
                    conn,
                    user_id=user_id,
                    title="Email professor",
                    source="manual_entry",
                )
                completed_task = create_task(
                    conn,
                    user_id=user_id,
                    title="Already done",
                    source="manual_entry",
                )
                complete_task(
                    conn,
                    user_id=user_id,
                    task_id=completed_task.id,
                    source="manual_entry",
                )

            update = self.build_update(telegram_user_id=1001)
            await _list_handler(config)(update, SimpleNamespace())

        self.assertEqual(update.message.replies, ["Active tasks:\nT1 — Email professor"])

    async def test_show_command_returns_full_localized_task_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                with conn:
                    conn.execute(
                        """
                        INSERT INTO categories (id, user_id, name, created_at)
                        VALUES ('cat-school', ?, 'school', '2026-07-01T00:00:00+00:00')
                        """,
                        (user_id,),
                    )
                create_task(
                    conn,
                    user_id=user_id,
                    title="Email professor",
                    source="manual_entry",
                    description="Ask about the syllabus.",
                    category_id="cat-school",
                    deadline_at=datetime(2026, 7, 10, 22, 0, tzinfo=timezone.utc),
                    deadline_type="soft",
                    planned_start_at=datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc),
                    planned_end_at=datetime(2026, 7, 8, 16, 0, tzinfo=timezone.utc),
                    estimated_minutes=30,
                    urgency="high",
                )

            update = self.build_update(telegram_user_id=1001, text="/show T1")
            await _show_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            [
                "T1 — Email professor\n"
                "Status: active\n"
                "Description: Ask about the syllabus.\n"
                "Category: school\n"
                "Deadline: Fri Jul 10, 2026 at 5:00 PM (soft)\n"
                "Planned window: Wed Jul 8, 2026 at 10:00 AM — "
                "Wed Jul 8, 2026 at 11:00 AM\n"
                "Urgency: high\n"
                "Estimate: 30 minutes\n"
                "Tags: None\n"
                "Reminders: None"
            ],
        )

    async def test_show_command_handles_invalid_and_missing_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            invalid_update = self.build_update(telegram_user_id=1001, text="/show 1")
            await _show_handler(config)(invalid_update, SimpleNamespace())
            missing_update = self.build_update(telegram_user_id=1001, text="/show t99")
            await _show_handler(config)(missing_update, SimpleNamespace())

        self.assertEqual(invalid_update.message.replies, ["Usage: /show T<number>"])
        self.assertEqual(
            missing_update.message.replies,
            ["Task T99 was not found. Use /list to see active task refs."],
        )

    async def test_show_command_respects_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(2002,))

            update = self.build_update(telegram_user_id=1001, text="/show T1")
            await _show_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )

    async def test_reopen_command_reopens_completed_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                task = create_task(
                    conn,
                    user_id=user_id,
                    title="Email professor",
                    source="manual_entry",
                )
                complete_task(
                    conn,
                    user_id=user_id,
                    task_id=task.id,
                    source="manual_entry",
                    completed_at=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
                )
                with conn:
                    conn.executemany(
                        """
                        INSERT INTO reminders (
                            id, item_id, scheduled_at, status, cancelled_at,
                            sent_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                "cancelled-reminder",
                                task.id,
                                "2026-07-18T12:00:00+00:00",
                                "cancelled",
                                "2026-07-17T12:00:00+00:00",
                                None,
                                "2026-07-17T12:00:00+00:00",
                                "2026-07-17T12:00:00+00:00",
                            ),
                            (
                                "sent-reminder",
                                task.id,
                                "2026-07-18T13:00:00+00:00",
                                "sent",
                                None,
                                "2026-07-17T12:00:00+00:00",
                                "2026-07-17T12:00:00+00:00",
                                "2026-07-17T12:00:00+00:00",
                            ),
                        ],
                    )

            update = self.build_update(telegram_user_id=1001, text="/reopen T1")
            await _reopen_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                task_row = conn.execute(
                    """
                    SELECT items.status, task_items.completed_at
                    FROM items
                    JOIN task_items ON task_items.item_id = items.id
                    WHERE items.id = ?
                    """,
                    (task.id,),
                ).fetchone()
                completion_events = conn.execute(
                    "SELECT event_type, source FROM completion_logs WHERE item_id = ?",
                    (task.id,),
                ).fetchall()
                reminder_rows = conn.execute(
                    "SELECT id, status FROM reminders WHERE item_id = ? ORDER BY id",
                    (task.id,),
                ).fetchall()

        self.assertEqual(update.message.replies, ['Reopened "Email professor".'])
        self.assertEqual(task_row["status"], "active")
        self.assertIsNone(task_row["completed_at"])
        self.assertCountEqual(
            [(row["event_type"], row["source"]) for row in completion_events],
            [("completed", "manual_entry"), ("reopened", "telegram_command")],
        )
        self.assertEqual(
            [(row["id"], row["status"]) for row in reminder_rows],
            [("cancelled-reminder", "cancelled"), ("sent-reminder", "sent")],
        )

    async def test_reopen_command_handles_invalid_missing_and_active_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                create_task(
                    conn,
                    user_id=user_id,
                    title="Already active",
                    source="manual_entry",
                )

            invalid_update = self.build_update(telegram_user_id=1001, text="/reopen 1")
            missing_update = self.build_update(telegram_user_id=1001, text="/reopen T99")
            active_update = self.build_update(telegram_user_id=1001, text="/reopen T1")
            for update in (invalid_update, missing_update, active_update):
                await _reopen_handler(config)(update, SimpleNamespace())

        self.assertEqual(invalid_update.message.replies, ["Usage: /reopen T<number>"])
        self.assertEqual(
            missing_update.message.replies,
            ["Task T99 was not found. Use /list to see active task refs."],
        )
        self.assertEqual(
            active_update.message.replies,
            ["Could not reopen task: Only completed tasks can be reopened."],
        )

    async def test_reopen_command_does_not_disclose_another_owners_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                with conn:
                    conn.execute(
                        """
                        INSERT INTO users (
                            id, telegram_user_id, timezone, created_at, updated_at
                        ) VALUES ('foreign-owner', 2002, 'America/Chicago', ?, ?)
                        """,
                        ("2026-07-17T00:00:00+00:00", "2026-07-17T00:00:00+00:00"),
                    )
                foreign_task = create_task(
                    conn,
                    user_id="foreign-owner",
                    title="Private completed task",
                    source="manual_entry",
                )
                complete_task(
                    conn,
                    user_id="foreign-owner",
                    task_id=foreign_task.id,
                    source="manual_entry",
                )

            update = self.build_update(telegram_user_id=1001, text="/reopen T1")
            await _reopen_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            ["Task T1 was not found. Use /list to see active task refs."],
        )

    async def test_reopen_command_respects_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(2002,))

            update = self.build_update(telegram_user_id=1001, text="/reopen T1")
            await _reopen_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )

    async def test_done_command_completes_an_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                task = create_task(
                    conn,
                    user_id=user_id,
                    title="Email professor",
                    source="manual_entry",
                )

            update = self.build_update(
                telegram_user_id=1001,
                text="/done@TeleSecretaryBot T1",
            )
            await _done_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                task_row = conn.execute(
                    """
                    SELECT items.status, task_items.completed_at
                    FROM items
                    JOIN task_items ON task_items.item_id = items.id
                    WHERE items.id = ?
                    """,
                    (task.id,),
                ).fetchone()
                completion_events = conn.execute(
                    "SELECT event_type, source FROM completion_logs WHERE item_id = ?",
                    (task.id,),
                ).fetchall()

        self.assertEqual(update.message.replies, ['Marked "Email professor" as done.'])
        self.assertEqual(task_row["status"], "completed")
        completed_at = datetime.fromisoformat(task_row["completed_at"])
        self.assertEqual(completed_at.utcoffset(), timezone.utc.utcoffset(completed_at))
        self.assertEqual(
            [(row["event_type"], row["source"]) for row in completion_events],
            [("completed", "telegram_command")],
        )

    async def test_done_command_handles_invalid_missing_and_completed_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                task = create_task(
                    conn,
                    user_id=user_id,
                    title="Already done",
                    source="manual_entry",
                )
                complete_task(
                    conn,
                    user_id=user_id,
                    task_id=task.id,
                    source="manual_entry",
                )

            missing_ref_update = self.build_update(telegram_user_id=1001, text="/done")
            invalid_ref_update = self.build_update(telegram_user_id=1001, text="/done T0")
            missing_task_update = self.build_update(telegram_user_id=1001, text="/done T99")
            completed_task_update = self.build_update(telegram_user_id=1001, text="/done T1")
            for update in (
                missing_ref_update,
                invalid_ref_update,
                missing_task_update,
                completed_task_update,
            ):
                await _done_handler(config)(update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                task_row = conn.execute(
                    "SELECT status FROM items WHERE id = ?",
                    (task.id,),
                ).fetchone()
                completion_event_count = conn.execute(
                    "SELECT COUNT(*) FROM completion_logs WHERE item_id = ?",
                    (task.id,),
                ).fetchone()[0]

        self.assertEqual(missing_ref_update.message.replies, ["Usage: /done T<number>"])
        self.assertEqual(invalid_ref_update.message.replies, ["Usage: /done T<number>"])
        self.assertEqual(
            missing_task_update.message.replies,
            ["Task T99 was not found. Use /list to see active task refs."],
        )
        self.assertEqual(
            completed_task_update.message.replies,
            ["Could not complete task: Only active tasks can be completed."],
        )
        self.assertEqual(task_row["status"], "completed")
        self.assertEqual(completion_event_count, 1)

    async def test_done_command_does_not_disclose_inaccessible_or_deleted_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                with conn:
                    conn.execute(
                        """
                        INSERT INTO users (
                            id, telegram_user_id, timezone, created_at, updated_at
                        ) VALUES ('foreign-owner', 2002, 'America/Chicago', ?, ?)
                        """,
                        ("2026-07-17T00:00:00+00:00", "2026-07-17T00:00:00+00:00"),
                    )
                inaccessible_task = create_task(
                    conn,
                    user_id="foreign-owner",
                    title="Private task",
                    source="manual_entry",
                )
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                deleted_task = create_task(
                    conn,
                    user_id=user_id,
                    title="Deleted task",
                    source="manual_entry",
                )
                soft_delete_task(
                    conn,
                    user_id=user_id,
                    task_id=deleted_task.id,
                    source="manual_entry",
                )

            inaccessible_update = self.build_update(telegram_user_id=1001, text="/done T1")
            deleted_update = self.build_update(telegram_user_id=1001, text="/done T1")
            await _done_handler(config)(inaccessible_update, SimpleNamespace())
            await _done_handler(config)(deleted_update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                inaccessible_status = conn.execute(
                    "SELECT status FROM items WHERE id = ?",
                    (inaccessible_task.id,),
                ).fetchone()["status"]
                deleted_status = conn.execute(
                    "SELECT status FROM items WHERE id = ?",
                    (deleted_task.id,),
                ).fetchone()["status"]

        self.assertEqual(
            inaccessible_update.message.replies,
            ["Task T1 was not found. Use /list to see active task refs."],
        )
        self.assertEqual(
            deleted_update.message.replies,
            ["Task T1 was not found. Use /list to see active task refs."],
        )
        self.assertEqual(inaccessible_status, "active")
        self.assertEqual(deleted_status, "deleted")

    async def test_done_command_respects_authorization_before_database_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(2002,))

            update = self.build_update(telegram_user_id=1001, text="/done T1")
            await _done_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )

    async def test_today_command_shows_focus_tasks_and_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            empty_update = self.build_update(telegram_user_id=1001, text="/today")
            await _today_handler(config)(empty_update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                user_id = get_or_create_telegram_user_id(
                    conn,
                    telegram_user_id=1001,
                    timezone=config.user_timezone,
                )
                create_task(
                    conn,
                    user_id=user_id,
                    title="Pay rent",
                    source="manual_entry",
                    urgency="top_priority",
                )

            focus_update = self.build_update(telegram_user_id=1001, text="/today")
            await _today_handler(config)(focus_update, SimpleNamespace())

        self.assertEqual(empty_update.message.replies, ["No tasks need your focus today."])
        self.assertEqual(
            focus_update.message.replies,
            ["Focus today:\n1. T1 — Pay rent — urgent undated"],
        )

    async def test_today_command_respects_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(2002,))

            update = self.build_update(telegram_user_id=1001, text="/today")
            await _today_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )

    def test_show_parser_accepts_refs_and_rejects_extra_or_invalid_arguments(self) -> None:
        self.assertEqual(parse_show_command_text("/show T12"), "T12")
        self.assertEqual(parse_show_command_text("/show@TeleSecretaryBot t7"), "T7")
        self.assertIsNone(parse_show_command_text("/show"))
        self.assertIsNone(parse_show_command_text("/show T0"))
        self.assertIsNone(parse_show_command_text("/show T1 extra"))

    def test_reopen_parser_accepts_refs_and_rejects_extra_or_invalid_arguments(self) -> None:
        self.assertEqual(parse_reopen_command_text("/reopen T12"), "T12")
        self.assertEqual(parse_reopen_command_text("/reopen@TeleSecretaryBot t7"), "T7")
        self.assertIsNone(parse_reopen_command_text("/reopen"))
        self.assertIsNone(parse_reopen_command_text("/reopen T0"))
        self.assertIsNone(parse_reopen_command_text("/reopen T1 extra"))

    def test_done_parser_accepts_refs_and_rejects_extra_or_invalid_arguments(self) -> None:
        self.assertEqual(parse_done_command_text("/done T12"), "T12")
        self.assertEqual(parse_done_command_text("/done@TeleSecretaryBot t7"), "T7")
        self.assertIsNone(parse_done_command_text("/done"))
        self.assertIsNone(parse_done_command_text("/done T0"))
        self.assertIsNone(parse_done_command_text("/done T1 extra"))

    async def test_list_command_has_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            update = self.build_update(telegram_user_id=1001)
            await _list_handler(config)(update, SimpleNamespace())

        self.assertEqual(update.message.replies, ["No active tasks."])

    async def test_list_command_requires_configured_owner_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=())

            update = self.build_update(telegram_user_id=1001)
            await _list_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            [
                "Set TELEGRAM_ALLOWED_USER_IDS to your Telegram user ID before using TeleSecretary."
            ],
        )

    async def test_list_command_respects_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(2002,))

            update = self.build_update(telegram_user_id=1001)
            await _list_handler(config)(update, SimpleNamespace())

        self.assertEqual(
            update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )

    def build_config(
        self,
        root: Path,
        *,
        allowed_user_ids: tuple[int, ...] = (1001,),
    ) -> AppConfig:
        return AppConfig(
            telegram_bot_token="test-token",
            telegram_allowed_user_ids=allowed_user_ids,
            data_dir=root / "data",
            log_dir=root / "logs",
            db_path=root / "data" / "secretary.sqlite3",
            user_timezone="America/Chicago",
            log_level="INFO",
        )

    def build_update(self, *, telegram_user_id: int, text: str = "/list") -> SimpleNamespace:
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=telegram_user_id),
            message=FakeMessage(text),
        )


if __name__ == "__main__":
    unittest.main()
