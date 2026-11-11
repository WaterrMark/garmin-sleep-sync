# Garmin to Notion

This syncs three kinds of Garmin Connect data into Notion:

- **Sleep** — the existing Sleep (Garmin) database
- **Activities** — runs, rides, strength sessions, and anything else Garmin lists
- **Daily metrics** — resting heart rate, HRV, steps, intensity minutes, stress, Body Battery, and training readiness

A run is safe to repeat. Sleep and daily metrics match an existing page by date. Activities match by Garmin's activity id. A second run updates those pages instead of creating duplicates.

The automatic run stays small. Once a day it refreshes the last 3 days. A full history pull is a separate button you press once.

## What happens every day

GitHub Actions runs **Sync Garmin to Notion** at 13:00 UTC (21:00 in Hong Kong).

That run is incremental:

- **Sleep** is the last 3 nights, including today. Garmin dates a completed night on the morning you wake, so last night's sleep is stored on today's date, the same day as that night's HRV. A night with no recorded sleep is skipped and does not create a page.
- **Activities** are anything that started in the last 3 days, including today, so a morning workout shows up the same day.
- **Daily metrics** are today and the previous 2 days. Today's step count updates on the next runs, and yesterday is written again after it has settled.

You do not need to do anything for this daily run once the secrets below are saved.

Dates follow the GitHub runner clock, which is UTC. At 13:00 UTC that date is the same calendar day as Hong Kong.

## One-time setup

Sleep already has a database and its id is already saved as `NOTION_SLEEP_DB`. Activities and daily metrics need their own databases. The sync can create them.

1. In the GitHub repo, open **Actions**.
2. Open **Sync Garmin to Notion**.
3. Click **Run workflow**.
4. Leave **mode** as **Incremental (recent days)**, **datasets** as **All**, and **sync_days** as **3**.
5. Run it and open the log when it finishes.

The first run creates two databases next to Sleep (Garmin), or reuses them if they already exist:

- **Activities (Garmin)**
- **Daily Metrics (Garmin)**

The log prints one line per database, for example:

```text
created NOTION_ACTIVITIES_DB=<the id>
created NOTION_DAILY_DB=<the id>
```

If a database with that title already exists, the line starts with `found` instead of `created`. Copy the id either way.

6. Copy each id into a repository secret. Go to **Settings → Secrets and variables → Actions → New repository secret**.

| Secret | What to paste |
| --- | --- |
| `NOTION_ACTIVITIES_DB` | The id printed for Activities (Garmin) |
| `NOTION_DAILY_DB` | The id printed for Daily Metrics (Garmin) |

Do not paste the token or your Garmin password into the repo, the workflow file, or a pull request. The id is not a token, but it should still live in secrets so the sync keeps using the same database if you rename it.

Until those secrets exist, later runs look up the databases by those exact titles. Saving the secrets is what pins them.

### If the databases are not created

The integration can only create a database on a page it can edit. Either:

- Open the Notion page that contains Sleep (Garmin) and share that page with the same integration that owns `NOTION_TOKEN`, then run the workflow again, or
- Create a page, share it with the integration, and save that page's id as the optional secret `NOTION_PARENT_PAGE`. The id is the long hexadecimal part of the page URL. Run the workflow again.

You can also create the two databases by hand. The title column has to be named **Activity** or **Day** (see the column lists below). Save their ids as the secrets above. On the next run, any missing columns are added. Columns you already have are left alone.

## Full history

Do this once, after the incremental run above has created the databases.

1. **Actions → Sync Garmin to Notion → Run workflow**.
2. Set **mode** to **Full history**.
3. Set **datasets** to **All**, or to one dataset if you only want that.
4. Leave **sync_days** at 3. Full history ignores it.
5. Run it. This one is much longer than the daily run, because it asks Garmin for older days one month at a time and then writes a Notion page for each day or activity. You only need to do it once.

What "full history" means:

- **Activities:** every activity Garmin still returns, newest first, until the list runs out. There is no year cutoff.
- **Sleep and daily metrics:** every month from today back through January 2007, which is as far back as Garmin Connect's daily history goes. Sleep includes today when Garmin has a completed night for that date. If an activity on the account is older than January 2007, that activity's date becomes the far end of the walk. Months with no real data are not written to Notion.

Empty stretches are fine. A month you did not wear the watch does not become a stack of blank pages, and it does not stop the walk. The walk still checks the older months.

## Refresh a specific number of days

This is the same idea as the earlier sleep backfill, and it now covers whichever datasets you pick.

1. **Actions → Sync Garmin to Notion → Run workflow**.
2. Set **mode** to **Incremental (recent days)**.
3. Set **datasets** to the data you want.
4. Set **sync_days**. `60` refreshes the last 60 days, including today. Sleep still skips a night that has no recorded sleep. Activities and daily metrics include today as well.

