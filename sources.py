"""yt-dlp wrapper: turns what the user typed into ``Track`` objects, and tracks
into playable stream URLs.

Two extraction modes are used:

* **flat** (``extract_flat``) — lists search results / playlist entries without
  resolving each video. Fast (~1–2 s), used for autocomplete, ``/play`` lookups
  and playlist links. Gives title/duration/uploader but *no* stream URL.
* **full** — resolves one video to a direct audio stream URL. Done lazily right
  before playback (and prefetched for the next track), because stream URLs
  expire after a few hours.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

import yt_dlp

log = logging.getLogger(__name__)

class _YtdlLogger:
    """Route yt-dlp's console output into logging (it prints errors even when quiet)."""

    def debug(self, msg: str) -> None:
        pass

    info = debug

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)  # callers log the DownloadError with context


YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "logger": _YtdlLogger(),
    "default_search": "ytsearch",
    "socket_timeout": 15,
    "retries": 3,
    # Let yt-dlp solve YouTube's signature/n challenges instead of falling back
    # to whichever JS-free client is still unblocked. Needs a JS runtime (deno)
    # on PATH; fetches the solver script from GitHub on first use.
    "remote_components": ["ejs:github"],
}

# Lists playlist entries without resolving each one. A single-video URL is
# still fully resolved (extract_flat only affects playlist entries).
FLAT_OPTIONS = {**YDL_OPTIONS, "extract_flat": "in_playlist"}

# Searches only need titles/ids, never formats or JS challenges — keep them lean.
SEARCH_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "logger": _YtdlLogger(),
    "extract_flat": True,
    "skip_download": True,
    "socket_timeout": 10,
}

MAX_PLAYLIST_TRACKS = 200
SEARCH_CACHE_TTL = 600      # seconds a search result list is reused
SEARCH_CACHE_SIZE = 256
STREAM_TTL = 3 * 3600       # YouTube stream URLs expire after ~6 h; refresh well before

# yt-dlp is blocking. Searches (fired on every autocomplete keystroke) get their
# own pool so they can never starve stream resolution for the player.
_search_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ytdl-search")
_resolve_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ytdl-resolve")

_UNAVAILABLE_TITLES = {"[Private video]", "[Deleted video]"}


class SourceError(Exception):
    """A user-facing problem resolving a query (no results, unavailable video…)."""


@dataclass(eq=False)
class Track:
    """One queue/playlist entry. Identity (``is``) is what distinguishes two
    entries of the same song, so ``eq`` is disabled on purpose."""

    url: str
    title: str = "Unknown"
    duration: int = 0
    uploader: str = ""
    thumbnail: str | None = None
    requester_id: int | None = None
    # --- transient, never persisted ---
    stream_url: str | None = field(default=None, repr=False)
    resolved_at: float = field(default=0.0, repr=False)
    start_at: float = field(default=0.0, repr=False)  # seek offset for the next play

    @property
    def has_stream(self) -> bool:
        return bool(self.stream_url) and time.monotonic() - self.resolved_at < STREAM_TTL

    def copy(self) -> "Track":
        return replace(self, stream_url=None, resolved_at=0.0, start_at=0.0)

    def to_dict(self, *, include_requester: bool = True) -> dict:
        data = {"url": self.url, "title": self.title, "duration": self.duration}
        if self.uploader:
            data["uploader"] = self.uploader
        if self.thumbnail:
            data["thumbnail"] = self.thumbnail
        if include_requester and self.requester_id:
            data["requester_id"] = self.requester_id
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        """Accepts the current format plus the legacy shapes: playlists stored
        ``{url, title, duration}`` and old sessions ``{webpage_url, title}``."""
        return cls(
            url=data.get("webpage_url") or data.get("url") or "",
            title=data.get("title") or "Unknown",
            duration=int(data.get("duration") or 0),
            uploader=data.get("uploader") or "",
            thumbnail=data.get("thumbnail"),
            requester_id=data.get("requester_id"),
        )

    @classmethod
    def from_info(cls, info: dict) -> "Track":
        """Build from a yt-dlp info dict (full or flat)."""
        thumb = info.get("thumbnail")
        if not thumb and info.get("thumbnails"):
            thumb = info["thumbnails"][-1].get("url")
        track = cls(
            url=info.get("webpage_url") or info.get("original_url") or info.get("url") or "",
            title=info.get("title") or "Unknown",
            duration=int(info.get("duration") or 0),
            uploader=info.get("uploader") or info.get("channel") or "",
            thumbnail=thumb,
        )
        # A fully-extracted video carries the stream URL already; keep it.
        if info.get("formats") and info.get("url"):
            track.stream_url = info["url"]
            track.resolved_at = time.monotonic()
        return track


