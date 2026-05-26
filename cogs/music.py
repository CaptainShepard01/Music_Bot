"""Music cog: voice playback and queue commands."""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

import player


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="join", description="Join your current voice channel")
    async def join(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        await interaction.response.defer()
        channel = interaction.user.voice.channel
        state = player.get_state(interaction.guild.id)
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.move_to(channel)
        else:
            await channel.connect()
        state["voice_channel"] = channel
        state["text_channel"] = interaction.channel
        await interaction.followup.send(f"Joined **{channel.name}**.")

    @app_commands.command(name="play", description="Play a YouTube link or search for a song")
    @app_commands.describe(query="YouTube URL or search terms")
    async def play(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return

        await interaction.response.defer()

        voice_channel = interaction.user.voice.channel
        state = player.get_state(interaction.guild.id)

        if not interaction.guild.voice_client:
            await voice_channel.connect()
        state["voice_channel"] = voice_channel
        state["text_channel"] = interaction.channel

        if not player._is_url(query):
            results = await player.fetch_search_results(query)
            if not results:
                await interaction.followup.send("No results found for that search.")
                return

            async def on_pick(info: dict, picker: discord.Interaction):
                title = info.get("title") or "Unknown"
                await state["queue"].put(info)
                if state["task"] is None or state["task"].done():
                    state["task"] = asyncio.ensure_future(
                        player.player_loop(interaction.guild, interaction.channel)
                    )
                    content = f"Loading: **{title}**"
                else:
                    content = f"Added to queue (#{state['queue'].qsize()}): **{title}**"
                await picker.response.edit_message(content=content, view=None)

            view = player.SearchSelectView(results, interaction.user.id, on_pick)
            msg = await interaction.followup.send("Select a song:", view=view)
            view.message = msg
            return

        info = await player.fetch_info(query)
        if info is None:
            await interaction.followup.send("Could not find or extract audio for that link/query.")
            return

        await state["queue"].put(info)
        title = info.get("title", "Unknown")

        if state["task"] is None or state["task"].done():
            state["task"] = asyncio.ensure_future(
                player.player_loop(interaction.guild, interaction.channel)
            )
            await interaction.followup.send(f"Loading: **{title}**")
        else:
            await interaction.followup.send(
                f"Added to queue (#{state['queue'].qsize()}): **{title}**"
            )

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("Paused.")
        else:
            await interaction.response.send_message("Nothing is playing.")

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("Resumed.")
        else:
            await interaction.response.send_message("Nothing is paused.")

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            player.get_state(interaction.guild.id)["current"] = None
            vc.stop()
            await interaction.response.send_message("Skipped.")
        else:
            await interaction.response.send_message("Nothing to skip.")

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        state = player.get_state(interaction.guild.id)
        player.clear_queue(state)
        if interaction.guild.voice_client:
            interaction.guild.voice_client.stop()
        await interaction.response.send_message("Stopped and cleared the queue.")

    @app_commands.command(name="queue", description="Show the current queue")
    async def queue_cmd(self, interaction: discord.Interaction):
        state = player.get_state(interaction.guild.id)
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

    @app_commands.command(name="leave", description="Disconnect the bot from voice")
    async def leave(self, interaction: discord.Interaction):
        state = player.get_state(interaction.guild.id)
        player.clear_queue(state)
        if state["task"] and not state["task"].done():
            state["task"].cancel()
        if interaction.guild.voice_client:
            await interaction.response.defer()
            await interaction.guild.voice_client.disconnect()
            await interaction.followup.send("Disconnected.")
        else:
            await interaction.response.send_message("I'm not in a voice channel.")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        _before: discord.VoiceState,
        _after: discord.VoiceState,
    ):
        guild = member.guild
        vc = guild.voice_client
        if not vc or not vc.channel:
            return

        human_members = [m for m in vc.channel.members if not m.bot]
        state = player.get_state(guild.id)

        if human_members:
            alone_task = state.get("alone_task")
            if alone_task and not alone_task.done():
                alone_task.cancel()
            state["alone_task"] = None
            return

        if state.get("alone_task") and not state["alone_task"].done():
            return

        async def _leave_if_alone() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return
            vc2 = guild.voice_client
            if vc2 and vc2.channel:
                if not any(not m.bot for m in vc2.channel.members):
                    player.clear_queue(state)
                    if state["task"] and not state["task"].done():
                        state["task"].cancel()
                    await vc2.disconnect()
                    text_ch = state.get("text_channel")
                    if text_ch:
                        await text_ch.send("Everyone left — disconnected from voice.")
            state["alone_task"] = None

        state["alone_task"] = asyncio.ensure_future(_leave_if_alone())


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
