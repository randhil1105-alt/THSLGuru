"""Player lookup and card display slash commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import send_player_card
from database import Database


class PlayerCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(name="player", description="Look up a hockey card player.")
    @app_commands.describe(name="Player name, such as Dan")
    async def player(
        self, interaction: discord.Interaction, name: str
    ) -> None:
        record = self.database.get_player(name)
        if record is None:
            await interaction.response.send_message(
                f"No player named **{name}** was found.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await send_player_card(interaction, record)

    @app_commands.command(name="card", description="Show a player's full card.")
    @app_commands.describe(name="Player name, such as Dan")
    async def card(
        self, interaction: discord.Interaction, name: str
    ) -> None:
        record = self.database.get_player(name)
        if record is None:
            await interaction.response.send_message(
                f"No player named **{name}** was found.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await send_player_card(interaction, record)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(PlayerCommands(bot.database))
