"""
Garmin -> Notion sleep sync.

Pulls sleep from Garmin Connect and writes it into the "Sleep (Garmin)" database.
Safe to run repeatedly: it updates an existing night rather than creating duplicates.

Incremental runs (the daily default) start at yesterday and go back SYNC_DAYS
nights (default 3). Today is left out because the current night is often unfinished.
Full history (SYNC_MODE=backfill) walks each month back through 2007, or earlier
if an activity on the account is older, and writes every night Garmin still returns.

Reads everything from environment variables (set as GitHub secrets):
    GARMIN_EMAIL / GARMIN_PASSWORD or GARMIN_TOKEN_B64
    NOTION_TOKEN
    NOTION_SLEEP_DB
    SYNC_DAYS          incremental window (default 3)
    SYNC_MODE          incremental (default) or backfill

Property names below must match the Notion database exactly.
"""

import os
import time
from datetime import date, timedelta

from garmin_auth import garmin_call, garmin_client, is_auth_error, safe_error
from garmin_data import as_number, as_rows, pick, row_date
from notion_store import (
    date_match,
    load_key_index,
    notion_client,
    sleep_database_id,
    upsert_page,
)
from sync_window import (
    history_floor,
    incremental_sleep_dates,
    iter_months,
    parse_days,
    parse_mode,
)

# A short pause between night-by-night reads during a full history walk.
SLEEP_BACKFILL_PAUSE = 0.2


def hours(seconds):
    if not seconds:
        return None
    return round(seconds / 3600.0, 2)


def parse_night(raw):
    """Turn Garmin's sleep payload into the fields our Notion db expects."""
    if not isinstance(raw, dict):
        return None
    dto = raw.get("dailySleepDTO") if isinstance(raw.get("dailySleepDTO"), dict) else raw
    total = dto.get("sleepTimeSeconds")
    if not total:  # no real sleep recorded that night
        return None

    scores = dto.get("sleepScores") or {}
    overall = (scores.get("overall") or {}).get("value")
    if overall is None:
        overall = dto.get("sleepScore")
        if overall is None:
            overall = dto.get("overallScore")

    return {
        "duration_h": hours(total),
        "score": overall,
        "deep_h": hours(dto.get("deepSleepSeconds")),
        "rem_h": hours(dto.get("remSleepSeconds")),
        "light_h": hours(dto.get("lightSleepSeconds")),
    }


def num(value):
    return {"number": value} if value is not None else {"number": None}


def sleep_properties(day_str, night):
    return {
        "Night": {"title": [{"text": {"content": day_str}}]},
        "Date": {"date": {"start": day_str}},
        "Duration (h)": num(night["duration_h"]),
        "Sleep Score": num(night["score"]),
        "Deep (h)": num(night["deep_h"]),
        "REM (h)": num(night["rem_h"]),
        "Light (h)": num(night["light_h"]),
    }


def sleep_candidate_dates(garmin, start, end):
    """Dates in the month that the sleep list says are worth opening.

    The list is only a directory. Stage minutes and the sleep score still come
    from get_sleep_data, which is what the original sync stores. If the list
    call fails, each night in the month is checked individually.
    """
    method = getattr(garmin, "get_sleep_daily", None)
    if method is None:
        return _each_day(start, end)
    try:
        payload = garmin_call(method, start.isoformat(), end.isoformat())
    except Exception as exc:
        if is_auth_error(exc):
            raise
        print(
            f"Sleep list {start.isoformat()}..{end.isoformat()} failed, "
            f"checking each night ({safe_error(exc)})",
            flush=True,
        )
        return _each_day(start, end)

    dates = []
    rows = as_rows(payload)
    for row in rows:
        day = row_date(row)
        if not day:
            continue
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            continue
        if parsed < start or parsed > end:
            continue
        seconds = pick(row, "sleepTimeSeconds", "sleepSeconds", "totalSleepSeconds")
        if seconds is not None and (as_number(seconds) or 0) <= 0:
            continue
        dates.append(parsed)
    if rows and not dates:
        print(
            f"Sleep {start.isoformat()}..{end.isoformat()}: "
            "Garmin returned a list but no dates. Skipping this month.",
            flush=True,
        )
    return dates


