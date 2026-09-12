#!/usr/bin/env python3
"""Build a local dashboard of models Cursor Auto routed historical queries to.

Reads ~/.cursor/chats/*/store.db. Stores timestamps, chat titles, and model
slugs only — never message text.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

CHATS_ROOT = Path.home() / ".cursor" / "chats"
OUT = Path(__file__).with_name("dashboard.html")

TS_RE = re.compile(r"<timestamp>([^<]+)</timestamp>")
MODEL_RE = re.compile(r'"modelName"\s*:\s*"([^"]+)"')


def parse_query_ts(raw: str) -> datetime | None:
    text = re.sub(r"\s*\([^)]+\)\s*$", "", raw.strip())
    text = re.sub(r"^[A-Za-z]+,\s*", "", text)
    for fmt in ("%b %d, %Y, %I:%M %p", "%B %d, %Y, %I:%M %p"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def ms_to_dt(value) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000)
    except (OSError, OverflowError, ValueError):
        return None


def blob_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return ""


def load_db_meta(con: sqlite3.Connection) -> dict:
    row = con.execute("select value from meta limit 1").fetchone()
    if not row or row[0] is None:
        return {}
    raw = row[0]
    if isinstance(raw, str):
        try:
            raw = bytes.fromhex(raw)
        except ValueError:
            return {}
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def iter_json_blobs(con: sqlite3.Connection):
    for (data,) in con.execute("select data from blobs order by rowid"):
        if isinstance(data, memoryview):
            data = data.tobytes()
        if not isinstance(data, (bytes, bytearray)):
            continue
        text = data.decode("utf-8", "replace").lstrip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj, text


def collect() -> dict:
    events = []
    chats = []
    scanned = 0
    unreadable = 0

    if not CHATS_ROOT.is_dir():
        raise SystemExit(f"No chat history at {CHATS_ROOT}")

    for dirpath, _dirs, files in os.walk(CHATS_ROOT):
        if "store.db" not in files:
            continue
        scanned += 1
        chat_dir = Path(dirpath)
        db_path = chat_dir / "store.db"
        file_meta = {}
        meta_path = chat_dir / "meta.json"
        if meta_path.is_file():
            try:
                file_meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                file_meta = {}

        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            unreadable += 1
            continue

        try:
            db_meta = load_db_meta(con)
            name = (db_meta.get("name") or "Untitled").strip() or "Untitled"
            chat_id = db_meta.get("agentId") or chat_dir.name
            created = ms_to_dt(db_meta.get("createdAt") or file_meta.get("createdAtMs"))
            updated = ms_to_dt(file_meta.get("updatedAtMs"))
            cwd = file_meta.get("cwd") or ""

            pending_ts = None
            last_ts = created
            models: list[str] = []
            seen_models: set[str] = set()
            query_count = 0

            for obj, raw in iter_json_blobs(con):
                role = obj.get("role")
                if role == "user":
                    text = blob_text(obj.get("content"))
                    if "<user_query>" not in text:
                        continue
                    match = TS_RE.search(text)
                    parsed = parse_query_ts(match.group(1)) if match else None
                    if parsed is None:
                        parsed = last_ts
                        approx = last_ts is not None
                    else:
                        approx = False
                        last_ts = parsed
                    pending_ts = (parsed, approx)
                elif role == "assistant":
                    found = MODEL_RE.findall(raw)
                    if not found or pending_ts is None:
                        continue
                    model = found[0]
                    when, approx = pending_ts
                    pending_ts = None
                    if model not in seen_models:
                        seen_models.add(model)
                        models.append(model)
                    query_count += 1
                    events.append(
                        {
                            "t": when.strftime("%Y-%m-%d %H:%M") if when else None,
                            "day": when.strftime("%Y-%m-%d") if when else None,
                            "approx": bool(approx or when is None),
                            "model": model,
                            "chat": name,
                            "cwd": cwd,
                            "id": chat_id,
                        }
                    )
        except sqlite3.Error:
            unreadable += 1
            continue
        finally:
            con.close()

        chats.append(
            {
                "id": chat_id,
                "name": name,
                "cwd": cwd,
                "created": created.strftime("%Y-%m-%d %H:%M") if created else None,
                "updated": updated.strftime("%Y-%m-%d %H:%M") if updated else None,
                "models": models,
                "queries": query_count,
            }
        )

    events.sort(key=lambda e: e["t"] or "", reverse=True)
    chats.sort(key=lambda c: c["updated"] or c["created"] or "", reverse=True)

    days = sorted({e["day"] for e in events if e["day"]})
    models = []
    seen = set()
    for event in events:
        if event["model"] not in seen:
            seen.add(event["model"])
            models.append(event["model"])
    # stable order by volume
    counts: dict[str, int] = {}
    for event in events:
        counts[event["model"]] = counts.get(event["model"], 0) + 1
    models.sort(key=lambda m: (-counts[m], m))

    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scanned": scanned,
        "unreadable": unreadable,
        "events": events,
        "chats": chats,
        "models": models,
        "days": days,
    }


def render(data: dict) -> str:
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    template = Path(__file__).with_name("template.html").read_text()
    return template.replace("__DATA__", payload, 1)



def main() -> None:
    data = collect()
    OUT.write_text(render(data))
    print(f"Wrote {OUT}")
    print(f"queries={len(data['events'])} chats={len(data['chats'])} models={len(data['models'])}")


if __name__ == "__main__":
    main()
