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
from sync_sleep import hours, parse_night, sync_sleep
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

    def test_incremental_sleep_skips_an_empty_today(self):
        os.environ["NOTION_SLEEP_DB"] = "sleep-db"
        garmin = IncrementalSleep(nights={"2024-05-02": 0, "2024-05-01": 3600})
        notion = FakeNotion()
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
