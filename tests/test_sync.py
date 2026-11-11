"""Unit tests for parsing, windows, and Notion upserts. No network calls."""

import io
import os
import re
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

from garminconnect import Garmin

from garmin_auth import safe_error
from garmin_data import (
    ACTIVITY_PROPERTIES,
    DAILY_PROPERTIES,
    build_daily_record,
    coerce_activities,
    humanize,
    index_hrv,
    index_readiness,
    parse_activity,
)
from notion_store import (
    ACTIVITIES_DB_ENV,
    DAILY_DB_ENV,
    ensure_database,
    load_key_index,
    text_match,
    upsert_page,
)
from sync_activities import fetch_all_activities, fetch_recent_activities, oldest_activity_date, sync_activities
from sync_daily import EndpointState, load_daily_window
from sync_sleep import SLEEP_PROPERTIES, hours, parse_night, sleep_properties, sync_sleep
from sync_window import (
    describe_run,
    history_floor,
    incremental_sleep_dates,
    incremental_span,
    iter_months,
    parse_datasets,
    parse_days,
    parse_mode,
)

ROOT = Path(__file__).resolve().parents[1]


class EnvCase(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)


class WindowTests(unittest.TestCase):
    def test_sleep_dates_include_the_wake_day(self):
        """A finished night is dated on the morning it ends, same as HRV."""
        today = date(2026, 11, 11)
        self.assertEqual(
            incremental_sleep_dates(today, 3),
            [date(2026, 11, 11), date(2026, 11, 10), date(2026, 11, 9)],
        )
        self.assertEqual(incremental_sleep_dates(today, 1), [today])
        start, end = incremental_span(today, 3, include_today=True)
        dates = incremental_sleep_dates(today, 3)
        self.assertEqual(dates[0], end)
        self.assertEqual(dates[-1], start)

    def test_activity_window_includes_today(self):
        start, end = incremental_span(date(2024, 6, 2), 3, include_today=True)
        self.assertEqual(start, date(2024, 5, 31))
        self.assertEqual(end, date(2024, 6, 2))

    def test_mode_and_dataset_labels(self):
        self.assertEqual(parse_mode("Full history"), "backfill")
        self.assertEqual(parse_mode("Incremental (recent days)"), "incremental")
        self.assertEqual(parse_mode(""), "incremental")
        self.assertEqual(parse_datasets("Daily metrics"), ["daily"])
        self.assertEqual(parse_datasets("All"), ["sleep", "activities", "daily"])
        self.assertEqual(parse_datasets("sleep,daily"), ["sleep", "daily"])
        self.assertEqual(parse_days("60"), 60)
        with self.assertRaises(SystemExit):
            parse_days("0")
        self.assertNotIn("60", describe_run("backfill", 60, ["sleep"]))
        self.assertIn("full history", describe_run("backfill", 60, ["sleep"]))

    def test_history_months_stop_at_the_floor_and_clip_today(self):
        months = list(iter_months(date(2024, 2, 14), date(2007, 1, 1)))
        self.assertEqual(months[0], (date(2024, 2, 1), date(2024, 2, 14)))
        self.assertEqual(months[1], (date(2024, 1, 1), date(2024, 1, 31)))
        self.assertEqual(months[-1], (date(2007, 1, 1), date(2007, 1, 31)))
        self.assertEqual(history_floor(None), date(2007, 1, 1))
        self.assertEqual(history_floor(date(2005, 6, 1)), date(2005, 6, 1))
        self.assertEqual(history_floor(date(2019, 1, 1)), date(2007, 1, 1))


class SleepParseTests(unittest.TestCase):
    def test_parse_night_keeps_the_original_fields(self):
        raw = {
            "dailySleepDTO": {
                "sleepTimeSeconds": 27000,
                "deepSleepSeconds": 3600,
                "remSleepSeconds": 5400,
                "lightSleepSeconds": 18000,
                "sleepScores": {"overall": {"value": 82}},
            }
        }
        night = parse_night(raw)
        self.assertEqual(night["duration_h"], 7.5)
        self.assertEqual(night["deep_h"], 1)
        self.assertEqual(night["rem_h"], 1.5)
        self.assertEqual(night["light_h"], 5)
        self.assertEqual(night["score"], 82)
        self.assertEqual(hours(1000), round(1000 / 3600.0, 2))

    def test_empty_nights_are_skipped(self):
        self.assertIsNone(parse_night({}))
        self.assertIsNone(parse_night({"dailySleepDTO": {}}))
        self.assertIsNone(parse_night({"dailySleepDTO": {"sleepTimeSeconds": 0}}))
        self.assertIsNone(parse_night(None))

    def test_sleep_enrichment_maps_the_nightly_summary(self):
        """Bed, wake, awake time, HRV, SpO2, respiration, and sub-scores come
        from the same get_sleep_data payload. Local epochs are the watch clock.
        """
        raw = {
            "dailySleepDTO": {
                "calendarDate": "2024-05-02",
                "sleepTimeSeconds": 27000,
                "deepSleepSeconds": 3600,
                "remSleepSeconds": 5400,
                "lightSleepSeconds": 18000,
                "awakeSleepSeconds": 1800,
                "awakeCount": 3,
                "sleepStartTimestampLocal": 1626740400000,
                "sleepEndTimestampLocal": 1626767940000,
                "avgSleepHRV": 42.4,
                "avgHeartRate": 51,
                "averageSpO2Value": 96,
                "averageRespirationValue": 15.2,
                "lowestRespirationValue": 11,
                "highestRespirationValue": 19,
                "sleepScores": {
                    "overall": {"value": 82},
                    "totalDuration": {"value": 80, "qualifierKey": "GOOD"},
                    "deepPercentage": {"value": 70},
                    "remPercentage": {"value": 74},
                    "lightPercentage": {"value": 60},
                    "restlessness": {"value": 55},
                    "stress": {"value": 88},
                },
            }
        }
        night = parse_night(raw)
        self.assertEqual(night["duration_h"], 7.5)
        self.assertEqual(night["score"], 82)
        self.assertEqual(night["deep_h"], 1)
        self.assertEqual(night["awake_h"], 0.5)
        self.assertEqual(night["awakenings"], 3)
        self.assertEqual(night["bed"], "2021-07-20 00:20")
        self.assertEqual(night["wake"], "2021-07-20 07:59")
        self.assertEqual(night["hrv"], 42.4)
        self.assertEqual(night["sleep_hr"], 51)
        self.assertEqual(night["spo2"], 96)
        self.assertEqual(night["respiration"], 15.2)
        self.assertEqual(night["respiration_low"], 11)
        self.assertEqual(night["respiration_high"], 19)
        self.assertEqual(night["duration_score"], 80)
        self.assertEqual(night["deep_score"], 70)
        props = sleep_properties("2024-05-02", night)
        self.assertEqual(props["Duration (h)"]["number"], 7.5)
        self.assertEqual(props["Sleep Score"]["number"], 82)
        self.assertEqual(props["Deep (h)"]["number"], 1)
        self.assertEqual(props["REM (h)"]["number"], 1.5)
        self.assertEqual(props["Light (h)"]["number"], 5)
        self.assertEqual(props["Awake (h)"]["number"], 0.5)
        self.assertEqual(props["Awakenings"]["number"], 3)
        self.assertEqual(props["Bed time"]["rich_text"][0]["text"]["content"], "2021-07-20 00:20")
        self.assertEqual(props["Wake time"]["rich_text"][0]["text"]["content"], "2021-07-20 07:59")
        self.assertEqual(props["Sleep HRV (ms)"]["number"], 42.4)
        self.assertEqual(props["Sleep HR"]["number"], 51)
        self.assertEqual(props["Sleep SpO2"]["number"], 96)
        self.assertEqual(props["Respiration avg"]["number"], 15.2)
        self.assertEqual(props["Respiration low"]["number"], 11)
        self.assertEqual(props["Respiration high"]["number"], 19)
        self.assertEqual(props["Duration score"]["number"], 80)
        self.assertEqual(props["Restlessness"]["number"], 55)
        self.assertEqual(props["Sleep stress score"]["number"], 88)
        for name in ("Compare period", "Last 14 nights", "Last 12 weeks", "Week of"):
            self.assertNotIn(name, SLEEP_PROPERTIES)
            self.assertNotIn(name, props)

    def test_sleep_enrichment_stays_off_when_the_payload_omits_it(self):
        night = parse_night(
            {
                "dailySleepDTO": {
                    "sleepTimeSeconds": 3600,
                    "sleepScores": {"overall": {"value": 70}},
                }
            }
        )
        props = sleep_properties("2024-05-01", night)
        self.assertEqual(props["Duration (h)"]["number"], 1)
        self.assertEqual(props["Sleep Score"]["number"], 70)
        self.assertNotIn("Awake (h)", props)
        self.assertNotIn("Bed time", props)
        self.assertNotIn("Sleep HRV (ms)", props)
        self.assertNotIn("Sleep SpO2", props)
        self.assertNotIn("Respiration avg", props)


