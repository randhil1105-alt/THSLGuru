"""Inventory, collection, and card sale slash commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import format_coins, inventory_embed, player_card_label
from database import (
    Database,
    EconomyError,
    InsufficientCards,
    PlayerNotFound,
)


class InventoryCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(name="inventory", description="View the cards you own.")
    async def inventory(self, interaction: discord.Interaction) -> None:
        entries = self.database.get_inventory(interaction.user.id)
        embed = inventory_embed("Your Inventory", entries)
        if not entries:
            embed.description = "Your inventory is empty. Claim coins or open a pack to get started."
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="collection", description="See the card catalog and your collection.")
    async def collection(self, interaction: discord.Interaction) -> None:
        entries = self.database.get_collection(interaction.user.id)
        embed = inventory_embed("Player Collection", entries, collection=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="sell", description="Sell owned copies of a player card.")
    @app_commands.describe(
        player="Player card to sell",
        quantity="Number of copies to sell",
    )
    async def sell(
        self,
        interaction: discord.Interaction,
        player: str,
        quantity: app_commands.Range[int, 1, 1000] = 1,
    ) -> None:
        try:
            proceeds, balance = self.database.sell_card(
                interaction.user.id,
                player,
                int(quantity),
            )
        except (InsufficientCards, PlayerNotFound) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except EconomyError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        card = self.database.get_player(player)
        if card is None:
            raise RuntimeError("Sold card was not found in the player catalog.")
        await interaction.response.send_message(
            f"Sold **{quantity}× {player_card_label(card)}** "
            f"for {format_coins(proceeds)}. "
            f"Your balance is now {format_coins(balance)}.",
            ephemeral=True,
        )


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(InventoryCommands(bot.database))
