"""Administrator-only player and economy management commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import format_coins, player_card_label
from commands.xp import mark_successful_command
from database import (
    Database,
    EconomyError,
    InvalidPlayer,
    PlayerAlreadyExists,
    PlayerNotFound,
    SUPPORTED_STATS,
)

STAT_HELP = "Comma-separated key=value pairs, such as SHO=83,DRI=88."


def parse_stats(value: str | None) -> dict[str, int]:
    if not value or not value.strip():
        return {}
    result: dict[str, int] = {}
    allowed = set(SUPPORTED_STATS)
    for item in value.split(","):
        if "=" not in item:
            raise InvalidPlayer(f"Stats must use key=value pairs. {STAT_HELP}")
        key, raw_number = item.split("=", 1)
        key = key.strip().upper()
        if key not in allowed:
            raise InvalidPlayer(f"Unknown stat {key}. Allowed: {', '.join(SUPPORTED_STATS)}.")
        if key in result:
            raise InvalidPlayer(f"Stat {key} was provided more than once.")
        try:
            result[key] = int(raw_number.strip())
        except ValueError as exc:
            raise InvalidPlayer(f"{key} must be a whole number.") from exc
    return result


class AdminCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(name="addplayer", description="Add a player card to the catalog.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        name="Player name",
        overall="Overall rating from 0 to 99",
        position="Player position",
        country="Player country",
        team="Player team",
        rarity="Card rarity, for example GOLD or TOTW",
        value="Sale value in coins",
        stats=STAT_HELP,
        image_path="Optional relative image path inside cards/",
    )
    async def addplayer(
        self,
        interaction: discord.Interaction,
        name: str,
        overall: app_commands.Range[int, 0, 99],
        position: str,
        country: str,
        team: str,
        rarity: str,
        value: app_commands.Range[int, 0, 2_000_000_000],
        stats: str | None = None,
        image_path: str | None = None,
    ) -> None:
        try:
            player = self.database.add_player(
                {
                    "name": name,
                    "overall": int(overall),
                    "position": position,
                    "country": country,
                    "team": team,
                    "rarity": rarity,
                    "value": int(value),
                    "stats": parse_stats(stats),
                    "image_path": image_path,
                }
            )
        except PlayerAlreadyExists:
            existing = self.database.get_player(name)
            message = (
                f"**{player_card_label(existing)}** is already in the catalog."
                if existing is not None
                else f"Player **{name}** is already in the catalog."
            )
            await interaction.response.send_message(message, ephemeral=True)
            return
        except InvalidPlayer as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Added **{player_card_label(player)}** to the catalog.",
            ephemeral=True,
        )
        mark_successful_command(interaction)

    @app_commands.command(name="setbalance", description="Set a member's coin balance.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Member to update", amount="New non-negative balance")
    async def setbalance(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 0, 2_000_000_000],
    ) -> None:
        balance = self.database.set_balance(member.id, int(amount))
        await interaction.response.send_message(
            f"Set {member.mention}'s balance to {format_coins(balance)}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        mark_successful_command(interaction)

    @app_commands.command(name="grantcoins", description="Grant coins to a member.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Member to reward", amount="Coins to grant")
    async def grantcoins(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 2_000_000_000],
    ) -> None:
        try:
            balance = self.database.add_coins(member.id, int(amount))
        except EconomyError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Granted {format_coins(int(amount))} to {member.mention}. "
            f"New balance: {format_coins(balance)}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        mark_successful_command(interaction)

    @app_commands.command(name="grantcard", description="Grant a player card to a member.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        member="Member to receive the card",
        player="Player card name",
        quantity="Number of copies to grant",
    )
    async def grantcard(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        player: str,
        quantity: app_commands.Range[int, 1, 1000] = 1,
    ) -> None:
        try:
            total = self.database.add_inventory(member.id, player, int(quantity))
        except (PlayerNotFound, EconomyError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        card = self.database.get_player(player)
        if card is None:
            raise RuntimeError("Granted card was not found in the player catalog.")
        await interaction.response.send_message(
            f"Granted **{quantity}× {player_card_label(card)}** to {member.mention}. "
            f"They now own {total} copy/copies.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        mark_successful_command(interaction)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(AdminCommands(bot.database))
