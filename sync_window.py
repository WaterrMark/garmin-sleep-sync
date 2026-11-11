"""Decide whether a run is the daily window or a full history walk.

Daily runs stay on a small number of days (SYNC_DAYS, default 3).
Full history is opt-in (SYNC_MODE=backfill) and is what the manual
GitHub Action calls "Full history".
"""

import os
from datetime import date, timedelta

# Garmin Connect itself dates from 2007. Daily metrics and sleep are not
# useful before that, so a full history walk stops there unless an activity
# on the account is older.
HISTORY_FLOOR = date(2007, 1, 1)


def parse_mode(value=None):
    raw = value if value is not None else os.environ.get("SYNC_MODE", "")
    raw = raw.strip().lower()
    if raw in ("", "incremental", "daily", "incremental (recent days)"):
        return "incremental"
    if raw in ("backfill", "full", "history", "full history"):
        return "backfill"
    raise SystemExit("SYNC_MODE must be incremental or full history.")


def parse_days(value=None):
    raw = value if value is not None else os.environ.get("SYNC_DAYS", "")
    raw = (raw or "3").strip()
    try:
        days = int(raw)
    except ValueError as exc:
        raise SystemExit("SYNC_DAYS must be a whole number.") from exc
    if days < 1:
        raise SystemExit("SYNC_DAYS must be at least 1.")
    if days > 3660:
        raise SystemExit("SYNC_DAYS is too large. Use full history for a complete backfill.")
    return days


def parse_datasets(value=None):
    raw = value if value is not None else os.environ.get("DATASETS", "")
    raw = raw.strip().lower()
    if raw in ("", "all"):
        return ["sleep", "activities", "daily"]
    singles = {
        "sleep": ["sleep"],
        "activities": ["activities"],
        "activity": ["activities"],
        "daily": ["daily"],
        "daily metrics": ["daily"],
        "metrics": ["daily"],
    }
    if raw in singles:
        return singles[raw]
    chosen = []
    for part in raw.replace(" ", "").split(","):
        if part not in singles:
            raise SystemExit(
                "DATASETS must be all, sleep, activities, daily, or a comma-separated list."
            )
        name = singles[part][0]
        if name not in chosen:
            chosen.append(name)
    if not chosen:
        raise SystemExit("DATASETS did not include anything to sync.")
    return chosen


def describe_run(mode, days, datasets):
    what = ", ".join(datasets)
    if mode == "backfill":
        return f"{what} (full history)"
    return f"{what} (last {days} days)"


def incremental_sleep_dates(today, days):
    """Nights to sync, newest first, including today.

    Garmin dates a completed night on the morning it ends, the same calendar
    day Daily Metrics uses for that night's HRV. The scheduled run is in the
    evening, so today's date is last night's finished sleep. A night with no
    recorded sleep is still skipped by the caller and does not create a page.
    """
    return [today - timedelta(days=offset) for offset in range(days)]


def incremental_span(today, days, include_today):
    """Inclusive start and end for activities and daily metrics.

    Those feeds include today so a workout or step count from this morning
    shows up on the same day's run. Sleep uses incremental_sleep_dates, which
    covers the same dates, including today.
    """
    end = today if include_today else today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start, end


def history_floor(oldest_activity):
    if oldest_activity is not None and oldest_activity < HISTORY_FLOOR:
        return oldest_activity
    return HISTORY_FLOOR


def month_end(year, month):
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def previous_month(year, month):
    if month == 1:
        return year - 1, 12
    return year, month - 1


def month_bounds(year, month, cap, floor):
    start = date(year, month, 1)
    end = month_end(year, month)
    if end < floor or start > cap:
        return None
    if start < floor:
        start = floor
    if end > cap:
        end = cap
    if start > end:
        return None
    return start, end


def iter_months(cap, floor):
    """Calendar months from `cap` backward through `floor`, newest first.

    The month that contains `cap` is clipped to `cap`, so the newest month
    does not run past the latest day the caller asked for.
    """
    if cap < floor:
        return
    year, month = cap.year, cap.month
    for _ in range(500):
        bounds = month_bounds(year, month, cap, floor)
        if bounds is None:
            return
        yield bounds
        year, month = previous_month(year, month)
    raise RuntimeError("History walk did not reach its floor.")