The scheduled run does not read this number. It stays at 3.

To fill Daily Metrics columns that were empty, or to add the new sleep columns, run this once with **datasets** set to **Daily metrics** or **Sleep** and **sync_days** large enough to cover the pages you already have (about 800 covers November 2024 through November 2026). Leave **mode** on **Incremental (recent days)**. That updates existing pages and does not pull activities. Use **Full history** with only that dataset if you also want months older than the current pages. Full history of daily metrics or sleep still asks Garmin once for the oldest activity date, and it does not rewrite activity pages when activities are not selected.

## Secrets

These are GitHub Actions secrets. The scripts only read them from the environment.

| Secret | Required | Used for |
| --- | --- | --- |
| `GARMIN_EMAIL` and `GARMIN_PASSWORD` | One Garmin login | Accounts without multi-factor authentication |
| `GARMIN_TOKEN_B64` | One Garmin login | Saved session if the account uses MFA |
| `NOTION_TOKEN` | Yes | Notion integration token |
| `NOTION_SLEEP_DB` | Yes for sleep | Existing Sleep (Garmin) database |
| `NOTION_ACTIVITIES_DB` | After the first activities run | Activities (Garmin) |
| `NOTION_DAILY_DB` | After the first daily run | Daily Metrics (Garmin) |
| `NOTION_PARENT_PAGE` | No | Page where new databases should be created, if the sleep page's parent is not shared |

`notion-client` stays pinned at `2.2.1`. `garminconnect` needs `0.3.20` or newer because the daily summary, HRV, and readiness range calls come from that version.

## Sleep columns

The page title is the night's date. The original columns are unchanged:

- Night, Date
- Duration (h), Sleep Score, Deep (h), REM (h), Light (h)

These are added from the same nightly `get_sleep_data` payload. A value Garmin omits is left off the page, so a later run does not wipe it.

