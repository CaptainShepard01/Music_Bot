import asyncio
import os

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

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
ydl = yt_dlp.YoutubeDL(YDL_OPTIONS)

# Per-guild state: {guild_id: {"queue": asyncio.Queue, "task": asyncio.Task}}
guild_state: dict[int, dict] = {}


def get_state(guild_id: int) -> dict:
    if guild_id not in guild_state:
        guild_state[guild_id] = {"queue": asyncio.Queue(), "task": None}
    return guild_state[guild_id]


def clear_queue(state: dict) -> None:
    while not state["queue"].empty():
        state["queue"].get_nowait()
        state["queue"].task_done()


async def fetch_info(query: str) -> dict | None:
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))
    except yt_dlp.utils.DownloadError:
        return None
    if "entries" in info:
        info = info["entries"][0]
    return info


async def player_loop(guild: discord.Guild, channel: discord.TextChannel):
    state = get_state(guild.id)
    try:
        while True:
            try:
                info = await asyncio.wait_for(state["queue"].get(), timeout=300)
            except asyncio.TimeoutError:
                if guild.voice_client:
                    await guild.voice_client.disconnect()
                await channel.send("Left voice channel due to inactivity.")
                break

            vc = guild.voice_client
            if vc is None or not vc.is_connected():
                break

            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(info["url"], **FFMPEG_OPTIONS)
            )

            done = asyncio.Event()
            vc.play(source, after=lambda _: done.set())
            await channel.send(f"Now playing: **{info.get('title', 'Unknown')}**")
            try:
                await done.wait()
            except asyncio.CancelledError:
                vc.stop()
                raise
    except asyncio.CancelledError:
        vc = guild.voice_client
        if vc and vc.is_connected():
            await vc.disconnect()
    finally:
        state["task"] = None


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
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.move_to(channel)
    else:
        await channel.connect()
    await interaction.followup.send(f"Joined **{channel.name}**.")


@bot.tree.command(name="play", description="Play a YouTube link or search for a song")
@app_commands.describe(query="YouTube URL or search terms")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        await interaction.response.send_message("You need to be in a voice channel first.")
        return

    await interaction.response.defer()

    if not interaction.guild.voice_client:
        await interaction.user.voice.channel.connect()

    info = await fetch_info(query)
    if info is None:
        await interaction.followup.send("Could not find or extract audio for that link/query.")
        return

    state = get_state(interaction.guild.id)
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
    items = list(state["queue"]._queue)
    if not items:
        await interaction.response.send_message("The queue is empty.")
        return
    lines = [f"{i + 1}. **{item.get('title', 'Unknown')}**" for i, item in enumerate(items)]
    await interaction.response.send_message("Queue:\n" + "\n".join(lines))


@bot.tree.command(name="leave", description="Disconnect the bot from voice")
async def leave(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    clear_queue(state)
    if interaction.guild.voice_client:
        await interaction.response.defer()
        await interaction.guild.voice_client.disconnect()
        await interaction.followup.send("Disconnected.")
    else:
        await interaction.response.send_message("I'm not in a voice channel.")


bot.run(os.getenv("DISCORD_TOKEN"))
