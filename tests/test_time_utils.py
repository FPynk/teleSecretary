from __future__ import annotations

import unittest
from datetime import datetime, timezone

import _path  # noqa: F401
from tele_secretary.time_utils import ensure_utc, local_to_utc, utc_to_local


class TimeUtilsTests(unittest.TestCase):
    def test_local_to_utc_uses_ianna_timezone(self) -> None:
        local_time = datetime(2026, 1, 1, 9, 0, 0)

        utc_time = local_to_utc(local_time, "America/Chicago")

        self.assertEqual(utc_time.hour, 15)
        self.assertEqual(utc_time.tzinfo, timezone.utc)

    def test_utc_to_local_round_trips_timezone(self) -> None:
        utc_time = datetime(2026, 1, 1, 15, 0, 0, tzinfo=timezone.utc)

        local_time = utc_to_local(utc_time, "America/Chicago")

        self.assertEqual(local_time.hour, 9)
        self.assertEqual(local_time.tzinfo.key, "America/Chicago")

    def test_naive_storage_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ensure_utc(datetime(2026, 1, 1, 9, 0, 0))


if __name__ == "__main__":
    unittest.main()