class ActivityTests(unittest.TestCase):
    def test_activity_fields_and_units(self):
        parsed = parse_activity(
            {
                "activityId": 1234567890123456,
                "activityName": "Morning run",
                "startTimeLocal": "2024-06-01T07:30:00.0",
                "activityType": {"typeKey": "trail_running"},
                "distance": 5000,
                "duration": 1800,
                "movingDuration": 1800,
                "averageSpeed": 3,
                "maxSpeed": 4,
                "averageHR": 142,
                "maxHR": 168,
                "calories": 320,
                "elevationGain": 40,
                "avgPower": 0,
                "aerobicTrainingEffect": 3.2,
                "trainingEffectLabel": "AEROBIC_BASE",
                "averageRunningCadenceInStepsPerMinute": 172,
                "steps": 6400,
                "vO2MaxValue": 48,
                "locationName": "Park",
            }
        )
        self.assertEqual(parsed["id"], "1234567890123456")
        props = parsed["properties"]
        self.assertEqual(props["Activity ID"]["rich_text"][0]["text"]["content"], "1234567890123456")
        self.assertEqual(props["Date"]["date"]["start"], "2024-06-01")
        self.assertEqual(props["Start"]["rich_text"][0]["text"]["content"], "2024-06-01 07:30")
        self.assertEqual(props["Type"]["rich_text"][0]["text"]["content"], "Trail Running")
        self.assertEqual(props["Distance (km)"]["number"], 5)
        self.assertEqual(props["Duration (min)"]["number"], 30)
        self.assertEqual(props["Pace (min/km)"]["number"], 6)
        self.assertEqual(props["Avg speed (km/h)"]["number"], 10.8)
        self.assertEqual(props["Max speed (km/h)"]["number"], 14.4)
        self.assertEqual(props["Avg HR"]["number"], 142)
        self.assertNotIn("Avg power (W)", props)
        self.assertEqual(props["Training effect"]["rich_text"][0]["text"]["content"], "Aerobic Base")
        self.assertEqual(humanize("open_water_swimming"), "Open Water Swimming")

    def test_missing_id_is_skipped_and_optional_fields_stay_off(self):
        self.assertIsNone(parse_activity({"activityName": "Nope"}))
        parsed = parse_activity(
            {
                "activityId": "55",
                "activityName": "Strength",
                "startTimeLocal": "2024-06-01 18:00:00",
                "activityType": {"typeKey": "strength_training"},
                "totalSets": 12,
                "totalReps": 80,
            }
        )
        self.assertNotIn("Distance (km)", parsed["properties"])
        self.assertNotIn("Pace (min/km)", parsed["properties"])
        self.assertEqual(parsed["properties"]["Sets"]["number"], 12)

    def test_pagination_stops_on_a_short_page_and_on_a_repeat(self):
        class Garmin:
            def __init__(self):
                self.calls = []

            def get_activities(self, start, limit):
                self.calls.append(start)
                if start == 0:
                    return [{"activityId": index} for index in range(limit)]
                if start == limit:
                    return [{"activityId": 1000 + index} for index in range(40)]
                return []

        garmin = Garmin()
        rows = fetch_all_activities(garmin)
        self.assertEqual(len(rows), 140)
        self.assertEqual(garmin.calls, [0, 100])

        class Repeat:
            def get_activities(self, start, limit):
                return [{"activityId": 1}]

        self.assertEqual(len(fetch_all_activities(Repeat())), 1)

    def test_recent_activities_drop_dates_outside_the_window(self):
        class Garmin:
            def get_activities_by_date(self, start, end):
                return [
                    {"activityId": 1, "startTimeLocal": "2024-06-02 08:00:00"},
                    {"activityId": 2, "startTimeLocal": "2024-05-01 08:00:00"},
                ]

        rows = fetch_recent_activities(Garmin(), date(2024, 5, 31), date(2024, 6, 2))
        self.assertEqual([row["activityId"] for row in rows], [1])

    def test_oldest_activity_uses_the_last_offset(self):
        class Garmin:
            def __init__(self):
                self.offset = None

            def count_activities(self):
                return 3

            def get_activities(self, start, limit):
                self.offset = (start, limit)
                return [{"activityId": 9, "startTimeLocal": "2016-04-02 09:00:00"}]

        garmin = Garmin()
        self.assertEqual(oldest_activity_date(garmin), date(2016, 4, 2))
        self.assertEqual(garmin.offset, (2, 1))

    def test_list_payload_shapes(self):
        self.assertEqual(coerce_activities({"activityList": [{"activityId": 1}]}), [{"activityId": 1}])
        self.assertEqual(coerce_activities(None), [])


