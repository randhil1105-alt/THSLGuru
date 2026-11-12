"""Public, once-per-day coin and TOTW pack rewards."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from database import (
    DAILY_REWARD_COINS,
    DAILY_REWARD_TOTW_PACKS,
    DailyCooldownActive,
    Database,
)


def _remaining_time(seconds: int) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 60 * 60)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class DailyCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(
        name="daily",
        description="Claim your daily coins and free TOTW pack.",
    )
    async def daily(self, interaction: discord.Interaction) -> None:
        try:
            result = self.database.claim_daily_reward(
                interaction.user.id,
                interaction.id,
            )
        except DailyCooldownActive as exc:
            await interaction.response.send_message(
                "⏳ DAILY REWARD COOLDOWN\n"
                f"You can claim again in **{_remaining_time(exc.remaining_seconds)}**.",
                ephemeral=False,
            )
            return

        if not result["awarded"]:
            await interaction.response.send_message(
                "🎁 DAILY REWARDS ALREADY CLAIMED!\n"
                "This claim was already processed.",
                ephemeral=False,
            )
            return

        player = getattr(interaction.user, "mention", f"<@{interaction.user.id}>")
        embed = discord.Embed(
            title="🎁 DAILY REWARDS CLAIMED!",
            description=(
                f"Player: {player}\n\n"
                "Rewards:\n"
                f"- {DAILY_REWARD_TOTW_PACKS}x TOTW Pack\n"
                f"- {DAILY_REWARD_COINS:,} coins\n\n"
                "Come back in 24 hours for your next reward!"
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(DailyCommands(bot.database))
