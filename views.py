"""Everything the user sees: formatting helpers, embeds and interactive views.

* ``PlayerControls``  — buttons on the live "Now Playing" panel (persistent)
* ``SeekModal``       — "jump to a time" form behind the panel's Seek button
* ``SavedQueueView``  — Resume / Discard on the card left after a stop (persistent)
* ``QueueView``       — paginated queue browser with per-track actions
* ``AddToPlaylistView`` / ``PlaylistNameModal`` — save tracks to a playlist
* ``PlaylistBrowser`` — browse playlists: play, remove songs, delete a playlist

Persistent views use fixed ``custom_id``s (prefix ``mb:``) and are registered in
``bot.setup_hook`` so their buttons keep working after a restart.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import time
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

import player
import session
import storage
from sources import Track

if TYPE_CHECKING:
    from player import GuildPlayer

ACCENT = discord.Colour(0x8B5CF6)
PAUSED = discord.Colour(0xF59E0B)
SUCCESS = discord.Colour(0x22C55E)
MUTED = discord.Colour(0x6B7280)

PAGE_SIZE = 10
VOLUME_STEP = 0.1


# ----------------------------------------------------------------- formatting

def fmt_time(seconds: float | int | None) -> str:
    total = int(seconds or 0)
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"


def parse_time(text: str) -> tuple[float, bool] | None:
    """Parse "1:23", "1:02:03", "83", "+30" or "-10". Returns ``(seconds,
    relative)`` — relative when the text starts with + or -. None if invalid."""
    text = text.strip().lower().removesuffix("s")
    sign = 0
    if text[:1] in "+-" and text[:1]:
        sign = 1 if text[0] == "+" else -1
        text = text[1:].strip()
    parts = text.split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return (sign * seconds, True) if sign else (float(seconds), False)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clean(text: str, limit: int) -> str:
    """Truncate, then make safe for markdown (and for use inside [link](…))."""
    text = truncate(text, limit).replace("[", "(").replace("]", ")")
    return discord.utils.escape_markdown(text)


def link(track: Track, limit: int = 70) -> str:
    title = clean(track.title, limit)
    return f"[{title}]({track.url})" if track.url else f"**{title}**"


def duration_tag(track: Track) -> str:
    return f"`{fmt_time(track.duration)}`" if track.duration else "`live`"


def _fit(lines: list[str], limit: int) -> str:
    """Join lines, dropping trailing ones so the text fits an embed limit."""
    out, size = [], 0
    for line in lines:
        if size + len(line) + 1 > limit - 20:
            out.append(f"…and {len(lines) - len(out)} more")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


# ------------------------------------------------------------ response helpers

async def reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = True,
    delete_after: float | None = None,
) -> None:
    """Respond whether or not the interaction was already deferred."""
    kwargs: dict = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if interaction.response.is_done():
        msg = await interaction.followup.send(wait=True, **kwargs)
        if delete_after:
            await msg.delete(delay=delete_after)
    else:
        await interaction.response.send_message(delete_after=delete_after, **kwargs)


async def ensure_listener(interaction: discord.Interaction, gp: GuildPlayer | None) -> bool:
    """Only people in the bot's voice channel may control playback."""
    vc = gp.vc if gp else None
    if vc is None or vc.channel is None:
        return True
    voice = getattr(interaction.user, "voice", None)
    if voice and voice.channel == vc.channel:
        return True
    await reply(interaction, f"🎧 Join **{vc.channel.name}** to control playback.")
    return False


async def voice_target(interaction: discord.Interaction, gp: GuildPlayer):
    """The caller's voice channel, if the bot may play there; else replies and returns None."""
    voice = getattr(interaction.user, "voice", None)
    if not voice or not voice.channel:
        await reply(interaction, "🎧 Join a voice channel first, then try again.")
        return None
    if gp.busy_elsewhere(voice.channel):
        await reply(interaction, f"🎧 I'm already playing in **{gp.vc.channel.name}** — come join!")
        return None
    return voice.channel


