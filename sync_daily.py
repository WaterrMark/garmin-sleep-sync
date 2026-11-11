"""Garmin daily metrics -> the Daily Metrics (Garmin) Notion database.

One page per calendar date. Incremental runs include today plus the previous
SYNC_DAYS - 1 days, so this morning's steps update and yesterday is written
again once it has settled. Full history checks every month back through
January 2007 (or earlier, if an activity is older).

Rested days that Garmin fills with zeros and null heart-rate, stress, and
body battery are not written.

Where each column comes from:
- get_daily_summaries is a v2 document: steps, calories, heart rate, stress,
  and Body Battery are grouped, not stored under the flat get_user_summary
  names. Those groups are copied onto the flat names first.
- When a flat value is still missing or null, one range call fills it:
  steps, floors, calories, heart rate, stress, Body Battery, respiration.
  A key that is present and null is treated as missing. A nested "values"
  object on those stat rows is unwrapped before the fields are read.
- HRV comes from get_hrv_data_range (last night's average, weekly average, status).
- Training readiness and recovery time come from get_training_readiness_range.
  When several snapshots exist for one day, the morning snapshot is kept.
- Training status is get_weekly_training_status (one call for the window,
  one row per day). Daily VO2 max is get_max_metrics_range, the generic
  running estimate, not the per-activity VO2 max.

Stress duration is not stored. The flat summary documents those fields as
seconds, the v2 summary names them milliseconds, and the stress stats chart
documents them as seconds. SpO2 averages and respiration averages come from
the daily summary. Per-minute SpO2, weight, and blood pressure are not requested.
"""

from datetime import date, timedelta

