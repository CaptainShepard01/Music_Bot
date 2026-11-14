"""Playlist cog: the /playlist command group (shared, per-guild playlists).

Playlists are created on the fly by ``add`` / ``save`` (or the ⭐ button on the
player), so there is no separate "create" step."""

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

    @group.command(name="play", description="Play a saved playlist")
    @app_commands.describe(name="Playlist to play", shuffle="Shuffle the tracks",
                           replace="Replace the current queue instead of adding to it")
    @app_commands.autocomplete(name=autocomplete.playlist_name)
    async def play(self, interaction: discord.Interaction, name: str,
                   shuffle: bool = False, replace: bool = False):
        await views.play_playlist(interaction, name, shuffle=shuffle, replace=replace)

    @group.command(name="view", description="Browse playlists, or open one")
    @app_commands.describe(name="Playlist to open (leave empty to list them all)")
    @app_commands.autocomplete(name=autocomplete.playlist_name)
    async def view(self, interaction: discord.Interaction, name: Optional[str] = None):
        if name and storage.get(interaction.guild_id, name) is None:
            await reply(interaction, f"No playlist named **{clean(name, 50)}**.")
            return
        browser = views.PlaylistBrowser(interaction.guild_id, name)
        await interaction.response.send_message(embed=browser.embed(), view=browser)

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

    @group.command(name="save", description="Save the current song and the whole queue as a playlist")
    @app_commands.describe(name="Playlist to save into — type a new name to create one")
    @app_commands.autocomplete(name=autocomplete.playlist_name_or_new)
    async def save(self, interaction: discord.Interaction, name: str):
        gp = player.find(interaction.guild_id)
        if gp is None or not gp.has_content:
            await reply(interaction, "The queue is empty — there's nothing to save.")
            return
        await self._save(interaction, name, ([gp.current] if gp.current else []) + list(gp.queue))

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

    @group.command(name="remove", description="Remove a song from a playlist")
    @app_commands.describe(name="Playlist", track="Song to remove")
    @app_commands.autocomplete(name=autocomplete.playlist_name, track=autocomplete.playlist_track)
    async def remove(self, interaction: discord.Interaction, name: str, track: str):
        try:
            index = int(track)
        except ValueError:
            await reply(interaction, "Pick a song from the suggestions list.")
            return
        try:
            removed = await storage.remove_track(interaction.guild_id, name, index)
        except storage.PlaylistError as exc:
            await reply(interaction, str(exc))
            return
        await reply(interaction, f"🗑️ Removed **{clean(removed.get('title', 'Unknown'), 80)}** "
                                 f"from **{storage.resolve_name(interaction.guild_id, name)}**.")

    @group.command(name="delete", description="Delete a playlist")
    @app_commands.describe(name="Playlist to delete")
    @app_commands.autocomplete(name=autocomplete.playlist_name)
    async def delete(self, interaction: discord.Interaction, name: str):
        pl = storage.get(interaction.guild_id, name)
        if not pl:
            await reply(interaction, f"No playlist named **{clean(name, 50)}**.")
            return
        key = storage.resolve_name(interaction.guild_id, name)
        confirm = views.ConfirmView()
        await interaction.response.send_message(
            f"Delete **{key}** ({len(pl['tracks'])} track(s))? This can't be undone.",
            view=confirm, ephemeral=True)
        await confirm.wait()
        if confirm.confirmed:
            try:
                await storage.delete(interaction.guild_id, key)
                msg = f"🗑️ Deleted **{key}**."
            except storage.PlaylistError as exc:
                msg = str(exc)
        else:
            msg = "Cancelled."
        await interaction.edit_original_response(content=msg, view=None)


async def setup(bot: commands.Bot):
    await bot.add_cog(Playlists(bot))
