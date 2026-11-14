"""Per-guild playback engine.

One :class:`GuildPlayer` per guild owns the queue, history, loop mode, volume,
the voice connection and the "Now Playing" panel message. Cogs and views only
call its public methods; everything Discord-visual is rendered by ``views``.

Resilience model (keep this intact when editing the loop):

* ``ensure_voice`` reconnects after a drop for up to ``RECONNECT_DEADLINE``,
  but only while a human is in the channel.
* A track that ends while disconnected, or noticeably before its duration, is
  treated as a drop: the stream URL is re-fetched and playback resumes from the
  position where it cut out.
* The live queue is snapshotted to ``session`` on every change so a crash or
  restart resumes automatically; deliberate stops *park* it for ``/continue``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import TYPE_CHECKING

import discord

import session
import sources
import views
from sources import SourceError, Track

if TYPE_CHECKING:
    from discord.ext import commands

log = logging.getLogger(__name__)

RECONNECT_DEADLINE = 600   # how long ensure_voice keeps retrying after a drop
IDLE_TIMEOUT = 300         # leave after this long with an empty queue
ALONE_TIMEOUT = 30         # leave (and save the queue) after this long alone
HISTORY_SIZE = 50
VOLUME_MAX = 2.0
PREVIOUS_RESTART_AFTER = 5  # "previous" restarts the current track past this many seconds

LOOP_MODES = ("off", "queue", "track")

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

_bot: commands.Bot | None = None
_players: dict[int, "GuildPlayer"] = {}


def setup(bot: commands.Bot) -> None:
    global _bot
    _bot = bot


def get(guild: discord.Guild) -> "GuildPlayer":
    gp = _players.get(guild.id)
    if gp is None:
        gp = _players[guild.id] = GuildPlayer(guild)
    else:
        gp.guild = guild  # Guild objects are replaced after a full gateway reconnect
    return gp


def find(guild_id: int) -> "GuildPlayer | None":
    return _players.get(guild_id)


def has_human(channel) -> bool:
    return channel is not None and any(not m.bot for m in channel.members)


class GuildPlayer:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.guild_id = guild.id
        self.queue: deque[Track] = deque()
        self.history: deque[Track] = deque(maxlen=HISTORY_SIZE)
        self.current: Track | None = None
        self.loop_mode = "off"
        self.volume = 1.0
        self.voice_channel: discord.abc.Connectable | None = None  # used to rejoin after a drop
        self.text_channel: discord.abc.Messageable | None = None   # where the panel lives
        self.last_action: str | None = None                        # shown in the panel footer
        self.persist_enabled = True  # gate disk writes while parking/stopping

        self.task: asyncio.Task | None = None
        self.alone_task: asyncio.Task | None = None
        self.panel: discord.Message | None = None
        self._panel_view: discord.ui.View | None = None
        self._panel_task: asyncio.Task | None = None
        self._prefetch: tuple[Track, asyncio.Task] | None = None

        self._wake = asyncio.Event()
        # Why the current source was stopped on purpose: "skip", "previous",
        # "replace", "stop", "failed", "seek" — None means it finished naturally.
        self._interrupt: str | None = None
        self._seek_to = 0.0
        self._source: discord.PCMVolumeTransformer | None = None
        self._play_started = 0.0
        self._offset = 0.0
        self._paused_at: float | None = None
        self._paused_total = 0.0

    # ------------------------------------------------------------------ state

    @property
    def vc(self) -> discord.VoiceClient | None:
        return self.guild.voice_client  # type: ignore[return-value]

    @property
    def connected(self) -> bool:
        return bool(self.vc and self.vc.is_connected())

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    @property
    def has_content(self) -> bool:
        return self.current is not None or bool(self.queue)

    def position(self) -> float:
        """Seconds into the current track (pauses excluded)."""
        if self.current is None or not self._play_started:
            return 0.0
        now = self._paused_at or time.monotonic()
        return max(0.0, self._offset + now - self._play_started - self._paused_total)

    def queue_duration(self) -> int:
        return sum(t.duration for t in self.queue)

    def busy_elsewhere(self, channel) -> bool:
        """True if we're playing for people in a *different* voice channel."""
        vc = self.vc
        return bool(
            vc and vc.is_connected() and vc.channel and vc.channel != channel
            and self.has_content and has_human(vc.channel)
        )

    def _note(self, text: str | None) -> None:
        self.last_action = text

    def _fresh_guild(self) -> discord.Guild:
        if _bot is not None:
            self.guild = _bot.get_guild(self.guild_id) or self.guild
        return self.guild

    # ------------------------------------------------------------- connecting

    async def connect(self, channel) -> None:
        """Join ``channel`` (or move there). Leaves an in-progress reconnect alone
        but retargets it at ``channel``."""
        vc = self.vc
        if vc is None:
            await channel.connect(timeout=30.0, self_deaf=True)
        elif vc.is_connected() and vc.channel != channel:
            await vc.move_to(channel)
        self.voice_channel = channel

    async def ensure_voice(self) -> discord.VoiceClient | None:
        """Return a live VoiceClient, reconnecting after a drop for up to
        ``RECONNECT_DEADLINE`` seconds.

        Reconnection is only attempted while at least one human is in the target
        channel — an internet blip during which everyone leaves should not drag
        the bot back in. Returns ``None`` if the window elapses without a
        successful, member-present reconnect (the caller then parks the queue)."""
        guild = self._fresh_guild()
        vc = guild.voice_client
        if vc and vc.is_connected():
            return vc  # type: ignore[return-value]
        if self.voice_channel is None:
            return None

        deadline = time.monotonic() + RECONNECT_DEADLINE
        delay = 5
        while time.monotonic() < deadline:
            guild = self._fresh_guild()
            channel = guild.get_channel(self.voice_channel.id) or self.voice_channel
            try:
                # Tear down any half-dead client left over from the drop, then
                # connect. While the gateway is down this raises and we retry; it
                # only succeeds once the internet is genuinely back.
                if guild.voice_client:
                    await guild.voice_client.disconnect(force=True)
                vc = await channel.connect(timeout=30.0, self_deaf=True)
            except Exception as exc:
                log.warning("Voice reconnect attempt failed: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)  # 5, 10, 20, 40, 60, 60 … s
                continue

            self.voice_channel = channel
            # Connected with a fresh gateway session, so the member list is
            # current — only stay if a human is actually here.
            if has_human(channel):
                log.info("Reconnected to voice in guild %s.", guild.id)
                return vc
            await vc.disconnect(force=True)
            log.info("Reconnected but %s is empty — giving up.", channel.name)
            return None

        log.warning("Reconnect window (%ss) elapsed for guild %s.", RECONNECT_DEADLINE, guild.id)
        return None

    # ---------------------------------------------------------------- queueing

    def add(self, tracks: list[Track], *, front: bool = False, shuffle: bool = False) -> int:
        """Queue tracks; returns the 1-based queue position of the first one."""
        tracks = list(tracks)
        if shuffle:
            random.shuffle(tracks)
        if front:
            self.queue.extendleft(reversed(tracks))
            position = 1
        else:
            position = len(self.queue) + 1
            self.queue.extend(tracks)
        self.persist_enabled = True
        self._wake.set()
        self._changed()
        return position

    def replace(self, tracks: list[Track], *, shuffle: bool = False) -> None:
        """Swap the whole queue (and the current track) for ``tracks``."""
        self.queue.clear()
        self.add(tracks, shuffle=shuffle)
        if self.current:
            self._stop_source("replace")

    def start(self, text_channel=None) -> None:
        """Make sure the player loop is running."""
        if text_channel is not None:
            self.text_channel = text_channel
        self.persist_enabled = True
        self._wake.set()
        if self.task is None or self.task.done():
            self.task = asyncio.ensure_future(self._run())

    def remove(self, track: Track) -> bool:
        try:
            self.queue.remove(track)
        except ValueError:
            return False
        self._changed()
        return True

    def move_to_front(self, track: Track) -> bool:
        if not self.remove(track):
            return False
        self.queue.appendleft(track)
        self._changed()
        return True

    def shuffle(self, by: str) -> int:
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)
        self._note(f"🔀 Shuffled by {by}")
        self._changed()
        return len(items)

    def clear(self, by: str) -> int:
        count = len(self.queue)
        self.queue.clear()
        self._note(f"🧹 Queue cleared by {by}")
        self._changed()
        return count

    def cycle_loop(self, by: str) -> str:
        self.loop_mode = LOOP_MODES[(LOOP_MODES.index(self.loop_mode) + 1) % len(LOOP_MODES)]
        self._note(f"🔁 Loop set to {self.loop_mode} by {by}")
        self._changed()
        return self.loop_mode

    def set_volume(self, volume: float, by: str) -> float:
        self.volume = round(min(max(volume, 0.0), VOLUME_MAX), 2)
        if self._source is not None:
            self._source.volume = self.volume
        self._note(f"🔊 Volume {int(self.volume * 100)}% by {by}")
        self._changed()
        return self.volume

    # --------------------------------------------------------------- controls

    def _stop_source(self, reason: str) -> bool:
        if self.current is None:
            return False
        self._interrupt = reason
        vc = self.vc
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        return True

    def skip(self, by: str, *, to: int | None = None) -> bool:
        """Skip the current track; with ``to`` (1-based) jump to that queue entry."""
        if self.current is None:
            return False
        if to is not None and 1 < to <= len(self.queue):
            for _ in range(to - 1):
                self.queue.popleft()
            self._note(f"⏭ Jumped ahead {to} tracks — {by}")
        else:
            self._note(f"⏭ Skipped by {by}")
        return self._stop_source("skip")

    def play_now(self, track: Track, by: str) -> bool:
        if not self.move_to_front(track):
            return False
        self._note(f"▶ {by} jumped to a queued track")
        if not self._stop_source("skip"):
            self._wake.set()
        return True

    def previous(self, by: str) -> bool:
        """Restart the current track, or go back one if we're near its start."""
        current = self.current
        if current and (self.position() > PREVIOUS_RESTART_AFTER or not self.history):
            self.queue.appendleft(current)
            self._note(f"⏮ Restarted by {by}")
            return self._stop_source("previous")
        if not self.history:
            return False
        if current:
            self.queue.appendleft(current)
        self.queue.appendleft(self.history.pop())
        self._note(f"⏮ Back one track — {by}")
        if not self._stop_source("previous"):
            self._wake.set()
        return True

    def seek(self, seconds: float, by: str) -> float | None:
        """Jump within the current track. Returns the clamped target position, or
        None if the current track can't be sought (nothing playing, or a live stream)."""
        track, vc = self.current, self.vc
        if track is None or not track.duration or vc is None or not (vc.is_playing() or vc.is_paused()):
            return None
        target = min(max(0.0, seconds), max(0.0, track.duration - 1))
        self._seek_to = target
        self._note(f"⏩ Jumped to {views.fmt_time(target)} — {by}")
        self._interrupt = "seek"
        vc.stop()  # the loop restarts the same stream at the new offset
        return target

    def toggle_pause(self, by: str) -> bool | None:
        """Returns True if now paused, False if resumed, None if nothing to do."""
        vc = self.vc
        if vc is None or self.current is None:
            return None
        if vc.is_paused():
            vc.resume()
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None
            self._note(f"▶ Resumed by {by}")
        elif vc.is_playing():
            vc.pause()
            self._paused_at = time.monotonic()
            self._note(f"⏸ Paused by {by}")
        else:
            return None
        self.schedule_panel_update()
        return self.paused

    # ------------------------------------------------------------ persistence

    def snapshot(self) -> dict:
        tracks = ([self.current] if self.current else []) + list(self.queue)
        return {
            "voice_channel_id": getattr(self.voice_channel, "id", None),
            "text_channel_id": getattr(self.text_channel, "id", None),
            "tracks": [t.to_dict() for t in tracks],
            "position": int(self.position()) if self.current else 0,
            "loop": self.loop_mode,
            "volume": self.volume,
            "panel_id": getattr(self.panel, "id", None),
        }

    def persist(self) -> None:
        """Fire-and-forget write of the live state. No-op while parking/stopping
        so the loop can't overwrite a freshly parked session."""
        if not self.persist_enabled:
            return
        if self.has_content:
            asyncio.ensure_future(session.save_live(self.guild_id, self.snapshot()))
        else:
            asyncio.ensure_future(session.clear_live(self.guild_id))

    async def park(self) -> int:
        """Freeze the live state into the saved slot for a later /continue."""
        snap = self.snapshot()
        self.persist_enabled = False
        if not snap["tracks"]:
            await session.clear_live(self.guild_id)
            return 0
        await session.park(self.guild_id, snap)
        return len(snap["tracks"])

    async def discard(self) -> None:
        self.persist_enabled = False
        await session.clear_live(self.guild_id)

    async def restore(self, voice_channel, text_channel) -> tuple[int, bool]:
        """Bring back the saved queue. Returns ``(count, appended)`` — appended
        is True when something was already playing."""
        saved = session.get_saved(self.guild_id)
        tracks = [Track.from_dict(d) for d in (saved or {}).get("tracks", [])]
        if not tracks:
            return 0, False
        await self.connect(voice_channel)
        appended = self.has_content
        if not appended:
            tracks[0].start_at = float(saved.get("position") or 0)
            self.loop_mode = saved.get("loop", self.loop_mode)
            self.volume = float(saved.get("volume", self.volume))
        await session.clear_saved(self.guild_id)
        self.add(tracks)
        self.start(text_channel)
        return len(tracks), appended

    def _changed(self) -> None:
        self.persist()
        self.schedule_panel_update()
        if self.current is not None:
            self._prefetch_next()

    # ---------------------------------------------------------------- shutdown

    async def shutdown(self, *, save: bool = True) -> int:
        """Stop playback and leave voice. Returns how many tracks were saved."""
        saved = await self.park() if save else 0
        if not save:
            await self.discard()
        self.queue.clear()
        self._interrupt = "stop"
        for task in (self.task, self.alone_task):
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
                await asyncio.wait([task], timeout=5)
        self.current = None
        self._reset_timing()
        vc = self.vc
        if vc and vc.is_connected():
            await vc.disconnect(force=True)
        return saved

    # ------------------------------------------------------------------ panel

    def render_panel(self) -> tuple[discord.Embed, discord.ui.View]:
        embed, view = views.render_panel(self)
        if self._panel_view is not None:
            self._panel_view.stop()  # drop the old view from discord.py's store
        self._panel_view = view
        return embed, view

    def schedule_panel_update(self, delay: float = 1.0) -> None:
        """Coalesce bursts of changes into one panel edit."""
        if self.panel is None or (self._panel_task and not self._panel_task.done()):
            return

        async def _later() -> None:
            await asyncio.sleep(delay)
            await self.update_panel()

        self._panel_task = asyncio.ensure_future(_later())

    async def update_panel(self, *, move_to_bottom: bool = False) -> None:
        """Edit the panel in place, or (re)post it at the bottom of the channel
        when ``move_to_bottom`` and it has been buried by other messages."""
        channel = self.text_channel
        msg = self.panel
        buried = msg is not None and (
            channel is None or msg.channel.id != channel.id
            or getattr(channel, "last_message_id", None) != msg.id
        )
        if msg is None and not move_to_bottom:
            return
        embed, view = self.render_panel()
        if msg is not None and not (move_to_bottom and buried):
            try:
                await msg.edit(embed=embed, view=view)
                return
            except discord.NotFound:
                self.panel = None
            except discord.HTTPException as exc:
                log.warning("Could not edit panel: %s", exc)
                return
        await self._delete_panel()
        if channel is not None:
            try:
                self.panel = await channel.send(embed=embed, view=view)
            except discord.HTTPException as exc:
                log.warning("Could not post panel: %s", exc)

    async def _delete_panel(self) -> None:
        msg, self.panel = self.panel, None
        if msg is not None:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

    async def close_panel(self, embed: discord.Embed, view: discord.ui.View | None) -> None:
        """Replace the live panel with a final card (e.g. "Stopped — Resume?")."""
        if self._panel_task and not self._panel_task.done():
            self._panel_task.cancel()
        if self._panel_view is not None:
            self._panel_view.stop()
            self._panel_view = None
        msg, self.panel = self.panel, None
        channel = self.text_channel
        if msg is not None and channel is not None and getattr(channel, "last_message_id", None) == msg.id:
            try:
                await msg.edit(embed=embed, view=view)
                return
            except discord.HTTPException:
                pass
        if msg is not None:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
        if channel is not None:
            try:
                await channel.send(embed=embed, view=view)
            except discord.HTTPException as exc:
                log.warning("Could not post final card: %s", exc)

    # ---------------------------------------------------------- alone handling

    def on_members_changed(self) -> None:
        vc = self.vc
        if not vc or not vc.channel:
            return
        if has_human(vc.channel):
            if self.alone_task and not self.alone_task.done():
                self.alone_task.cancel()
            self.alone_task = None
        elif self.alone_task is None or self.alone_task.done():
            self.alone_task = asyncio.ensure_future(self._leave_if_alone())

    async def _leave_if_alone(self) -> None:
        try:
            await asyncio.sleep(ALONE_TIMEOUT)
        except asyncio.CancelledError:
            return
        vc = self.vc
        if not vc or not vc.channel or has_human(vc.channel):
            return
        # Nobody is here to answer a prompt, so save anything worth resuming.
        saved = await self.shutdown(save=True)
        await self.close_panel(*views.ended_card(self, "👋 Everyone left, so I did too.", saved))

    # ------------------------------------------------------------ player loop

    def _reset_timing(self) -> None:
        self._play_started = 0.0
        self._offset = 0.0
        self._paused_at = None
        self._paused_total = 0.0

    def _begin_timing(self, offset: float) -> None:
        self._offset = offset
        self._play_started = time.monotonic()
        self._paused_at = None
        self._paused_total = 0.0

    def _prefetch_next(self) -> None:
        """Resolve the next track's stream in the background so it starts instantly."""
        nxt = self.queue[0] if self.queue else None
        if nxt is None or nxt.has_stream:
            return
        if self._prefetch and self._prefetch[0] is nxt and not self._prefetch[1].done():
            return

        async def _go() -> None:
            try:
                await sources.resolve(nxt)
            except Exception as exc:  # the loop retries properly when it's needed
                log.info("Prefetch of %r failed: %s", nxt.title, exc)

        self._prefetch = (nxt, asyncio.ensure_future(_go()))

    async def _say(self, text: str) -> None:
        if self.text_channel is not None:
            try:
                await self.text_channel.send(text)
            except discord.HTTPException:
                pass

    async def _next_track(self) -> Track | None:
        while not self.queue:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                return None
        return self.queue.popleft()

    def _finish(self, track: Track) -> None:
        """Book-keeping after a track ends: history and loop handling."""
        reason = self._interrupt
        track.start_at = 0.0
        if reason in ("stop", "failed", "previous"):
            return  # "previous" already re-queued what it needs
        if reason is None and self.loop_mode == "track":
            self.queue.appendleft(track)
            return
        self.history.append(track)
        if self.loop_mode == "queue" and reason in (None, "skip"):
            self.queue.append(track)

    async def _run(self) -> None:
        try:
            while True:
                track = await self._next_track()
                if track is None:
                    await self.discard()  # nothing left to resume
                    if self.vc:
                        await self.vc.disconnect(force=True)
                    await self.close_panel(*views.ended_card(
                        self, "💤 Left after 5 minutes of silence.", 0))
                    return

                self.current = track
                self._interrupt = None
                self.persist()
                if not await self._play(track):
                    return  # parked after a failed reconnect
                self._finish(track)
                self.current = None
                self._reset_timing()
                self.persist()  # queue advanced; reflect the new head on disk
                if not self.queue:
                    await self.update_panel()  # show the idle state

        except asyncio.CancelledError:
            vc = self.vc
            if vc and vc.is_connected():
                await vc.disconnect(force=True)
        except Exception as exc:
            log.error("Player loop crashed: %s", exc, exc_info=True)
            try:
                saved = await self.park()
                await self.close_panel(*views.ended_card(
                    self, "⚠️ Playback stopped because of an unexpected error.", saved))
            except Exception:
                log.exception("Could not report player crash")
        finally:
            if self.task is asyncio.current_task():
                self.task = None
            self.current = None
            self._source = None

    async def _play(self, track: Track) -> bool:
        """Play one track to completion, riding out drops. Returns False if the
        session had to be parked because voice couldn't be re-established."""
        announced = False
        dropped = False
        failures = 0
        offset, track.start_at = track.start_at, 0.0

        while True:
            # 1. Connect first. After an internet drop this blocks (up to
            #    RECONNECT_DEADLINE) until the link is back and a human is
            #    present, so every network call below runs while we're online.
            vc = await self.ensure_voice()
            if vc is None:
                saved = await self.park()
                self.current = None
                await self.close_panel(*views.ended_card(
                    self, "📡 Couldn't get back into voice within 10 minutes.", saved))
                return False
            if self._interrupt:
                return True  # skipped while we were reconnecting/resolving

            # 2. Resolve a fresh stream URL if needed (lazy entries, expired
            #    URLs, or one dropped after an error). Reuse an in-flight prefetch.
            if self._prefetch and self._prefetch[0] is track:
                await asyncio.wait([self._prefetch[1]])
                self._prefetch = None
            if not track.has_stream:
                try:
                    await sources.resolve(track)
                except SourceError as exc:
                    failures += 1
                    if failures >= 3:
                        self._interrupt = "failed"
                        await self._say(f"⚠️ Couldn't load **{track.title}** ({exc}) — skipping.")
                        return True
                    await asyncio.sleep(5)
                    continue
            if self._interrupt:
                return True

            # 3. Play it.
            self._begin_timing(offset)
            if not announced:
                announced = True
                await self.update_panel(move_to_bottom=True)
                self._prefetch_next()
            else:  # restarted after a drop or a seek: refresh the "ends in" time
                if dropped:
                    self._note(f"📡 Reconnected — resumed at {views.fmt_time(offset)}")
                    dropped = False
                await self.update_panel()

            err = await self._stream(vc, track, offset)
            played = self.position()

            # A seek restarts the same (still fresh) stream at the new offset.
            if self._interrupt == "seek":
                self._interrupt = None
                offset = self._seek_to
                continue
            # Commands like skip intentionally stop the source — not a drop.
            if self._interrupt:
                return True

            # A long outage invalidates the gateway session and kills the voice
            # connection; the `after` callback can then fire with no error even
            # though the song was cut short. So a "clean finish" only counts if
            # we're still connected and the track actually ran its length.
            disconnected = not self.connected
            ended_early = bool(track.duration) and played < track.duration - 10
            if err is None and not disconnected and not ended_early:
                return True

            if err is not None:
                log.warning("Playback error for %r: %s", track.title, err)
            else:
                log.warning("Playback of %r stopped at %.0fs of %ss — treating as a drop.",
                            track.title, played, track.duration or "?")
            failures += 1
            if failures >= 6:
                self._interrupt = "failed"
                await self._say(f"⚠️ Couldn't keep **{track.title}** playing — skipping.")
                return True
            track.stream_url = None       # stream URLs expire; always re-fetch
            offset = max(0.0, played - 2)  # pick up roughly where it cut out
            dropped = True
            await asyncio.sleep(3)

    async def _stream(self, vc: discord.VoiceClient, track: Track, offset: float) -> Exception | None:
        """Stream one source. Returns None on a clean finish, the Exception on error."""
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        result: list[Exception | None] = [None]

        def after(exc: Exception | None) -> None:
            result[0] = exc
            # after() runs in the audio thread; hop back onto the event loop
            loop.call_soon_threadsafe(done.set)

        before = FFMPEG_BEFORE + (f" -ss {offset:.1f}" if offset > 0 else "")
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(track.stream_url, before_options=before, options=FFMPEG_OPTIONS),
            volume=self.volume,
        )
        self._source = source
        vc.play(source, after=after)
        try:
            await done.wait()
        except asyncio.CancelledError:
            if vc.is_connected():
                vc.stop()
            raise
        finally:
            self._source = None
        return result[0]


