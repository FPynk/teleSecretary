from __future__ import annotations

import unittest

import _path  # noqa: F401
from tele_secretary.telegram.responses import build_help_response, build_ping_response


class TelegramResponseTests(unittest.TestCase):
    def test_ping_response(self) -> None:
        self.assertEqual(build_ping_response(), "pong")

    def test_help_response_lists_phase_zero_commands(self) -> None:
        response = build_help_response()

        self.assertIn("/ping", response)
        self.assertIn("/help", response)
        self.assertIn("Phase 2", response)


if __name__ == "__main__":
    unittest.main()