async def enqueue(
    interaction: discord.Interaction,
    gp: GuildPlayer,
    channel,
    tracks: list[Track],
    *,
    front: bool = False,
    shuffle: bool = False,
    replace: bool = False,
    label: str | None = None,
) -> None:
    """Shared tail of every "play something" action. Expects a deferred interaction."""
    for t in tracks:
        t.requester_id = interaction.user.id
    try:
        await gp.connect(channel)
    except (discord.ClientException, asyncio.TimeoutError) as exc:
        await reply(interaction, f"⚠️ Couldn't join **{channel.name}** ({exc or 'timed out'}).",
                    delete_after=20)
        return

    starting = not gp.has_content
    if replace and not starting:
        gp.replace(tracks, shuffle=shuffle)
        position = 1
    else:
        position = gp.add(tracks, front=front, shuffle=shuffle)
    gp.start(interaction.channel)

    embed = queued_embed(
        tracks, user=interaction.user, position=position, starting=starting,
        front=front, replaced=replace and not starting, shuffled=shuffle, label=label,
    )
    if starting and session.get_saved(gp.guild_id):
        embed.set_footer(text="💾 There's also a saved queue — /continue brings it back.")
    await reply(interaction, embed=embed, ephemeral=False)


# --------------------------------------------------------------------- embeds

def queued_embed(
    tracks: list[Track],
    *,
    user: discord.abc.User,
    position: int,
    starting: bool,
    front: bool = False,
    replaced: bool = False,
    shuffled: bool = False,
    label: str | None = None,
) -> discord.Embed:
    if starting:
        head = "Starting playback"
    elif replaced:
        head = "Replaced the queue"
    elif front:
        head = "Playing next"
    else:
        head = f"Added to queue · #{position}"

    if len(tracks) == 1:
        t = tracks[0]
        desc = f"**{link(t)}** · {duration_tag(t)}"
    else:
        total = sum(t.duration for t in tracks)
        source = f" from **{clean(label, 80)}**" if label else ""
        desc = f"**{len(tracks)} tracks**{source} · `{fmt_time(total)}`"
        if shuffled:
            desc += " · 🔀 shuffled"

    embed = discord.Embed(description=desc, colour=SUCCESS)
    embed.set_author(name=f"{head} — {user.display_name}", icon_url=user.display_avatar.url)
    if len(tracks) == 1 and tracks[0].thumbnail:
        embed.set_thumbnail(url=tracks[0].thumbnail)
    return embed


def render_panel(gp: GuildPlayer) -> tuple[discord.Embed, "PlayerControls"]:
    t = gp.current
    if t is None:
        embed = discord.Embed(
            title="Nothing playing",
            description="The queue is finished — add more with `/play`.\n"
                        "I'll leave after 5 minutes of silence.",
            colour=MUTED,
        )
    else:
        embed = discord.Embed(
            title=truncate(t.title, 256), url=t.url or None,
            colour=PAUSED if gp.paused else ACCENT,
        )
        embed.set_author(name="⏸  Paused" if gp.paused else "🎶  Now playing")
        lines = []
        if t.uploader:
            lines.append(f"by **{clean(t.uploader, 60)}**")
        pos = gp.position()
        if not t.duration:
            lines.append("🔴 Live")
        elif gp.paused:
            lines.append(f"`{fmt_time(pos)} / {fmt_time(t.duration)}`")
        else:
            ends = int(time.time() + t.duration - pos)
            lines.append(f"`{fmt_time(t.duration)}` · ends <t:{ends}:R>")
        if t.requester_id:
            lines.append(f"Requested by <@{t.requester_id}>")
        embed.description = "\n".join(lines)
        if t.thumbnail:
            embed.set_thumbnail(url=t.thumbnail)

    if gp.queue:
        upcoming = list(itertools.islice(gp.queue, 3))
        lines = [f"`{i}.` {link(x, 50)} {duration_tag(x)}" for i, x in enumerate(upcoming, 1)]
        more = len(gp.queue) - len(upcoming)
        if more:
            lines.append(f"…and **{more}** more")
        embed.add_field(
            name=f"Up next — {len(gp.queue)} track(s) · {fmt_time(gp.queue_duration())}",
            value=_fit(lines, 1024), inline=False,
        )

    footer = [f"Loop: {gp.loop_mode}", f"Volume: {int(gp.volume * 100)}%"]
    if gp.last_action:
        footer.append(gp.last_action)
    embed.set_footer(text="  ·  ".join(footer))
    return embed, PlayerControls(gp)


