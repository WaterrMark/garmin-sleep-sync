"""Turn Garmin payloads into Notion fields.

Only values Garmin actually returned are included. A missing metric stays off
the page so a later run does not wipe a value that this response omitted.
"""

from datetime import date


def as_number(value, ndigits=None):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if abs(number) >= 1e15:
        return None
    if ndigits is not None:
        number = round(number, ndigits)
    if number == int(number):
        return int(number)
    return number


def humanize(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NONE", "NO_DATA", "UNKNOWN", "NULL"}:
        return None
    if "_" in text or text.isupper():
        return " ".join(text.replace("_", " ").split()).title()
    return text


def row_date(row):
    if not isinstance(row, dict):
        return None
    for key in ("calendarDate", "date", "calendar_date", "summaryDate"):
        value = row.get(key)
        if isinstance(value, str) and len(value) >= 10:
            day = value[:10]
            try:
                date.fromisoformat(day)
            except ValueError:
                continue
            return day
    return None


def pick(row, *names):
    if not isinstance(row, dict):
        return None
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def as_rows(payload):
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in (
            "data",
            "values",
            "stats",
            "dailyStats",
            "summaries",
            "individualStats",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        if row_date(payload):
            return [payload]
    return []


def split_local_start(value):
    """Return (YYYY-MM-DD, display text) from a Garmin local timestamp."""
    if not isinstance(value, str):
        return None, None
    text = value.strip().replace("T", " ")
    if len(text) < 10:
        return None, None
    day = text[:10]
    try:
        date.fromisoformat(day)
    except ValueError:
        return None, None
    clock = text[11:16] if len(text) >= 16 and text[10] == " " else ""
    shown = f"{day} {clock}".strip() if clock else day
    return day, shown


def activity_day(raw):
    if not isinstance(raw, dict):
        return None
    for key in ("startTimeLocal", "startTimeGMT"):
        day, _shown = split_local_start(raw.get(key))
        if day:
            return date.fromisoformat(day)
    return None


def coerce_activities(payload):
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("activityList", "activities", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _positive(value, ndigits=None):
    number = as_number(value, ndigits)
    if number is None or number <= 0:
        return None
    return number


def _heart_rate(value):
    return _positive(value, 0)


def _kilometers(meters):
    number = as_number(meters)
    if number is None:
        return None
    return as_number(number / 1000.0, 2)


def _minutes(seconds, ndigits=1):
    number = as_number(seconds)
    if number is None:
        return None
    return as_number(number / 60.0, ndigits)


def _speed_kmh(meters_per_second):
    # Activity list payloads report speed in meters per second.
    number = as_number(meters_per_second)
    if number is None:
        return None
    return as_number(number * 3.6, 2)


def _type_key(raw):
    for field in ("activityType", "sportType"):
        value = raw.get(field)
        if isinstance(value, dict):
            key = value.get("typeKey") or value.get("key")
            if key:
                return str(key)
        elif isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _cadence(raw):
    for field in (
        "averageRunningCadenceInStepsPerMinute",
        "averageBikingCadenceInRevPerMinute",
        "averageSwimCadenceInStrokesPerMinute",
    ):
        number = _positive(raw.get(field), 0)
        if number is not None:
            return number
    return None


def parse_activity(raw):
    """Map one activity-list record to Notion properties.

    Returns None when Garmin did not give an activity id, because that id is
    how reruns find the existing page.
    """
    if not isinstance(raw, dict) or raw.get("activityId") in (None, ""):
        return None
    activity_id = str(raw.get("activityId")).strip()
    if not activity_id:
        return None

    day, start_text = split_local_start(raw.get("startTimeLocal"))
    if day is None:
        day, start_text = split_local_start(raw.get("startTimeGMT"))
    type_label = humanize(_type_key(raw))
    name = raw.get("activityName")
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        name = type_label or "Activity"
        if day:
            name = f"{name} {day}"

    distance_m = as_number(raw.get("distance"))
    distance_km = _kilometers(distance_m) if distance_m else None
    duration_min = _minutes(raw.get("duration"))
    moving_min = _minutes(raw.get("movingDuration"))
    moving_seconds = as_number(raw.get("movingDuration"))
    if moving_seconds is None or moving_seconds <= 0:
        moving_seconds = as_number(raw.get("duration"))
    pace = None
    # Skip tiny GPS fragments. Under 100 meters a min/km pace is noise.
    if distance_m and distance_m >= 100 and moving_seconds and moving_seconds > 0:
        pace = as_number((moving_seconds / 60.0) / (distance_m / 1000.0), 2)

    properties = {
        "Activity": {"title": [{"type": "text", "text": {"content": name[:200]}}]},
        "Activity ID": {
            "rich_text": [{"type": "text", "text": {"content": activity_id[:200]}}]
        },
    }
    _put(properties, "Date", _date_prop(day))
    _put(properties, "Start", _text_prop(start_text))
    _put(properties, "Type", _text_prop(type_label))
    _put(properties, "Duration (min)", _number_prop(_positive(duration_min, 1)))
    _put(properties, "Moving (min)", _number_prop(_positive(moving_min, 1)))
    _put(properties, "Distance (km)", _number_prop(distance_km))
    _put(properties, "Pace (min/km)", _number_prop(pace))
    _put(properties, "Avg speed (km/h)", _number_prop(_positive(_speed_kmh(raw.get("averageSpeed")), 2)))
    _put(properties, "Max speed (km/h)", _number_prop(_positive(_speed_kmh(raw.get("maxSpeed")), 2)))
    _put(properties, "Avg HR", _number_prop(_heart_rate(pick(raw, "averageHR", "avgHR"))))
    _put(properties, "Max HR", _number_prop(_heart_rate(pick(raw, "maxHR", "maxHeartRate"))))
    _put(properties, "Calories", _number_prop(_positive(raw.get("calories"), 0)))
    _put(properties, "Elevation gain (m)", _number_prop(_positive(raw.get("elevationGain"), 1)))
    _put(properties, "Elevation loss (m)", _number_prop(_positive(raw.get("elevationLoss"), 1)))
    _put(properties, "Avg power (W)", _number_prop(_positive(raw.get("avgPower"), 0)))
    _put(properties, "Normalized power (W)", _number_prop(_positive(raw.get("normPower"), 0)))
    _put(properties, "Max power (W)", _number_prop(_positive(raw.get("maxPower"), 0)))
    _put(
        properties,
        "Aerobic effect",
        _number_prop(_positive(raw.get("aerobicTrainingEffect"), 1)),
    )
    _put(
        properties,
        "Anaerobic effect",
        _number_prop(_positive(raw.get("anaerobicTrainingEffect"), 1)),
    )
    _put(
        properties,
        "Training load",
        _number_prop(_positive(raw.get("activityTrainingLoad"), 1)),
    )
    _put(properties, "Training effect", _text_prop(humanize(raw.get("trainingEffectLabel"))))
    _put(properties, "Avg cadence", _number_prop(_cadence(raw)))
    _put(properties, "Steps", _number_prop(_positive(raw.get("steps"), 0)))
    _put(properties, "Sets", _number_prop(_positive(raw.get("totalSets"), 0)))
    _put(properties, "Reps", _number_prop(_positive(raw.get("totalReps"), 0)))
    _put(properties, "Volume", _number_prop(_positive(raw.get("totalVolume"), 0)))
    _put(properties, "VO2 max", _number_prop(_positive(pick(raw, "vO2MaxValue", "vo2MaxValue"), 1)))
    location = raw.get("locationName")
    if isinstance(location, str):
        _put(properties, "Location", _text_prop(location))
    return {"id": activity_id, "date": day, "name": name, "properties": properties}


ACTIVITY_PROPERTIES = {
    "Activity": {"title": {}},
    "Activity ID": {"rich_text": {}},
    "Date": {"date": {}},
    "Start": {"rich_text": {}},
    "Type": {"rich_text": {}},
    "Duration (min)": {"number": {"format": "number"}},
    "Moving (min)": {"number": {"format": "number"}},
    "Distance (km)": {"number": {"format": "number"}},
    "Pace (min/km)": {"number": {"format": "number"}},
    "Avg speed (km/h)": {"number": {"format": "number"}},
    "Max speed (km/h)": {"number": {"format": "number"}},
    "Avg HR": {"number": {"format": "number"}},
    "Max HR": {"number": {"format": "number"}},
    "Calories": {"number": {"format": "number"}},
    "Elevation gain (m)": {"number": {"format": "number"}},
    "Elevation loss (m)": {"number": {"format": "number"}},
    "Avg power (W)": {"number": {"format": "number"}},
    "Normalized power (W)": {"number": {"format": "number"}},
    "Max power (W)": {"number": {"format": "number"}},
    "Aerobic effect": {"number": {"format": "number"}},
    "Anaerobic effect": {"number": {"format": "number"}},
    "Training load": {"number": {"format": "number"}},
    "Training effect": {"rich_text": {}},
    "Avg cadence": {"number": {"format": "number"}},
    "Steps": {"number": {"format": "number"}},
    "Sets": {"number": {"format": "number"}},
    "Reps": {"number": {"format": "number"}},
    "Volume": {"number": {"format": "number"}},
    "VO2 max": {"number": {"format": "number"}},
    "Location": {"rich_text": {}},
}


def index_hrv(payload):
    """Map calendar date -> HRV summary. Unknown shapes produce no rows."""
    rows = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("hrvSummaries", "hrvSummaryList", "summaries", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        else:
            if isinstance(payload.get("hrvSummary"), dict):
                rows = [payload]
            elif row_date(payload) or payload.get("lastNightAvg") is not None:
                rows = [payload]
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        summary = row.get("hrvSummary") if isinstance(row.get("hrvSummary"), dict) else row
        day = row_date(summary) or row_date(row)
        if day:
            indexed[day] = summary
    return indexed


def _readiness_rank(row):
    morning = 1 if row.get("inputContext") == "AFTER_WAKEUP_RESET" else 0
    stamp = str(row.get("timestamp") or row.get("timestampLocal") or "")
    return morning, stamp


def index_readiness(payload):
    """Map calendar date -> one readiness snapshot.

    Garmin can return several snapshots in a day. The morning snapshot
    (AFTER_WAKEUP_RESET) is the recovery score. If that tag is missing, the
    latest timestamp is used.
    """
    rows = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("data", "trainingReadiness", "trainingReadinessList"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        else:
            if row_date(payload) or payload.get("score") is not None:
                rows = [payload]
    best = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = row_date(row)
        if not day:
            continue
        current = best.get(day)
        if current is None or _readiness_rank(row) >= _readiness_rank(current):
            best[day] = row
    return best


def _body_battery(value):
    number = as_number(value, 0)
    if number is None or number < 0:
        return None
    return number


def _stress(value):
    # Garmin's stress scale starts above zero. Zero means the day was not scored.
    return _positive(value, 0)


def intensity_minutes(moderate, vigorous):
    """Garmin intensity minutes: vigorous minutes count double."""
    if moderate is None and vigorous is None:
        return None
    return (moderate or 0) + 2 * (vigorous or 0)


def daily_values(summary, hrv, readiness):
    summary = summary or {}
    hrv = hrv or {}
    readiness = readiness or {}
    values = {}

    resting = _heart_rate(summary.get("restingHeartRate"))
    minimum = _heart_rate(summary.get("minHeartRate"))
    maximum = _heart_rate(summary.get("maxHeartRate"))
    if resting is not None:
        values["resting_hr"] = resting
    if minimum is not None:
        values["min_hr"] = minimum
    if maximum is not None:
        values["max_hr"] = maximum

    hrv_ms = _positive(hrv.get("lastNightAvg"), 1)
    hrv_week = _positive(hrv.get("weeklyAvg"), 1)
    hrv_status = humanize(hrv.get("status"))
    if hrv_ms is not None:
        values["hrv"] = hrv_ms
    if hrv_week is not None:
        values["hrv_week"] = hrv_week
    if hrv_status:
        values["hrv_status"] = hrv_status

    steps = as_number(summary.get("totalSteps"), 0)
    if steps is not None and steps >= 0:
        values["steps"] = steps
    goal = _positive(summary.get("dailyStepGoal"), 0)
    if goal is not None:
        values["step_goal"] = goal
    distance = _kilometers(summary.get("totalDistanceMeters"))
    if distance is not None and distance >= 0 and summary.get("totalDistanceMeters") is not None:
        values["distance_km"] = distance

    moderate = as_number(summary.get("moderateIntensityMinutes"), 0)
    vigorous = as_number(summary.get("vigorousIntensityMinutes"), 0)
    if moderate is not None and moderate >= 0 and summary.get("moderateIntensityMinutes") is not None:
        values["moderate"] = moderate
    if vigorous is not None and vigorous >= 0 and summary.get("vigorousIntensityMinutes") is not None:
        values["vigorous"] = vigorous
    total_intensity = intensity_minutes(
        values.get("moderate") if "moderate" in values else None,
        values.get("vigorous") if "vigorous" in values else None,
    )
    if total_intensity is not None:
        values["intensity"] = total_intensity

    active = as_number(summary.get("activeKilocalories"), 0)
    total_cal = as_number(summary.get("totalKilocalories"), 0)
    if active is not None and summary.get("activeKilocalories") is not None and active >= 0:
        values["active_cal"] = active
    if total_cal is not None and summary.get("totalKilocalories") is not None and total_cal >= 0:
        values["total_cal"] = total_cal
    floors = as_number(summary.get("floorsAscended"), 0)
    if floors is not None and summary.get("floorsAscended") is not None and floors >= 0:
        values["floors"] = floors

    avg_stress = _stress(summary.get("averageStressLevel"))
    max_stress = _stress(summary.get("maxStressLevel"))
    if avg_stress is not None:
        values["avg_stress"] = avg_stress
    if max_stress is not None:
        values["max_stress"] = max_stress

    for key, field in (
        ("bb_high", "bodyBatteryHighestValue"),
        ("bb_low", "bodyBatteryLowestValue"),
        ("bb_charged", "bodyBatteryChargedValue"),
        ("bb_drained", "bodyBatteryDrainedValue"),
        ("bb_latest", "bodyBatteryMostRecentValue"),
    ):
        number = _body_battery(summary.get(field))
        if number is not None and summary.get(field) is not None:
            values[key] = number

    score = _positive(readiness.get("score"), 0)
    level = humanize(readiness.get("level"))
    recovery_min = as_number(readiness.get("recoveryTime"))
    phrase_raw = readiness.get("recoveryTimeChangePhrase")
    phrase = humanize(phrase_raw)
    if score is not None:
        values["readiness"] = score
    if level:
        values["readiness_level"] = level
    # REACHED_ZERO means the recovery clock has finished. Garmin may still send
    # the previous non-zero minute count, so the hours field follows the phrase.
    if isinstance(phrase_raw, str) and phrase_raw.strip().upper() == "REACHED_ZERO":
        values["recovery_h"] = 0
        values["recovery"] = "Fully recovered"
    else:
        if recovery_min is not None and recovery_min >= 0:
            values["recovery_h"] = as_number(recovery_min / 60.0, 1)
        if phrase:
            values["recovery"] = phrase
    return values


def has_daily_evidence(values):
    """True when the day looks like a worn watch, not a zero-filled placeholder.

    Total calories alone do not count: Garmin fills those from the profile
    even on days with no device data. A stress level of zero is Garmin's
    "not scored" value, so it does not count either.
    """
    if values.get("steps", 0) > 0:
        return True
    if values.get("distance_km", 0) > 0:
        return True
    if values.get("moderate", 0) > 0 or values.get("vigorous", 0) > 0:
        return True
    if values.get("active_cal", 0) > 0 or values.get("floors", 0) > 0:
        return True
    for key in ("resting_hr", "min_hr", "max_hr", "hrv", "avg_stress", "max_stress", "readiness"):
        if key in values:
            return True
    battery = [values[key] for key in ("bb_high", "bb_low", "bb_charged", "bb_drained", "bb_latest") if key in values]
    return any(number > 0 for number in battery)


def build_daily_record(summary, hrv, readiness):
    day = row_date(summary or {}) or row_date(hrv or {}) or row_date(readiness or {})
    if not day:
        return None
    values = daily_values(summary, hrv, readiness)
    if not has_daily_evidence(values):
        return None
    properties = {
        "Day": {"title": [{"type": "text", "text": {"content": day}}]},
        "Date": {"date": {"start": day}},
    }
    _put(properties, "Resting HR", _number_prop(values.get("resting_hr")))
    _put(properties, "Min HR", _number_prop(values.get("min_hr")))
    _put(properties, "Max HR", _number_prop(values.get("max_hr")))
    _put(properties, "HRV (ms)", _number_prop(values.get("hrv")))
    _put(properties, "HRV status", _text_prop(values.get("hrv_status")))
    _put(properties, "HRV weekly avg", _number_prop(values.get("hrv_week")))
    _put(properties, "Steps", _number_prop(values.get("steps") if "steps" in values else None))
    _put(properties, "Step goal", _number_prop(values.get("step_goal")))
    _put(properties, "Distance (km)", _number_prop(values.get("distance_km") if "distance_km" in values else None))
    _put(properties, "Moderate intensity (min)", _number_prop(values.get("moderate") if "moderate" in values else None))
    _put(properties, "Vigorous intensity (min)", _number_prop(values.get("vigorous") if "vigorous" in values else None))
    _put(properties, "Intensity minutes", _number_prop(values.get("intensity") if "intensity" in values else None))
    _put(properties, "Active calories", _number_prop(values.get("active_cal") if "active_cal" in values else None))
    _put(properties, "Total calories", _number_prop(values.get("total_cal") if "total_cal" in values else None))
    _put(properties, "Floors", _number_prop(values.get("floors") if "floors" in values else None))
    _put(properties, "Avg stress", _number_prop(values.get("avg_stress")))
    _put(properties, "Max stress", _number_prop(values.get("max_stress")))
    _put(properties, "Body Battery high", _number_prop(values.get("bb_high") if "bb_high" in values else None))
    _put(properties, "Body Battery low", _number_prop(values.get("bb_low") if "bb_low" in values else None))
    _put(properties, "Body Battery charged", _number_prop(values.get("bb_charged") if "bb_charged" in values else None))
    _put(properties, "Body Battery drained", _number_prop(values.get("bb_drained") if "bb_drained" in values else None))
    _put(properties, "Body Battery", _number_prop(values.get("bb_latest") if "bb_latest" in values else None))
    _put(properties, "Training readiness", _number_prop(values.get("readiness")))
    _put(properties, "Readiness level", _text_prop(values.get("readiness_level")))
    _put(properties, "Recovery time (h)", _number_prop(values.get("recovery_h") if "recovery_h" in values else None))
    _put(properties, "Recovery", _text_prop(values.get("recovery")))
    return {"date": day, "properties": properties}


DAILY_PROPERTIES = {
    "Day": {"title": {}},
    "Date": {"date": {}},
    "Resting HR": {"number": {"format": "number"}},
    "Min HR": {"number": {"format": "number"}},
    "Max HR": {"number": {"format": "number"}},
    "HRV (ms)": {"number": {"format": "number"}},
    "HRV status": {"rich_text": {}},
    "HRV weekly avg": {"number": {"format": "number"}},
    "Steps": {"number": {"format": "number"}},
    "Step goal": {"number": {"format": "number"}},
    "Distance (km)": {"number": {"format": "number"}},
    "Moderate intensity (min)": {"number": {"format": "number"}},
    "Vigorous intensity (min)": {"number": {"format": "number"}},
    "Intensity minutes": {"number": {"format": "number"}},
    "Active calories": {"number": {"format": "number"}},
    "Total calories": {"number": {"format": "number"}},
    "Floors": {"number": {"format": "number"}},
    "Avg stress": {"number": {"format": "number"}},
    "Max stress": {"number": {"format": "number"}},
    "Body Battery high": {"number": {"format": "number"}},
    "Body Battery low": {"number": {"format": "number"}},
    "Body Battery charged": {"number": {"format": "number"}},
    "Body Battery drained": {"number": {"format": "number"}},
    "Body Battery": {"number": {"format": "number"}},
    "Training readiness": {"number": {"format": "number"}},
    "Readiness level": {"rich_text": {}},
    "Recovery time (h)": {"number": {"format": "number"}},
    "Recovery": {"rich_text": {}},
}


def merge_alias(summaries, extra_rows, canonical, aliases):
    """Fill `canonical` on summary rows when the daily summary left it out."""
    by_date = {}
    for row in summaries:
        day = row_date(row)
        if day and day not in by_date:
            by_date[day] = row
    for extra in extra_rows:
        day = row_date(extra)
        if not day:
            continue
        target = by_date.get(day)
        if target is None:
            target = {"calendarDate": day}
            summaries.append(target)
            by_date[day] = target
        if target.get(canonical) is not None:
            continue
        value = pick(extra, canonical, *aliases)
        if value is not None:
            target[canonical] = value


def _put(properties, name, prop):
    if prop is not None:
        properties[name] = prop


def _text_prop(value):
    if value is None:
        return None
    content = str(value).strip()
    if not content:
        return None
    return {"rich_text": [{"type": "text", "text": {"content": content[:2000]}}]}


def _number_prop(value):
    if value is None:
        return None
    return {"number": value}


def _date_prop(day):
    if not day:
        return None
    return {"date": {"start": day}}
