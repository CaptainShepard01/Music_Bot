"""Playlist cog: ``/playlist view`` and ``/playlist add`` (shared, per-guild playlists).

Everything else is on buttons: playing, removing songs and deleting playlists
happen in the browser (also behind 📚 on the player), saving the queue is in
📜 Queue, and ⭐ saves the current song. Playlists are created on the fly, so
there is no separate "create" step."""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import autocomplete
import player
import sources
import storage
import views
from views import clean, reply


class Playlists(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.Group(name="playlist", description="Save, browse and play shared playlists")

    @group.command(name="view", description="Browse, play and edit playlists")
    @app_commands.describe(name="Playlist to open (leave empty to list them all)")
    @app_commands.autocomplete(name=autocomplete.playlist_name)
    async def view(self, interaction: discord.Interaction, name: Optional[str] = None):
        if name and storage.get(interaction.guild_id, name) is None:
            await reply(interaction, f"No playlist named **{clean(name, 50)}**.")
            return
        browser = views.PlaylistBrowser(interaction.guild_id, name)
        await interaction.response.send_message(embed=browser.embed(), view=browser, ephemeral=True)

    @group.command(name="add", description="Add a song to a playlist (creates the playlist if it's new)")
    @app_commands.describe(name="Playlist to add to — type a new name to create one",
                           query="Song name or YouTube link — leave empty to add what's playing now")
    @app_commands.autocomplete(name=autocomplete.playlist_name_or_new, query=autocomplete.track_query)
    async def add(self, interaction: discord.Interaction, name: str, query: Optional[str] = None):
        if query is None:
            gp = player.find(interaction.guild_id)
            if gp is None or gp.current is None:
                await reply(interaction, "Nothing is playing — tell me what to add with the `query` option.")
                return
            tracks = [gp.current]
        else:
            await interaction.response.defer(ephemeral=True)
            try:
                tracks, _label = await sources.find(query)
            except sources.SourceError as exc:
                await reply(interaction, f"⚠️ {exc}")
                return
        await self._save(interaction, name, tracks)

    async def _save(self, interaction: discord.Interaction, name: str, tracks: list) -> None:
        try:
            key, created = await storage.add_tracks(
                interaction.guild_id, name,
                [t.to_dict(include_requester=False) for t in tracks], interaction.user.id)
        except storage.PlaylistError as exc:
            await reply(interaction, str(exc))
            return
        what = f"**{clean(tracks[0].title, 80)}**" if len(tracks) == 1 else f"**{len(tracks)} tracks**"
        note = " (new playlist)" if created else ""
        await reply(interaction, f"✅ Saved {what} to **{key}**{note}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Playlists(bot))
