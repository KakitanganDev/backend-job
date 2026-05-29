"""
Tests for the pure date math module: src/leave_math.py

These tests cover:
- Weekday-only range (no weekends, no holidays)
- Weekend exclusion
- Public holiday exclusion
- Half-day on start
- Half-day on end
- Half-day on single-day request
- Half-day on weekend/holiday → 0.0
- All-weekend range → 0.0
- Year-spanning range
- Year-spanning with half-days
- Year-spanning with holidays crossing year boundary
- Single day, full
- Range with multiple holidays
- Large range
"""

import pytest
from datetime import date

from src.leave_math import count_working_days, partition_by_year


# --- count_working_days ---

def test_weekday_only_range():
    """Mon 2026-06-01 to Wed 2026-06-03: 3 full working days."""
    # 2026-06-01=Mon, 2026-06-02=Tue, 2026-06-03=Wed
    result = count_working_days(date(2026, 6, 1), date(2026, 6, 3), False, False, set())
    assert result == 3.0


def test_weekend_exclusion():
    """Fri 2026-06-05 to Mon 2026-06-08: only Fri + Mon = 2 days."""
    # 2026-06-05=Fri, 06=Sat, 07=Sun, 08=Mon
    result = count_working_days(date(2026, 6, 5), date(2026, 6, 8), False, False, set())
    assert result == 2.0


def test_public_holiday_exclusion():
    """Mon-Wed but Tuesday is a holiday → 2 days."""
    result = count_working_days(
        date(2026, 6, 1), date(2026, 6, 3),
        False, False,
        {date(2026, 6, 2)}
    )
    assert result == 2.0


def test_half_day_on_start():
    """Mon-Wed, half_day_start=True → 0.5 + 1 + 1 = 2.5."""
    result = count_working_days(date(2026, 6, 1), date(2026, 6, 3), True, False, set())
    assert result == 2.5


def test_half_day_on_end():
    """Mon-Wed, half_day_end=True → 1 + 1 + 0.5 = 2.5."""
    result = count_working_days(date(2026, 6, 1), date(2026, 6, 3), False, True, set())
    assert result == 2.5


def test_half_day_both_start_and_end():
    """Mon-Wed, both half days → 0.5 + 1 + 0.5 = 2.0."""
    result = count_working_days(date(2026, 6, 1), date(2026, 6, 3), True, True, set())
    assert result == 2.0


def test_single_day_full():
    """Single working day, not half → 1.0."""
    result = count_working_days(date(2026, 6, 1), date(2026, 6, 1), False, False, set())
    assert result == 1.0


def test_single_day_half():
    """Single working day, half_day_start=True → 0.5 (half_day_end ignored)."""
    result = count_working_days(date(2026, 6, 1), date(2026, 6, 1), True, True, set())
    assert result == 0.5


def test_half_day_on_weekend_returns_zero():
    """half_day_start on a Saturday → 0.0."""
    # 2026-06-06 is a Saturday
    result = count_working_days(date(2026, 6, 6), date(2026, 6, 6), True, False, set())
    assert result == 0.0


def test_half_day_on_holiday_returns_zero():
    """half_day_start on a holiday → 0.0."""
    result = count_working_days(
        date(2026, 6, 1), date(2026, 6, 1),
        True, False,
        {date(2026, 6, 1)}
    )
    assert result == 0.0


def test_all_weekend_range():
    """Sat 2026-06-06 to Sun 2026-06-07 → 0.0."""
    result = count_working_days(date(2026, 6, 6), date(2026, 6, 7), False, False, set())
    assert result == 0.0


def test_multiple_holidays():
    """Mon-Fri with Mon and Fri as holidays → Wed, Thu, Fri still counted for middle days.
    Mon(holiday)=0, Tue=1, Wed=1, Thu=1, Fri(holiday)=0 → 3.0."""
    result = count_working_days(
        date(2026, 6, 1), date(2026, 6, 5),
        False, False,
        {date(2026, 6, 1), date(2026, 6, 5)}
    )
    assert result == 3.0


def test_large_range():
    """Jan 2026: 2026-01-01=Thu, 2026-01-31=Sat.
    Working days: 1,2(Thu,Fri), 5-9, 12-16, 19-23, 26-30 = 22 working days."""
    result = count_working_days(date(2026, 1, 1), date(2026, 1, 31), False, False, set())
    assert result == 22.0


def test_single_day_weekend():
    """Single Saturday → 0.0."""
    # 2026-06-06 is Saturday
    result = count_working_days(date(2026, 6, 6), date(2026, 6, 6), False, False, set())
    assert result == 0.0


def test_two_day_range_half_days():
    """Mon-Tue with half_day_start and half_day_end → 0.5 + 0.5 = 1.0."""
    result = count_working_days(date(2026, 6, 1), date(2026, 6, 2), True, True, set())
    assert result == 1.0


# --- partition_by_year ---

def test_partition_single_year():
    """Range entirely within one year."""
    result = partition_by_year(date(2026, 6, 1), date(2026, 6, 3), False, False, set())
    assert result == {2026: 3.0}


def test_partition_year_spanning():
    """2026-12-29 (Tue) to 2027-01-05 (Tue), holiday on 2027-01-01 (Fri).
    2026: Dec 29 Tue=1, Dec 30 Wed=1, Dec 31 Thu=1 → 3.0
    2027: Jan 1 Fri(holiday)=0, Jan 2 Sat=0, Jan 3 Sun=0, Jan 4 Mon=1, Jan 5 Tue=1 → 2.0
    """
    result = partition_by_year(
        date(2026, 12, 29), date(2027, 1, 5),
        False, False,
        {date(2027, 1, 1)}
    )
    assert result == {2026: 3.0, 2027: 2.0}


def test_partition_year_spanning_with_half_days():
    """Year spanning with half_day_start on first day of range.
    Dec 29 Tue (half)=0.5, Dec 30 Wed=1, Dec 31 Thu=1 → 2.5 in 2026.
    """
    result = partition_by_year(
        date(2026, 12, 29), date(2026, 12, 31),
        True, False,
        set()
    )
    assert result == {2026: 2.5}


def test_partition_excludes_zero_years():
    """Years with 0 working days should not appear in result."""
    result = partition_by_year(date(2026, 6, 1), date(2026, 6, 1), False, False, set())
    assert 2025 not in result
    assert 2027 not in result
    assert result == {2026: 1.0}


def test_partition_holiday_crossing_year_boundary():
    """Holiday on Dec 31 (Thu) 2026 within a year-spanning range.
    Dec 30 Wed=1, Dec 31 Thu(holiday)=0 → 2026: 1.0
    Jan 1 Fri=1, Jan 2 Sat=0 → 2027: 1.0
    """
    result = partition_by_year(
        date(2026, 12, 30), date(2027, 1, 2),
        False, False,
        {date(2026, 12, 31)}
    )
    assert result == {2026: 1.0, 2027: 1.0}


def test_partition_half_day_start_crosses_year():
    """half_day_start on Dec 31 (Thu, last working day of 2026), full days in 2027.
    Dec 31 Thu (half_day_start)=0.5 → 2026: 0.5
    Jan 1 Fri=1, Jan 4 Mon=1, Jan 5 Tue=1 (end, half_day_end=False)
    But Jan 2 Sat=0, Jan 3 Sun=0
    → 2027: 3.0 (Jan 1 Fri, Jan 4 Mon, Jan 5 Tue)
    """
    result = partition_by_year(
        date(2026, 12, 31), date(2027, 1, 5),
        True, False,
        set()
    )
    assert result == {2026: 0.5, 2027: 3.0}
