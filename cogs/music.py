"""Music cog: the few playback commands that have no button equivalent.

Everything else (pause, skip, seek, stop, volume, queue, playlists) lives on the
Now Playing panel's buttons, so there is exactly one way to do each thing."""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

import autocomplete
import player
import session
import sources
import views
from views import reply


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------ play

    @app_commands.command(name="play", description="Play a song, YouTube link or playlist (queues it if something's on)")
    @app_commands.describe(
        query="Song name, YouTube link (video or playlist), or one of your saved playlists",
        play_next="Put it at the front of the queue",
        shuffle="Shuffle the tracks when adding a playlist",
    )
    @app_commands.rename(play_next="next")
    @app_commands.autocomplete(query=autocomplete.play_query)
    async def play(self, interaction: discord.Interaction, query: str,
                   play_next: bool = False, shuffle: bool = False):
        if query.startswith(autocomplete.PLAYLIST_PREFIX):
            await views.play_playlist(interaction, query[len(autocomplete.PLAYLIST_PREFIX):], shuffle=shuffle)
            return

        gp = player.get(interaction.guild)
        channel = await views.voice_target(interaction, gp)
        if channel is None:
            return
        await interaction.response.defer(thinking=True)
        try:
            tracks, label = await sources.find(query)
        except sources.SourceError as exc:
            await reply(interaction, f"⚠️ {exc}", delete_after=20)
            return
        await views.enqueue(interaction, gp, channel, tracks, front=play_next, shuffle=shuffle, label=label)

    # ----------------------------------------------------------------- panel

    @app_commands.command(name="nowplaying", description="Bring the player panel into this channel")
    async def nowplaying(self, interaction: discord.Interaction):
        gp = player.find(interaction.guild_id)
        if gp is None or not gp.connected:
            await reply(interaction, "Nothing is playing right now — start something with `/play`.")
            return
        gp.text_channel = interaction.channel
        await reply(interaction, "👇 Here's the player.")
        await gp.update_panel(move_to_bottom=True)

    # -------------------------------------------------------------- continue

    @app_commands.command(name="continue", description="Bring back the queue saved by the last stop")
    async def continue_(self, interaction: discord.Interaction):
        saved = session.get_saved(interaction.guild_id)
        if not saved or not saved.get("tracks"):
            await reply(interaction, "There's no saved queue. Queues are saved automatically "
                                     "when you press ⏹️ Stop or everyone leaves.")
            return
        gp = player.get(interaction.guild)
        channel = await views.voice_target(interaction, gp)
        if channel is None:
            return
        await interaction.response.defer()
        try:
            count, appended = await gp.restore(channel, interaction.channel)
        except (discord.ClientException, asyncio.TimeoutError) as exc:
            await reply(interaction, f"⚠️ Couldn't join **{channel.name}** ({exc or 'timed out'}).", delete_after=20)
            return
        await reply(interaction, embed=views.restored_embed(interaction.user, count, appended), ephemeral=False)

    # ---------------------------------------------------------------- events

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState, after: discord.VoiceState):
        gp = player.find(member.guild.id)
        if gp is None:
            return
        # Someone dragged the bot to another channel: follow it on reconnects.
        if member.id == self.bot.user.id and after.channel is not None and before.channel != after.channel:
            gp.voice_channel = after.channel
        gp.on_members_changed()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Chat below the panel: float it back to the bottom once things settle.
        if message.guild is not None and (gp := player.find(message.guild.id)) is not None:
            gp.on_channel_message(message)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
