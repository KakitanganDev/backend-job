"""Unit tests for the working-day calendar."""

from datetime import date

import pytest

from src.calendar import is_working_day, working_days_between


def test_is_working_day_weekday():
    # Wed 2026-05-06
    assert is_working_day(date(2026, 5, 6)) is True


def test_is_working_day_weekend():
    # Sat 2026-05-09
    assert is_working_day(date(2026, 5, 9)) is False


def test_is_working_day_my_public_holiday():
    # Labour Day 2026-05-01 (Friday) — federal MY public holiday.
    assert is_working_day(date(2026, 5, 1)) is False


def test_working_days_full_week():
    # Mon 2026-05-04 to Fri 2026-05-08 — 5 working days, no holidays.
    assert working_days_between(date(2026, 5, 4), date(2026, 5, 8)) == 5.0


def test_working_days_includes_weekend():
    # Mon 2026-05-04 to Sun 2026-05-10 — still 5 working days.
    assert working_days_between(date(2026, 5, 4), date(2026, 5, 10)) == 5.0


def test_working_days_skips_my_holiday():
    # Mon 2026-04-27 to Fri 2026-05-01.
    # Mon-Thu = 4 working days. Fri 2026-05-01 = Labour Day, not counted.
    assert working_days_between(date(2026, 4, 27), date(2026, 5, 1)) == 4.0


def test_working_days_single_day_half():
    d = date(2026, 5, 6)  # Wed
    assert working_days_between(d, d, start_half_day=True) == 0.5
    assert working_days_between(d, d, end_half_day=True) == 0.5
    # Both flags on a single day collapse to one 0.5 deduction.
    assert working_days_between(d, d, start_half_day=True, end_half_day=True) == 0.5


def test_working_days_half_day_at_each_boundary():
    # Mon-Fri = 5; -0.5 -0.5 = 4
    assert working_days_between(
        date(2026, 5, 4), date(2026, 5, 8),
        start_half_day=True, end_half_day=True,
    ) == 4.0


def test_working_days_half_on_weekend_ignored():
    # Range Fri-Sat: Fri counts 1, Sat 0. Marking end_half on Sat must not subtract.
    assert working_days_between(
        date(2026, 5, 8), date(2026, 5, 9),
        end_half_day=True,
    ) == 1.0


def test_working_days_inverted_raises():
    with pytest.raises(ValueError):
        working_days_between(date(2026, 5, 6), date(2026, 5, 4))