from garmin_auth import garmin_call, garmin_client, is_auth_error, safe_error
from garmin_data import (
    DAILY_PROPERTIES,
    as_rows,
    build_daily_record,
    flatten_daily_summary,
    index_hrv,
    index_readiness,
    index_training_status,
    index_vo2,
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


def _needs(rows, *keys):
    """True when any day is missing a non-null value for one of these keys.

    A key that is present and null still counts as missing. That is the usual
    shape when the daily summary lists a column Garmin did not score.
    """
    return any(row.get(key) is None for row in rows for key in keys)


def normalize_stat_rows(rows):
    """Lift fields out of the {calendarDate, values: {...}} stat-chart shape."""
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        values = row.get("values")
        if not isinstance(values, dict):
            normalized.append(row)
            continue
        merged = dict(values)
        for key, value in row.items():
            if key == "values":
                continue
            if value is not None or key not in merged:
                merged[key] = value
        normalized.append(merged)
    return normalized


def _stat_rows(garmin, method_name, start, end, state, label):
    return normalize_stat_rows(call_rows(garmin, method_name, start, end, state, label))


def overlay_missing(garmin, summaries, start, end, state):
    """Fill summary fields from one range call per metric when values are null."""
    if _needs(summaries, "totalSteps", "dailyStepGoal", "totalDistanceMeters"):
        rows = _stat_rows(garmin, "get_daily_steps", start, end, state, "Steps")
        merge_alias(summaries, rows, "totalSteps", ("totalSteps",))
        merge_alias(summaries, rows, "dailyStepGoal", ("stepGoal", "dailyStepGoal"))
        merge_alias(
            summaries,
            rows,
            "totalDistanceMeters",
            ("totalDistance", "wellnessDistanceMeters", "totalDistanceMeters"),
        )

    if _needs(summaries, "floorsAscended", "floorsDescended"):
        rows = _stat_rows(garmin, "get_daily_floors_stats", start, end, state, "Floors")
        merge_alias(summaries, rows, "floorsAscended", ("ascended", "floorsAscended"))
        merge_alias(summaries, rows, "floorsDescended", ("descended", "floorsDescended"))

    if _needs(summaries, "activeKilocalories", "totalKilocalories", "bmrKilocalories"):
        rows = _stat_rows(garmin, "get_calories_daily", start, end, state, "Calories")
        merge_alias(summaries, rows, "activeKilocalories", ("active",))
        merge_alias(summaries, rows, "bmrKilocalories", ("resting", "bmr"))
        merge_alias(summaries, rows, "totalKilocalories", ("total",))

    if _needs(summaries, "restingHeartRate", "minHeartRate", "maxHeartRate"):
        rows = _stat_rows(
            garmin, "get_daily_heart_rate_stats", start, end, state, "Heart rate"
        )
        merge_alias(summaries, rows, "restingHeartRate", ("restingHR", "restingValue"))
        merge_alias(summaries, rows, "minHeartRate", ("minHR", "minValue"))
        merge_alias(summaries, rows, "maxHeartRate", ("maxHR", "maxValue"))
    if _needs(summaries, "restingHeartRate"):
        rows = _stat_rows(garmin, "get_rhr_daily", start, end, state, "Resting heart rate")
        merge_alias(summaries, rows, "restingHeartRate", ("value", "restingHR"))

    if _needs(
        summaries,
        "bodyBatteryHighestValue",
        "bodyBatteryLowestValue",
        "bodyBatteryChargedValue",
        "bodyBatteryDrainedValue",
        "bodyBatteryMostRecentValue",
    ):
        rows = _stat_rows(
            garmin, "get_daily_body_battery_stats", start, end, state, "Body Battery"
        )
        merge_alias(
            summaries,
            rows,
            "bodyBatteryHighestValue",
            ("highest", "high", "maxValue"),
        )
        merge_alias(
            summaries,
            rows,
            "bodyBatteryLowestValue",
            ("lowest", "low", "minValue"),
        )
        merge_alias(
            summaries,
            rows,
            "bodyBatteryChargedValue",
            ("charged", "chargedValue"),
        )
        merge_alias(
            summaries,
            rows,
            "bodyBatteryDrainedValue",
            ("drained", "drainedValue"),
        )
        merge_alias(
            summaries,
            rows,
            "bodyBatteryMostRecentValue",
            ("latest", "latestValue", "mostRecent", "bodyBatteryMostRecentValue"),
        )
        merge_alias(
            summaries,
            rows,
            "bodyBatteryEndOfDayValue",
            ("endOfDay", "endOfDayValue"),
        )

    if _needs(summaries, "averageStressLevel", "maxStressLevel"):
        rows = _stat_rows(garmin, "get_daily_stress_stats", start, end, state, "Stress")
        merge_alias(
            summaries,
            rows,
            "averageStressLevel",
            ("avgStressLevel", "overallStressLevel", "avgLevel"),
        )
        merge_alias(summaries, rows, "maxStressLevel", ("maxStress", "maxLevel"))

    if _needs(summaries, "moderateIntensityMinutes", "vigorousIntensityMinutes"):
        rows = _stat_rows(
            garmin,
            "get_daily_intensity_minutes_stats",
            start,
            end,
            state,
            "Intensity minutes",
        )
        merge_alias(summaries, rows, "moderateIntensityMinutes", ("moderate", "moderateValue"))
        merge_alias(summaries, rows, "vigorousIntensityMinutes", ("vigorous", "vigorousValue"))

    if _needs(summaries, "avgWakingRespirationValue"):
        rows = _stat_rows(
            garmin, "get_daily_respiration_stats", start, end, state, "Respiration"
        )
        merge_alias(
            summaries,
            rows,
            "avgWakingRespirationValue",
            ("avgValue", "averageRespirationValue"),
        )
        merge_alias(
            summaries,
            rows,
            "lowestRespirationValue",
            ("minValue", "lowestRespirationValue"),
        )
        merge_alias(
            summaries,
            rows,
            "highestRespirationValue",
            ("maxValue", "highestRespirationValue"),
        )


def load_daily_window(garmin, start, end, state):
    summaries = fetch_summaries(garmin, start, end)
    if not summaries:
        return []
    for row in summaries:
        flatten_daily_summary(row)
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
    training = call_indexed(
        garmin,
        "get_weekly_training_status",
        start,
        end,
        state,
        "Training status",
        index_training_status,
    )
    vo2 = call_indexed(
        garmin,
        "get_max_metrics_range",
        start,
        end,
        state,
        "Daily VO2 max",
        index_vo2,
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
            training.get(day),
            vo2.get(day),
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