class DailyTests(unittest.TestCase):
    def test_zero_placeholder_days_are_skipped(self):
        self.assertIsNone(
            build_daily_record(
                {
                    "calendarDate": "2024-05-01",
                    "totalSteps": 0,
                    "totalKilocalories": 1800,
                    "averageStressLevel": 0,
                },
                None,
                None,
            )
        )

    def test_intensity_counts_vigorous_minutes_twice(self):
        record = build_daily_record(
            {
                "calendarDate": "2024-05-01",
                "totalSteps": 8000,
                "moderateIntensityMinutes": 20,
                "vigorousIntensityMinutes": 10,
            },
            None,
            None,
        )
        self.assertEqual(record["properties"]["Intensity minutes"]["number"], 40)
        self.assertEqual(record["properties"]["Moderate intensity (min)"]["number"], 20)
        self.assertNotIn("HRV (ms)", record["properties"])

    def test_resting_heart_rate_without_invented_steps(self):
        record = build_daily_record(
            {"calendarDate": "2024-05-02", "restingHeartRate": 47},
            None,
            None,
        )
        self.assertEqual(record["properties"]["Resting HR"]["number"], 47)
        self.assertNotIn("Steps", record["properties"])

    def test_reached_zero_overrides_a_stale_recovery_time(self):
        record = build_daily_record(
            {"calendarDate": "2024-05-02", "restingHeartRate": 50},
            None,
            {
                "calendarDate": "2024-05-02",
                "score": 80,
                "recoveryTime": 300,
                "recoveryTimeChangePhrase": "REACHED_ZERO",
            },
        )
        self.assertEqual(record["properties"]["Recovery time (h)"]["number"], 0)
        self.assertEqual(
            record["properties"]["Recovery"]["rich_text"][0]["text"]["content"],
            "Fully recovered",
        )

    def test_hrv_shapes_and_morning_readiness(self):
        indexed = index_hrv(
            {
                "hrvSummaries": [
                    {"calendarDate": "2024-05-01", "lastNightAvg": 42, "status": "BALANCED"}
                ]
            }
        )
        self.assertEqual(indexed["2024-05-01"]["lastNightAvg"], 42)
        self.assertEqual(index_hrv({"hrvSummary": {"calendarDate": "2024-05-02", "lastNightAvg": 40}})["2024-05-02"]["lastNightAvg"], 40)
        readiness = index_readiness(
            [
                {"calendarDate": "2024-05-01", "score": 30, "timestamp": "2024-05-01T18:00:00"},
                {
                    "calendarDate": "2024-05-01",
                    "score": 75,
                    "timestamp": "2024-05-01T07:00:00",
                    "inputContext": "AFTER_WAKEUP_RESET",
                },
            ]
        )
        self.assertEqual(readiness["2024-05-01"]["score"], 75)

    def test_window_merges_range_endpoints_and_drops_extra_days(self):
        class Garmin:
            def get_daily_summaries(self, start, end):
                return {
                    "data": [
                        {
                            "calendarDate": "2024-05-01",
                            "totalSteps": 12000,
                            "moderateIntensityMinutes": 20,
                            "vigorousIntensityMinutes": 10,
                        },
                        {"calendarDate": "2024-05-04", "totalSteps": 5000},
                    ]
                }

            def get_daily_heart_rate_stats(self, start, end):
                return []

            def get_rhr_daily(self, start, end):
                return [{"calendarDate": "2024-05-01", "value": 48}]

            def get_daily_body_battery_stats(self, start, end):
                return [{"calendarDate": "2024-05-01", "highest": 90, "lowest": 15}]

            def get_daily_stress_stats(self, start, end):
                return []

            def get_hrv_data_range(self, start, end):
                return {
                    "hrvSummaries": [
                        {
                            "calendarDate": "2024-05-01",
                            "lastNightAvg": 42,
                            "weeklyAvg": 44,
                            "status": "BALANCED",
                        }
                    ]
                }

            def get_training_readiness_range(self, start, end):
                return [
                    {
                        "calendarDate": "2024-05-01",
                        "score": 70,
                        "level": "HIGH",
                        "recoveryTime": 120,
                        "inputContext": "AFTER_WAKEUP_RESET",
                    }
                ]

            def get_daily_steps(self, start, end):
                return []

            def get_daily_floors_stats(self, start, end):
                return []

            def get_calories_daily(self, start, end):
                return []

            def get_daily_respiration_stats(self, start, end):
                return []

            def get_daily_intensity_minutes_stats(self, start, end):
                return []

            def get_weekly_training_status(self, start, end):
                return []

            def get_max_metrics_range(self, start, end):
                return []

        records = load_daily_window(Garmin(), date(2024, 5, 1), date(2024, 5, 3), EndpointState())
        self.assertEqual([record["date"] for record in records], ["2024-05-01"])
        props = records[0]["properties"]
        self.assertEqual(props["Steps"]["number"], 12000)
        self.assertEqual(props["Resting HR"]["number"], 48)
        self.assertEqual(props["Body Battery high"]["number"], 90)
        self.assertEqual(props["Body Battery low"]["number"], 15)
        self.assertEqual(props["HRV (ms)"]["number"], 42)
        self.assertEqual(props["HRV status"]["rich_text"][0]["text"]["content"], "Balanced")
        self.assertEqual(props["Training readiness"]["number"], 70)
        self.assertEqual(props["Readiness level"]["rich_text"][0]["text"]["content"], "High")
        self.assertEqual(props["Recovery time (h)"]["number"], 2)
        self.assertEqual(props["Intensity minutes"]["number"], 40)

    def test_flat_summary_writes_the_columns_that_were_empty(self):
        record = build_daily_record(
            {
                "calendarDate": "2024-05-01",
                "restingHeartRate": 47,
                "minHeartRate": 42,
                "maxHeartRate": 168,
                "totalSteps": 8432,
                "dailyStepGoal": 10000,
                "totalDistanceMeters": 6543,
                "floorsAscended": 11,
                "floorsDescended": 8,
                "moderateIntensityMinutes": 22,
                "vigorousIntensityMinutes": 6,
                "activeKilocalories": 480,
                "bmrKilocalories": 1750,
                "totalKilocalories": 2230,
                "averageStressLevel": 28,
                "maxStressLevel": 86,
                "stressDuration": 14400,
                "bodyBatteryHighestValue": 95,
                "bodyBatteryLowestValue": 18,
                "bodyBatteryChargedValue": 46,
                "bodyBatteryDrainedValue": 61,
                "bodyBatteryMostRecentValue": 34,
                "avgWakingRespirationValue": 14.5,
                "lowestRespirationValue": 10,
                "highestRespirationValue": 22,
                "averageSpo2": 96,
                "lowestSpo2": 91,
                "latestSpo2": 97,
                "hydrationValueInML": 1800,
                "hydrationGoalInML": 2500,
            },
            {"lastNightAvg": 44, "weeklyAvg": 46, "status": "BALANCED"},
            {
                "score": 72,
                "level": "MODERATE",
                "recoveryTime": 90,
                "recoveryTimeChangePhrase": "RECOVERING",
            },
        )
        props = record["properties"]
        self.assertEqual(props["Resting HR"]["number"], 47)
        self.assertEqual(props["Min HR"]["number"], 42)
        self.assertEqual(props["Max HR"]["number"], 168)
        self.assertEqual(props["Steps"]["number"], 8432)
        self.assertEqual(props["Step goal"]["number"], 10000)
        self.assertEqual(props["Distance (km)"]["number"], 6.54)
        self.assertEqual(props["Floors"]["number"], 11)
        self.assertEqual(props["Floors descended"]["number"], 8)
        self.assertEqual(props["Moderate intensity (min)"]["number"], 22)
        self.assertEqual(props["Vigorous intensity (min)"]["number"], 6)
        self.assertEqual(props["Intensity minutes"]["number"], 34)
        self.assertEqual(props["Active calories"]["number"], 480)
        self.assertEqual(props["BMR calories"]["number"], 1750)
        self.assertEqual(props["Total calories"]["number"], 2230)
        self.assertEqual(props["Avg stress"]["number"], 28)
        self.assertEqual(props["Max stress"]["number"], 86)
        self.assertNotIn("Stress duration", props)
        self.assertEqual(props["Body Battery high"]["number"], 95)
        self.assertEqual(props["Body Battery low"]["number"], 18)
        self.assertEqual(props["Body Battery charged"]["number"], 46)
        self.assertEqual(props["Body Battery drained"]["number"], 61)
        self.assertEqual(props["Body Battery"]["number"], 34)
        self.assertEqual(props["HRV (ms)"]["number"], 44)
        self.assertEqual(props["HRV weekly avg"]["number"], 46)
        self.assertEqual(props["HRV status"]["rich_text"][0]["text"]["content"], "Balanced")
        self.assertEqual(props["Training readiness"]["number"], 72)
        self.assertEqual(props["Readiness level"]["rich_text"][0]["text"]["content"], "Moderate")
        self.assertEqual(props["Recovery time (h)"]["number"], 1.5)
        self.assertEqual(props["Respiration avg"]["number"], 14.5)
        self.assertEqual(props["Respiration low"]["number"], 10)
        self.assertEqual(props["Respiration high"]["number"], 22)
        self.assertEqual(props["SpO2 avg"]["number"], 96)
        self.assertEqual(props["SpO2 low"]["number"], 91)
        self.assertEqual(props["SpO2 latest"]["number"], 97)
        self.assertEqual(props["Hydration (ml)"]["number"], 1800)
        self.assertEqual(props["Hydration goal (ml)"]["number"], 2500)

    def test_grouped_v2_summary_fills_the_same_columns(self):
        """get_daily_summaries returns topic groups, not the flat user-summary keys."""
        record = build_daily_record(
            {
                "calendarDate": "2024-05-03",
                "includesWellnessData": True,
                "movement": {
                    "steps": {"goal": 8000, "value": 11234, "distanceInMeters": 9010},
                    "floorsAscended": {"goal": 10, "value": 14},
                    "floorsDescended": {"value": 6},
                },
                "calories": {"burnedResting": 1600, "burnedActive": 510, "burnedTotal": 2110},
                "heartRate": {"minValue": 45, "maxValue": 154, "restingValue": 49},
                "intensityMinutes": {"goal": 150, "moderate": 18, "vigorous": 7},
                "stress": {
                    "avgLevel": 33,
                    "maxLevel": 91,
                    "totalDurationInMillis": 3600000,
                },
                "bodyBattery": {
                    "minValue": 12,
                    "maxValue": 88,
                    "chargedValue": 41,
                    "drainedValue": 52,
                    "latestValue": 27,
                    "endOfDayValue": 30,
                },
                "respiration": {"avgValue": 15, "minValue": 9, "maxValue": 21},
                "pulseOx": {"avgValue": 95, "minValue": 90, "latestValue": 97},
                "hydration": {"goalInMl": 2000, "consumedInFractionalMl": 1250.4},
            },
            None,
            None,
        )
        props = record["properties"]
        self.assertEqual(props["Steps"]["number"], 11234)
        self.assertEqual(props["Step goal"]["number"], 8000)
        self.assertEqual(props["Distance (km)"]["number"], 9.01)
        self.assertEqual(props["Floors"]["number"], 14)
        self.assertEqual(props["Floors descended"]["number"], 6)
        self.assertEqual(props["Active calories"]["number"], 510)
        self.assertEqual(props["BMR calories"]["number"], 1600)
        self.assertEqual(props["Total calories"]["number"], 2110)
        self.assertEqual(props["Resting HR"]["number"], 49)
        self.assertEqual(props["Min HR"]["number"], 45)
        self.assertEqual(props["Max HR"]["number"], 154)
        self.assertEqual(props["Moderate intensity (min)"]["number"], 18)
        self.assertEqual(props["Vigorous intensity (min)"]["number"], 7)
        self.assertEqual(props["Intensity minutes"]["number"], 32)
        self.assertEqual(props["Avg stress"]["number"], 33)
        self.assertEqual(props["Max stress"]["number"], 91)
        self.assertNotIn("Stress duration", props)
        self.assertEqual(props["Body Battery"]["number"], 27)
        self.assertEqual(props["Body Battery high"]["number"], 88)
        self.assertEqual(props["Body Battery low"]["number"], 12)
        self.assertEqual(props["Body Battery charged"]["number"], 41)
        self.assertEqual(props["Body Battery drained"]["number"], 52)
        self.assertEqual(props["Respiration avg"]["number"], 15)
        self.assertEqual(props["SpO2 avg"]["number"], 95)
        self.assertEqual(props["Hydration (ml)"]["number"], 1250)
        self.assertEqual(props["Hydration goal (ml)"]["number"], 2000)

    def test_body_battery_uses_end_of_day_when_latest_is_missing(self):
        record = build_daily_record(
            {
                "calendarDate": "2024-05-03",
                "totalSteps": 4000,
                "bodyBattery": {"maxValue": 80, "minValue": 20, "endOfDayValue": 36},
            },
            None,
            None,
        )
        self.assertEqual(record["properties"]["Body Battery"]["number"], 36)
        self.assertEqual(record["properties"]["Body Battery high"]["number"], 80)

    def test_null_summary_keys_are_filled_from_range_stats(self):
        class Garmin:
            def __init__(self):
                self.calls = []

            def get_daily_summaries(self, start, end):
                return {
                    "data": [
                        {
                            "calendarDate": "2024-05-01",
                            "restingHeartRate": 47,
                            "moderateIntensityMinutes": 20,
                            "vigorousIntensityMinutes": 10,
                            "totalSteps": None,
                            "dailyStepGoal": None,
                            "totalDistanceMeters": None,
                            "floorsAscended": None,
                            "floorsDescended": None,
                            "activeKilocalories": None,
                            "totalKilocalories": None,
                            "bmrKilocalories": None,
                            "minHeartRate": None,
                            "maxHeartRate": None,
                            "averageStressLevel": None,
                            "maxStressLevel": None,
                            "bodyBatteryHighestValue": None,
                            "bodyBatteryLowestValue": None,
                            "bodyBatteryChargedValue": None,
                            "bodyBatteryDrainedValue": None,
                            "bodyBatteryMostRecentValue": None,
                            "avgWakingRespirationValue": None,
                            "averageSpo2": None,
                        }
                    ]
                }

            def get_daily_steps(self, start, end):
                self.calls.append("steps")
                return [
                    {
                        "calendarDate": "2024-05-01",
                        "totalSteps": 9000,
                        "stepGoal": 10000,
                        "totalDistance": 7200,
                    }
                ]

            def get_daily_floors_stats(self, start, end):
                self.calls.append("floors")
                return [
                    {
                        "calendarDate": "2024-05-01",
                        "values": {"floorsAscended": 12, "floorsDescended": 9},
                    }
                ]

            def get_calories_daily(self, start, end):
                self.calls.append("calories")
                return [
                    {
                        "calendarDate": "2024-05-01",
                        "active": 400,
                        "resting": 1600,
                        "total": 2000,
                    }
                ]

            def get_daily_heart_rate_stats(self, start, end):
                self.calls.append("hr")
                return [
                    {
                        "calendarDate": "2024-05-01",
                        "values": {
                            "restingHeartRate": 99,
                            "minHeartRate": 44,
                            "maxHeartRate": 161,
                        },
                    }
                ]

            def get_rhr_daily(self, start, end):
                self.calls.append("rhr")
                return [{"calendarDate": "2024-05-01", "value": 40}]

            def get_daily_stress_stats(self, start, end):
                self.calls.append("stress")
                return [
                    {
                        "calendarDate": "2024-05-01",
                        "values": {
                            "overallStressLevel": 31,
                            "highStressDuration": 1200,
                            "maxStressLevel": 88,
                        },
                    }
                ]

            def get_daily_body_battery_stats(self, start, end):
                self.calls.append("battery")
                return [
                    {
                        "calendarDate": "2024-05-01",
                        "values": {
                            "highest": 92,
                            "lowest": 16,
                            "charged": 38,
                            "drained": 57,
                            "latest": 29,
                        },
                    }
                ]

            def get_daily_respiration_stats(self, start, end):
                self.calls.append("respiration")
                return [
                    {
                        "calendarDate": "2024-05-01",
                        "values": {
                            "avgWakingRespirationValue": 14,
                            "lowestRespirationValue": 9,
                            "highestRespirationValue": 20,
                        },
                    }
                ]

            def get_hrv_data_range(self, start, end):
                return {
                    "hrvSummaries": [
                        {
                            "calendarDate": "2024-05-01",
                            "lastNightAvg": 41,
                            "weeklyAvg": 43,
                            "status": "BALANCED",
                        }
                    ]
                }

            def get_training_readiness_range(self, start, end):
                return [
                    {
                        "calendarDate": "2024-05-01",
                        "score": 68,
                        "level": "MODERATE",
                        "recoveryTime": 180,
                        "inputContext": "AFTER_WAKEUP_RESET",
                    }
                ]

            def get_weekly_training_status(self, start, end):
                self.calls.append("training")
                return {
                    "weeklyTrainingStatus": {
                        "payload": {
                            "reportData": {
                                "111": [
                                    {
                                        "calendarDate": "2024-05-01",
                                        "trainingStatus": 4,
                                        "trainingStatusFeedbackPhrase": "MAINTAINING_2",
                                        "primaryTrainingDevice": True,
                                        "acuteTrainingLoadDTO": {
                                            "dailyTrainingLoadAcute": 224,
                                            "acwrStatus": "OPTIMAL",
                                        },
                                    }
                                ],
                                "222": [
                                    {
                                        "calendarDate": "2024-05-01",
                                        "trainingStatusFeedbackPhrase": "UNPRODUCTIVE",
                                        "primaryTrainingDevice": False,
                                        "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 10},
                                    }
                                ],
                            }
                        }
                    }
                }

            def get_max_metrics_range(self, start, end):
                self.calls.append("vo2")
                return [
                    {
                        "generic": {
                            "calendarDate": "2024-05-01",
                            "vo2MaxPreciseValue": 47.6,
                            "vo2MaxValue": 48,
                        },
                        "cycling": {"calendarDate": "2024-05-01", "vo2MaxPreciseValue": 51.2},
                    }
                ]

        garmin = Garmin()
        records = load_daily_window(garmin, date(2024, 5, 1), date(2024, 5, 3), EndpointState())
        self.assertEqual([record["date"] for record in records], ["2024-05-01"])
        props = records[0]["properties"]
        self.assertEqual(props["Resting HR"]["number"], 47)
        self.assertNotEqual(props["Resting HR"]["number"], 99)
        self.assertEqual(props["Min HR"]["number"], 44)
        self.assertEqual(props["Max HR"]["number"], 161)
        self.assertEqual(props["Steps"]["number"], 9000)
        self.assertEqual(props["Step goal"]["number"], 10000)
        self.assertEqual(props["Distance (km)"]["number"], 7.2)
        self.assertEqual(props["Floors"]["number"], 12)
        self.assertEqual(props["Floors descended"]["number"], 9)
        self.assertEqual(props["Active calories"]["number"], 400)
        self.assertEqual(props["BMR calories"]["number"], 1600)
        self.assertEqual(props["Total calories"]["number"], 2000)
        self.assertEqual(props["Avg stress"]["number"], 31)
        self.assertEqual(props["Max stress"]["number"], 88)
        self.assertNotIn("Stress duration", props)
        self.assertEqual(props["Body Battery high"]["number"], 92)
        self.assertEqual(props["Body Battery low"]["number"], 16)
        self.assertEqual(props["Body Battery charged"]["number"], 38)
        self.assertEqual(props["Body Battery drained"]["number"], 57)
        self.assertEqual(props["Body Battery"]["number"], 29)
        self.assertEqual(props["Intensity minutes"]["number"], 40)
        self.assertEqual(props["HRV (ms)"]["number"], 41)
        self.assertEqual(props["Training readiness"]["number"], 68)
        self.assertEqual(props["Recovery time (h)"]["number"], 3)
        self.assertEqual(props["Respiration avg"]["number"], 14)
        self.assertEqual(props["Training status"]["rich_text"][0]["text"]["content"], "Maintaining")
        self.assertEqual(props["Acute load"]["number"], 224)
        self.assertEqual(props["Daily VO2 max"]["number"], 47.6)
        self.assertFalse(any("cycling" in name.lower() or name == "VO2 max" for name in props))
        self.assertIn("steps", garmin.calls)
        self.assertIn("stress", garmin.calls)
        self.assertIn("battery", garmin.calls)
        self.assertNotIn("rhr", garmin.calls)

    def test_grouped_summary_does_not_refetch_fields_it_already_has(self):
        class Garmin:
            def __init__(self):
                self.calls = []

            def get_daily_summaries(self, start, end):
                return {
                    "data": [
                        {
                            "calendarDate": "2024-05-02",
                            "movement": {
                                "steps": {"value": 5000, "goal": 7500, "distanceInMeters": 4000},
                                "floorsAscended": {"value": 3},
                                "floorsDescended": {"value": 2},
                            },
                            "calories": {"burnedActive": 200, "burnedResting": 1500, "burnedTotal": 1700},
                            "heartRate": {"restingValue": 50, "minValue": 46, "maxValue": 140},
                            "intensityMinutes": {"moderate": 5, "vigorous": 0},
                            "stress": {"avgLevel": 22, "maxLevel": 70},
                            "bodyBattery": {
                                "maxValue": 90,
                                "minValue": 25,
                                "chargedValue": 30,
                                "drainedValue": 40,
                                "latestValue": 55,
                            },
                            "respiration": {"avgValue": 13, "minValue": 10, "maxValue": 18},
                            "pulseOx": {"avgValue": 97, "minValue": 94, "latestValue": 98},
                        }
                    ]
                }

            def _unused(self, name):
                def method(start, end):
                    self.calls.append(name)
                    return []

                return method

            def __getattr__(self, name):
                if name.startswith("get_"):
                    return self._unused(name)
                raise AttributeError(name)

        garmin = Garmin()
        records = load_daily_window(garmin, date(2024, 5, 2), date(2024, 5, 2), EndpointState())
        props = records[0]["properties"]
        self.assertEqual(props["Steps"]["number"], 5000)
        self.assertEqual(props["Body Battery"]["number"], 55)
        self.assertEqual(props["Avg stress"]["number"], 22)
        for name in (
            "get_daily_steps",
            "get_daily_stress_stats",
            "get_daily_body_battery_stats",
            "get_daily_heart_rate_stats",
            "get_daily_floors_stats",
            "get_calories_daily",
            "get_daily_respiration_stats",
            "get_rhr_daily",
        ):
            self.assertNotIn(name, garmin.calls, name)
        self.assertIn("get_weekly_training_status", garmin.calls)
        self.assertIn("get_max_metrics_range", garmin.calls)