def _each_day(start, end):
    days = []
    day = start
    while day <= end:
        days.append(day)
        day += timedelta(days=1)
    return days


def fetch_sleep_records(garmin, start, end, pause=0.0):
    records = []
    dates = sorted(set(sleep_candidate_dates(garmin, start, end)), reverse=True)
    for index, day in enumerate(dates):
        if pause and index:
            time.sleep(pause)
        day_str = day.isoformat()
        try:
            raw = garmin_call(garmin.get_sleep_data, day_str)
        except Exception as exc:
            if is_auth_error(exc):
                raise
            print(f"{day_str}: could not fetch ({safe_error(exc)})", flush=True)
            continue
        night = parse_night(raw)
        if night is None:
            continue
        records.append({"date": day_str, "night": night})
    return records


def _upsert_night(notion, database_id, day_str, night, cache):
    return upsert_page(
        notion,
        database_id,
        date_match("Date", day_str),
        sleep_properties(day_str, night),
        cache=cache,
    )


def sync_sleep(garmin, notion, *, mode, days, today, oldest_activity=None):
    database_id = sleep_database_id()
    if mode == "backfill":
        return _sync_sleep_history(garmin, notion, database_id, today, oldest_activity)
    return _sync_sleep_window(garmin, notion, database_id, today, days)


def _sync_sleep_window(garmin, notion, database_id, today, days):
    created = updated = skipped = failed = 0
    for day in incremental_sleep_dates(today, days):
        day_str = day.isoformat()
        try:
            raw = garmin_call(garmin.get_sleep_data, day_str)
        except Exception as exc:  # keep going even if one day fails
            failed += 1
            print(f"{day_str}: could not fetch ({safe_error(exc)})", flush=True)
            continue

        night = parse_night(raw)
        if night is None:
            skipped += 1
            print(f"{day_str}: no sleep data, skipped", flush=True)
            continue

        result = _upsert_night(notion, database_id, day_str, night, cache=None)
        if result == "created":
            created += 1
        else:
            updated += 1
        print(
            f"{day_str}: {result}  ({night['duration_h']}h, score {night['score']})",
            flush=True,
        )
    print(
        f"Sleep: created {created}, updated {updated}, skipped {skipped}, failed {failed}",
        flush=True,
    )


def _sync_sleep_history(garmin, notion, database_id, today, oldest_activity):
    cache = load_key_index(notion, database_id, "Date")
    floor = history_floor(oldest_activity)
    cap = today - timedelta(days=1)
    created = updated = 0
    print(f"Sleep history {floor.isoformat()} through {cap.isoformat()}", flush=True)
    for start, end in iter_months(cap, floor):
        print(f"Sleep {start:%Y-%m}", flush=True)
        for record in fetch_sleep_records(garmin, start, end, pause=SLEEP_BACKFILL_PAUSE):
            result = _upsert_night(
                notion, database_id, record["date"], record["night"], cache
            )
            if result == "created":
                created += 1
            else:
                updated += 1
            night = record["night"]
            print(
                f"{record['date']}: {result}  ({night['duration_h']}h, score {night['score']})",
                flush=True,
            )
    print(f"Sleep: created {created}, updated {updated}", flush=True)


def main():
    mode = parse_mode()
    days = parse_days()
    today = date.today()
    garmin = garmin_client()
    notion = notion_client()
    oldest = None
    if mode == "backfill":
        from sync_activities import oldest_activity_date

        oldest = oldest_activity_date(garmin)
    sync_sleep(
        garmin,
        notion,
        mode=mode,
        days=days,
        today=today,
        oldest_activity=oldest,
    )


if __name__ == "__main__":
    main()
