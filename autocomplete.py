"""Slash-command autocomplete providers.

Discord drops an autocomplete response that takes longer than 3 s, so live
YouTube search is capped at ``SEARCH_TIMEOUT``; a search that runs over keeps
going in the background and lands in the cache for the next keystroke.
"""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands

import player
import sources
import storage
import views
from views import fmt_time

# Value prefix marking a saved playlist picked from /play's suggestions.
PLAYLIST_PREFIX = "playlist::"
SEARCH_TIMEOUT = 2.4
MIN_SEARCH_LENGTH = 3

Choice = app_commands.Choice


def _label(title: str, extra: str = "", suffix: str = "") -> str:
    text = f"{title} — {extra}" if extra else title
    room = 100 - len(suffix)
    if len(text) > room:
        text = text[: room - 1] + "…"
    return text + suffix


def _consume(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()  # mark retrieved so a late failure isn't logged as "never retrieved"


async def _search(query: str) -> list[sources.Track]:
    task = asyncio.ensure_future(sources.search(query))
    task.add_done_callback(_consume)
    try:
        return await asyncio.wait_for(asyncio.shield(task), SEARCH_TIMEOUT)
    except (asyncio.TimeoutError, Exception):
        return []


def _playlist_choices(guild_id: int, text: str, limit: int) -> list[Choice[str]]:
    playlists = storage.list_playlists(guild_id)
    names = [n for n in storage.names(guild_id) if text.casefold() in n.casefold()][:limit]
    return [
        Choice(name=_label(f"📁 Playlist: {n}", suffix=f" ({len(playlists[n]['tracks'])} tracks)"),
               value=(PLAYLIST_PREFIX + n)[:100])
        for n in names
    ]


async def _track_choices(text: str) -> list[Choice[str]]:
    if sources.is_url(text):
        return [Choice(name=_label(f"🔗 {text}"), value=text)] if len(text) <= 100 else []
    if len(text) < MIN_SEARCH_LENGTH:
        return []
    return [
        Choice(name=_label(t.title, t.uploader, f" ({fmt_time(t.duration)})" if t.duration else ""),
               value=t.url)
        for t in await _search(text)
        if t.url and len(t.url) <= 100
    ]


async def play_query(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
    """/play: live YouTube results plus matching saved playlists."""
    text = current.strip()
    if not text:
        return _playlist_choices(interaction.guild_id, "", 25)
    choices = await _track_choices(text)
    return (choices + _playlist_choices(interaction.guild_id, text, 5))[:25]


async def track_query(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
    """Live YouTube results only (e.g. adding to a playlist)."""
    return (await _track_choices(current.strip()))[:25]


async def playlist_name(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
    text = current.strip()
    playlists = storage.list_playlists(interaction.guild_id)
    return [
        Choice(name=_label(f"📁 {n}", suffix=f" · {len(playlists[n]['tracks'])} tracks"), value=n[:100])
        for n in storage.names(interaction.guild_id)
        if text.casefold() in n.casefold()
    ][:25]


async def playlist_name_or_new(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
    """Existing playlists, plus a "create" entry when the typed name is new."""
    choices = await playlist_name(interaction, current)
    text = " ".join(current.split())
    if text and storage.resolve_name(interaction.guild_id, text) is None:
        choices.insert(0, Choice(name=_label(f"➕ New playlist “{text}”"), value=text[:100]))
    return choices[:25]


async def playlist_track(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
    name = getattr(interaction.namespace, "name", None)
    pl = storage.get(interaction.guild_id, name) if name else None
    if not pl:
        return []
    text = current.casefold()
    out = []
    for i, t in enumerate(pl["tracks"]):
        label = _label(f"{i + 1}. {t.get('title', 'Unknown')}")
        if text in label.casefold():
            out.append(Choice(name=label, value=str(i)))
            if len(out) == 25:
                break
    return out


async def queue_position(interaction: discord.Interaction, current: str) -> list[Choice[int]]:
    gp = player.find(interaction.guild_id)
    if gp is None:
        return []
    text = current.casefold()
    out = []
    for i, t in enumerate(gp.queue, 1):
        label = _label(f"{i}. {t.title}")
        if text in label.casefold():
            out.append(Choice(name=label, value=i))
            if len(out) == 25:
                break
    return out


async def seek_position(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
    """Quick jumps relative to the current position, plus whatever was typed."""
    gp = player.find(interaction.guild_id)
    if gp is None or gp.current is None or not gp.current.duration:
        return []
    pos, length = gp.position(), gp.current.duration
    shortcuts = [("⏪ Back 10 s", "-10"), ("⏩ Forward 30 s", "+30"),
                 ("⏩ Forward 1 min", "+60"), ("⏮ Restart", "0")]
    choices = []
    text = current.strip()
    parsed = views.parse_time(text) if text else None
    if parsed is not None:
        seconds, relative = parsed
        target = pos + seconds if relative else seconds
        target = min(max(0, target), length)
        choices.append(Choice(name=f"Jump to {fmt_time(target)}", value=text[:100]))
    choices += [Choice(name=f"{label} → {fmt_time(min(max(0, pos + int(v)) if v[0] in '+-' else 0, length))}",
                       value=v) for label, v in shortcuts]
    choices.append(Choice(name=f"Now at {fmt_time(pos)} of {fmt_time(length)} — type a time like 1:30",
                          value=str(int(pos))))
    return choices[:25]