class NotionTests(EnvCase):
    def test_database_is_created_once_and_then_reused(self):
        os.environ["NOTION_SLEEP_DB"] = "sleep-db"
        notion = FakeNotion()
        notion.database["parent"] = {"type": "page_id", "page_id": "parent-page"}
        notion.database["properties"] = {"Activity": {"type": "title"}}
        with redirect_stdout(io.StringIO()) as buf:
            first = ensure_database(
                notion,
                env_name=ACTIVITIES_DB_ENV,
                title="Activities (Garmin)",
                properties=ACTIVITY_PROPERTIES,
                icon="🏃",
            )
        self.assertEqual(first, "created-db")
        self.assertIn(f"{ACTIVITIES_DB_ENV}=created-db", buf.getvalue())
        self.assertEqual(len([event for event in notion.events if event[0] == "db.create"]), 1)
        notion.child_blocks.append(
            {
                "id": "created-db",
                "type": "child_database",
                "child_database": {"title": "Activities (Garmin)"},
            }
        )
        with redirect_stdout(io.StringIO()):
            second = ensure_database(
                notion,
                env_name=ACTIVITIES_DB_ENV,
                title="Activities (Garmin)",
                properties=ACTIVITY_PROPERTIES,
                icon="🏃",
            )
        self.assertEqual(second, "created-db")
        self.assertEqual(len([event for event in notion.events if event[0] == "db.create"]), 1)

    def test_configured_secret_skips_creation(self):
        os.environ[DAILY_DB_ENV] = "daily-from-secret"
        notion = FakeNotion()
        notion.database["properties"] = {name: {"type": "number"} for name in DAILY_PROPERTIES}
        notion.database["properties"]["Day"] = {"type": "title"}
        with redirect_stdout(io.StringIO()) as buf:
            found = ensure_database(
                notion,
                env_name=DAILY_DB_ENV,
                title="Daily Metrics (Garmin)",
                properties=DAILY_PROPERTIES,
                icon="💓",
            )
        self.assertEqual(found, "daily-from-secret")
        self.assertNotIn("daily-from-secret", buf.getvalue())
        self.assertFalse(any(event[0] == "db.create" for event in notion.events))

    def test_upsert_updates_a_cached_page_and_creates_a_new_one(self):
        notion = FakeNotion()
        cache = {"111": "page-existing"}
        updated = upsert_page(
            notion,
            "db",
            text_match("Activity ID", "111"),
            {"Activity": {"title": [{"text": {"content": "Run"}}]}},
            cache=cache,
        )
        created = upsert_page(
            notion,
            "db",
            text_match("Activity ID", "222"),
            {"Activity": {"title": [{"text": {"content": "Ride"}}]}},
            cache=cache,
        )
        self.assertEqual(updated, "updated")
        self.assertEqual(created, "created")
        self.assertEqual(cache["222"], "page-1")
        self.assertFalse(any(event[0] == "query" for event in notion.events))

    def test_index_reads_duplicate_keys_once(self):
        notion = FakeNotion()
        notion.query_pages = [
            {
                "id": "page-a",
                "properties": {
                    "Date": {"type": "date", "date": {"start": "2024-05-01T00:00:00.000Z"}}
                },
            },
            {
                "id": "page-b",
                "properties": {"Date": {"type": "date", "date": {"start": "2024-05-01"}}},
            },
        ]
        with redirect_stdout(io.StringIO()) as buf:
            index = load_key_index(notion, "db", "Date")
        self.assertEqual(index, {"2024-05-01": "page-a"})
        self.assertIn("extra Notion pages", buf.getvalue())


