"""Shared playback engine: queue state, yt-dlp helpers, and the player loop.

Both the Music and Playlist cogs build on the functions here.
"""

import asyncio
import logging
import random

import discord
import yt_dlp

log = logging.getLogger(__name__)

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "socket_timeout": 15,
    "retries": 3,
}

# Per-guild state
guild_state: dict[int, dict] = {}


def get_state(guild_id: int) -> dict:
    if guild_id not in guild_state:
        guild_state[guild_id] = {
            "queue": asyncio.Queue(),
            "task": None,
            "voice_channel": None,  # last known VoiceChannel; used to rejoin after drop
            "current": None,        # info dict of the track currently playing
            "alone_task": None,     # pending task to disconnect when bot is alone
            "text_channel": None,   # last text channel used for commands
        }
    return guild_state[guild_id]


def clear_queue(state: dict) -> None:
    while not state["queue"].empty():
        state["queue"].get_nowait()
        state["queue"].task_done()


def _is_url(query: str) -> bool:
    return query.startswith("http://") or query.startswith("https://")


async def fetch_info(query: str) -> dict | None:
    loop = asyncio.get_running_loop()

    def _extract() -> dict:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as y:
            return y.extract_info(query, download=False)

    try:
        info = await loop.run_in_executor(None, _extract)
    except yt_dlp.utils.DownloadError:
        return None
    if "entries" in info:
        info = info["entries"][0]
    return info


async def fetch_search_results(query: str, max_results: int = 5) -> list[dict]:
    loop = asyncio.get_running_loop()

    def _extract() -> dict:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as y:
            return y.extract_info(f"ytsearch{max_results}:{query}", download=False)

    try:
        result = await loop.run_in_executor(None, _extract)
    except yt_dlp.utils.DownloadError:
        return []
    return [e for e in result.get("entries", []) if e]


async def ensure_voice(guild: discord.Guild, state: dict) -> discord.VoiceClient | None:
    """Return a live VoiceClient, reconnecting with backoff if the connection dropped."""
    vc = guild.voice_client
    if vc and vc.is_connected():
        return vc

    voice_channel = state.get("voice_channel")
    if voice_channel is None:
        return None

    # Exponential backoff: try immediately, then after 5 s, 15 s, 30 s
    for delay in (0, 5, 15, 30):
        if delay:
            await asyncio.sleep(delay)
        try:
            if guild.voice_client:
                await guild.voice_client.disconnect(force=True)
            return await voice_channel.connect()
        except Exception as exc:
            log.warning("Voice reconnect attempt failed: %s", exc)

    return None


async def play_track(vc: discord.VoiceClient, info: dict) -> Exception | None:
    """Stream one track. Returns None on clean finish, the Exception on error."""
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    result: list[Exception | None] = [None]

    def after(exc: Exception | None) -> None:
        result[0] = exc
        # after() runs in the audio thread; schedule set() onto the event loop
        loop.call_soon_threadsafe(done.set)

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(info["url"], **FFMPEG_OPTIONS)
    )
    vc.play(source, after=after)

    try:
        await done.wait()
    except asyncio.CancelledError:
        if vc.is_connected():
            vc.stop()
        raise

    return result[0]


