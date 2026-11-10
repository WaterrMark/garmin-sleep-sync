"""Notion helpers: find or create a database, then upsert pages by a stable key.

Database ids come from the environment. When an id secret is missing, the sync
looks for a database with the expected title and otherwise creates one next to
Sleep (Garmin). The new id is printed so it can be saved as a GitHub secret.
Tokens are never printed.
"""

import os
import time

from notion_client import APIResponseError, Client

from garmin_auth import safe_error

ACTIVITIES_DB_ENV = "NOTION_ACTIVITIES_DB"
DAILY_DB_ENV = "NOTION_DAILY_DB"
ACTIVITIES_TITLE = "Activities (Garmin)"
DAILY_TITLE = "Daily Metrics (Garmin)"


def notion_client():
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        raise SystemExit("NOTION_TOKEN is not set.")
    return Client(auth=token)


def sleep_database_id():
    value = os.environ.get("NOTION_SLEEP_DB", "").strip()
    if not value:
        raise SystemExit(
            "NOTION_SLEEP_DB is not set. Add the Sleep (Garmin) database id as that secret."
        )
    return value


def call_notion(fn, *args, **kwargs):
    """Call the Notion API, backing off when it asks us to slow down."""
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except APIResponseError as exc:
            if exc.status == 401:
                raise SystemExit("Notion rejected NOTION_TOKEN. Check that secret.") from exc
            if exc.status in (429, 500, 502, 503) and attempt < 5:
                retry_after = 0
                try:
                    retry_after = float(exc.headers.get("Retry-After", 0))
                except (TypeError, ValueError):
                    retry_after = 0
                time.sleep(max(retry_after, min(2**attempt, 30)))
                continue
            raise
    raise RuntimeError("Notion retry loop ended")


def plain_title(obj):
    chunks = []
    for item in obj.get("title") or []:
        if isinstance(item, str):
            chunks.append(item)
            continue
        if isinstance(item, dict):
            chunks.append(
                item.get("plain_text") or (item.get("text") or {}).get("content") or ""
            )
    return "".join(chunks).strip()


def _create_help(title, env_name, detail):
    return (
        f'Could not create the Notion database "{title}". {detail}\n'
        "Share the page that contains Sleep (Garmin) with the same Notion integration, "
        f"or create the database yourself and set the {env_name} secret to its id. "
        "You can also set NOTION_PARENT_PAGE to a page the integration can edit, then run "
        "this again. The id is the long hexadecimal part of the database URL."
    )


def parent_page_id(notion):
    explicit = os.environ.get("NOTION_PARENT_PAGE", "").strip()
    if explicit:
        return explicit
    sleep_id = os.environ.get("NOTION_SLEEP_DB", "").strip()
    if not sleep_id:
        return None
    try:
        database = call_notion(notion.databases.retrieve, database_id=sleep_id)
    except APIResponseError as exc:
        print(f"Could not read the Sleep database ({safe_error(exc)}).", flush=True)
        return None
    parent = database.get("parent") or {}
    if parent.get("type") == "page_id" and parent.get("page_id"):
        return parent["page_id"]
    return None


