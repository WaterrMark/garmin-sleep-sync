"""Garmin daily metrics -> the Daily Metrics (Garmin) Notion database.

One page per calendar date. Incremental runs include today plus the previous
SYNC_DAYS - 1 days, so this morning's steps update and yesterday is written
again once it has settled. Full history checks every month back through
January 2007 (or earlier, if an activity is older).

Rested days that Garmin fills with zeros and null heart-rate, stress, and
body battery are not written.

Where each column comes from:
- Steps, heart rate, intensity, calories, floors, stress, and body battery
  come from the daily summary (get_daily_summaries). If that payload omits a
  column, the matching range endpoint is tried once for the same dates.
- HRV comes from get_hrv_data_range (last night's average, weekly average, status).
- Training readiness and recovery time come from get_training_readiness_range.
  When several snapshots exist for one day, the morning snapshot is kept.

Stress duration is not stored. The library does not document whether those
fields are minutes or seconds. SpO2, respiration, hydration, and weight are
separate feeds and are not requested.
"""

from datetime import date, timedelta

from garmin_auth import garmin_call, garmin_client, is_auth_error, safe_error
from garmin_data import (
    DAILY_PROPERTIES,
    as_rows,
    build_daily_record,
    index_hrv,
    index_readiness,
    merge_alias,
    row_date,
)
from notion_store import (
    DAILY_DB_ENV,
    DAILY_TITLE,
    date_match,
    ensure_database,
    load_key_index,
    notion_client,
    upsert_page,
)
from sync_window import history_floor, incremental_span, iter_months, parse_days, parse_mode


class EndpointState:
    def __init__(self):
        self.disabled = set()
        self.failures = {}
        self.succeeded = set()


def _note_failure(state, method_name, label, day, exc):
    if method_name in state.succeeded:
        print(f"{day}: {label} unavailable ({safe_error(exc)})", flush=True)
        return
    state.failures[method_name] = state.failures.get(method_name, 0) + 1
    if state.failures[method_name] >= 3:
        if method_name not in state.disabled:
            print(
                f"{label} is not available from Garmin; skipping it for the rest of this run.",
                flush=True,
            )
        state.disabled.add(method_name)
        return
    print(f"{day}: {label} unavailable ({safe_error(exc)})", flush=True)


def _call_once(garmin, method_name, start, end, state, label):
    if method_name in state.disabled:
        return None
    method = getattr(garmin, method_name, None)
    if method is None:
        state.disabled.add(method_name)
        print(
            f"{label} is not exposed by this version of garminconnect; skipping it.",
            flush=True,
        )
        return None
    try:
        payload = garmin_call(method, start.isoformat(), end.isoformat())
    except Exception as exc:
        if is_auth_error(exc):
            raise
        return exc
    state.succeeded.add(method_name)
    return payload