async def resume_all(bot: commands.Bot) -> None:
    """After a restart, rejoin and resume every live session that still has an
    audience. Sessions with nobody to play for are parked for /continue."""
    for gid_str, snap in session.all_live().items():
        gid = int(gid_str)
        try:
            guild = bot.get_guild(gid)
            if guild is None:
                continue
            voice_channel = guild.get_channel(snap.get("voice_channel_id") or 0)
            text_channel = guild.get_channel(snap.get("text_channel_id") or 0)
            tracks = [Track.from_dict(d) for d in snap.get("tracks", [])]
            if not tracks or voice_channel is None or text_channel is None or not has_human(voice_channel):
                await session.park(gid, snap)
                continue

            if snap.get("panel_id"):  # the previous run's panel is stale now
                try:
                    await text_channel.get_partial_message(snap["panel_id"]).delete()
                except discord.HTTPException:
                    pass

            gp = get(guild)
            tracks[0].start_at = float(snap.get("position") or 0)
            gp.loop_mode = snap.get("loop", "off")
            gp.volume = float(snap.get("volume", 1.0))
            await gp.connect(voice_channel)
            gp.add(tracks)
            gp._note("🔄 Back after a restart — picking up where we left off")
            gp.start(text_channel)
        except Exception as exc:
            log.warning("Could not resume session for guild %s: %s", gid_str, exc)