def is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


def _clean_error(exc: Exception) -> str:
    """'ERROR: [youtube] abc123: Video unavailable' -> 'Video unavailable'."""
    msg = str(exc).replace("ERROR: ", "", 1)
    msg = re.sub(r"^\[[^\]]+\] [^:]+: ", "", msg)
    return msg.strip()[:200] or "Unknown error"


def _extract(options: dict, query: str) -> dict:
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(query, download=False)


async def _run(pool: ThreadPoolExecutor, options: dict, query: str) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(pool, functools.partial(_extract, options, query))


_search_cache: dict[str, tuple[float, list[Track]]] = {}


async def search(query: str, limit: int = 5) -> list[Track]:
    """Fast YouTube search. Results are cached and returned as fresh copies."""
    key = f"{limit}:{query.strip().casefold()}"
    hit = _search_cache.get(key)
    if hit and time.monotonic() - hit[0] < SEARCH_CACHE_TTL:
        return [t.copy() for t in hit[1]]

    try:
        info = await _run(_search_pool, SEARCH_OPTIONS, f"ytsearch{limit}:{query}")
    except yt_dlp.utils.DownloadError as exc:
        log.warning("Search failed for %r: %s", query, exc)
        return []

    tracks = [
        Track.from_info(e)
        for e in info.get("entries") or []
        if e and e.get("url") and e.get("title") not in _UNAVAILABLE_TITLES
    ]
    if len(_search_cache) >= SEARCH_CACHE_SIZE:
        _search_cache.pop(next(iter(_search_cache)))  # drop oldest
    _search_cache[key] = (time.monotonic(), tracks)
    return [t.copy() for t in tracks]


async def find(query: str) -> tuple[list[Track], str | None]:
    """Resolve a ``/play`` query into tracks.

    Returns ``(tracks, playlist_title)``; ``playlist_title`` is set when the
    query was a playlist link. Raises :class:`SourceError` with a readable
    message when nothing playable is found."""
    query = query.strip()
    if not is_url(query):
        results = await search(query, limit=1)
        if not results:
            raise SourceError(f"No results for **{query}**.")
        return results, None

    try:
        info = await _run(_resolve_pool, FLAT_OPTIONS, query)
    except yt_dlp.utils.DownloadError as exc:
        raise SourceError(_clean_error(exc)) from exc

    if info.get("_type") == "playlist" or "entries" in info:
        entries = [
            e for e in (info.get("entries") or [])
            if e and e.get("url") and e.get("title") not in _UNAVAILABLE_TITLES
        ]
        tracks = [Track.from_info(e) for e in entries[:MAX_PLAYLIST_TRACKS]]
        if not tracks:
            raise SourceError("That playlist is empty or unavailable.")
        return tracks, info.get("title") or "playlist"

    return [Track.from_info(info)], None


async def resolve(track: Track) -> None:
    """Fetch a fresh stream URL for ``track`` (filling in missing metadata).
    Raises :class:`SourceError` on failure."""
    query = track.url or f"ytsearch1:{track.title}"
    try:
        info = await _run(_resolve_pool, YDL_OPTIONS, query)
    except yt_dlp.utils.DownloadError as exc:
        raise SourceError(_clean_error(exc)) from exc
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise SourceError("No results.")
        info = entries[0]
    if not info.get("url"):
        raise SourceError("No playable audio stream.")

    track.stream_url = info["url"]
    track.resolved_at = time.monotonic()
    if not track.url:
        track.url = info.get("webpage_url") or ""
    if track.title == "Unknown" and info.get("title"):
        track.title = info["title"]
    if not track.duration and info.get("duration"):
        track.duration = int(info["duration"])
    if not track.uploader:
        track.uploader = info.get("uploader") or info.get("channel") or ""
    if info.get("thumbnail"):
        track.thumbnail = info["thumbnail"]
