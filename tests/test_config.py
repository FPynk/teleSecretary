from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from tele_secretary.config import AppConfig, ConfigError, load_env_file


class ConfigTests(unittest.TestCase):
    def test_config_loads_defaults_and_allowed_users(self) -> None:
        config = AppConfig.from_env(
            {
                "TELEGRAM_ALLOWED_USER_IDS": "123, 456",
                "SECRETARY_DATA_DIR": "./tmp-data",
                "SECRETARY_LOG_DIR": "./tmp-logs",
                "SECRETARY_USER_TIMEZONE": "America/Chicago",
            }
        )

        self.assertEqual(config.telegram_allowed_user_ids, (123, 456))
        self.assertEqual(config.db_path, Path("tmp-data") / "secretary.sqlite3")
        self.assertEqual(config.log_level, "INFO")

    def test_bot_token_can_be_required(self) -> None:
        with self.assertRaises(ConfigError):
            AppConfig.from_env({}, require_bot_token=True)

    def test_invalid_timezone_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            AppConfig.from_env({"SECRETARY_USER_TIMEZONE": "CDT"})

    def test_env_file_parser_skips_comments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text(
                "# comment\nSECRETARY_LOG_LEVEL=debug\nEMPTY=\n",
                encoding="utf-8",
            )

            values = load_env_file(path)

        self.assertEqual(values["SECRETARY_LOG_LEVEL"], "debug")
        self.assertEqual(values["EMPTY"], "")


if __name__ == "__main__":
    unittest.main()
