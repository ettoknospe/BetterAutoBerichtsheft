"""Pure time/week-id helpers - no network, no filesystem, no globals."""

import datetime as dt


def _hm(untis_time: int) -> str:
    """745 -> 07:45, 1330 -> 13:30"""
    return f"{untis_time // 100:02d}:{untis_time % 100:02d}"


def week_bounds(week_id: str):
    """'2026-W29' -> (monday, sunday)"""
    year, wk = week_id.split("-W")
    monday = dt.date.fromisocalendar(int(year), int(wk), 1)
    return monday, monday + dt.timedelta(days=6)


def current_week_id(today=None) -> str:
    today = today or dt.date.today()
    iso = today.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"
