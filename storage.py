"""Persistent, guild-scoped playlist storage backed by a JSON file.

Playlists are *shared* within a guild: anyone may view, edit, play, or delete
any playlist. All mutations are serialized behind a lock and written atomically.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

PLAYLISTS_FILE = "playlists.json"
MAX_NAME_LENGTH = 50

# { "<guild_id>": { "<playlist name>": {tracks, created_by, created_at} } }
_data: dict[str, dict] = {}
_lock = asyncio.Lock()


class PlaylistError(Exception):
    """Raised for expected, user-facing problems (duplicate name, missing playlist…)."""


def load() -> None:
    """Load playlists from disk into memory. Safe to call once at startup."""
    global _data
    if not os.path.exists(PLAYLISTS_FILE):
        _data = {}
        return
    try:
        with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f:
            _data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read %s (%s); starting with empty playlists.", PLAYLISTS_FILE, exc)
        _data = {}


def _save() -> None:
    """Atomically persist the in-memory store. Call while holding ``_lock``."""
    tmp = f"{PLAYLISTS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PLAYLISTS_FILE)


def _guild(gid: int) -> dict:
    return _data.setdefault(str(gid), {})


def _find(gid: int, name: str) -> str | None:
    """Return the stored playlist key matching ``name`` case-insensitively."""
    lowered = name.casefold()
    for key in _guild(gid):
        if key.casefold() == lowered:
            return key
    return None


# --- read helpers (no lock needed; reads are atomic enough for this use) ---

def names(gid: int) -> list[str]:
    return sorted(_guild(gid).keys(), key=str.casefold)


def list_playlists(gid: int) -> dict[str, dict]:
    return dict(_guild(gid))


def get(gid: int, name: str) -> dict | None:
    key = _find(gid, name)
    return _guild(gid)[key] if key else None


def resolve_name(gid: int, name: str) -> str | None:
    """The stored spelling of ``name`` (lookups are case-insensitive)."""
    return _find(gid, name)


def total_duration(playlist: dict) -> int:
    return sum(int(t.get("duration") or 0) for t in playlist["tracks"])


# --- mutations (lock-guarded, persisted) ---

def _new_playlist(user_id: int | None) -> dict:
    return {
        "tracks": [],
        "created_by": str(user_id) if user_id else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _valid_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise PlaylistError("Playlist name cannot be empty.")
    if len(name) > MAX_NAME_LENGTH:
        raise PlaylistError(f"Playlist names can be at most {MAX_NAME_LENGTH} characters.")
    return name


async def delete(gid: int, name: str) -> None:
    async with _lock:
        key = _find(gid, name)
        if not key:
            raise PlaylistError(f"No playlist named **{name}**.")
        del _guild(gid)[key]
        _save()


async def add_tracks(gid: int, name: str, tracks: list[dict], user_id: int | None = None) -> tuple[str, bool]:
    """Append tracks ({url, title, duration, …}), creating the playlist if it
    doesn't exist. Returns ``(resolved playlist name, created)``."""
    async with _lock:
        key = _find(gid, name)
        created = key is None
        if created:
            key = _valid_name(name)
            _guild(gid)[key] = _new_playlist(user_id)
        _guild(gid)[key]["tracks"].extend(tracks)
        _save()
        return key, created


async def remove_track(gid: int, name: str, index: int) -> dict:
    """Remove the track at zero-based ``index``; returns the removed track."""
    async with _lock:
        key = _find(gid, name)
        if not key:
            raise PlaylistError(f"No playlist named **{name}**.")
        tracks = _guild(gid)[key]["tracks"]
        if index < 0 or index >= len(tracks):
            raise PlaylistError("That track number is out of range.")
        removed = tracks.pop(index)
        _save()
        return removed
