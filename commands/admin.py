"""Administrator-only player and economy management commands."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import catalog_card_choices, format_coins, player_card_label
from commands.xp import mark_successful_command
from database import (
    Database,
    EconomyError,
    InvalidPlayer,
    PlayerAlreadyExists,
    PlayerNotFound,
    SUPPORTED_STATS,
)

LOGGER = logging.getLogger("thsl_guru.admin")
STAT_HELP = "Comma-separated key=value pairs, such as SHO=83,DRI=88."


def _is_server_administrator(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return interaction.guild is not None and bool(
        getattr(permissions, "administrator", False)
    )


class ResetAllConfirmationView(discord.ui.View):
    """User-bound confirmation for the destructive global reset."""

    def __init__(
        self,
        command: "AdminCommands",
        *,
        administrator_id: int,
        guild_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self.command = command
        self.administrator_id = int(administrator_id)
        self.guild_id = int(guild_id)
        self._handled = False
        self._lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.administrator_id:
            await interaction.response.send_message(
                "Only the administrator who started this reset can use these buttons.",
                ephemeral=True,
            )
            return False
        if self._handled:
            await interaction.response.send_message(
                "This reset confirmation has already been handled.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="🔴 Confirm Reset",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_reset(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        async with self._lock:
            if self._handled:
                await interaction.response.send_message(
                    "This reset confirmation has already been handled.",
                    ephemeral=True,
                )
                return
            if not _is_server_administrator(interaction):
                await interaction.response.send_message(
                    "You need server administrator permission to confirm this reset.",
                    ephemeral=True,
                )
                return

            self._handled = True
            for child in self.children:
                child.disabled = True
            reset_at = int(time.time())
            try:
                affected_players = self.command.database.reset_all_player_progress(
                    self.administrator_id,
                    self.guild_id,
                    now=reset_at,
                )
            except Exception:
                LOGGER.exception(
                    "The global player-data reset failed for administrator %s.",
                    self.administrator_id,
                )
                await interaction.response.edit_message(
                    content=(
                        "Reset failed. No partial data changes were saved; "
                        "check the server logs before trying again."
                    ),
                    view=None,
                )
                self.stop()
                return

            await self.command._cancel_active_sessions()
            reset_timestamp = datetime.fromtimestamp(
                reset_at,
                timezone.utc,
            ).isoformat(timespec="seconds")
            LOGGER.info(
                "Global player-data reset confirmed by administrator %s in guild "
                "%s at %s; registered players reset: %d.",
                self.administrator_id,
                self.guild_id,
                reset_timestamp,
                affected_players,
            )
            await interaction.response.edit_message(
                content=(
                    f"Reset complete. Cleared game progress for "
                    f"**{affected_players}** registered players. "
                    "The card catalogue and server configuration were preserved."
                ),
                view=None,
            )
            self.stop()

    @discord.ui.button(
        label="🟢 Cancel",
        style=discord.ButtonStyle.success,
    )
    async def cancel_reset(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        async with self._lock:
            if self._handled:
                await interaction.response.send_message(
                    "This reset confirmation has already been handled.",
                    ephemeral=True,
                )
                return
            self._handled = True
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content="Reset cancelled. No player data was changed.",
                view=None,
            )
            self.stop()


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
    def __init__(
        self,
        database: Database,
        bot: discord_commands.Bot | None = None,
    ) -> None:
        self.database = database
        self.bot = bot

    async def _cancel_active_sessions(self) -> None:
        if self.bot is None:
            return
        get_cog = getattr(self.bot, "get_cog", None)
        if get_cog is None:
            return
        for cog_name in ("MatchCommands", "MinigameCommands"):
            cog = get_cog(cog_name)
            cancel = getattr(cog, "cancel_active_sessions", None)
            if cancel is None:
                continue
            try:
                await cancel()
            except Exception:
                LOGGER.exception(
                    "Could not cancel in-memory %s sessions after reset.",
                    cog_name,
                )

    @app_commands.command(
        name="resetall",
        description="Reset all registered players' game progress.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def resetall(self, interaction: discord.Interaction) -> None:
        if not _is_server_administrator(interaction):
            await interaction.response.send_message(
                "You need server administrator permission to use this command.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return
        view = ResetAllConfirmationView(
            self,
            administrator_id=interaction.user.id,
            guild_id=interaction.guild.id,
        )
        await interaction.response.send_message(
            content=(
                "⚠️ **RESET ALL PLAYER DATA**\n\n"
                "Are you sure you want to reset EVERY player's progress?"
            ),
            view=view,
            ephemeral=True,
        )

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
        lookup = player.strip()
        card_identifier: str | int = int(lookup) if lookup.isdecimal() else lookup
        try:
            total = self.database.add_inventory(
                member.id,
                card_identifier,
                int(quantity),
            )
        except (PlayerNotFound, EconomyError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        card = (
            self.database.get_player_by_id(card_identifier)
            if isinstance(card_identifier, int)
            else self.database.get_player(card_identifier)
        )
        if card is None:
            raise RuntimeError("Granted card was not found in the player catalog.")
        await interaction.response.send_message(
            f"Granted **{quantity}× {player_card_label(card)}** to {member.mention}. "
            f"They now own {total} copy/copies.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        mark_successful_command(interaction)

    @grantcard.autocomplete("player")
    async def grantcard_player_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        return catalog_card_choices(self.database.get_players(), current)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(AdminCommands(bot.database, bot))