class SyncFlowTests(EnvCase):
    def test_incremental_sleep_writes_todays_completed_night(self):
        os.environ["NOTION_SLEEP_DB"] = "sleep-db"
        garmin = IncrementalSleep()
        notion = FakeNotion()
        seed_sleep_db(notion)
        with redirect_stdout(io.StringIO()) as buf:
            sync_sleep(
                garmin,
                notion,
                mode="incremental",
                days=3,
                today=date(2024, 5, 2),
            )
        log = buf.getvalue()
        self.assertIn("2024-05-02: created  (7.0h, score 81)", log)
        self.assertIn("2024-05-01: created  (1.0h, score 70)", log)
        self.assertIn("2024-04-30: no sleep data, skipped", log)
        self.assertEqual(
            garmin.calls,
            [("sleep", "2024-05-02"), ("sleep", "2024-05-01"), ("sleep", "2024-04-30")],
        )
        created = [event for event in notion.events if event[0] == "page.create"]
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0][1]["parent"], {"database_id": "sleep-db"})
        self.assertEqual(created[0][1]["properties"]["Date"]["date"]["start"], "2024-05-02")
        self.assertEqual(created[0][1]["properties"]["Duration (h)"]["number"], 7)
        self.assertEqual(created[1][1]["properties"]["Date"]["date"]["start"], "2024-05-01")
        updates = [event for event in notion.events if event[0] == "db.update"]
        self.assertTrue(updates)
        added = updates[0][1]["properties"]
        self.assertIn("Awake (h)", added)
        self.assertIn("Bed time", added)
        self.assertNotIn("Compare period", added)
        self.assertNotIn("Last 14 nights", added)
        self.assertNotIn("Last 12 weeks", added)
        self.assertNotIn("Week of", added)
        self.assertNotIn("Night", added)

    def test_incremental_sleep_skips_an_empty_today(self):
        os.environ["NOTION_SLEEP_DB"] = "sleep-db"
        garmin = IncrementalSleep(nights={"2024-05-02": 0, "2024-05-01": 3600})
        notion = FakeNotion()
        seed_sleep_db(notion)
        with redirect_stdout(io.StringIO()) as buf:
            sync_sleep(
                garmin,
                notion,
                mode="incremental",
                days=2,
                today=date(2024, 5, 2),
            )
        log = buf.getvalue()
        self.assertIn("2024-05-02: no sleep data, skipped", log)
        self.assertIn("2024-05-01: created  (1.0h, score 70)", log)
        created = [event for event in notion.events if event[0] == "page.create"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][1]["properties"]["Date"]["date"]["start"], "2024-05-01")

    def test_sleep_backfill_includes_todays_completed_night(self):
        os.environ["NOTION_SLEEP_DB"] = "sleep-db"
        garmin = HistorySleep()
        notion = FakeNotion()
        seed_sleep_db(notion)
        with redirect_stdout(io.StringIO()) as buf:
            sync_sleep(
                garmin,
                notion,
                mode="backfill",
                days=3,
                today=date(2024, 2, 10),
            )
        log = buf.getvalue()
        self.assertIn("Sleep history 2007-01-01 through 2024-02-10", log)
        self.assertIn("2024-02-10: created  (6.0h, score 74)", log)
        self.assertIn("2024-01-15: created  (8.0h, score 80)", log)
        self.assertNotIn("2024-02-09", log)
        self.assertNotIn("2024-02-08: created", log)
        night_calls = [day for kind, day in garmin.detail_calls]
        self.assertEqual(night_calls, ["2024-02-10", "2024-02-08", "2024-01-15"])
        listed_ends = [end for _start, end in garmin.list_calls]
        self.assertIn("2024-02-10", listed_ends)
        self.assertTrue(all(end <= "2024-02-10" for end in listed_ends))
        created_dates = [
            event[1]["properties"]["Date"]["date"]["start"]
            for event in notion.events
            if event[0] == "page.create"
        ]
        self.assertEqual(created_dates, ["2024-02-10", "2024-01-15"])

    def test_incremental_activities_upsert_inside_the_window(self):
        os.environ[ACTIVITIES_DB_ENV] = "activities-db"
        notion = FakeNotion()
        notion.database["properties"] = {"Activity": {"type": "title"}}
        garmin = RecentActivities()
        with redirect_stdout(io.StringIO()):
            sync_activities(
                garmin,
                notion,
                mode="incremental",
                days=3,
                today=date(2024, 6, 2),
            )
        created = [event for event in notion.events if event[0] == "page.create"]
        self.assertEqual(len(created), 1)
        title = created[0][1]["properties"]["Activity"]["title"][0]["text"]["content"]
        self.assertEqual(title, "Today run")
        self.assertTrue(any(event[0] == "db.update" for event in notion.events))