def call_rows(garmin, method_name, start, end, state, label):
    result = _call_once(garmin, method_name, start, end, state, label)
    if isinstance(result, Exception):
        if start < end:
            mid = start + timedelta(days=(end - start).days // 2)
            left = call_rows(garmin, method_name, start, mid, state, label)
            right = call_rows(garmin, method_name, mid + timedelta(days=1), end, state, label)
            return left + right
        _note_failure(state, method_name, label, start.isoformat(), result)
        return []
    if result is None:
        return []
    return as_rows(result)


def call_indexed(garmin, method_name, start, end, state, label, parse):
    result = _call_once(garmin, method_name, start, end, state, label)
    if isinstance(result, Exception):
        if start < end:
            mid = start + timedelta(days=(end - start).days // 2)
            left = call_indexed(garmin, method_name, start, mid, state, label, parse)
            right = call_indexed(garmin, method_name, mid + timedelta(days=1), end, state, label, parse)
            merged = dict(left)
            merged.update(right)
            return merged
        _note_failure(state, method_name, label, start.isoformat(), result)
        return {}
    if result is None:
        return {}
    return parse(result)


def fetch_summaries(garmin, start, end):
    method = getattr(garmin, "get_daily_summaries", None)
    if method is None:
        return _summaries_by_day(garmin, start, end)
    try:
        payload = garmin_call(method, start.isoformat(), end.isoformat())
    except Exception as exc:
        if is_auth_error(exc):
            raise
        if start >= end:
            try:
                payload = garmin_call(garmin.get_user_summary, start.isoformat())
            except Exception as day_exc:
                if is_auth_error(day_exc):
                    raise
                print(
                    f"{start.isoformat()}: could not fetch daily summary ({safe_error(day_exc)})",
                    flush=True,
                )
                return []
            return as_rows(payload)
        mid = start + timedelta(days=(end - start).days // 2)
        return fetch_summaries(garmin, start, mid) + fetch_summaries(
            garmin, mid + timedelta(days=1), end
        )
    return as_rows(payload)


def _summaries_by_day(garmin, start, end):
    rows = []
    day = start
    while day <= end:
        try:
            payload = garmin_call(garmin.get_user_summary, day.isoformat())
        except Exception as exc:
            if is_auth_error(exc):
                raise
            print(
                f"{day.isoformat()}: could not fetch daily summary ({safe_error(exc)})",
                flush=True,
            )
            day += timedelta(days=1)
            continue
        rows.extend(as_rows(payload))
        day += timedelta(days=1)
    return rows


def _seen_keys(rows):
    seen = set()
    for row in rows:
        seen.update(row.keys())
    return seen


def overlay_missing(garmin, summaries, start, end, state):
    """Fill summary fields from the dedicated range calls when the keys are absent."""
    seen = _seen_keys(summaries)
    heart_keys = ("restingHeartRate", "minHeartRate", "maxHeartRate")
    if any(key not in seen for key in heart_keys):
        rows = call_rows(
            garmin, "get_daily_heart_rate_stats", start, end, state, "Heart rate"
        )
        merge_alias(summaries, rows, "restingHeartRate", ("restingHR",))
        merge_alias(summaries, rows, "minHeartRate", ("minHR",))
        merge_alias(summaries, rows, "maxHeartRate", ("maxHR",))
        seen = _seen_keys(summaries)
    if "restingHeartRate" not in seen:
        rows = call_rows(garmin, "get_rhr_daily", start, end, state, "Resting heart rate")
        merge_alias(summaries, rows, "restingHeartRate", ("value", "restingHR"))
        seen = _seen_keys(summaries)

    battery_keys = {
        "bodyBatteryHighestValue",
        "bodyBatteryLowestValue",
        "bodyBatteryChargedValue",
        "bodyBatteryDrainedValue",
    }
    if not (battery_keys & seen):
        rows = call_rows(
            garmin, "get_daily_body_battery_stats", start, end, state, "Body Battery"
        )
        merge_alias(summaries, rows, "bodyBatteryHighestValue", ("highest", "high"))
        merge_alias(summaries, rows, "bodyBatteryLowestValue", ("lowest", "low"))
        merge_alias(summaries, rows, "bodyBatteryChargedValue", ("charged",))
        merge_alias(summaries, rows, "bodyBatteryDrainedValue", ("drained",))
        seen = _seen_keys(summaries)

    if "averageStressLevel" not in seen and "maxStressLevel" not in seen:
        rows = call_rows(garmin, "get_daily_stress_stats", start, end, state, "Stress")
        merge_alias(summaries, rows, "averageStressLevel", ("avgStressLevel", "overallStressLevel"))
        merge_alias(summaries, rows, "maxStressLevel", ("maxStress",))
        seen = _seen_keys(summaries)

    if "moderateIntensityMinutes" not in seen and "vigorousIntensityMinutes" not in seen:
        rows = call_rows(
            garmin,
            "get_daily_intensity_minutes_stats",
            start,
            end,
            state,
            "Intensity minutes",
        )
        merge_alias(summaries, rows, "moderateIntensityMinutes", ("moderate", "moderateValue"))
        merge_alias(summaries, rows, "vigorousIntensityMinutes", ("vigorous", "vigorousValue"))


def load_daily_window(garmin, start, end, state):
    summaries = fetch_summaries(garmin, start, end)
    if not summaries:
        return []
    overlay_missing(garmin, summaries, start, end, state)
    if not any(build_daily_record(row, None, None) for row in summaries):
        return []

    hrv = call_indexed(
        garmin, "get_hrv_data_range", start, end, state, "HRV", index_hrv
    )
    readiness = call_indexed(
        garmin,
        "get_training_readiness_range",
        start,
        end,
        state,
        "Training readiness",
        index_readiness,
    )
    by_date = {}
    for row in summaries:
        day = row_date(row)
        if day and day not in by_date:
            by_date[day] = row
    days = set(by_date) | set(hrv) | set(readiness)
    records = []
    for day in sorted(days, reverse=True):
        if day < start.isoformat() or day > end.isoformat():
            continue
        record = build_daily_record(
            by_date.get(day) or {"calendarDate": day},
            hrv.get(day),
            readiness.get(day),
        )
        if record:
            records.append(record)
    return records


def _upsert_records(notion, database_id, records, cache):
    created = updated = 0
    for record in records:
        result = upsert_page(
            notion,
            database_id,
            date_match("Date", record["date"]),
            record["properties"],
            cache=cache,
        )
        if result == "created":
            created += 1
        else:
            updated += 1
        print(f"{record['date']}: {result}", flush=True)
    return created, updated


def sync_daily(garmin, notion, *, mode, days, today, oldest_activity=None):
    database_id = ensure_database(
        notion,
        env_name=DAILY_DB_ENV,
        title=DAILY_TITLE,
        properties=DAILY_PROPERTIES,
        icon="💓",
    )
    state = EndpointState()
    if mode == "backfill":
        cache = load_key_index(notion, database_id, "Date")
        floor = history_floor(oldest_activity)
        print(f"Daily metrics history {floor.isoformat()} through {today.isoformat()}", flush=True)
        created = updated = 0
        for start, end in iter_months(today, floor):
            print(f"Daily metrics {start:%Y-%m}", flush=True)
            records = load_daily_window(garmin, start, end, state)
            made, changed = _upsert_records(notion, database_id, records, cache)
            created += made
            updated += changed
        print(f"Daily metrics: created {created}, updated {updated}", flush=True)
        return

    cache = None
    start, end = incremental_span(today, days, include_today=True)
    print(f"Daily metrics {start.isoformat()} through {end.isoformat()}", flush=True)
    records = load_daily_window(garmin, start, end, state)
    created, updated = _upsert_records(notion, database_id, records, cache)
    print(f"Daily metrics: created {created}, updated {updated}", flush=True)


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
    sync_daily(
        garmin,
        notion,
        mode=mode,
        days=days,
        today=today,
        oldest_activity=oldest,
    )


if __name__ == "__main__":
    main()
