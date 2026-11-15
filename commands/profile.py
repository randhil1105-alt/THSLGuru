"""Player profile and XP progression command."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.xp import xp_level_up_embed, xp_notification_text
from database import (
    PACK_COST_BY_RARITY,
    Database,
    EconomyError,
    format_card_type,
)
from xp_system import NORMAL_COMMAND_XP


def xp_progress_bar(percent: int, width: int = 15) -> str:
    bounded_percent = min(100, max(0, int(percent)))
    filled = (bounded_percent * width) // 100
    return f"{'█' * filled}{'░' * (width - filled)}"


class ProfileCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(
        name="profile",
        description="View your THSL Guru level and XP progress.",
    )
    async def profile(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        try:
            grant = self.database.award_xp(
                user_id,
                f"profile:{interaction.id}",
                NORMAL_COMMAND_XP,
                "command",
            )
            progress = self.database.get_xp_profile(user_id)
            free_packs = self.database.get_free_pack_inventory(user_id)
            free_pack_lines = "\n".join(
                f"- {format_card_type(rarity)} Packs: "
                f"**{free_packs.get(rarity, 0)}**"
                for rarity in ("GOLD", "TOTW")
            )
            granted_packs = self.database.get_granted_pack_inventory(user_id)
            granted_pack_lines = "\n".join(
                f"- {format_card_type(rarity)} Packs: "
                f"**{granted_packs.get(rarity, 0)}**"
                for rarity in PACK_COST_BY_RARITY
            )
        except EconomyError as exc:
            await interaction.response.send_message(str(exc), ephemeral=False)
            return

        username = str(getattr(interaction.user, "name", "Player"))
        progress_line = (
            f"{progress.progress_xp:,} / {progress.xp_to_next_level:,} XP\n"
            f"`{xp_progress_bar(progress.progress_percent)}`\n"
            f"{progress.progress_percent}% toward Level {progress.level + 1}"
        )
        embed = discord.Embed(
            title="🏆 PLAYER PROFILE",
            description=f"**{discord.utils.escape_markdown(username)}**",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Level", value=f"**{progress.level}**", inline=True)
        embed.add_field(
            name="Total XP",
            value=f"⭐ **{progress.total_xp:,}**",
            inline=True,
        )
        embed.add_field(
            name="XP to next level",
            value=f"**{progress.xp_remaining:,} XP**",
            inline=True,
        )
        embed.add_field(
            name=f"Level {progress.level + 1} progress",
            value=progress_line,
            inline=False,
        )
        embed.add_field(
            name="🎁 Available Free Packs",
            value=free_pack_lines,
            inline=False,
        )
        embed.add_field(
            name="🎁 Available Granted Packs",
            value=granted_pack_lines,
            inline=False,
        )
        embed.add_field(
            name="XP earned",
            value=(
                xp_notification_text(
                    grant,
                    include_level_up=not grant.leveled_up,
                )
                if grant.is_new
                else "XP for this interaction was already credited."
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
        if grant.is_new and grant.leveled_up:
            await interaction.followup.send(
                embed=xp_level_up_embed(grant, user=interaction.user),
                ephemeral=False,
            )


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(ProfileCommands(bot.database))
