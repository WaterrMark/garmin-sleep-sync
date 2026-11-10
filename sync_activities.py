"""Garmin activities -> the Activities (Garmin) Notion database.

Incremental runs pull activities whose local start falls in the last SYNC_DAYS
days, including today. Full history pages through every activity Garmin still
lists. Pages are matched on Activity ID, so reruns update instead of duplicating.

The fields come from the activity list payload (the same summary garminconnect
models). GPS tracks, laps, and per-second streams are not requested.
"""

from datetime import date

from garmin_auth import garmin_call, garmin_client, is_auth_error, safe_error
from garmin_data import (
    ACTIVITY_PROPERTIES,
    activity_day,
    coerce_activities,
    parse_activity,
)
from notion_store import (
    ACTIVITIES_DB_ENV,
    ACTIVITIES_TITLE,
    ensure_database,
    load_key_index,
    notion_client,
    text_match,
    upsert_page,
)
from sync_window import incremental_span, parse_days, parse_mode

_PAGE_SIZE = 100
_MAX_PAGES = 500


def fetch_all_activities(garmin):
    """Every activity, newest first. Stops when Garmin returns a short page."""
    activities = []
    seen = set()
    start = 0
    for _ in range(_MAX_PAGES):
        payload = garmin_call(garmin.get_activities, start, _PAGE_SIZE)
        page = coerce_activities(payload)
        if not page:
            break
        fresh = 0
        for item in page:
            marker = activity_marker(item)
            if marker in seen:
                continue
            seen.add(marker)
            activities.append(item)
            fresh += 1
        if fresh == 0 or len(page) < _PAGE_SIZE:
            break
        start += len(page)
    else:
        print(
            f"Stopped after {_MAX_PAGES} activity pages. "
            "If the account really has more, raise the page cap.",
            flush=True,
        )
    return activities


def activity_marker(item):
    activity_id = item.get("activityId") if isinstance(item, dict) else None
    if activity_id is not None and str(activity_id).strip():
        return str(activity_id)
    return str(id(item))


def fetch_recent_activities(garmin, start, end):
    payload = garmin_call(
        garmin.get_activities_by_date,
        start.isoformat(),
        end.isoformat(),
    )
    kept = []
    for item in coerce_activities(payload):
        day = activity_day(item)
        if day is None or start <= day <= end:
            kept.append(item)
    return kept


def oldest_date_in(activities):
    days = [activity_day(item) for item in activities]
    days = [day for day in days if day is not None]
    return min(days) if days else None


def oldest_activity_date(garmin):
    """Date of the oldest activity, or None if the account has none.

    Two calls: the count, then the last page. Used so a sleep or daily
    backfill can include data older than 2007 when an activity is older.
    """
    try:
        count = int(garmin_call(garmin.count_activities))
    except Exception as exc:
        if is_auth_error(exc):
            raise
        print(f"Could not count Garmin activities ({safe_error(exc)}).", flush=True)
        return None
    if count <= 0:
        return None
    try:
        payload = garmin_call(garmin.get_activities, count - 1, 1)
    except Exception as exc:
        if is_auth_error(exc):
            raise
        print(f"Could not read the oldest Garmin activity ({safe_error(exc)}).", flush=True)
        return None
    page = coerce_activities(payload)
    if not page:
        return None
    return activity_day(page[0])


def sync_activities(garmin, notion, *, mode, days, today, prefetched=None):
    database_id = ensure_database(
        notion,
        env_name=ACTIVITIES_DB_ENV,
        title=ACTIVITIES_TITLE,
        properties=ACTIVITY_PROPERTIES,
        icon="🏃",
    )
    cache = None
    if mode == "backfill":
        activities = prefetched if prefetched is not None else fetch_all_activities(garmin)
        cache = load_key_index(notion, database_id, "Activity ID")
        print(f"Activities to sync: {len(activities)}", flush=True)
    else:
        start, end = incremental_span(today, days, include_today=True)
        activities = fetch_recent_activities(garmin, start, end)
        print(
            f"Activities {start.isoformat()} through {end.isoformat()}: {len(activities)}",
            flush=True,
        )

    created = updated = skipped = 0
    for raw in activities:
        parsed = parse_activity(raw)
        if parsed is None:
            skipped += 1
            print("Skipped an activity Garmin returned without an id.", flush=True)
            continue
        result = upsert_page(
            notion,
            database_id,
            text_match("Activity ID", parsed["id"]),
            parsed["properties"],
            cache=cache,
        )
        if result == "created":
            created += 1
        else:
            updated += 1
        print(f"{parsed['date'] or '?'}: {result}  {parsed['name']}", flush=True)
    print(
        f"Activities: created {created}, updated {updated}, skipped {skipped}",
        flush=True,
    )


def main():
    mode = parse_mode()
    days = parse_days()
    today = date.today()
    garmin = garmin_client()
    notion = notion_client()
    prefetched = fetch_all_activities(garmin) if mode == "backfill" else None
    sync_activities(
        garmin,
        notion,
        mode=mode,
        days=days,
        today=today,
        prefetched=prefetched,
    )


if __name__ == "__main__":
    main()
