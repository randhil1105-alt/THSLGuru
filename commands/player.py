"""Player lookup and card display slash commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import catalog_card_choices, send_player_card
from commands.xp import mark_successful_command
from database import Database


class PlayerCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(name="card", description="Show a player's full card.")
    @app_commands.describe(player="Choose a player card from the catalog")
    async def card(
        self, interaction: discord.Interaction, player: str
    ) -> None:
        lookup = player.strip()
        record = (
            self.database.get_player_by_id(int(lookup))
            if lookup.isdecimal()
            else self.database.get_player(lookup)
        )
        if record is None:
            await interaction.response.send_message(
                f"No player named **{player}** was found.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await send_player_card(interaction, record)
        mark_successful_command(interaction)

    @card.autocomplete("player")
    async def card_player_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        return catalog_card_choices(self.database.get_players(), current)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(PlayerCommands(bot.database))
