"""
Working-day counter with Malaysian public holidays.

Kept deliberately separate from services.py so that:
  - the calendar policy is one swappable thing (a different region or a
    company-specific holiday list could replace it without touching the
    service layer),
  - tests for service logic can monkey-patch the holiday set easily.

Half-day handling: a leave request carries two boolean flags. If
`start_half_day` is True, the start date counts as 0.5 (provided it's
a working day). Same for `end_half_day`. For single-day leaves
(start == end), both flags collapse to one — setting either yields 0.5.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import holidays


@lru_cache(maxsize=8)
def _holiday_set(year: int) -> set[date]:
    # `holidays.MY()` returns federal MY public holidays. State-specific
    # holidays (e.g. Sultan's birthday differs per state) are deliberately
    # out of scope — see DESIGN.md §4.
    return set(holidays.MY(years=year).keys())


def is_working_day(d: date) -> bool:
    """Mon–Fri and not a Malaysian federal public holiday."""
    if d.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    if d in _holiday_set(d.year):
        return False
    return True


def working_days_between(
    start: date,
    end: date,
    *,
    start_half_day: bool = False,
    end_half_day: bool = False,
) -> float:
    """Count working days in the inclusive range [start, end].

    Half-day flags subtract 0.5 each, only if the corresponding boundary
    day is a working day (no point discounting a Saturday). For the
    single-day case (start == end) both flags refer to the same day, and
    we apply at most one 0.5 deduction.
    """
    if start > end:
        raise ValueError("start must be on or before end")

    days = 0.0
    cursor = start
    while cursor <= end:
        if is_working_day(cursor):
            days += 1.0
        cursor += timedelta(days=1)

    if days == 0:
        return 0.0

    if start == end:
        if start_half_day or end_half_day:
            days -= 0.5
        return days

    if start_half_day and is_working_day(start):
        days -= 0.5
    if end_half_day and is_working_day(end):
        days -= 0.5
    return days