def ended_card(gp: GuildPlayer, reason: str, saved: int) -> tuple[discord.Embed, discord.ui.View | None]:
    embed = discord.Embed(description=reason, colour=MUTED)
    if not saved:
        return embed, None
    embed.description += (
        f"\n\n💾 Saved **{saved}** track(s). Press **Resume** (or use `/continue`) "
        "to pick up where you left off."
    )
    snap = session.get_saved(gp.guild_id)
    if snap and snap.get("tracks"):
        first = Track.from_dict(snap["tracks"][0])
        at = f" at `{fmt_time(snap['position'])}`" if snap.get("position") else ""
        embed.add_field(name="Starts with", value=f"{link(first)}{at}", inline=False)
    return embed, SavedQueueView()


def restored_embed(user: discord.abc.User, count: int, appended: bool) -> discord.Embed:
    where = "added to the current queue" if appended else "resuming now"
    embed = discord.Embed(description=f"♻️ Restored **{count}** track(s) — {where}.", colour=SUCCESS)
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    return embed


# --------------------------------------------------------------- player panel

class PlayerControls(discord.ui.View):
    """Buttons under the Now Playing panel. Persistent: one instance with no
    player is registered at startup to catch presses on panels from before a
    restart; live panels get an instance styled for the current state."""

    def __init__(self, gp: GuildPlayer | None = None):
        super().__init__(timeout=None)
        if gp is not None:
            self._sync(gp)

    def _sync(self, gp: GuildPlayer) -> None:
        active = gp.current is not None
        self.previous_btn.disabled = not active and not gp.history
        self.pause_btn.emoji = "▶️" if gp.paused else "⏸️"
        self.pause_btn.style = discord.ButtonStyle.success if gp.paused else discord.ButtonStyle.secondary
        self.pause_btn.disabled = not active
        self.skip_btn.disabled = not active
        self.shuffle_btn.disabled = len(gp.queue) < 2
        self.loop_btn.emoji = "🔂" if gp.loop_mode == "track" else "🔁"
        self.loop_btn.label = {"off": "Loop", "queue": "Loop: queue", "track": "Loop: track"}[gp.loop_mode]
        self.loop_btn.style = (
            discord.ButtonStyle.primary if gp.loop_mode != "off" else discord.ButtonStyle.secondary
        )
        self.volume_down_btn.disabled = gp.volume <= 0
        self.volume_up_btn.disabled = gp.volume >= player.VOLUME_MAX
        self.seek_btn.disabled = not active or not gp.current.duration
        self.save_btn.disabled = not active

    async def _player(self, interaction: discord.Interaction, *, control: bool = True) -> GuildPlayer | None:
        gp = player.find(interaction.guild_id)
        if gp is None or (not gp.has_content and not gp.connected):
            await reply(interaction, "Nothing is playing right now — start something with `/play`.")
            if interaction.message is not None:
                try:
                    await interaction.message.edit(view=None)  # stale panel from an old session
                except discord.HTTPException:
                    pass
            return None
        if control and not await ensure_listener(interaction, gp):
            return None
        return gp

    async def _refresh(self, interaction: discord.Interaction, gp: GuildPlayer) -> None:
        if gp.panel is not None and interaction.message and interaction.message.id == gp.panel.id:
            embed, view = gp.render_panel()
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.defer()
            gp.schedule_panel_update(0)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="mb:prev", row=0)
    async def previous_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if (gp := await self._player(interaction)) is None:
            return
        if not gp.previous(interaction.user.display_name):
            await reply(interaction, "There's nothing before this track.")
            return
        await self._refresh(interaction, gp)

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.secondary, custom_id="mb:pause", row=0)
    async def pause_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if (gp := await self._player(interaction)) is None:
            return
        if gp.toggle_pause(interaction.user.display_name) is None:
            await reply(interaction, "Nothing is playing right now.")
            return
        await self._refresh(interaction, gp)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="mb:skip", row=0)
    async def skip_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if (gp := await self._player(interaction)) is None:
            return
        if not gp.skip(interaction.user.display_name):
            await reply(interaction, "Nothing to skip.")
            return
        await self._refresh(interaction, gp)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="mb:stop", row=0)
    async def stop_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if (gp := await self._player(interaction)) is None:
            return
        await interaction.response.defer()
        saved = await gp.shutdown(save=True)
        await gp.close_panel(*ended_card(gp, f"⏹️ Stopped by {interaction.user.display_name}.", saved))

    @discord.ui.button(emoji="📜", label="Queue", style=discord.ButtonStyle.secondary, custom_id="mb:queue", row=0)
    async def queue_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if (gp := await self._player(interaction, control=False)) is None:
            return
        view = QueueView(gp)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="mb:shuffle", row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if (gp := await self._player(interaction)) is None:
            return
        gp.shuffle(interaction.user.display_name)
        await self._refresh(interaction, gp)

    @discord.ui.button(emoji="🔁", label="Loop", style=discord.ButtonStyle.secondary, custom_id="mb:loop", row=1)
    async def loop_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if (gp := await self._player(interaction)) is None:
            return
        gp.cycle_loop(interaction.user.display_name)
        await self._refresh(interaction, gp)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="mb:voldown", row=1)
    async def volume_down_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if (gp := await self._player(interaction)) is None:
            return
        gp.set_volume(gp.volume - VOLUME_STEP, interaction.user.display_name)
        await self._refresh(interaction, gp)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="mb:volup", row=1)
    async def volume_up_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if (gp := await self._player(interaction)) is None:
            return
        gp.set_volume(gp.volume + VOLUME_STEP, interaction.user.display_name)
        await self._refresh(interaction, gp)

    @discord.ui.button(emoji="⏩", label="Seek", style=discord.ButtonStyle.secondary, custom_id="mb:seek", row=2)
    async def seek_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if (gp := await self._player(interaction)) is None:
            return
        if gp.current is None or not gp.current.duration:
            await reply(interaction, "This track can't be sought (it's live or nothing is playing).")
            return
        await interaction.response.send_modal(SeekModal(gp))

    @discord.ui.button(emoji="⭐", label="Save", style=discord.ButtonStyle.secondary, custom_id="mb:save", row=2)
    async def save_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if (gp := await self._player(interaction, control=False)) is None:
            return
        if gp.current is None:
            await reply(interaction, "Nothing is playing right now.")
            return
        view = AddToPlaylistView(interaction.guild_id, [gp.current.copy()], f"**{clean(gp.current.title, 80)}**")
        await interaction.response.send_message(view.prompt(), view=view, ephemeral=True)

    @discord.ui.button(emoji="📚", label="Playlists", style=discord.ButtonStyle.secondary,
                       custom_id="mb:playlists", row=2)
    async def playlists_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        browser = PlaylistBrowser(interaction.guild_id)
        await interaction.response.send_message(embed=browser.embed(), view=browser, ephemeral=True)


