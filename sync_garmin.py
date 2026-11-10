"""Sync Garmin sleep, activities, and daily metrics into Notion.

The daily GitHub Action runs this in incremental mode for the last 3 days.
A manual run can ask for full history, or for just one of the three datasets.

    SYNC_MODE     incremental (default) or backfill
    DATASETS      all (default), sleep, activities, daily
    SYNC_DAYS     incremental window, default 3. Ignored for full history.
"""

from datetime import date

from garmin_auth import garmin_client, is_auth_error, safe_error
from notion_store import notion_client
from sync_activities import fetch_all_activities, oldest_activity_date, oldest_date_in, sync_activities
from sync_daily import sync_daily
from sync_sleep import sync_sleep
from sync_window import describe_run, parse_datasets, parse_days, parse_mode


def main():
    mode = parse_mode()
    days = parse_days()
    datasets = parse_datasets()
    today = date.today()
    print(f"Garmin sync: {describe_run(mode, days, datasets)}", flush=True)

    garmin = garmin_client()
    notion = notion_client()

    prefetched = None
    oldest = None
    if mode == "backfill":
        try:
            if "activities" in datasets:
                prefetched = fetch_all_activities(garmin)
                oldest = oldest_date_in(prefetched)
            elif "sleep" in datasets or "daily" in datasets:
                oldest = oldest_activity_date(garmin)
        except Exception as exc:
            if is_auth_error(exc):
                raise
            print(f"Could not read Garmin activity history ({safe_error(exc)}).", flush=True)
            prefetched = None
        if oldest:
            print(f"Oldest Garmin activity: {oldest.isoformat()}", flush=True)

    failures = []
    if "sleep" in datasets:
        try:
            sync_sleep(
                garmin,
                notion,
                mode=mode,
                days=days,
                today=today,
                oldest_activity=oldest,
            )
        except SystemExit as exc:
            if "NOTION_TOKEN" in str(exc):
                raise
            failures.append(f"Sleep: {exc}")
        except Exception as exc:
            if is_auth_error(exc):
                failures.append("Sleep: Garmin rejected the login.")
            else:
                failures.append(f"Sleep: {safe_error(exc)}")

    if "activities" in datasets:
        try:
            sync_activities(
                garmin,
                notion,
                mode=mode,
                days=days,
                today=today,
                prefetched=prefetched,
            )
        except SystemExit as exc:
            if "NOTION_TOKEN" in str(exc):
                raise
            failures.append(f"Activities: {exc}")
        except Exception as exc:
            if is_auth_error(exc):
                failures.append("Activities: Garmin rejected the login.")
            else:
                failures.append(f"Activities: {safe_error(exc)}")

    if "daily" in datasets:
        try:
            sync_daily(
                garmin,
                notion,
                mode=mode,
                days=days,
                today=today,
                oldest_activity=oldest,
            )
        except SystemExit as exc:
            if "NOTION_TOKEN" in str(exc):
                raise
            failures.append(f"Daily metrics: {exc}")
        except Exception as exc:
            if is_auth_error(exc):
                failures.append("Daily metrics: Garmin rejected the login.")
            else:
                failures.append(f"Daily metrics: {safe_error(exc)}")

    if failures:
        print("Sync finished with errors:", flush=True)
        for line in failures:
            print(line, flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
