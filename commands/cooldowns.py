"""Personal cooldown status for existing THSL Guru commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.xp import mark_successful_command
from database import Database


def format_cooldown_time(seconds: int) -> str:
    remaining = max(0, int(seconds))
    hours, remainder = divmod(remaining, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


class CooldownCommands(discord_commands.Cog):
    def __init__(self, bot: discord_commands.Bot, database: Database) -> None:
        self.bot = bot
        self.database = database

    @app_commands.command(
        name="cooldowns",
        description="Check your claim, Guess Who, and Draft timers.",
    )
    async def cooldowns(self, interaction: discord.Interaction) -> None:
        user_id = int(interaction.user.id)
        status = self.database.get_cooldown_status(user_id)
        minigame = self.bot.get_cog("MinigameCommands")

        active_games = getattr(minigame, "_active_games_by_owner", {})
        guess_game = active_games.get(user_id)
        if guess_game is not None and not guess_game.finished:
            remaining = int(guess_game.remaining_seconds)
            guess_status = (
                f"In progress · {format_cooldown_time(remaining)} remaining"
                if remaining
                else "In progress · ending now"
            )
        else:
            guess_status = "Ready"

        active_drafts = getattr(minigame, "_active_drafts_by_owner", {})
        draft_game = active_drafts.get(user_id)
        if status.draft_remaining_seconds:
            draft_status = (
                f"{format_cooldown_time(status.draft_remaining_seconds)} remaining"
            )
        elif draft_game is not None and not draft_game.finished:
            draft_status = "In progress · cooldown elapsed"
        else:
            draft_status = "Ready"

        claim_status = (
            f"{format_cooldown_time(status.claim_remaining_seconds)} remaining"
            if status.claim_remaining_seconds
            else "Ready"
        )
        embed = discord.Embed(
            title="Your Cooldowns",
            color=discord.Color.blurple(),
            description="Personal status based on your saved cooldowns and active games.",
        )
        embed.add_field(name="Gold Claim", value=claim_status, inline=False)
        embed.add_field(name="Guess Who", value=guess_status, inline=False)
        embed.add_field(name="Draft", value=draft_status, inline=False)
        embed.set_footer(
            text=(
                "Claim and Draft timers are read from SQLite. Guess Who has an "
                "active-session timer, not a separate cooldown."
            )
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        mark_successful_command(interaction)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(CooldownCommands(bot, bot.database))
