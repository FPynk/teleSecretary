from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.reminders import (
    ReminderSelectionError,
    ReminderValidationError,
    cancel_pending_reminder,
    claim_due_reminders,
    create_reminder,
)
from tele_secretary.app.tasks import TaskRecord, create_task
from tele_secretary.app.users import get_or_create_telegram_user_id
from tele_secretary.config import AppConfig
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.bot import (
    UNREMIND_SELECTION_CONTEXT_KEY,
    ParsedUnremindCommand,
    UnremindSelectionSnapshot,
    _unremind_handler,
    build_application,
    parse_unremind_command_text,
)
from tele_secretary.telegram.responses import (
    build_unremind_cancelled_response,
    build_unremind_invalid_option_response,
    build_unremind_no_pending_response,
    build_unremind_persistence_error_response,
    build_unremind_selection_cancelled_response,
    build_unremind_selection_prompt_response,
    build_unremind_selection_stale_response,
    build_unremind_stale_response,
    build_unremind_usage_response,
)


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class UnremindCommandParserTests(unittest.TestCase):
    def test_parser_accepts_mentions_normalizes_refs_and_keeps_choice_order(self) -> None:
        self.assertEqual(
            parse_unremind_command_text("/unremind T12"),
            ParsedUnremindCommand(task_ref="T12", selection_numbers=()),
        )
        self.assertEqual(
            parse_unremind_command_text(" /UNREMIND@TeleSecretaryBot t7 2 1 "),
            ParsedUnremindCommand(task_ref="T7", selection_numbers=(2, 1)),
        )

    def test_parser_rejects_missing_malformed_and_extra_arguments(self) -> None:
        invalid_commands = (
            "/unremind",
            "/unremind 12",
            "/unremind T0",
            "/unremind T01",
            "/unremind T1 0",
            "/unremind T1 -1",
            "/unremind T1 next",
            "/unremind T1 1 1",
            "/remind T1",
        )

        for command_text in invalid_commands:
            with self.subTest(command_text=command_text):
                self.assertIsNone(parse_unremind_command_text(command_text))


class TelegramUnremindCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_or_unauthorized_commands_do_not_open_a_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid_config = self.build_config(root / "invalid")
            invalid_update = self.build_update("/unremind T1 1 1")
            unauthorized_config = self.build_config(
                root / "unauthorized",
                allowed_user_ids=(2002,),
            )
            unauthorized_update = self.build_update("/unremind T1")
            invalid_context = self.build_context()
            unauthorized_context = self.build_context()
            invalid_context.user_data[UNREMIND_SELECTION_CONTEXT_KEY] = "unchanged"
            unauthorized_context.user_data[UNREMIND_SELECTION_CONTEXT_KEY] = "unchanged"

            with patch("tele_secretary.telegram.bot.connect") as connect:
                await _unremind_handler(invalid_config)(invalid_update, invalid_context)
                await _unremind_handler(unauthorized_config)(unauthorized_update, unauthorized_context)

        connect.assert_not_called()
        self.assertEqual(invalid_context.user_data[UNREMIND_SELECTION_CONTEXT_KEY], "unchanged")
        self.assertEqual(unauthorized_context.user_data[UNREMIND_SELECTION_CONTEXT_KEY], "unchanged")
        self.assertEqual(invalid_update.message.replies, [build_unremind_usage_response()])
        self.assertEqual(
            unauthorized_update.message.replies,
            ["This Telegram account is not authorized to use TeleSecretary."],
        )

    async def test_zero_pending_reminders_returns_a_clear_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")

            update = self.build_update("/unremind T1")
            await _unremind_handler(config)(update, self.build_context())

        self.assertEqual(update.message.replies, [build_unremind_no_pending_response(task)])

    async def test_sole_pending_reminder_is_cancelled_and_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminder = create_reminder(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                )

            update = self.build_update("/unremind T1")
            await _unremind_handler(config)(update, self.build_context())

            with open_test_database(config.db_path) as conn:
                row = conn.execute(
                    "SELECT status, cancelled_at, updated_at FROM reminders WHERE id = ?",
                    (reminder.id,),
                ).fetchone()

        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["cancelled_at"])
        self.assertEqual(row["cancelled_at"], row["updated_at"])
        self.assertEqual(update.message.replies, [build_unremind_cancelled_response(task)])

    async def test_multiple_pending_reminders_are_left_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminders = []
                for hour in (20, 21):
                    reminders.append(create_reminder(
                        conn,
                        user_id=task.user_id,
                        task_id=task.id,
                        scheduled_at=datetime(2099, 1, 1, hour, 0, tzinfo=timezone.utc),
                    ))

            update = self.build_update("/unremind T1")
            context = self.build_context()
            await _unremind_handler(config)(update, context)

            with open_test_database(config.db_path) as conn:
                statuses = tuple(
                    row["status"]
                    for row in conn.execute(
                        "SELECT status FROM reminders ORDER BY scheduled_at"
                    ).fetchall()
                )

        self.assertEqual(statuses, ("pending", "pending"))
        self.assertEqual(
            update.message.replies,
            [
                build_unremind_selection_prompt_response(
                    task,
                    reminders=tuple(reminders),
                    timezone_name=config.user_timezone,
                )
            ],
        )
        self.assertEqual(
            context.user_data[UNREMIND_SELECTION_CONTEXT_KEY],
            UnremindSelectionSnapshot(
                task_id=task.id,
                task_ref="T1",
                reminder_ids=tuple(reminder.id for reminder in reminders),
            ),
        )

    async def test_new_prompt_replaces_the_previous_task_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                first_task = self.create_task_for_user(conn, config, 1001, "First task")
                second_task = self.create_task_for_user(conn, config, 1001, "Second task")
                for task, start_hour in ((first_task, 20), (second_task, 22)):
                    for hour in (start_hour, start_hour + 1):
                        create_reminder(
                            conn,
                            user_id=task.user_id,
                            task_id=task.id,
                            scheduled_at=datetime(2099, 1, 1, hour, 0, tzinfo=timezone.utc),
                        )

            context = self.build_context()
            await _unremind_handler(config)(self.build_update("/unremind T1"), context)
            await _unremind_handler(config)(self.build_update("/unremind T2"), context)

        snapshot = context.user_data[UNREMIND_SELECTION_CONTEXT_KEY]
        self.assertEqual(snapshot.task_id, second_task.id)
        self.assertEqual(snapshot.task_ref, "T2")

    async def test_selection_without_a_snapshot_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminders = self.create_reminders(conn, task, 20, 21)

            update = self.build_update("/unremind T1 1")
            context = self.build_context()
            with patch(
                "tele_secretary.telegram.bot.list_pending_reminders_for_task"
            ) as list_pending_reminders:
                await _unremind_handler(config)(update, context)

            with open_test_database(config.db_path) as conn:
                statuses = self.read_reminder_statuses(conn, reminders)

        list_pending_reminders.assert_not_called()
        self.assertEqual(statuses, ("pending", "pending"))
        self.assertEqual(update.message.replies, [build_unremind_selection_stale_response("T1")])
        self.assertNotIn(UNREMIND_SELECTION_CONTEXT_KEY, context.user_data)

    async def test_snapshot_for_another_task_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                first_task = self.create_task_for_user(conn, config, 1001, "First task")
                second_task = self.create_task_for_user(conn, config, 1001, "Second task")
                first_reminders = self.create_reminders(conn, first_task, 20, 21)
                second_reminders = self.create_reminders(conn, second_task, 22, 23)

            context = self.build_context()
            await _unremind_handler(config)(self.build_update("/unremind T1"), context)
            selection_update = self.build_update("/unremind T2 1")
            await _unremind_handler(config)(selection_update, context)

            with open_test_database(config.db_path) as conn:
                statuses = self.read_reminder_statuses(
                    conn,
                    (*first_reminders, *second_reminders),
                )

        self.assertEqual(statuses, ("pending", "pending", "pending", "pending"))
        self.assertEqual(
            selection_update.message.replies,
            [build_unremind_selection_stale_response("T2")],
        )
        self.assertNotIn(UNREMIND_SELECTION_CONTEXT_KEY, context.user_data)

    async def test_selection_cancels_one_displayed_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminders = self.create_reminders(conn, task, 20, 21)

            context = self.build_context()
            await _unremind_handler(config)(self.build_update("/unremind T1"), context)
            selection_update = self.build_update("/unremind T1 1")
            await _unremind_handler(config)(selection_update, context)

            with open_test_database(config.db_path) as conn:
                statuses = self.read_reminder_statuses(conn, reminders)

        self.assertEqual(statuses, ("cancelled", "pending"))
        self.assertEqual(
            selection_update.message.replies,
            [build_unremind_selection_cancelled_response(task, 1)],
        )
        self.assertNotIn(UNREMIND_SELECTION_CONTEXT_KEY, context.user_data)

    async def test_selection_cancels_several_displayed_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminders = self.create_reminders(conn, task, 20, 21, 22)

            context = self.build_context()
            await _unremind_handler(config)(self.build_update("/unremind T1"), context)
            selection_update = self.build_update("/unremind T1 3 1")
            await _unremind_handler(config)(selection_update, context)

            with open_test_database(config.db_path) as conn:
                statuses = self.read_reminder_statuses(conn, reminders)

        self.assertEqual(statuses, ("cancelled", "pending", "cancelled"))
        self.assertEqual(
            selection_update.message.replies,
            [build_unremind_selection_cancelled_response(task, 2)],
        )
        self.assertNotIn(UNREMIND_SELECTION_CONTEXT_KEY, context.user_data)

    async def test_out_of_range_selection_keeps_the_snapshot_and_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminders = self.create_reminders(conn, task, 20, 21)

            context = self.build_context()
            await _unremind_handler(config)(self.build_update("/unremind T1"), context)
            snapshot = context.user_data[UNREMIND_SELECTION_CONTEXT_KEY]
            selection_update = self.build_update("/unremind T1 3")
            await _unremind_handler(config)(selection_update, context)

            with open_test_database(config.db_path) as conn:
                statuses = self.read_reminder_statuses(conn, reminders)

        self.assertEqual(statuses, ("pending", "pending"))
        self.assertEqual(
            selection_update.message.replies,
            [build_unremind_invalid_option_response()],
        )
        self.assertEqual(context.user_data[UNREMIND_SELECTION_CONTEXT_KEY], snapshot)

    async def test_pending_list_insertion_makes_the_snapshot_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminders = self.create_reminders(conn, task, 20, 21)

            context = self.build_context()
            await _unremind_handler(config)(self.build_update("/unremind T1"), context)
            with open_test_database(config.db_path) as conn:
                added_reminder = create_reminder(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    scheduled_at=datetime(2099, 1, 1, 22, 0, tzinfo=timezone.utc),
                )

            selection_update = self.build_update("/unremind T1 1")
            await _unremind_handler(config)(selection_update, context)

            with open_test_database(config.db_path) as conn:
                statuses = self.read_reminder_statuses(
                    conn,
                    (*reminders, added_reminder),
                )

        self.assertEqual(statuses, ("pending", "pending", "pending"))
        self.assertEqual(
            selection_update.message.replies,
            [build_unremind_selection_stale_response("T1")],
        )
        self.assertNotIn(UNREMIND_SELECTION_CONTEXT_KEY, context.user_data)

    async def test_cancelled_or_claimed_reminder_makes_the_snapshot_stale(self) -> None:
        for state_change in ("cancelled", "processing"):
            with self.subTest(state_change=state_change), tempfile.TemporaryDirectory() as temp_dir:
                config = self.build_config(Path(temp_dir))
                with open_test_database(config.db_path) as conn:
                    apply_migrations(conn)
                    task = self.create_task_for_user(conn, config, 1001, "Email professor")
                    reminders = self.create_reminders(conn, task, 20, 21)

                context = self.build_context()
                await _unremind_handler(config)(self.build_update("/unremind T1"), context)
                with open_test_database(config.db_path) as conn:
                    if state_change == "cancelled":
                        cancel_pending_reminder(
                            conn,
                            user_id=task.user_id,
                            reminder_id=reminders[0].id,
                        )
                    else:
                        claim_due_reminders(
                            conn,
                            now=datetime(2100, 1, 1, tzinfo=timezone.utc),
                            batch_size=1,
                        )

                selection_update = self.build_update("/unremind T1 2")
                await _unremind_handler(config)(selection_update, context)

                with open_test_database(config.db_path) as conn:
                    statuses = self.read_reminder_statuses(conn, reminders)

            self.assertIn(statuses[0], ("cancelled", "processing"))
            self.assertEqual(statuses[1], "pending")
            self.assertEqual(
                selection_update.message.replies,
                [build_unremind_selection_stale_response("T1")],
            )
            self.assertNotIn(UNREMIND_SELECTION_CONTEXT_KEY, context.user_data)

    async def test_atomic_selection_failure_clears_the_snapshot_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminders = self.create_reminders(conn, task, 20, 21)

            context = self.build_context()
            await _unremind_handler(config)(self.build_update("/unremind T1"), context)
            selection_update = self.build_update("/unremind T1 1 2")
            with patch(
                "tele_secretary.telegram.bot.cancel_selected_pending_reminders",
                side_effect=ReminderSelectionError(
                    "reminder_selection_unavailable",
                    "Reminder selection is no longer available.",
                ),
            ):
                await _unremind_handler(config)(selection_update, context)

            with open_test_database(config.db_path) as conn:
                statuses = self.read_reminder_statuses(conn, reminders)

        self.assertEqual(statuses, ("pending", "pending"))
        self.assertEqual(
            selection_update.message.replies,
            [build_unremind_selection_stale_response("T1")],
        )
        self.assertNotIn(UNREMIND_SELECTION_CONTEXT_KEY, context.user_data)

    async def test_two_owners_keep_selection_snapshots_and_cancellations_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(1001, 2002))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                first_task = self.create_task_for_user(conn, config, 1001, "First owner's task")
                second_task = self.create_task_for_user(conn, config, 2002, "Second owner's task")
                first_reminders = self.create_reminders(conn, first_task, 20, 21)
                second_reminders = self.create_reminders(conn, second_task, 22, 23)

            first_context = self.build_context()
            second_context = self.build_context()
            await _unremind_handler(config)(self.build_update("/unremind T1"), first_context)
            await _unremind_handler(config)(
                self.build_update("/unremind T1", telegram_user_id=2002),
                second_context,
            )
            selection_update = self.build_update("/unremind T1 2", telegram_user_id=2002)
            await _unremind_handler(config)(selection_update, second_context)

            with open_test_database(config.db_path) as conn:
                first_statuses = self.read_reminder_statuses(conn, first_reminders)
                second_statuses = self.read_reminder_statuses(conn, second_reminders)

        self.assertEqual(first_statuses, ("pending", "pending"))
        self.assertEqual(second_statuses, ("pending", "cancelled"))
        self.assertIn(UNREMIND_SELECTION_CONTEXT_KEY, first_context.user_data)
        self.assertNotIn(UNREMIND_SELECTION_CONTEXT_KEY, second_context.user_data)

    async def test_corrupted_snapshot_cannot_cancel_another_owner_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(1001, 2002))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                first_task = self.create_task_for_user(conn, config, 1001, "First owner's task")
                second_task = self.create_task_for_user(conn, config, 2002, "Second owner's task")
                first_reminders = self.create_reminders(conn, first_task, 20, 21)
                second_reminders = self.create_reminders(conn, second_task, 22, 23)

            context = self.build_context()
            context.user_data[UNREMIND_SELECTION_CONTEXT_KEY] = UnremindSelectionSnapshot(
                task_id=second_task.id,
                task_ref="T1",
                reminder_ids=tuple(reminder.id for reminder in first_reminders),
            )
            selection_update = self.build_update("/unremind T1 1", telegram_user_id=2002)
            await _unremind_handler(config)(selection_update, context)

            with open_test_database(config.db_path) as conn:
                statuses = self.read_reminder_statuses(
                    conn,
                    (*first_reminders, *second_reminders),
                )

        self.assertEqual(statuses, ("pending", "pending", "pending", "pending"))
        self.assertEqual(
            selection_update.message.replies,
            [build_unremind_selection_stale_response("T1")],
        )
        self.assertNotIn(UNREMIND_SELECTION_CONTEXT_KEY, context.user_data)

    async def test_second_owner_cancels_only_their_own_task_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(1001, 2002))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                first_task = self.create_task_for_user(conn, config, 1001, "First owner's task")
                second_task = self.create_task_for_user(conn, config, 2002, "Second owner's task")
                first_reminder = create_reminder(
                    conn,
                    user_id=first_task.user_id,
                    task_id=first_task.id,
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                )
                second_reminder = create_reminder(
                    conn,
                    user_id=second_task.user_id,
                    task_id=second_task.id,
                    scheduled_at=datetime(2099, 1, 1, 21, 0, tzinfo=timezone.utc),
                )

            update = self.build_update("/unremind T1", telegram_user_id=2002)
            await _unremind_handler(config)(update, self.build_context())

            with open_test_database(config.db_path) as conn:
                statuses_by_id = {
                    row["id"]: row["status"]
                    for row in conn.execute("SELECT id, status FROM reminders").fetchall()
                }

        self.assertEqual(statuses_by_id[first_reminder.id], "pending")
        self.assertEqual(statuses_by_id[second_reminder.id], "cancelled")
        self.assertEqual(
            update.message.replies,
            [build_unremind_cancelled_response(second_task)],
        )

    async def test_missing_or_cross_owner_task_ref_does_not_cancel_a_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir), allowed_user_ids=(1001, 2002))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminder = create_reminder(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                )

            update = self.build_update("/unremind T1", telegram_user_id=2002)
            await _unremind_handler(config)(update, self.build_context())

            with open_test_database(config.db_path) as conn:
                reminder_status = conn.execute(
                    "SELECT status FROM reminders WHERE id = ?",
                    (reminder.id,),
                ).fetchone()["status"]

        self.assertEqual(reminder_status, "pending")
        self.assertEqual(
            update.message.replies,
            ["Task T1 was not found. Use /list to see active task refs."],
        )

    async def test_changed_reminder_state_returns_stale_response_without_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                reminder = create_reminder(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                )

            update = self.build_update("/unremind T1")
            with patch(
                "tele_secretary.telegram.bot.cancel_pending_reminder",
                side_effect=ReminderValidationError(
                    "reminder_not_cancellable",
                    "Only pending reminders can be cancelled.",
                ),
            ):
                await _unremind_handler(config)(update, self.build_context())

            with open_test_database(config.db_path) as conn:
                reminder_status = conn.execute(
                    "SELECT status FROM reminders WHERE id = ?",
                    (reminder.id,),
                ).fetchone()["status"]

        self.assertEqual(reminder_status, "pending")
        self.assertEqual(update.message.replies, [build_unremind_stale_response("T1")])

    async def test_persistence_failure_returns_generic_reply_and_logs_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                task = self.create_task_for_user(conn, config, 1001, "Email professor")
                create_reminder(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    scheduled_at=datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
                )

            update = self.build_update("/unremind T1")
            with (
                patch(
                    "tele_secretary.telegram.bot.cancel_pending_reminder",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ),
                patch("tele_secretary.telegram.bot.LOGGER.exception") as log_exception,
            ):
                await _unremind_handler(config)(update, self.build_context())

        log_exception.assert_called_once_with("Could not cancel reminder for %s", "T1")
        self.assertEqual(
            update.message.replies,
            [build_unremind_persistence_error_response()],
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

    def build_update(self, text: str, *, telegram_user_id: int = 1001) -> SimpleNamespace:
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=telegram_user_id),
            message=FakeMessage(text),
        )

    def build_context(self) -> SimpleNamespace:
        return SimpleNamespace(user_data={})

    def create_task_for_user(
        self,
        conn,
        config: AppConfig,
        telegram_user_id: int,
        title: str,
    ) -> TaskRecord:
        user_id = get_or_create_telegram_user_id(
            conn,
            telegram_user_id=telegram_user_id,
            timezone=config.user_timezone,
        )
        return create_task(
            conn,
            user_id=user_id,
            title=title,
            source="test_fixture",
        )

    def create_reminders(
        self,
        conn,
        task: TaskRecord,
        *hours: int,
    ) -> tuple:
        return tuple(
            create_reminder(
                conn,
                user_id=task.user_id,
                task_id=task.id,
                scheduled_at=datetime(2099, 1, 1, hour, 0, tzinfo=timezone.utc),
            )
            for hour in hours
        )

    def read_reminder_statuses(self, conn, reminders: tuple) -> tuple[str, ...]:
        return tuple(
            conn.execute(
                "SELECT status FROM reminders WHERE id = ?",
                (reminder.id,),
            ).fetchone()["status"]
            for reminder in reminders
        )