def find_child_database(notion, parent, title):
    cursor = None
    while True:
        kwargs = {"block_id": parent, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        try:
            response = call_notion(notion.blocks.children.list, **kwargs)
        except APIResponseError as exc:
            print(f"Could not list the parent page ({safe_error(exc)}).", flush=True)
            return None
        for block in response.get("results") or []:
            if block.get("type") != "child_database":
                continue
            name = str((block.get("child_database") or {}).get("title") or "").strip()
            if name == title:
                return block.get("id")
        if not response.get("has_more"):
            return None
        cursor = response.get("next_cursor")


def find_databases_by_title(notion, title):
    matches = []
    cursor = None
    while True:
        kwargs = {
            "query": title,
            "filter": {"property": "object", "value": "database"},
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor
        response = call_notion(notion.search, **kwargs)
        for item in response.get("results") or []:
            if item.get("object") not in (None, "database"):
                continue
            if plain_title(item) == title and item.get("id"):
                matches.append(item)
        if not response.get("has_more"):
            return matches
        cursor = response.get("next_cursor")


def _choose_database(matches, title, parent):
    if not matches:
        return None
    if parent:
        for item in matches:
            if (item.get("parent") or {}).get("page_id") == parent:
                return item["id"]
    if len(matches) > 1:
        print(
            f'Found {len(matches)} Notion databases named "{title}". '
            "Using the first until you pin one with its secret.",
            flush=True,
        )
    return matches[0]["id"]


def _report_database(env_name, database_id, source):
    print(f"{source} {env_name}={database_id}", flush=True)
    print(
        f"Save that value as a GitHub Actions secret named {env_name} "
        "(Settings → Secrets and variables → Actions → New repository secret). "
        "Until you save it, later runs find this database by its title.",
        flush=True,
    )


def ensure_properties(notion, database_id, properties):
    current = call_notion(notion.databases.retrieve, database_id=database_id)
    have = current.get("properties") or {}
    title_name = next(name for name, schema in properties.items() if "title" in schema)
    if title_name not in have:
        existing = [
            name
            for name, schema in have.items()
            if isinstance(schema, dict) and schema.get("type") == "title"
        ]
        raise SystemExit(
            f'The Notion database title column is {existing or "missing"}, '
            f'but this sync writes "{title_name}". Rename that column, or let the '
            "sync create a new database."
        )
    missing = {
        name: schema
        for name, schema in properties.items()
        if name not in have and "title" not in schema
    }
    if missing:
        call_notion(notion.databases.update, database_id=database_id, properties=missing)
        print(f"Added {len(missing)} missing Notion columns.", flush=True)


def ensure_database(notion, *, env_name, title, properties, icon):
    configured = os.environ.get(env_name, "").strip()
    if configured:
        ensure_properties(notion, configured, properties)
        return configured

    parent = parent_page_id(notion)
    database_id = find_child_database(notion, parent, title) if parent else None
    if not database_id:
        database_id = _choose_database(find_databases_by_title(notion, title), title, parent)
    source = "found"
    if not database_id:
        if not parent:
            raise SystemExit(_create_help(title, env_name, "No parent page was available."))
        try:
            created = call_notion(
                notion.databases.create,
                parent={"type": "page_id", "page_id": parent},
                title=[{"type": "text", "text": {"content": title}}],
                icon={"type": "emoji", "emoji": icon},
                is_inline=False,
                properties=properties,
            )
        except APIResponseError as exc:
            raise SystemExit(_create_help(title, env_name, safe_error(exc))) from exc
        database_id = created["id"]
        source = "created"
    _report_database(env_name, database_id, source)
    ensure_properties(notion, database_id, properties)
    return database_id


def read_key(page, property_name):
    prop = (page.get("properties") or {}).get(property_name) or {}
    kind = prop.get("type")
    if kind == "date":
        start = (prop.get("date") or {}).get("start")
        return start[:10] if isinstance(start, str) else None
    if kind in ("rich_text", "title"):
        text = "".join(part.get("plain_text", "") for part in prop.get(kind) or []).strip()
        return text or None
    if kind == "number" and prop.get("number") is not None:
        number = prop["number"]
        if isinstance(number, float) and number.is_integer():
            return str(int(number))
        return str(number)
    return None


def load_key_index(notion, database_id, property_name):
    """Map an existing property value to its page id. Used by full-history runs."""
    index = {}
    duplicates = 0
    cursor = None
    while True:
        kwargs = {"database_id": database_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        response = call_notion(notion.databases.query, **kwargs)
        for page in response.get("results") or []:
            key = read_key(page, property_name)
            if not key:
                continue
            if key in index:
                duplicates += 1
                continue
            index[key] = page["id"]
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    if duplicates:
        print(
            f"Found {duplicates} extra Notion pages with a repeated {property_name}. "
            "The sync updates the first page and leaves the extras.",
            flush=True,
        )
    return index


def date_match(property_name, day):
    return {
        "value": day,
        "filter": {"property": property_name, "date": {"equals": day}},
    }


def text_match(property_name, value):
    return {
        "value": value,
        "filter": {"property": property_name, "rich_text": {"equals": value}},
    }


def upsert_page(notion, database_id, match, properties, cache=None):
    key = match["value"]
    page_id = None
    if cache is not None:
        page_id = cache.get(key)
    else:
        found = (
            call_notion(
                notion.databases.query,
                database_id=database_id,
                filter=match["filter"],
                page_size=1,
            ).get("results")
            or []
        )
        if found:
            page_id = found[0]["id"]
    if page_id:
        call_notion(notion.pages.update, page_id=page_id, properties=properties)
        return "updated"
    created = call_notion(
        notion.pages.create,
        parent={"database_id": database_id},
        properties=properties,
    )
    if cache is not None and created.get("id"):
        cache[key] = created["id"]
    return "created"
