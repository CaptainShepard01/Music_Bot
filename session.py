"""Persistent, guild-scoped playback-session storage backed by a JSON file.

Each guild has two independent slots:

* ``live``  — the queue that is playing *right now*, rewritten on every change.
  If the bot crashes or restarts, it rejoins and resumes this automatically.
* ``saved`` — a queue parked by the ⏹️ Stop button, everyone leaving, or a failed
  reconnect. Restored only on request (``/continue`` or the Resume button), and
  never overwritten by new playback.

A snapshot looks like::

    {voice_channel_id, text_channel_id, tracks: [track dicts, current first],
     position: seconds into tracks[0], loop, volume, updated_at}
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SESSIONS_FILE = "sessions.json"

# { "<guild_id>": {"live": snapshot | None, "saved": snapshot | None} }
_data: dict[str, dict] = {}
_lock = asyncio.Lock()


def _migrate(entry: dict) -> dict:
    """Convert the legacy flat format ({current, queue, auto_resume, …})."""
    if "live" in entry or "saved" in entry:
        return entry
    tracks = ([entry["current"]] if entry.get("current") else []) + list(entry.get("queue") or [])
    snap = {
        "voice_channel_id": entry.get("voice_channel_id"),
        "text_channel_id": entry.get("text_channel_id"),
        "tracks": tracks,
        "position": 0,
        "updated_at": entry.get("updated_at"),
    }
    if not tracks:
        return {"live": None, "saved": None}
    slot = "live" if entry.get("auto_resume") else "saved"
    return {"live": None, "saved": None, slot: snap}


def load() -> None:
    """Load sessions from disk into memory. Safe to call once at startup."""
    global _data
    if not os.path.exists(SESSIONS_FILE):
        _data = {}
        return
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        _data = {gid: _migrate(entry) for gid, entry in raw.items()}
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read %s (%s); starting with no saved sessions.", SESSIONS_FILE, exc)
        _data = {}


def _save() -> None:
    """Atomically persist the in-memory store. Call while holding ``_lock``."""
    tmp = f"{SESSIONS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSIONS_FILE)


def _stamp(snapshot: dict) -> dict:
    return {**snapshot, "updated_at": datetime.now(timezone.utc).isoformat()}


def _slot(gid: int) -> dict:
    return _data.setdefault(str(gid), {"live": None, "saved": None})


# --- read helpers (no lock needed; reads are atomic enough for this use) ---

def get_saved(gid: int) -> dict | None:
    entry = _data.get(str(gid))
    return entry.get("saved") if entry else None


def all_live() -> dict[str, dict]:
    return {gid: e["live"] for gid, e in _data.items() if e.get("live")}


# --- mutations (lock-guarded, persisted) ---

async def save_live(gid: int, snapshot: dict) -> None:
    async with _lock:
        _slot(gid)["live"] = _stamp(snapshot)
        _save()


async def clear_live(gid: int) -> None:
    async with _lock:
        entry = _data.get(str(gid))
        if entry and entry.get("live") is not None:
            entry["live"] = None
            _save()


async def park(gid: int, snapshot: dict) -> None:
    """Store ``snapshot`` as the saved queue and clear the live slot."""
    async with _lock:
        entry = _slot(gid)
        entry["saved"] = _stamp(snapshot)
        entry["live"] = None
        _save()


async def clear_saved(gid: int) -> None:
    async with _lock:
        entry = _data.get(str(gid))
        if entry and entry.get("saved") is not None:
            entry["saved"] = None
            _save()