class SeekModal(discord.ui.Modal, title="Jump to a time"):
    position_input = discord.ui.TextInput(label="Time", placeholder="1:30, 90, +30 or -10", max_length=12)

    def __init__(self, gp: GuildPlayer):
        super().__init__()
        self.gp = gp
        self.position_input.placeholder = (f"Now at {fmt_time(gp.position())} of "
                                           f"{fmt_time(gp.current.duration)} — try 1:30, +30 or -10")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        gp = self.gp
        if gp.current is None:
            await reply(interaction, "Nothing is playing right now.")
            return
        if not await ensure_listener(interaction, gp):
            return
        parsed = parse_time(self.position_input.value)
        if parsed is None:
            await reply(interaction, "I couldn't read that time — try `1:30`, `90`, `+30` or `-10`.")
            return
        seconds, relative = parsed
        target = gp.seek(gp.position() + seconds if relative else seconds, interaction.user.display_name)
        if target is None:
            await reply(interaction, "This track can't be sought (it's live or still loading).")
            return
        await interaction.response.defer()  # the panel refreshes itself once the stream restarts


class SavedQueueView(discord.ui.View):
    """Resume / Discard buttons on the card left behind after a stop."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Resume", emoji="▶️", style=discord.ButtonStyle.success, custom_id="mb:saved:resume")
    async def resume_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not session.get_saved(interaction.guild_id):
            await interaction.response.edit_message(view=None)
            await interaction.followup.send(
                "That saved queue is gone — it was already resumed or discarded.", ephemeral=True)
            return
        gp = player.get(interaction.guild)
        channel = await voice_target(interaction, gp)
        if channel is None:
            return
        await interaction.response.defer()
        try:
            count, appended = await gp.restore(channel, interaction.channel)
        except (discord.ClientException, asyncio.TimeoutError) as exc:
            await reply(interaction, f"⚠️ Couldn't join **{channel.name}** ({exc or 'timed out'}).")
            return
        await interaction.edit_original_response(
            embed=restored_embed(interaction.user, count, appended), view=None)

    @discord.ui.button(label="Discard", emoji="🗑️", style=discord.ButtonStyle.secondary, custom_id="mb:saved:discard")
    async def discard_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await session.clear_saved(interaction.guild_id)
        embed = discord.Embed(
            description=f"🗑️ Saved queue discarded by {interaction.user.display_name}.", colour=MUTED)
        await interaction.response.edit_message(embed=embed, view=None)


# ---------------------------------------------------------------- queue view

class QueueView(discord.ui.View):
    """Ephemeral, paginated queue with per-track actions (play now / next / remove)."""

    def __init__(self, gp: GuildPlayer):
        super().__init__(timeout=300)
        self.gp = gp
        self.page = 0
        self.selected: Track | None = None
        self.confirm_clear = False
        self._page_tracks: list[Track] = []
        self._build()

    @property
    def pages(self) -> int:
        return max(1, math.ceil(len(self.gp.queue) / PAGE_SIZE))

    def _index(self, track: Track) -> int | None:
        for i, t in enumerate(self.gp.queue):
            if t is track:
                return i
        return None

    def embed(self) -> discord.Embed:
        gp = self.gp
        self.page = min(self.page, self.pages - 1)
        if self.selected is not None:
            t = self.selected
            idx = self._index(t)
            lines = [f"**{link(t, 200)}**", duration_tag(t)]
            lines.append(f"Position **#{idx + 1}** in the queue" if idx is not None
                         else "*No longer in the queue.*")
            if t.requester_id:
                lines.append(f"Requested by <@{t.requester_id}>")
            embed = discord.Embed(title="Queued track", description="\n".join(lines), colour=ACCENT)
            if t.thumbnail:
                embed.set_thumbnail(url=t.thumbnail)
            return embed

        header = ""
        if gp.current:
            header = (f"**Now playing:** {link(gp.current, 60)} "
                      f"`{fmt_time(gp.position())} / {fmt_time(gp.current.duration)}`\n\n")
        start = self.page * PAGE_SIZE
        lines = []
        for i, t in enumerate(itertools.islice(gp.queue, start, start + PAGE_SIZE), start + 1):
            who = f" · <@{t.requester_id}>" if t.requester_id else ""
            lines.append(f"`{i:>2}.` {link(t, 55)} · {duration_tag(t)}{who}")
        if not gp.queue:
            lines.append("*Nothing queued — add songs with `/play`.*")
        embed = discord.Embed(
            title=f"Queue · {len(gp.queue)} track(s) · {fmt_time(gp.queue_duration())}",
            description=header + _fit(lines, 4096 - len(header)),
            colour=ACCENT,
        )
        embed.set_footer(text=f"Page {self.page + 1}/{self.pages}  ·  Loop: {gp.loop_mode}")
        return embed

    def _button(self, label, emoji, style, callback, *, row: int, disabled: bool = False) -> discord.ui.Button:
        button = discord.ui.Button(label=label, emoji=emoji, style=style, row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)
        return button

    def _build(self) -> None:
        self.clear_items()
        S = discord.ButtonStyle
        if self.selected is not None:
            gone = self._index(self.selected) is None
            self._button("Play now", "▶️", S.success, self._play_now, row=0, disabled=gone)
            self._button("Play next", "⏫", S.primary, self._play_next, row=0, disabled=gone)
            self._button("Remove", "🗑️", S.danger, self._remove, row=0, disabled=gone)
            self._button("Back", "↩️", S.secondary, self._back, row=0)
            return

        start = self.page * PAGE_SIZE
        self._page_tracks = list(itertools.islice(self.gp.queue, start, start + PAGE_SIZE))
        if self._page_tracks:
            select = discord.ui.Select(
                placeholder="Pick a track to play, move or remove…",
                options=[
                    discord.SelectOption(
                        label=truncate(f"{start + i + 1}. {t.title}", 100),
                        description=truncate(" · ".join(filter(None, [t.uploader, fmt_time(t.duration) if t.duration else ""])), 100) or None,
                        value=str(i),
                    )
                    for i, t in enumerate(self._page_tracks)
                ],
                row=0,
            )
            select.callback = self._on_select
            self.add_item(select)

        self._button(None, "◀️", S.secondary, self._prev_page, row=1, disabled=self.page == 0)
        self._button(f"{self.page + 1}/{self.pages}", None, S.secondary, self._noop, row=1, disabled=True)
        self._button(None, "▶️", S.secondary, self._next_page, row=1, disabled=self.page >= self.pages - 1)
        self._button("Shuffle", "🔀", S.secondary, self._shuffle, row=1, disabled=len(self.gp.queue) < 2)
        if self.confirm_clear:
            self._button("Tap again to clear", "🧹", S.danger, self._clear, row=1)
        else:
            self._button("Clear", "🧹", S.secondary, self._clear, row=1, disabled=not self.gp.queue)
        self._button("Save as playlist", "💾", S.secondary, self._save, row=2,
                     disabled=not self.gp.has_content)
        self._button("Refresh", "🔄", S.secondary, self._noop_refresh, row=2)

    async def _update(self, interaction: discord.Interaction) -> None:
        embed = self.embed()
        self._build()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _noop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

    async def _noop_refresh(self, interaction: discord.Interaction) -> None:
        self.confirm_clear = False
        await self._update(interaction)

    async def _prev_page(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self._update(interaction)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        self.page = min(self.pages - 1, self.page + 1)
        await self._update(interaction)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        idx = int(interaction.data["values"][0])
        if idx < len(self._page_tracks):
            self.selected = self._page_tracks[idx]
        await self._update(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        self.selected = None
        await self._update(interaction)

    async def _play_now(self, interaction: discord.Interaction) -> None:
        if not await ensure_listener(interaction, self.gp):
            return
        self.gp.play_now(self.selected, interaction.user.display_name)
        self.selected = None
        await self._update(interaction)

    async def _play_next(self, interaction: discord.Interaction) -> None:
        if not await ensure_listener(interaction, self.gp):
            return
        self.gp.move_to_front(self.selected)
        self.gp._note(f"⏫ {interaction.user.display_name} moved a track up next")
        self.selected = None
        await self._update(interaction)

    async def _remove(self, interaction: discord.Interaction) -> None:
        if not await ensure_listener(interaction, self.gp):
            return
        self.gp.remove(self.selected)
        self.selected = None
        await self._update(interaction)

    async def _shuffle(self, interaction: discord.Interaction) -> None:
        if not await ensure_listener(interaction, self.gp):
            return
        self.gp.shuffle(interaction.user.display_name)
        await self._update(interaction)

    async def _clear(self, interaction: discord.Interaction) -> None:
        if not await ensure_listener(interaction, self.gp):
            return
        if self.confirm_clear:
            self.gp.clear(interaction.user.display_name)
        self.confirm_clear = not self.confirm_clear
        await self._update(interaction)

    async def _save(self, interaction: discord.Interaction) -> None:
        tracks = [t.copy() for t in ([self.gp.current] if self.gp.current else []) + list(self.gp.queue)]
        view = AddToPlaylistView(interaction.guild_id, tracks, f"**{len(tracks)} track(s)**")
        await interaction.response.send_message(view.prompt(), view=view, ephemeral=True)


# ---------------------------------------------------------- saving to playlists

class PlaylistNameModal(discord.ui.Modal, title="New playlist"):
    name_input = discord.ui.TextInput(
        label="Playlist name", placeholder="e.g. Friday night", max_length=storage.MAX_NAME_LENGTH)

    def __init__(self, on_name: Callable[[discord.Interaction, str], Awaitable[None]]):
        super().__init__()
        self._on_name = on_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_name(interaction, self.name_input.value)


class AddToPlaylistView(discord.ui.View):
    """Ephemeral picker: add some tracks to an existing or a new playlist."""

    def __init__(self, guild_id: int, tracks: list[Track], what: str):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.tracks = tracks
        self.what = what
        playlists = storage.list_playlists(guild_id)
        names = storage.names(guild_id)[:25]
        if names:
            select = discord.ui.Select(
                placeholder="Add to an existing playlist…",
                options=[
                    discord.SelectOption(label=truncate(n, 100), value=n[:100], emoji="📁",
                                         description=f"{len(playlists[n]['tracks'])} track(s)")
                    for n in names
                ],
                row=0,
            )
            select.callback = self._on_select
            self.add_item(select)
        new = discord.ui.Button(label="New playlist…", emoji="➕", style=discord.ButtonStyle.primary, row=1)
        new.callback = self._on_new
        self.add_item(new)

    def prompt(self) -> str:
        return f"Save {self.what} to which playlist?"

    async def save(self, interaction: discord.Interaction, name: str) -> None:
        try:
            key, created = await storage.add_tracks(
                self.guild_id, name, [t.to_dict(include_requester=False) for t in self.tracks],
                interaction.user.id)
        except storage.PlaylistError as exc:
            await reply(interaction, str(exc))
            return
        note = " (new playlist)" if created else ""
        await interaction.response.edit_message(content=f"✅ Saved {self.what} to **{key}**{note}.", view=None)
        self.stop()

    async def _on_select(self, interaction: discord.Interaction) -> None:
        await self.save(interaction, interaction.data["values"][0])

    async def _on_new(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PlaylistNameModal(self.save))


# ------------------------------------------------------------ playlist browser

class PlaylistBrowser(discord.ui.View):
    """``/playlist view`` and 📚 on the player: the list of playlists ↔ one
    playlist's tracks, with play / remove-a-song / delete-playlist controls."""

    def __init__(self, guild_id: int, name: str | None = None):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.name = storage.resolve_name(guild_id, name) if name else None
        self.page = 0
        self.notice: str | None = None           # one-shot line shown above the list
        self.confirm_delete = False
        self.pending_remove: tuple[int, dict] | None = None  # (index, track) picked for removal
        self._build()

    def _playlist(self) -> dict | None:
        return storage.get(self.guild_id, self.name) if self.name else None

    def _pages(self, pl: dict) -> int:
        return max(1, math.ceil(len(pl["tracks"]) / PAGE_SIZE))

    def embed(self) -> discord.Embed:
        if self.name is None:
            playlists = storage.list_playlists(self.guild_id)
            notice = f"{self.notice}\n\n" if self.notice else ""
            if not playlists:
                return discord.Embed(
                    title="Playlists",
                    description=f"{notice}No playlists yet.\nPress ⭐ **Save** on the player, "
                                "**Save as playlist** in 📜 Queue, or use `/playlist add`.",
                    colour=MUTED)
            lines = ([self.notice, ""] if self.notice else []) + [
                f"📁 **{clean(n, 40)}** · {len(pl['tracks'])} track(s) · `{fmt_time(storage.total_duration(pl))}`"
                for n, pl in sorted(playlists.items(), key=lambda kv: kv[0].casefold())
            ]
            embed = discord.Embed(title=f"Playlists · {len(playlists)}", description=_fit(lines, 4096),
                                  colour=ACCENT)
            embed.set_footer(text="Pick one below to open it")
            return embed

        pl = self._playlist()
        if pl is None:
            return discord.Embed(description=f"**{clean(self.name, 50)}** no longer exists.", colour=MUTED)
        tracks = pl["tracks"]
        self.page = min(self.page, self._pages(pl) - 1)
        start = self.page * PAGE_SIZE
        lines = [
            f"`{i:>2}.` {link(Track.from_dict(t), 55)} · {duration_tag(Track.from_dict(t))}"
            for i, t in enumerate(tracks[start:start + PAGE_SIZE], start + 1)
        ] or ["*Empty — add songs with `/playlist add` or the ⭐ button on the player.*"]
        if self.notice:
            lines = [self.notice, ""] + lines
        embed = discord.Embed(title=f"📁 {self.name}", description=_fit(lines, 4096), colour=ACCENT)
        embed.set_footer(text=f"{len(tracks)} track(s) · {fmt_time(storage.total_duration(pl))}"
                              f"  ·  Page {self.page + 1}/{self._pages(pl)}")
        return embed

    def _button(self, label, emoji, style, callback, *, row: int, disabled: bool = False) -> None:
        button = discord.ui.Button(label=label, emoji=emoji, style=style, row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)

    def _build(self) -> None:
        self.clear_items()
        S = discord.ButtonStyle
        if self.name is None:
            playlists = storage.list_playlists(self.guild_id)
            names = storage.names(self.guild_id)[:25]
            if names:
                select = discord.ui.Select(
                    placeholder="Open a playlist…",
                    options=[discord.SelectOption(label=truncate(n, 100), value=n[:100], emoji="📁",
                                                  description=f"{len(playlists[n]['tracks'])} track(s)")
                             for n in names])
                select.callback = self._open
                self.add_item(select)
            return

        pl = self._playlist()
        has_tracks = bool(pl and pl["tracks"])
        pages = self._pages(pl) if pl else 1
        self._button("Play", "▶️", S.success, self._play, row=0, disabled=not has_tracks)
        self._button("Shuffle play", "🔀", S.primary, self._shuffle_play, row=0, disabled=not has_tracks)
        self._button("All playlists", "📚", S.secondary, self._back, row=0)
        if self.confirm_delete:
            self._button("Tap again to delete", "🗑️", S.danger, self._delete, row=0)
        else:
            self._button("Delete", "🗑️", S.secondary, self._delete, row=0, disabled=pl is None)
        if pages > 1:
            self._button(None, "◀️", S.secondary, self._prev, row=1, disabled=self.page == 0)
            self._button(None, "▶️", S.secondary, self._next, row=1, disabled=self.page >= pages - 1)
        if not has_tracks:
            return
        if self.pending_remove is not None:
            title = self.pending_remove[1].get("title", "Unknown")
            self._button(truncate(f"Remove “{title}”", 80), "➖", S.danger, self._remove, row=2)
            self._button("Cancel", None, S.secondary, self._cancel, row=2)
            return
        start = self.page * PAGE_SIZE
        select = discord.ui.Select(
            placeholder="Remove a song…",
            options=[discord.SelectOption(label=truncate(f"{i + 1}. {t.get('title', 'Unknown')}", 100),
                                          value=str(i))
                     for i, t in enumerate(pl["tracks"][start:start + PAGE_SIZE], start)],
            row=2)
        select.callback = self._pick_remove
        self.add_item(select)

    async def _update(self, interaction: discord.Interaction) -> None:
        embed = self.embed()
        self._build()
        self.notice = None  # shown once
        await interaction.response.edit_message(embed=embed, view=self)

    def _reset(self) -> None:
        self.confirm_delete = False
        self.pending_remove = None

    async def _open(self, interaction: discord.Interaction) -> None:
        self.name = storage.resolve_name(self.guild_id, interaction.data["values"][0])
        self.page = 0
        self._reset()
        await self._update(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        self.name = None
        self._reset()
        await self._update(interaction)

    async def _prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._reset()
        await self._update(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page += 1
        self._reset()
        await self._update(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self._reset()
        await self._update(interaction)

    async def _pick_remove(self, interaction: discord.Interaction) -> None:
        pl = self._playlist()
        index = int(interaction.data["values"][0])
        self._reset()
        if pl and index < len(pl["tracks"]):
            self.pending_remove = (index, pl["tracks"][index])
        await self._update(interaction)

    async def _remove(self, interaction: discord.Interaction) -> None:
        if self.pending_remove is None:
            await self._update(interaction)
            return
        (index, picked), self.pending_remove = self.pending_remove, None
        pl = self._playlist()
        # Someone may have edited the playlist meanwhile: only remove the very same entry.
        if pl is None or index >= len(pl["tracks"]) or pl["tracks"][index] is not picked:
            self.notice = "⚠️ The playlist changed in the meantime — pick the song again."
        else:
            try:
                removed = await storage.remove_track(self.guild_id, self.name, index)
                self.notice = f"➖ Removed **{clean(removed.get('title', 'Unknown'), 80)}**."
            except storage.PlaylistError as exc:
                self.notice = f"⚠️ {exc}"
        await self._update(interaction)

    async def _delete(self, interaction: discord.Interaction) -> None:
        if not self.confirm_delete:
            self._reset()
            self.confirm_delete = True
            await self._update(interaction)
            return
        name, self.name = self.name, None
        self._reset()
        try:
            await storage.delete(self.guild_id, name)
            self.notice = f"🗑️ Deleted **{clean(name, 50)}**."
        except storage.PlaylistError as exc:
            self.notice = f"⚠️ {exc}"
        await self._update(interaction)

    async def _play(self, interaction: discord.Interaction, *, shuffle: bool = False) -> None:
        await play_playlist(interaction, self.name, shuffle=shuffle)

    async def _shuffle_play(self, interaction: discord.Interaction) -> None:
        await self._play(interaction, shuffle=True)


async def play_playlist(interaction: discord.Interaction, name: str | None, *,
                        shuffle: bool = False, replace: bool = False) -> None:
    pl = storage.get(interaction.guild_id, name) if name else None
    if pl is None:
        await reply(interaction, f"No playlist named **{clean(name or '', 50)}**.")
        return
    if not pl["tracks"]:
        await reply(interaction, f"**{clean(name, 50)}** is empty.")
        return
    gp = player.get(interaction.guild)
    channel = await voice_target(interaction, gp)
    if channel is None:
        return
    if not interaction.response.is_done():
        await interaction.response.defer()
    tracks = [Track.from_dict(t) for t in pl["tracks"]]
    await enqueue(interaction, gp, channel, tracks, shuffle=shuffle, replace=replace,
                  label=storage.resolve_name(interaction.guild_id, name))