async def player_loop(guild: discord.Guild, channel: discord.TextChannel) -> None:
    state = get_state(guild.id)
    try:
        while True:
            try:
                info = await asyncio.wait_for(state["queue"].get(), timeout=300)
            except asyncio.TimeoutError:
                vc = guild.voice_client
                if vc:
                    await vc.disconnect()
                await channel.send("Left voice channel due to inactivity.")
                break

            # Lazy resolution: playlist entries carry only a webpage URL/title, not
            # an expiring stream URL. Resolve it now so play_track has info["url"].
            if "url" not in info:
                ref = info.get("webpage_url") or info.get("title")
                resolved = await fetch_info(ref) if ref else None
                if resolved is None:
                    await channel.send(
                        f"Skipping **{info.get('title', 'Unknown')}** — couldn't load it."
                    )
                    continue
                # Keep the stored title if the resolved info is missing one.
                resolved.setdefault("title", info.get("title", "Unknown"))
                info = resolved

            state["current"] = info
            title = info.get("title", "Unknown")
            announced = False

            for attempt in range(4):  # first try + up to 3 retries
                if attempt > 0:
                    await asyncio.sleep(5 * attempt)  # 5 s, 10 s, 15 s back-off

                    # Re-fetch a fresh stream URL — the previous one may have expired
                    ref = info.get("webpage_url") or info.get("original_url") or title
                    fresh = await fetch_info(ref)
                    if fresh is None:
                        await channel.send(f"Could not recover stream for **{title}**, skipping.")
                        break
                    info = fresh
                    state["current"] = info

                vc = await ensure_voice(guild, state)
                if vc is None:
                    await channel.send("Could not reconnect to voice channel — stopping playback.")
                    clear_queue(state)
                    return

                if not announced:
                    await channel.send(f"Now playing: **{title}**")
                    announced = True
                elif attempt > 0:
                    await channel.send(f"Reconnected — resuming **{title}** (attempt {attempt + 1})")

                err = await play_track(vc, info)

                if err is None:
                    break  # clean finish, move to next track

                log.warning("Playback error on attempt %d for %r: %s", attempt + 1, title, err)
                if attempt == 3:
                    await channel.send(f"Playback failed after several attempts, skipping **{title}**.")

            state["current"] = None

    except asyncio.CancelledError:
        vc = guild.voice_client
        if vc and vc.is_connected():
            await vc.disconnect()
    except Exception as exc:
        log.error("Player loop crashed: %s", exc, exc_info=True)
        try:
            text_ch = state.get("text_channel") or channel
            await text_ch.send("Playback stopped due to an unexpected error. Use /play to resume the queue.")
        except Exception:
            pass
    finally:
        state["task"] = None
        state["current"] = None


async def enqueue_and_start(
    guild: discord.Guild,
    text_channel: discord.TextChannel,
    tracks: list[dict],
    *,
    replace: bool = False,
    shuffle: bool = False,
) -> int:
    """Add tracks to the queue and ensure the player loop is running.

    Each entry may be a fully-resolved info dict (with ``url``) or a lightweight
    ``{"webpage_url", "title"}`` dict that player_loop resolves lazily.
    Returns the number of tracks enqueued.
    """
    state = get_state(guild.id)
    state["text_channel"] = text_channel

    if replace:
        clear_queue(state)
        vc = guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            state["current"] = None
            vc.stop()

    tracks = list(tracks)
    if shuffle:
        random.shuffle(tracks)

    for track in tracks:
        await state["queue"].put(track)

    if state["task"] is None or state["task"].done():
        state["task"] = asyncio.ensure_future(player_loop(guild, text_channel))

    return len(tracks)


class SearchSelectView(discord.ui.View):
    """Dropdown of search results. ``on_pick(info, interaction)`` decides what
    happens to the chosen track (enqueue, save to a playlist, etc.)."""

    def __init__(
        self,
        results: list[dict],
        invoker_id: int,
        on_pick,
    ):
        super().__init__(timeout=60)
        self.results = results
        self.invoker_id = invoker_id
        self.on_pick = on_pick
        self.message: discord.Message | None = None

        options = []
        for i, info in enumerate(results):
            title = (info.get("title") or "Unknown")[:100]
            duration = info.get("duration") or 0
            mins, secs = divmod(int(duration), 60)
            uploader = (info.get("uploader") or "")[:50]
            desc = f"{uploader} • {mins}:{secs:02d}" if uploader else f"{mins}:{secs:02d}"
            options.append(discord.SelectOption(label=title, value=str(i), description=desc[:100]))

        select = discord.ui.Select(placeholder="Choose a song…", options=options)
        select.callback = self._on_select
        self.add_item(select)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who searched can cancel.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Search cancelled.", view=None)
        self.stop()

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who searched can pick a song.", ephemeral=True)
            return

        idx = int(interaction.data["values"][0])
        info = self.results[idx]
        await self.on_pick(info, interaction)
        self.stop()

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(content="Search timed out.", view=None)
            except discord.HTTPException:
                pass
