"""Music cog: playback commands. Most day-to-day control happens on the
Now Playing panel's buttons; these commands are the keyboard equivalents."""

import asyncio
from typing import Optional

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

    async def _controllable(self, interaction: discord.Interaction) -> "player.GuildPlayer | None":
        """The guild's player if something is playing and the caller may control it."""
        gp = player.find(interaction.guild_id)
        if gp is None or gp.current is None:
            await reply(interaction, "Nothing is playing right now — start something with `/play`.")
            return None
        if not await views.ensure_listener(interaction, gp):
            return None
        return gp

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

    # -------------------------------------------------------------- controls

    @app_commands.command(name="pause", description="Pause or resume playback")
    async def pause(self, interaction: discord.Interaction):
        if (gp := await self._controllable(interaction)) is None:
            return
        paused = gp.toggle_pause(interaction.user.display_name)
        if paused is None:
            await reply(interaction, "Nothing is playing right now.")
        else:
            await reply(interaction, "⏸️ Paused." if paused else "▶️ Resumed.")

    @app_commands.command(name="skip", description="Skip the current song, or jump ahead in the queue")
    @app_commands.describe(to="Jump straight to this track in the queue")
    @app_commands.autocomplete(to=autocomplete.queue_position)
    async def skip(self, interaction: discord.Interaction, to: Optional[app_commands.Range[int, 1, 1000]] = None):
        if (gp := await self._controllable(interaction)) is None:
            return
        if to is not None and to > len(gp.queue):
            await reply(interaction, f"The queue only has {len(gp.queue)} track(s).")
            return
        title = gp.queue[to - 1].title if to else None
        gp.skip(interaction.user.display_name, to=to)
        await reply(interaction, f"⏭️ Jumping to **{views.clean(title, 80)}**." if title else "⏭️ Skipped.")

    @app_commands.command(name="seek", description="Jump to a time in the current song")
    @app_commands.describe(position="A time like 1:30, or +30 / -10 to move from where you are")
    @app_commands.autocomplete(position=autocomplete.seek_position)
    async def seek(self, interaction: discord.Interaction, position: str):
        if (gp := await self._controllable(interaction)) is None:
            return
        parsed = views.parse_time(position)
        if parsed is None:
            await reply(interaction, "I couldn't read that time — try `1:30`, `90`, `+30` or `-10`.")
            return
        seconds, relative = parsed
        target = gp.seek(gp.position() + seconds if relative else seconds, interaction.user.display_name)
        if target is None:
            await reply(interaction, "This track can't be sought (it's live or still loading).")
            return
        await reply(interaction, f"⏩ Jumped to `{views.fmt_time(target)}`.")

    @app_commands.command(name="stop", description="Stop, leave the voice channel and save the queue for later")
    async def stop(self, interaction: discord.Interaction):
        gp = player.find(interaction.guild_id)
        if gp is None or (not gp.connected and not gp.has_content):
            await reply(interaction, "I'm not playing anything.")
            return
        if not await views.ensure_listener(interaction, gp):
            return
        await interaction.response.defer(ephemeral=True)
        saved = await gp.shutdown(save=True)
        await gp.close_panel(*views.ended_card(gp, f"⏹️ Stopped by {interaction.user.display_name}.", saved))
        await reply(interaction, "Stopped." + (" The queue is saved — press **Resume** or use `/continue`." if saved else ""))

    @app_commands.command(name="volume", description="Show or set the playback volume")
    @app_commands.describe(level="Volume in percent (0–200)")
    async def volume(self, interaction: discord.Interaction,
                     level: Optional[app_commands.Range[int, 0, 200]] = None):
        gp = player.find(interaction.guild_id)
        if level is None:
            current = int((gp.volume if gp else 1.0) * 100)
            await reply(interaction, f"🔊 Volume is **{current}%**.")
            return
        if gp is None:
            gp = player.get(interaction.guild)
        elif not await views.ensure_listener(interaction, gp):
            return
        gp.set_volume(level / 100, interaction.user.display_name)
        await reply(interaction, f"🔊 Volume set to **{level}%**.")

    # ----------------------------------------------------------------- views

    @app_commands.command(name="queue", description="Browse and manage the queue")
    async def queue(self, interaction: discord.Interaction):
        gp = player.find(interaction.guild_id)
        if gp is None or not gp.has_content:
            await reply(interaction, "The queue is empty — add songs with `/play`.")
            return
        view = views.QueueView(gp)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @app_commands.command(name="nowplaying", description="Bring the player panel to the bottom of this channel")
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
                                     "when you `/stop` or everyone leaves.")
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