class TelegramUnremindRegistrationTests(unittest.TestCase):
    def test_application_registers_unremind_command(self) -> None:
        class FakeCommandHandler:
            def __init__(self, command, callback) -> None:
                self.command = command
                self.callback = callback

        class FakeApplication:
            def __init__(self) -> None:
                self.handlers = []

            @classmethod
            def builder(cls):
                return FakeApplicationBuilder()

            def add_handler(self, handler) -> None:
                self.handlers.append(handler)

        class FakeApplicationBuilder:
            def __init__(self) -> None:
                self.application = FakeApplication()

            def token(self, token):
                return self

            def build(self):
                return self.application

        telegram_module = types.ModuleType("telegram")
        telegram_ext_module = types.ModuleType("telegram.ext")
        telegram_ext_module.Application = FakeApplication
        telegram_ext_module.CommandHandler = FakeCommandHandler
        telegram_module.ext = telegram_ext_module
        with tempfile.TemporaryDirectory() as temp_dir:
            config = TelegramUnremindCommandTests().build_config(Path(temp_dir))
            with patch.dict(
                sys.modules,
                {"telegram": telegram_module, "telegram.ext": telegram_ext_module},
            ):
                application = build_application(config)

        self.assertIn("unremind", [handler.command for handler in application.handlers])


if __name__ == "__main__":
    unittest.main()
