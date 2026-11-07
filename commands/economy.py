"""Currency balance, timed card claim, and leaderboard slash commands."""

from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import format_coins, player_card_label, send_player_card
from commands.xp import mark_successful_command
from database import (
    CooldownActive,
    Database,
    InvalidAmount,
    NoEligibleCards,
)


def format_remaining(seconds: int) -> str:
    duration = timedelta(seconds=max(0, int(seconds)))
    hours, remainder = divmod(int(duration.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes}m"


class EconomyCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(
        name="claim",
        description="Claim one GOLD player card once every 30 minutes.",
    )
    async def claim(self, interaction: discord.Interaction) -> None:
        try:
            player, next_claim_at = self.database.claim_gold_card(interaction.user.id)
        except CooldownActive as exc:
            await interaction.response.send_message(
                f"You can claim again in {format_remaining(exc.remaining_seconds)}.",
                ephemeral=True,
            )
            return
        except NoEligibleCards:
            await interaction.response.send_message(
                "No GOLD card rewards are currently available.",
                ephemeral=True,
            )
            return
        except InvalidAmount as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await send_player_card(
            interaction,
            player,
            ephemeral=True,
            content=(
                f"Your claim reward: **{player_card_label(player)}**. "
                f"Next claim: <t:{next_claim_at}:R>."
            ),
        )
        mark_successful_command(interaction)

    @app_commands.command(name="balance", description="Check your coin balance.")
    async def balance(self, interaction: discord.Interaction) -> None:
        amount = self.database.get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"Your balance: **{format_coins(amount)}**.",
            ephemeral=False,
        )
        mark_successful_command(interaction)

    @app_commands.command(name="leaderboard", description="See the top coin balances.")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        rows = self.database.get_leaderboard(10)
        embed = discord.Embed(
            title="Coin Leaderboard",
            color=discord.Color.gold(),
        )
        if not rows:
            embed.description = "No balances have been recorded yet."
        else:
            embed.description = "\n".join(
                f"**{index}.** <@{user_id}> — {format_coins(balance)}"
                for index, (user_id, balance) in enumerate(rows, start=1)
            )
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        mark_successful_command(interaction)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(EconomyCommands(bot.database))
