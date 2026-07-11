from __future__ import annotations

from contextlib import redirect_stderr
import io
import logging
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from tele_secretary.logging_config import configure_logging, shutdown_logging


class LoggingConfigTests(unittest.TestCase):
    def test_telegram_bot_token_in_url_is_redacted_from_file_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            configure_logging(log_dir, "INFO")

            logging.getLogger("test").info(
                "HTTP Request: POST "
                "https://api.telegram.org/bot123456:SECRET/getUpdates"
            )

            shutdown_logging()
            log_text = (log_dir / "secretary.log").read_text(encoding="utf-8")

        self.assertIn("https://api.telegram.org/bot<redacted>/getUpdates", log_text)
        self.assertNotIn("123456:SECRET", log_text)

    def test_http_dependency_request_logs_are_not_emitted_at_info(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            configure_logging(log_dir, "INFO")

            logging.getLogger("httpx").info(
                "HTTP Request: POST "
                "https://api.telegram.org/bot123456:SECRET/getUpdates"
            )

            shutdown_logging()
            log_text = (log_dir / "secretary.log").read_text(encoding="utf-8")

        self.assertEqual(log_text, "")

    def test_shutdown_detaches_temporary_file_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            configure_logging(log_dir, "INFO")
            temporary_log_path = log_dir / "secretary.log"

            shutdown_logging()

            self.assertTrue(temporary_log_path.exists())
            self.assertEqual(logging.getLogger().handlers, [])

        with redirect_stderr(io.StringIO()):
            logging.getLogger("asyncio").warning(
                "A later warning must not reopen the deleted temporary log."
            )


if __name__ == "__main__":
    unittest.main()
