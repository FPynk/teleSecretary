from __future__ import annotations

import unittest

import _path  # noqa: F401
from tele_secretary.telegram.responses import build_help_response, build_ping_response


class TelegramResponseTests(unittest.TestCase):
    def test_ping_response(self) -> None:
        self.assertEqual(build_ping_response(), "pong")

    def test_help_response_lists_available_commands(self) -> None:
        response = build_help_response()

        self.assertIn("/ping", response)
        self.assertIn("/reopen T<number>", response)
        self.assertIn("/list", response)
        self.assertIn("/addtask", response)
        self.assertIn("/show", response)
        self.assertIn("/edit", response)
        self.assertIn("/help", response)
        self.assertIn("Phase 2", response)
        self.assertIn("/help edit", response)

    def test_edit_help_is_verbose_and_fits_one_telegram_message(self) -> None:
        response = build_help_response("edit")

        expected_flags = (
            "-title",
            "-description",
            "-category",
            "-deadline",
            "-deadline-type",
            "-planned-start",
            "-planned-end",
            "-estimate",
            "-urgency",
            "-add-tag",
            "-remove-tag",
            "-clear-description",
            "-clear-category",
            "-clear-deadline",
            "-clear-planned-start",
            "-clear-planned-end",
            "-clear-planned-window",
            "-clear-estimate",
            "-clear-urgency",
            "-clear-tags",
        )
        for flag in expected_flags:
            with self.subTest(flag=flag):
                self.assertIn(flag, response)
        self.assertIn("curly", response)
        self.assertIn("No partial edits are saved", response)
        self.assertLessEqual(len(response), 4096)

    def test_unknown_and_multiple_help_topics_return_guidance(self) -> None:
        unknown_response = build_help_response("done")
        multiple_response = build_help_response("edit extra")

        self.assertIn("Unknown help topic: done", unknown_response)
        self.assertIn("Available topics: edit", unknown_response)
        self.assertIn("Use one help topic at a time", multiple_response)


if __name__ == "__main__":
    unittest.main()
