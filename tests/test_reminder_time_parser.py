from __future__ import annotations

import unittest
from datetime import datetime, timezone

import _path  # noqa: F401
from tele_secretary.app.reminder_time_parser import (
    InvalidReminderTimeError,
    PastReminderTimeError,
    ReminderTimeWarning,
    parse_reminder_time_expression,
)


CHICAGO = "America/Chicago"
JULY_REFERENCE_TIME = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


class ReminderTimeParserTests(unittest.TestCase):
    def parse(self, expression: str, *, now: datetime = JULY_REFERENCE_TIME):
        return parse_reminder_time_expression(expression, CHICAGO, now=now)

    def assert_scheduled_at(self, expression: str, expected: datetime) -> None:
        parsed_time = self.parse(expression)

        self.assertEqual(parsed_time.scheduled_at, expected)
        self.assertEqual(parsed_time.scheduled_at.tzinfo, timezone.utc)
        self.assertEqual(parsed_time.warning, None)

    def test_supported_expression_forms_resolve_to_utc(self) -> None:
        cases = {
            "tomorrow": datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc),
            "tomorrow 2pm": datetime(2026, 7, 21, 19, 0, tzinfo=timezone.utc),
            "tomorrow 2:30 PM": datetime(2026, 7, 21, 19, 30, tzinfo=timezone.utc),
            "friday": datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc),
            "fri 14:30": datetime(2026, 7, 24, 19, 30, tzinfo=timezone.utc),
            "25/07/2026": datetime(2026, 7, 25, 14, 0, tzinfo=timezone.utc),
            "25/07/2026 2:30 PM": datetime(2026, 7, 25, 19, 30, tzinfo=timezone.utc),
            "25/07/2026 14:30": datetime(2026, 7, 25, 19, 30, tzinfo=timezone.utc),
        }

        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assert_scheduled_at(expression, expected)

    def test_matching_is_case_insensitive_and_whitespace_is_normalized(self) -> None:
        self.assert_scheduled_at("  ToMoRrOw   2:30 pM  ", datetime(2026, 7, 21, 19, 30, tzinfo=timezone.utc))
        self.assert_scheduled_at(" MON ", datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc))

    def test_all_weekday_aliases_are_supported(self) -> None:
        expected_dates = {
            "mon": 27,
            "tue": 21,
            "wed": 22,
            "thu": 23,
            "fri": 24,
            "sat": 25,
            "sun": 26,
        }
        for weekday, expected_day in expected_dates.items():
            with self.subTest(weekday=weekday):
                self.assert_scheduled_at(
                    weekday,
                    datetime(2026, 7, expected_day, 14, 0, tzinfo=timezone.utc),
                )

    def test_twelve_hour_midnight_and_noon_are_resolved_correctly(self) -> None:
        self.assert_scheduled_at("tomorrow 12am", datetime(2026, 7, 21, 5, 0, tzinfo=timezone.utc))
        self.assert_scheduled_at("tomorrow 12 pm", datetime(2026, 7, 21, 17, 0, tzinfo=timezone.utc))

    def test_same_weekday_rolls_over_only_when_the_selected_time_has_passed(self) -> None:
        before_nine_am = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        at_nine_am = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
        before_two_pm = datetime(2026, 7, 24, 18, 59, tzinfo=timezone.utc)
        at_two_pm = datetime(2026, 7, 24, 19, 0, tzinfo=timezone.utc)

        self.assertEqual(self.parse("friday", now=before_nine_am).scheduled_at, datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(self.parse("friday", now=at_nine_am).scheduled_at, datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(self.parse("fri 14:00", now=before_two_pm).scheduled_at, datetime(2026, 7, 24, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(self.parse("friday 2pm", now=at_two_pm).scheduled_at, datetime(2026, 7, 31, 19, 0, tzinfo=timezone.utc))

    def test_calendar_boundaries_use_local_calendar_arithmetic(self) -> None:
        january_reference = datetime(2026, 1, 31, 18, 0, tzinfo=timezone.utc)
        december_reference = datetime(2026, 12, 31, 18, 0, tzinfo=timezone.utc)

        self.assertEqual(self.parse("tomorrow", now=january_reference).scheduled_at, datetime(2026, 2, 1, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(self.parse("tomorrow", now=december_reference).scheduled_at, datetime(2027, 1, 1, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(self.parse("29/02/2028").scheduled_at, datetime(2028, 2, 29, 15, 0, tzinfo=timezone.utc))

    def test_nonexistent_times_move_to_the_first_valid_minute(self) -> None:
        reference_time = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

        for expression in ("08/03/2026 02:30", "08/03/2026 2:30 am"):
            with self.subTest(expression=expression):
                parsed_time = self.parse(expression, now=reference_time)
                self.assertEqual(parsed_time.scheduled_at, datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc))
                self.assertEqual(parsed_time.warning, ReminderTimeWarning.NONEXISTENT_TIME_MOVED_FORWARD)

    def test_ambiguous_times_select_the_first_utc_occurrence(self) -> None:
        reference_time = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

        for expression in ("01/11/2026 01:30", "01/11/2026 1:30 am"):
            with self.subTest(expression=expression):
                parsed_time = self.parse(expression, now=reference_time)
                self.assertEqual(parsed_time.scheduled_at, datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
                self.assertEqual(parsed_time.warning, ReminderTimeWarning.AMBIGUOUS_TIME_FIRST_OCCURRENCE)

    def test_absolute_ambiguous_time_is_past_when_its_first_occurrence_has_passed(self) -> None:
        reference_time = datetime(2026, 11, 1, 6, 45, tzinfo=timezone.utc)

        with self.assertRaises(PastReminderTimeError):
            self.parse("01/11/2026 01:30", now=reference_time)

    def test_same_day_ambiguous_weekday_rolls_to_the_following_week(self) -> None:
        reference_time = datetime(2026, 11, 1, 6, 45, tzinfo=timezone.utc)

        parsed_time = self.parse("sunday 1:30am", now=reference_time)

        self.assertEqual(parsed_time.scheduled_at, datetime(2026, 11, 8, 7, 30, tzinfo=timezone.utc))
        self.assertEqual(parsed_time.warning, None)

    def test_tomorrow_across_a_dst_boundary_preserves_nine_am_local_time(self) -> None:
        reference_time = datetime(2026, 3, 7, 18, 0, tzinfo=timezone.utc)

        parsed_time = self.parse("tomorrow", now=reference_time)

        self.assertEqual(parsed_time.scheduled_at, datetime(2026, 3, 8, 14, 0, tzinfo=timezone.utc))

    def test_invalid_expressions_return_actionable_errors(self) -> None:
        invalid_expressions = (
            "",
            "   ",
            "today",
            "next friday",
            "tomorrow 2",
            "31/02/2026",
            "29/02/2026",
            "25/07/2026 24:00",
            "25/07/2026 14:60",
            "tomorrow 0pm",
            "tomorrow 13pm",
        )

        for expression in invalid_expressions:
            with self.subTest(expression=expression):
                with self.assertRaisesRegex(InvalidReminderTimeError, "Use tomorrow"):
                    self.parse(expression)

    def test_absolute_past_and_equal_times_return_actionable_errors(self) -> None:
        for expression in ("20/07/2026 09:59", "20/07/2026 10:00"):
            with self.subTest(expression=expression):
                with self.assertRaisesRegex(PastReminderTimeError, "already passed"):
                    self.parse(expression)

    def test_naive_injected_clock_is_a_programmer_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_reminder_time_expression("tomorrow", CHICAGO, now=datetime(2026, 7, 20, 10, 0))


if __name__ == "__main__":
    unittest.main()