class ContractTests(unittest.TestCase):
    def test_required_garmin_methods_exist(self):
        for name in (
            "get_sleep_data",
            "get_sleep_daily",
            "get_activities",
            "get_activities_by_date",
            "count_activities",
            "get_daily_summaries",
            "get_user_summary",
            "get_hrv_data",
            "get_hrv_data_range",
            "get_training_readiness",
            "get_training_readiness_range",
            "get_daily_body_battery_stats",
            "get_daily_stress_stats",
            "get_daily_intensity_minutes_stats",
            "get_daily_heart_rate_stats",
            "get_rhr_daily",
            "get_daily_steps",
            "get_daily_floors_stats",
            "get_calories_daily",
            "get_daily_respiration_stats",
            "get_weekly_training_status",
            "get_max_metrics_range",
        ):
            self.assertTrue(callable(getattr(Garmin, name)), name)

    def test_requirements_keep_the_notion_pin(self):
        text = (ROOT / "requirements.txt").read_text()
        self.assertIn("notion-client==2.2.1", text)
        self.assertIn("garminconnect>=0.3.20", text)

    def test_workflow_stays_incremental_on_the_schedule(self):
        text = (ROOT / ".github/workflows/sync-sleep.yml").read_text()
        self.assertIn('cron: "0 13 * * *"', text)
        self.assertIn("python sync_garmin.py", text)
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("\n  push:", text)
        self.assertIn("|| '3'", text)
        for name in (
            "GARMIN_EMAIL",
            "GARMIN_PASSWORD",
            "GARMIN_TOKEN_B64",
            "NOTION_TOKEN",
            "NOTION_SLEEP_DB",
            "NOTION_ACTIVITIES_DB",
            "NOTION_DAILY_DB",
            "NOTION_PARENT_PAGE",
        ):
            self.assertIn(name, text)
        script = text.split("run: |", 1)[1].split("env:", 1)[0]
        self.assertNotIn("GARMIN", script)
        self.assertNotIn("NOTION", script)

    def test_readme_explains_daily_and_backfill(self):
        text = (ROOT / "README.md").read_text()
        for phrase in (
            "13:00 UTC",
            "Full history",
            "NOTION_ACTIVITIES_DB",
            "NOTION_DAILY_DB",
            "NOTION_PARENT_PAGE",
            "NOTION_SLEEP_DB",
            "Intensity minutes",
            "HRV",
            "Stress duration",
            "SpO2",
            "Incremental (recent days)",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("ending yesterday", text)
        self.assertNotIn("Sleep stops at yesterday", text)
        self.assertNotIn("Sleep still ends yesterday", text)
        self.assertNotIn("Today is skipped", text)
        daily = text.split("## What happens every day", 1)[1].split("## ", 1)[0]
        self.assertIn("last 3 nights, including today", daily)
        history = text.split("## Full history", 1)[1].split("## ", 1)[0]
        self.assertIn("Sleep includes today", history)

    def test_repo_has_no_hardcoded_secret_material(self):
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {".git", "__pycache__", ".venv"} for part in path.parts):
                continue
            if path.suffix not in {".py", ".md", ".yml", ".yaml", ".txt"}:
                continue
            text = path.read_text()
            self.assertIsNone(re.search(r"\b[0-9a-fA-F]{32}\b", text), str(path))
            self.assertIsNone(re.search(r"ghp_[A-Za-z0-9]", text), str(path))
            self.assertIsNone(re.search(r"ntn_[A-Za-z0-9]", text), str(path))
            self.assertIsNone(re.search(r"secret_[A-Za-z0-9]{20,}", text), str(path))

    def test_errors_do_not_echo_credentials(self):
        self.assertEqual(safe_error(RuntimeError("bad password=example")), "RuntimeError")
        self.assertEqual(safe_error(RuntimeError("boom")), "boom")