- Awake (h), Awakenings
- Bed time, Wake time (the watch's local clock, stored as text)
- Sleep HR, Sleep HRV (ms), Sleep SpO2
- Respiration avg, Respiration low, Respiration high
- Duration score, Deep score, REM score, Light score, Restlessness, Sleep stress score

Formula columns already on Sleep (Garmin) are not part of the sync and are not rewritten: Compare period, Last 14 nights, Last 12 weeks, Week of.

A night Garmin did not score is skipped. Stages Garmin omits are left empty. Bed time and wake time come from `sleepStartTimestampLocal` and `sleepEndTimestampLocal`. Garmin stores that local clock as a timestamp, so the sync formats it as the watch time instead of letting Notion move it into another timezone.

## Activity columns

The page title is the activity name. Rows match on **Activity ID**.

The values come from Garmin's activity list, which is the summary `garminconnect` exposes. The sync converts Garmin's meters to kilometers and meters per second to kilometers per hour. Start time is the watch's local time, stored as text so Notion does not shift it into another timezone. Pace is minutes per kilometer and is left blank under 100 meters.

- When: Date, Start, Type, Location
- Effort: Duration (min), Moving (min), Distance (km), Pace (min/km), Avg speed (km/h), Max speed (km/h), Calories, Elevation gain (m), Elevation loss (m), Steps, Avg cadence, Sets, Reps, Volume
- Training: Avg HR, Max HR, Avg power (W), Normalized power (W), Max power (W), Aerobic effect, Anaerobic effect, Training load, Training effect, VO2 max

A column stays blank when that activity has no such value (a strength session often has no distance; a run often has no power). Activity **Steps** are the steps counted inside that workout. They are not the day's step total; that number is on Daily Metrics. GPS tracks, laps, and per-second heart-rate streams are not synced.

## Daily metric columns

The page title is the date. One page per day.

- Heart: Resting HR, Min HR, Max HR, HRV (ms), HRV status, HRV weekly avg
- Movement: Steps, Step goal, Distance (km), Floors, Floors descended, Moderate intensity (min), Vigorous intensity (min), Intensity minutes
- Energy and stress: Active calories, BMR calories, Total calories, Avg stress, Max stress
- Recovery: Body Battery, Body Battery high, Body Battery low, Body Battery charged, Body Battery drained, Training readiness, Readiness level, Recovery time (h), Recovery
- Daily stats: Respiration avg, Respiration low, Respiration high, SpO2 avg, SpO2 low, SpO2 latest, Hydration (ml), Hydration goal (ml), Training status, Acute load, Daily VO2 max

`get_daily_summaries` returns Garmin's v2 daily summary. Steps, calories, heart rate, stress, Body Battery, respiration, SpO2, and hydration are grouped (`movement`, `calories`, `heartRate`, and so on), not stored under the flat names `totalSteps` or `averageStressLevel`. The sync copies those groups onto the flat names before it writes the page. A flat `get_user_summary` row still works, and a flat value that is already filled is kept.

If a value is missing or null after that, one range call for the same dates fills it. A key that is present and null is still missing. Stat rows shaped as `{calendarDate, values: {...}}` are unwrapped. The range calls are `get_daily_steps`, `get_daily_floors_stats`, `get_calories_daily`, `get_daily_heart_rate_stats`, `get_rhr_daily`, `get_daily_stress_stats`, `get_daily_body_battery_stats`, and `get_daily_respiration_stats`. They are skipped when the summary already has the value.

**Intensity minutes** are Garmin's own total: moderate minutes plus vigorous minutes counted twice. Moderate and vigorous are stored as well. If Garmin sends neither, Intensity minutes is left blank.

**HRV** is last night's average from `get_hrv_data_range`. It is only requested for a month that already has a worn-watch day in the daily summary. If the watch or that night has no HRV, the HRV columns stay blank.

**Training readiness** is the morning snapshot when Garmin marks one (`AFTER_WAKEUP_RESET`). Otherwise the latest snapshot that day is used. Recovery time is that snapshot's minutes, converted to hours. When Garmin says the recovery clock has finished (`REACHED_ZERO`), Recovery time is 0 and Recovery says "Fully recovered", because the minute count Garmin still sends can be the previous value.

**Body Battery** is the latest reading (`bodyBatteryMostRecentValue`, or `bodyBattery.latestValue` on the v2 summary). The library's typed `DailyStats` model does not list that field; the live summary does. When the latest reading is absent, the column uses the end-of-day reading (`endOfDayValue`, the last level before sleep). High, low, charged, and drained are stored beside it. Average and max stress are Garmin's stress levels.

**Training status** comes from `get_weekly_training_status`, one call for the whole window. That payload has one row per day. The text is Garmin's feedback phrase (`MAINTAINING_2` is stored as "Maintaining"). When more than one device reports the day, the primary training device is kept. Acute load is that row's acute training load. The numeric status code is not stored, because the same number has meant different phrases.

**Daily VO2 max** comes from `get_max_metrics_range`. It is the generic running and walking estimate (`vo2MaxPreciseValue`), on the date Garmin stamps on that estimate. It is not the VO2 max stored on an activity, and cycling VO2 max is not copied into this column.

**Distance (km)** on a v2 summary is the step distance Garmin reports with the step count (`movement.steps.distanceInMeters`). A flat summary's `totalDistanceMeters` is used when that field is already present.

**Stress duration** is not stored. The flat summary documents those fields as seconds, the v2 summary names them milliseconds, and the stress stats chart documents them as seconds.

**What is not synced:** per-minute steps, heart rate, stress, Body Battery, and SpO2 streams; weight; blood pressure; cycling VO2 max; sedentary and active seconds. Weight and blood pressure are sparse and would need another database. SpO2 averages on the daily page come from the summary's `pulseOx` group, not from the per-day SpO2 feed, because that feed is the intraday chart. SpO2 acclimation is not stored either: `get_spo2_acclimation_data` includes that hourly chart, and `get_acclimation_stats` is not in the published model, so its field names were not guessed. Sedentary and active seconds exist on the flat summary only. The v2 summary this job reads does not include them.

A day is skipped when it looks like a placeholder: zeros and null heart rate, stress, Body Battery, steps, and intensity. Total calories alone do not count, because Garmin fills those from your profile even when you did not wear the watch. A zero stress level does not count either; Garmin uses zero for "not scored".

## Running it on your own machine

The GitHub Action is the normal path. To run the same entry point locally, export the same variables and then:

```bash
pip install -r requirements.txt
python sync_garmin.py
```

Useful overrides:

```bash
SYNC_MODE=incremental SYNC_DAYS=60 DATASETS=sleep python sync_garmin.py
SYNC_MODE=backfill DATASETS=all python sync_garmin.py
DATASETS=activities python sync_activities.py
```

`SYNC_MODE=backfill` is the full history walk. Leaving it unset, or setting `incremental`, is the short window.

## When a column stays empty

That metric was not in Garmin's response for that day or activity. The sync does not fill in a guess. Typical reasons:

- The watch did not record HRV, Body Battery, or training readiness.
- The activity type has no distance, power, or cadence.
- The account's Garmin response uses a shape the range call does not document. Those values are skipped rather than mapped to the wrong column.
- The run logged `HRV is not available from Garmin` or the same sentence for another metric. The rest of the sync still finishes.
