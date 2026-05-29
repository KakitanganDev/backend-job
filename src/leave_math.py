"""
Pure date math functions for leave management.

No database or model dependencies — pure Python only.
"""

from datetime import date, timedelta


def count_working_days(
    start: date,
    end: date,
    half_day_start: bool,
    half_day_end: bool,
    holidays: set[date],
) -> float:
    """Count the total number of working days in the range [start, end].

    Working day = Monday–Friday, not in holidays.

    Each working day contributes:
    - start date: 0.5 if half_day_start=True, else 1.0 (0.0 if weekend/holiday)
    - end date (when end != start): 0.5 if half_day_end=True, else 1.0 (0.0 if weekend/holiday)
    - middle days: always 1.0 (0.0 if weekend/holiday)
    - if start == end: 0.5 if half_day_start=True, else 1.0 (0.0 if weekend/holiday);
      half_day_end is ignored.

    Returns the total float sum.
    """
    total = 0.0
    current = start

    while current <= end:
        is_working = current.weekday() < 5 and current not in holidays

        if not is_working:
            contribution = 0.0
        elif start == end:
            contribution = 0.5 if half_day_start else 1.0
        elif current == start:
            contribution = 0.5 if half_day_start else 1.0
        elif current == end:
            contribution = 0.5 if half_day_end else 1.0
        else:
            contribution = 1.0

        total += contribution
        current += timedelta(days=1)

    return total


def partition_by_year(
    start: date,
    end: date,
    half_day_start: bool,
    half_day_end: bool,
    holidays: set[date],
) -> dict[int, float]:
    """Partition working days across calendar years in the range [start, end].

    Returns a dict mapping year → days for each calendar year in the range.
    Only includes years where days > 0.

    Applies half_day_start to the first day of the range and half_day_end to
    the last day of the range, regardless of which year those days fall in.
    """
    result: dict[int, float] = {}
    current = start

    while current <= end:
        is_working = current.weekday() < 5 and current not in holidays

        if not is_working:
            contribution = 0.0
        elif start == end:
            contribution = 0.5 if half_day_start else 1.0
        elif current == start:
            contribution = 0.5 if half_day_start else 1.0
        elif current == end:
            contribution = 0.5 if half_day_end else 1.0
        else:
            contribution = 1.0

        if contribution > 0.0:
            result[current.year] = result.get(current.year, 0.0) + contribution

        current += timedelta(days=1)

    return result
