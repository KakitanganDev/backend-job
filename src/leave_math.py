"""
Pure helper functions for leave day counting.

These functions have no database dependencies and are testable in isolation.
"""

from datetime import date, timedelta
from typing import Set


def count_working_days(
    start: date,
    end: date,
    holidays: Set[date],
    half_day_start: bool = False,
    half_day_end: bool = False,
) -> float:
    """
    Count working days (Mon–Fri, excluding holidays) between start and end (inclusive).

    half_day_start: the first day counts as 0.5 instead of 1.
    half_day_end:   the last day counts as 0.5 instead of 1.
    """
    if end < start:
        return 0.0

    total = 0.0
    current = start
    while current <= end:
        is_weekend = current.weekday() >= 5  # Saturday=5, Sunday=6
        is_holiday = current in holidays
        if not is_weekend and not is_holiday:
            weight = 1.0
            if current == start and half_day_start:
                weight = 0.5
            if current == end and half_day_end:
                # If start==end, both flags can apply → 0.5
                weight = 0.5
            total += weight
        current += timedelta(days=1)
    return total


def partition_by_year(
    start: date,
    end: date,
    half_day_start: bool,
    half_day_end: bool,
    holidays: Set[date],
) -> dict[int, float]:
    """
    Split a date range across calendar years and count working days per year.

    Returns a dict {year: working_days_float}.  Years with 0 days are omitted.
    """
    if end < start:
        return {}

    years = range(start.year, end.year + 1)
    result: dict[int, float] = {}

    for yr in years:
        yr_start = max(start, date(yr, 1, 1))
        yr_end = min(end, date(yr, 12, 31))

        # Half-day flags apply to the global start/end, not per-year boundaries
        hs = half_day_start and yr_start == start
        he = half_day_end and yr_end == end

        days = count_working_days(yr_start, yr_end, holidays, hs, he)
        if days > 0:
            result[yr] = days

    return result
