"""Player lookup and card display slash commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import player_card_label, send_player_card
from database import Database


class PlayerCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(name="card", description="Show a player's full card.")
    @app_commands.describe(player="Choose a player card from the catalog")
    async def card(
        self, interaction: discord.Interaction, player: str
    ) -> None:
        record = self.database.get_player(player)
        if record is None:
            await interaction.response.send_message(
                f"No player named **{player}** was found.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await send_player_card(interaction, record)

    @card.autocomplete("player")
    async def card_player_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        query = current.strip().casefold()
        choices: list[app_commands.Choice[str]] = []
        seen_names: set[str] = set()
        for record in self.database.get_players():
            normalized_name = record.name.casefold()
            if normalized_name in seen_names:
                continue
            label = player_card_label(record)
            if query and query not in normalized_name and query not in label.casefold():
                continue
            seen_names.add(normalized_name)
            choices.append(
                app_commands.Choice(name=label[:100], value=record.name)
            )
            if len(choices) == 25:
                break
        return choices


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(PlayerCommands(bot.database))
