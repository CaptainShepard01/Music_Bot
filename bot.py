import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

import player
import session
import storage

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


async def setup_hook():
    storage.load()
    session.load()
    await bot.load_extension("cogs.music")
    await bot.load_extension("cogs.playlists")
    await bot.tree.sync()
    print("Slash commands synced.")


bot.setup_hook = setup_hook

_resumed = False


async def _resume_sessions():
    """After a restart, rejoin and resume any session that was interrupted
    unintentionally (auto_resume=True) when a human is still in the channel.
    Parked sessions are left untouched for /continue."""
    for gid_str, saved in session.all().items():
        if not saved.get("auto_resume"):
            continue
        try:
            gid = int(gid_str)
            guild = bot.get_guild(gid)
            if guild is None:
                continue
            voice_channel = guild.get_channel(saved.get("voice_channel_id"))
            text_channel = guild.get_channel(saved.get("text_channel_id"))
            if voice_channel is None or text_channel is None:
                continue
            if not any(not m.bot for m in voice_channel.members):
                continue  # nobody to play for; keep it on disk for /continue

            state = player.get_state(gid)
            state["voice_channel"] = voice_channel
            state["text_channel"] = text_channel
            if guild.voice_client is None:
                await voice_channel.connect()

            entries = []
            if saved.get("current"):
                entries.append(saved["current"])
            entries.extend(saved.get("queue", []))
            if entries:
                await player.enqueue_and_start(guild, text_channel, entries, replace=True)
                await text_channel.send("Reconnected after a restart — resuming the queue.")
        except Exception as exc:
            log.warning("Could not resume session for guild %s: %s", gid_str, exc)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    global _resumed
    if not _resumed:
        _resumed = True
        await _resume_sessions()


token = os.getenv("DISCORD_TOKEN")
if not token:
    raise SystemExit(
        "DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token."
    )

bot.run(token)
