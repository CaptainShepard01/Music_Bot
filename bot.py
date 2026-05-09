import asyncio
import logging
import os

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
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

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Per-guild state
guild_state: dict[int, dict] = {}


def get_state(guild_id: int) -> dict:
    if guild_id not in guild_state:
        guild_state[guild_id] = {
            "queue": asyncio.Queue(),
            "task": None,
            "voice_channel": None,  # last known VoiceChannel; used to rejoin after drop
            "current": None,        # info dict of the track currently playing
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
    now_playing_msg: discord.Message | None = None

    async def update_now_playing(content: str) -> None:
        nonlocal now_playing_msg
        if now_playing_msg:
            try:
                await now_playing_msg.edit(content=content)
                return
            except discord.HTTPException:
                pass
        now_playing_msg = await channel.send(content)

    try:
        while True:
            try:
                info = await asyncio.wait_for(state["queue"].get(), timeout=300)
            except asyncio.TimeoutError:
                vc = guild.voice_client
                if vc:
                    await vc.disconnect()
                await update_now_playing("Left voice channel due to inactivity.")
                break

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
                    await update_now_playing(f"Now playing: **{title}**")
                    announced = True
                elif attempt > 0:
                    await update_now_playing(f"Now playing: **{title}** (reconnected, attempt {attempt + 1})")

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
    finally:
        state["task"] = None
        state["current"] = None


class SearchSelectView(discord.ui.View):
    def __init__(
        self,
        results: list[dict],
        guild: discord.Guild,
        state: dict,
        text_channel: discord.TextChannel,
        invoker_id: int,
    ):
        super().__init__(timeout=60)
        self.results = results
        self.guild = guild
        self.state = state
        self.text_channel = text_channel
        self.invoker_id = invoker_id
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
        title = info.get("title") or "Unknown"

        await self.state["queue"].put(info)

        if self.state["task"] is None or self.state["task"].done():
            self.state["task"] = asyncio.ensure_future(
                player_loop(self.guild, self.text_channel)
            )
            content = f"Loading: **{title}**"
        else:
            pos = self.state["queue"].qsize()
            content = f"Added to queue (#{pos}): **{title}**"

        await interaction.response.edit_message(content=content, view=None)
        self.stop()

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(content="Search timed out.", view=None)
            except discord.HTTPException:
                pass


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print("Slash commands synced.")


@bot.tree.command(name="join", description="Join your current voice channel")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message("You need to be in a voice channel first.")
        return
    await interaction.response.defer()
    channel = interaction.user.voice.channel
    state = get_state(interaction.guild.id)
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.move_to(channel)
    else:
        await channel.connect()
    state["voice_channel"] = channel
    await interaction.followup.send(f"Joined **{channel.name}**.")


@bot.tree.command(name="play", description="Play a YouTube link or search for a song")
@app_commands.describe(query="YouTube URL or search terms")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        await interaction.response.send_message("You need to be in a voice channel first.")
        return

    await interaction.response.defer()

    voice_channel = interaction.user.voice.channel
    state = get_state(interaction.guild.id)

    if not interaction.guild.voice_client:
        await voice_channel.connect()
    state["voice_channel"] = voice_channel

    if not _is_url(query):
        results = await fetch_search_results(query)
        if not results:
            await interaction.followup.send("No results found for that search.")
            return
        view = SearchSelectView(results, interaction.guild, state, interaction.channel, interaction.user.id)
        msg = await interaction.followup.send("Select a song:", view=view)
        view.message = msg
        return

    info = await fetch_info(query)
    if info is None:
        await interaction.followup.send("Could not find or extract audio for that link/query.")
        return

    await state["queue"].put(info)
    title = info.get("title", "Unknown")

    if state["task"] is None or state["task"].done():
        state["task"] = asyncio.ensure_future(player_loop(interaction.guild, interaction.channel))
        await interaction.followup.send(f"Loading: **{title}**")
    else:
        pos = state["queue"].qsize()
        await interaction.followup.send(f"Added to queue (#{pos}): **{title}**")


@bot.tree.command(name="pause", description="Pause playback")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Paused.")
    else:
        await interaction.response.send_message("Nothing is playing.")


@bot.tree.command(name="resume", description="Resume playback")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Resumed.")
    else:
        await interaction.response.send_message("Nothing is paused.")


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("Skipped.")
    else:
        await interaction.response.send_message("Nothing to skip.")


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    clear_queue(state)
    if interaction.guild.voice_client:
        interaction.guild.voice_client.stop()
    await interaction.response.send_message("Stopped and cleared the queue.")


@bot.tree.command(name="queue", description="Show the current queue")
async def queue_cmd(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    current = state.get("current")
    items = list(state["queue"]._queue)
    if not current and not items:
        await interaction.response.send_message("The queue is empty.")
        return
    lines = []
    if current:
        lines.append(f"Now playing: **{current.get('title', 'Unknown')}**")
    lines += [f"{i + 1}. **{item.get('title', 'Unknown')}**" for i, item in enumerate(items)]
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="leave", description="Disconnect the bot from voice")
async def leave(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    clear_queue(state)
    if state["task"] and not state["task"].done():
        state["task"].cancel()
    if interaction.guild.voice_client:
        await interaction.response.defer()
        await interaction.guild.voice_client.disconnect()
        await interaction.followup.send("Disconnected.")
    else:
        await interaction.response.send_message("I'm not in a voice channel.")


bot.run(os.getenv("DISCORD_TOKEN"))