def seed_sleep_db(notion):
    notion.database["properties"] = {
        "Night": {"type": "title"},
        "Date": {"type": "date"},
        "Duration (h)": {"type": "number"},
        "Sleep Score": {"type": "number"},
        "Deep (h)": {"type": "number"},
        "REM (h)": {"type": "number"},
        "Light (h)": {"type": "number"},
        "Compare period": {"type": "formula"},
        "Last 14 nights": {"type": "formula"},
        "Last 12 weeks": {"type": "formula"},
        "Week of": {"type": "formula"},
    }


class FakeNotion:
    def __init__(self):
        self.events = []
        self.child_blocks = []
        self.query_pages = []
        self.database = {"id": "db", "parent": {}, "properties": {}}
        self.databases = self.Databases(self)
        self.pages = self.Pages(self)
        self.blocks = self.Blocks(self)
        self._page_count = 0

    def search(self, **kwargs):
        self.events.append(("search", kwargs))
        return {"results": [], "has_more": False}

    class Databases:
        def __init__(self, outer):
            self.outer = outer

        def query(self, **kwargs):
            self.outer.events.append(("query", kwargs))
            return {"results": list(self.outer.query_pages), "has_more": False}

        def retrieve(self, **kwargs):
            self.outer.events.append(("retrieve", kwargs))
            return self.outer.database

        def create(self, **kwargs):
            self.outer.events.append(("db.create", kwargs))
            return {"id": "created-db"}

        def update(self, **kwargs):
            self.outer.events.append(("db.update", kwargs))
            props = self.outer.database.setdefault("properties", {})
            props.update(kwargs.get("properties") or {})
            return {"id": kwargs.get("database_id")}

    class Pages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.events.append(("page.create", kwargs))
            self.outer._page_count += 1
            return {"id": f"page-{self.outer._page_count}"}

        def update(self, **kwargs):
            self.outer.events.append(("page.update", kwargs))
            return {"id": kwargs.get("page_id")}

    class Blocks:
        def __init__(self, outer):
            self.outer = outer
            self.children = self.Children(outer)

        class Children:
            def __init__(self, outer):
                self.outer = outer

            def list(self, **kwargs):
                self.outer.events.append(("children", kwargs))
                return {"results": list(self.outer.child_blocks), "has_more": False}


