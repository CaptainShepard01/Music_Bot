import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

import storage

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


async def setup_hook():
    storage.load()
    await bot.load_extension("cogs.music")
    await bot.load_extension("cogs.playlists")
    await bot.tree.sync()
    print("Slash commands synced.")


bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


bot.run(os.getenv("DISCORD_TOKEN"))
