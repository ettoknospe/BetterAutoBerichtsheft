"""Pure date-range logic for holiday/school-year gap detection.

No network, no filesystem - takes WebUntis-derived lists as plain args.
"""

import datetime as dt


def _is_full_holiday_week(monday: dt.date, sunday: dt.date, holiday_periods: list) -> bool:
    """True if every Mon-Fri date of the week falls inside some holiday period."""
    weekdays = [monday + dt.timedelta(days=i) for i in range(5)]
    ranges = []
    for h in holiday_periods:
        start = dt.datetime.strptime(str(h["startDate"]), "%Y%m%d").date()
        end = dt.datetime.strptime(str(h["endDate"]), "%Y%m%d").date()
        ranges.append((start, end))
    return all(any(start <= d <= end for start, end in ranges) for d in weekdays)


def _is_between_school_years(monday: dt.date, sunday: dt.date, school_years: list) -> bool:
    """True if every Mon-Fri date of the week falls in the gap between two
    consecutive school years — i.e. after one ends and before the next
    starts. No calendar dates hardcoded: derived entirely from WebUntis's
    own `getSchoolyears` data."""
    ranges = sorted(
        (
            dt.datetime.strptime(str(sy["startDate"]), "%Y%m%d").date(),
            dt.datetime.strptime(str(sy["endDate"]), "%Y%m%d").date(),
        )
        for sy in school_years
    )
    if not ranges:
        return False
    weekdays = [monday + dt.timedelta(days=i) for i in range(5)]
    if any(any(start <= d <= end for start, end in ranges) for d in weekdays):
        return False  # at least one weekday is inside a school year, not between them
    return ranges[0][0] <= weekdays[0] and weekdays[-1] <= ranges[-1][1]