class IncrementalSleep:
    def __init__(self, nights=None):
        self.calls = []
        self.nights = (
            {"2024-05-02": 7 * 3600, "2024-05-01": 3600}
            if nights is None
            else nights
        )

    def get_sleep_data(self, day):
        self.calls.append(("sleep", day))
        if day not in self.nights or not self.nights[day]:
            if day in self.nights:
                return {"dailySleepDTO": {"sleepTimeSeconds": 0}}
            return {}
        score = 81 if day == "2024-05-02" else 70
        return {
            "dailySleepDTO": {
                "sleepTimeSeconds": self.nights[day],
                "sleepScores": {"overall": {"value": score}},
            }
        }

    def get_sleep_daily(self, start, end):
        raise AssertionError("incremental sleep must not walk history")


class HistorySleep:
    def __init__(self):
        self.list_calls = []
        self.detail_calls = []

    def get_sleep_daily(self, start, end):
        self.list_calls.append((start, end))
        if start == "2024-02-01":
            return {
                "individualStats": [
                    {"calendarDate": "2024-02-10", "sleepTimeSeconds": 6 * 3600},
                    {"calendarDate": "2024-02-09", "sleepTimeSeconds": 0},
                    {"calendarDate": "2024-02-08", "sleepTimeSeconds": 5000},
                ]
            }
        if start == "2024-01-01":
            return {
                "individualStats": [
                    {"calendarDate": "2024-01-15", "sleepTimeSeconds": 100},
                    {"calendarDate": "2024-01-16", "sleepTimeSeconds": 0},
                ]
            }
        return {"individualStats": []}

    def get_sleep_data(self, day):
        self.detail_calls.append(("night", day))
        if day == "2024-02-10":
            return {
                "dailySleepDTO": {
                    "sleepTimeSeconds": 6 * 3600,
                    "sleepScores": {"overall": {"value": 74}},
                }
            }
        if day == "2024-01-15":
            return {
                "dailySleepDTO": {
                    "sleepTimeSeconds": 8 * 3600,
                    "deepSleepSeconds": 3600,
                    "remSleepSeconds": 7200,
                    "lightSleepSeconds": 4 * 3600,
                    "sleepScores": {"overall": {"value": 80}},
                }
            }
        return {}


class RecentActivities:
    def get_activities_by_date(self, start, end):
        return [
            {
                "activityId": 1,
                "activityName": "Today run",
                "startTimeLocal": "2024-06-02 08:00:00",
                "activityType": {"typeKey": "running"},
                "duration": 1800,
                "distance": 5000,
            },
            {
                "activityId": 2,
                "activityName": "Old run",
                "startTimeLocal": "2024-05-01 08:00:00",
                "activityType": {"typeKey": "running"},
                "duration": 1800,
            },
        ]


if __name__ == "__main__":
    unittest.main()
