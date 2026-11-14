import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import player
import session
import storage
import views

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            # Every command needs a guild (voice, per-guild playlists).
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False),
            activity=discord.Activity(type=discord.ActivityType.listening, name="/play"),
        )
        self._resumed = False

    async def setup_hook(self) -> None:
        storage.load()
        session.load()
        player.setup(self)
        # Persistent views: buttons on panels/cards keep working across restarts.
        self.add_view(views.PlayerControls())
        self.add_view(views.SavedQueueView())
        await self.load_extension("cogs.music")
        await self.load_extension("cogs.playlists")
        self.tree.on_error = self.on_app_command_error

        # DEV_GUILD_ID syncs instantly to one server (global sync can take up to an hour).
        dev_guild = os.getenv("DEV_GUILD_ID")
        if dev_guild:
            guild = discord.Object(int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash commands to dev guild %s.", len(synced), dev_guild)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands globally.", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        if not self._resumed:
            self._resumed = True
            await player.resume_all(self)

    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "You can't use that here."
        else:
            log.error("Error in /%s", getattr(interaction.command, "qualified_name", "?"), exc_info=error)
            message = "⚠️ Something went wrong — please try again."
        try:
            await views.reply(interaction, message)
        except discord.HTTPException:
            pass


token = os.getenv("DISCORD_TOKEN")
if not token:
    raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token.")

MusicBot().run(token, log_handler=None)  # logging is configured above
