from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _path  # noqa: F401
from db_helpers import open_test_database
from tele_secretary.app.tasks import get_task_details_by_ref, list_active_tasks
from tele_secretary.app.users import (
    LEGACY_SINGLE_OWNER_USER_ID,
    get_or_create_telegram_user,
)
from tele_secretary.config import AppConfig
from tele_secretary.persistence.migrations import apply_migrations
from tele_secretary.telegram.bot import (
    _addtask_handler,
    _done_handler,
    _edit_handler,
    _help_handler,
    _list_handler,
    _ping_handler,
    _remind_handler,
    _reopen_handler,
    _show_handler,
    _today_handler,
    run_bot,
)


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class FakeApplication:
    def __init__(self) -> None:
        self.did_run_polling = False

    def run_polling(self) -> None:
        self.did_run_polling = True


class TelegramMultiUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_and_unauthorized_commands_do_not_create_user_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            ping_update = self.build_update(1001, "/ping")
            help_update = self.build_update(2002, "/help")
            edit_help_update = self.build_update(1001, "/edit -help")
            unauthorized_update = self.build_update(3003, "/list")
            missing_user_update = SimpleNamespace(
                effective_user=None,
                message=FakeMessage("/list"),
            )

            await _ping_handler(config)(ping_update, SimpleNamespace())
            await _help_handler(config)(help_update, SimpleNamespace())
            await _edit_handler(config)(edit_help_update, SimpleNamespace())
            await _list_handler(config)(unauthorized_update, SimpleNamespace())
            await _list_handler(config)(missing_user_update, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

        self.assertEqual(user_count, 0)
        self.assertEqual(ping_update.message.replies, ["pong"])
        self.assertEqual(unauthorized_update.message.replies, [
            "This Telegram account is not authorized to use TeleSecretary."
        ])
        self.assertEqual(missing_user_update.message.replies, [
            "This Telegram account is not authorized to use TeleSecretary."
        ])

    async def test_two_users_keep_commands_references_and_reminders_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            first_add = self.build_update(1001, "/addtask First private task")
            second_add = self.build_update(2002, "/addtask Second private task")
            await _addtask_handler(config)(first_add, SimpleNamespace())
            await _addtask_handler(config)(second_add, SimpleNamespace())

            first_show = self.build_update(1001, "/show T1")
            second_show = self.build_update(2002, "/show T1")
            second_list = self.build_update(2002, "/list")
            await _show_handler(config)(first_show, SimpleNamespace())
            await _show_handler(config)(second_show, SimpleNamespace())
            await _list_handler(config)(second_list, SimpleNamespace())

            second_edit = self.build_update(2002, "/edit T1 -title 'Updated second task'")
            second_done = self.build_update(2002, "/done T1")
            second_reopen = self.build_update(2002, "/reopen T1")
            await _edit_handler(config)(second_edit, SimpleNamespace())
            await _done_handler(config)(second_done, SimpleNamespace())
            await _reopen_handler(config)(second_reopen, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                first_user = get_or_create_telegram_user(
                    conn,
                    telegram_user_id=1001,
                    default_timezone=config.user_timezone,
                )
                second_user = get_or_create_telegram_user(
                    conn,
                    telegram_user_id=2002,
                    default_timezone=config.user_timezone,
                )
                conn.execute(
                    "UPDATE users SET timezone = 'Asia/Singapore' WHERE id = ?",
                    (second_user.user_id,),
                )
                conn.commit()
                first_task = get_task_details_by_ref(
                    conn,
                    user_id=first_user.user_id,
                    task_ref="T1",
                )
                second_task = get_task_details_by_ref(
                    conn,
                    user_id=second_user.user_id,
                    task_ref="T1",
                )

            second_remind = self.build_update(2002, "/remind T1 01/01/2099 2pm")
            await _remind_handler(config)(second_remind, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                reminder = conn.execute(
                    """
                    SELECT reminders.item_id, reminders.scheduled_at, items.user_id
                    FROM reminders
                    JOIN items ON items.id = reminders.item_id
                    """
                ).fetchone()

        self.assertIn("First private task", first_add.message.replies[0])
        self.assertIn("Second private task", second_add.message.replies[0])
        self.assertIn("First private task", first_show.message.replies[0])
        self.assertNotIn("Second private task", first_show.message.replies[0])
        self.assertIn("Second private task", second_show.message.replies[0])
        self.assertNotIn("First private task", second_list.message.replies[0])
        self.assertEqual(first_task.title, "First private task")
        self.assertEqual(first_task.status, "active")
        self.assertEqual(second_task.title, "Updated second task")
        self.assertEqual(second_task.status, "active")
        self.assertEqual((reminder["item_id"], reminder["user_id"]), (second_task.id, second_user.user_id))
        self.assertEqual(reminder["scheduled_at"], "2099-01-01T06:00:00+00:00")
        self.assertIn("at 2:00 PM.", second_remind.message.replies[0])

    async def test_timezone_dependent_add_edit_show_and_today_use_the_persisted_user_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                first_user = get_or_create_telegram_user(
                    conn,
                    telegram_user_id=1001,
                    default_timezone="America/Chicago",
                )
                second_user = get_or_create_telegram_user(
                    conn,
                    telegram_user_id=2002,
                    default_timezone="Asia/Singapore",
                )

            first_add = self.build_update(1001, "/addtask Chicago due -due 12/07/2099")
            second_add = self.build_update(2002, "/addtask Singapore due -due 12/07/2099")
            await _addtask_handler(config)(first_add, SimpleNamespace())
            await _addtask_handler(config)(second_add, SimpleNamespace())

            second_edit = self.build_update(
                2002,
                "/edit T1 -deadline '13/07/2099 20:00' -deadline-type hard -urgency high",
            )
            await _edit_handler(config)(second_edit, SimpleNamespace())
            second_show = self.build_update(2002, "/show T1")
            await _show_handler(config)(second_show, SimpleNamespace())

            await _addtask_handler(config)(
                self.build_update(2002, "/addtask Singapore focus"),
                SimpleNamespace(),
            )
            await _edit_handler(config)(
                self.build_update(2002, "/edit T2 -urgency high"),
                SimpleNamespace(),
            )
            second_today = self.build_update(2002, "/today")
            await _today_handler(config)(second_today, SimpleNamespace())

            with open_test_database(config.db_path) as conn:
                first_task = get_task_details_by_ref(
                    conn,
                    user_id=first_user.user_id,
                    task_ref="T1",
                )
                second_task = get_task_details_by_ref(
                    conn,
                    user_id=second_user.user_id,
                    task_ref="T1",
                )

        self.assertEqual(first_task.deadline_at, "2099-07-13T04:59:00+00:00")
        self.assertEqual(second_task.deadline_at, "2099-07-13T12:00:00+00:00")
        self.assertIn("at 8:00 PM (hard)", second_show.message.replies[0])
        self.assertIn("Singapore focus", second_today.message.replies[0])

    async def test_cross_owner_missing_reference_is_not_disclosed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)

            await _addtask_handler(config)(
                self.build_update(1001, "/addtask First T1"),
                SimpleNamespace(),
            )
            await _addtask_handler(config)(
                self.build_update(1001, "/addtask Private T2"),
                SimpleNamespace(),
            )
            cross_owner_update = self.build_update(2002, "/show T2")
            await _show_handler(config)(cross_owner_update, SimpleNamespace())

        self.assertEqual(
            cross_owner_update.message.replies,
            ["Task T2 was not found. Use /list to see active task refs."],
        )

    async def test_run_bot_binds_an_unassigned_legacy_owner_before_polling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.build_config(Path(temp_dir))
            with open_test_database(config.db_path) as conn:
                apply_migrations(conn)
                conn.execute(
                    """
                    INSERT INTO users (id, telegram_user_id, timezone)
                    VALUES (?, NULL, 'America/Chicago')
                    """,
                    (LEGACY_SINGLE_OWNER_USER_ID,),
                )
                conn.commit()

            application = FakeApplication()
            with patch("tele_secretary.telegram.bot.build_application", return_value=application):
                run_bot(config)

            with open_test_database(config.db_path) as conn:
                telegram_user_id = conn.execute(
                    "SELECT telegram_user_id FROM users WHERE id = ?",
                    (LEGACY_SINGLE_OWNER_USER_ID,),
                ).fetchone()[0]

        self.assertTrue(application.did_run_polling)
        self.assertEqual(telegram_user_id, 1001)

    def build_config(self, root: Path) -> AppConfig:
        return AppConfig(
            telegram_bot_token="test-token",
            telegram_allowed_user_ids=(1001, 2002),
            data_dir=root / "data",
            log_dir=root / "logs",
            db_path=root / "data" / "secretary.sqlite3",
            user_timezone="America/Chicago",
            log_level="INFO",
        )

    def build_update(self, telegram_user_id: int, text: str) -> SimpleNamespace:
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=telegram_user_id),
            message=FakeMessage(text),
        )


if __name__ == "__main__":
    unittest.main()
