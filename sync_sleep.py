"""
Garmin -> Notion sleep sync.

Pulls the last few nights of sleep from Garmin Connect and writes them into the
"Sleep (Garmin)" database in Notion. Safe to run repeatedly: it updates an
existing night rather than creating duplicates.

Reads everything from environment variables (set as GitHub secrets):
    GARMIN_TOKEN_B64   base64 token from garmin_login.py
    NOTION_TOKEN       your Notion internal integration secret
    NOTION_SLEEP_DB    the Sleep database id (default already filled in below)
    SYNC_DAYS          how many past days to sync each run (default 3)

Property names below must match the Notion database exactly.
"""

import base64
import io
import os
import tarfile
from datetime import date, timedelta

from garminconnect import Garmin
from notion_client import Client

TOKEN_DIR = os.path.expanduser("~/.garminconnect")
NOTION_SLEEP_DB = os.environ.get("NOTION_SLEEP_DB", "8f64e4bca84e47eeb087f325b7d8f027")
SYNC_DAYS = int(os.environ.get("SYNC_DAYS", "3"))


def restore_garmin_token():
    """Unpack the base64 token secret into the local token directory."""
    token_b64 = os.environ["GARMIN_TOKEN_B64"]
    raw = base64.b64decode(token_b64)
    os.makedirs(TOKEN_DIR, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(TOKEN_DIR)


def garmin_client():
    # Simple path: email + password set as secrets (works if your Garmin
    # account has no multi-factor authentication).
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if email and password:
        garmin = Garmin(email=email, password=password)
        garmin.login()
        return garmin

    # Secure path: a token created once with garmin_login.py (needed if you use MFA).
    if os.environ.get("GARMIN_TOKEN_B64"):
        restore_garmin_token()
        garmin = Garmin()
        garmin.login(TOKEN_DIR)  # resumes the saved session, no password needed
        return garmin

    raise SystemExit(
        "No Garmin credentials found. Set either GARMIN_EMAIL + GARMIN_PASSWORD, "
        "or GARMIN_TOKEN_B64."
    )


def hours(seconds):
    if not seconds:
        return None
    return round(seconds / 3600.0, 2)


def parse_night(raw):
    """Turn Garmin's sleep payload into the fields our Notion db expects."""
    dto = (raw or {}).get("dailySleepDTO") or {}
    total = dto.get("sleepTimeSeconds")
    if not total:  # no real sleep recorded that night
        return None

    scores = dto.get("sleepScores") or {}
    overall = (scores.get("overall") or {}).get("value")

    return {
        "duration_h": hours(total),
        "score": overall,
        "deep_h": hours(dto.get("deepSleepSeconds")),
        "rem_h": hours(dto.get("remSleepSeconds")),
        "light_h": hours(dto.get("lightSleepSeconds")),
    }


def num(value):
    return {"number": value} if value is not None else {"number": None}


def upsert_night(notion, day_str, night):
    existing = notion.databases.query(
        database_id=NOTION_SLEEP_DB,
        filter={"property": "Date", "date": {"equals": day_str}},
    ).get("results", [])

    properties = {
        "Night": {"title": [{"text": {"content": day_str}}]},
        "Date": {"date": {"start": day_str}},
        "Duration (h)": num(night["duration_h"]),
        "Sleep Score": num(night["score"]),
        "Deep (h)": num(night["deep_h"]),
        "REM (h)": num(night["rem_h"]),
        "Light (h)": num(night["light_h"]),
    }

    if existing:
        notion.pages.update(page_id=existing[0]["id"], properties=properties)
        return "updated"
    notion.pages.create(parent={"database_id": NOTION_SLEEP_DB}, properties=properties)
    return "created"


def main():
    garmin = garmin_client()
    notion = Client(auth=os.environ["NOTION_TOKEN"])

    for offset in range(1, SYNC_DAYS + 1):
        day = date.today() - timedelta(days=offset)
        day_str = day.isoformat()
        try:
            raw = garmin.get_sleep_data(day_str)
        except Exception as exc:  # keep going even if one day fails
            print(f"{day_str}: could not fetch ({exc})")
            continue

        night = parse_night(raw)
        if night is None:
            print(f"{day_str}: no sleep data, skipped")
            continue

        result = upsert_night(notion, day_str, night)
        print(f"{day_str}: {result}  ({night['duration_h']}h, score {night['score']})")


if __name__ == "__main__":
    main()
