"""Pack catalog and confirmed, single-use pack purchases."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import format_coins, send_player_card
from database import (
    GOLD_PACK_COST,
    TOTW_PACK_COST,
    Database,
    InsufficientFunds,
    NoEligibleCards,
    Player,
)

LOGGER = logging.getLogger("thsl_guru.packs")

PACKS = (
    ("GOLD", GOLD_PACK_COST),
    ("TOTW", TOTW_PACK_COST),
)


class PackBuyButton(discord.ui.Button["PackPurchaseView"]):
    def __init__(self, rarity: str, cost: int, *, disabled: bool = False) -> None:
        super().__init__(
            label=f"Buy {rarity} · {cost:,}",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
        )
        self.rarity = rarity
        self.cost = cost

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            await interaction.response.send_message(
                "This pack menu is no longer available.",
                ephemeral=True,
            )
            return
        await self.view.buy(interaction, rarity=self.rarity, cost=self.cost)


class PackPurchaseView(discord.ui.View):
    """A user-bound, single-use pack menu with an idempotent purchase callback."""

    def __init__(
        self,
        command: "PackCommands",
        user_id: str | int,
        cards_by_rarity: dict[str, list[Player]],
        *,
        only_rarity: str | None = None,
    ) -> None:
        super().__init__(timeout=180)
        self.command = command
        self.user_id = str(user_id)
        self.cards_by_rarity = cards_by_rarity
        self._purchase_lock = asyncio.Lock()
        self._used = False

        for rarity, cost in PACKS:
            if only_rarity is not None and rarity != only_rarity:
                continue
            self.add_item(
                PackBuyButton(
                    rarity,
                    cost,
                    disabled=not bool(cards_by_rarity.get(rarity)),
                )
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "Only the member who opened this pack menu can use it.",
                ephemeral=True,
            )
            return False
        return True

    async def buy(
        self,
        interaction: discord.Interaction,
        *,
        rarity: str,
        cost: int,
    ) -> None:
        async with self._purchase_lock:
            if self._used:
                await interaction.response.send_message(
                    "This pack menu has already been used. Run `/packs` to open a new one.",
                    ephemeral=True,
                )
                return

            self._used = True
            try:
                await self.command._open(
                    interaction,
                    rarity=rarity,
                    cost=cost,
                    purchase_id=interaction.id,
                )
            finally:
                for child in self.children:
                    child.disabled = True
                message = getattr(interaction, "message", None)
                if message is not None:
                    try:
                        await message.edit(view=self)
                    except discord.HTTPException:
                        LOGGER.debug("Could not disable the completed pack menu.")


class PackCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def _open(
        self,
        interaction: discord.Interaction,
        *,
        rarity: str,
        cost: int,
        purchase_id: str | int,
    ) -> None:
        try:
            player, balance = self.database.open_pack(
                interaction.user.id,
                rarity,
                cost,
                purchase_id=purchase_id,
            )
        except NoEligibleCards:
            await interaction.response.send_message(
                f"No **{rarity}** cards are available yet. Your balance was not charged.",
                ephemeral=True,
            )
            return
        except InsufficientFunds:
            await interaction.response.send_message(
                f"This pack costs {format_coins(cost)}. Your balance was not charged.",
                ephemeral=True,
            )
            return

        await send_player_card(
            interaction,
            player,
            ephemeral=True,
            content=(
                f"You opened a **{rarity} pack** for {format_coins(cost)}. "
                f"Remaining balance: {format_coins(balance)}."
            ),
        )

    def _pack_cards(self) -> dict[str, list[Player]]:
        return {
            rarity: self.database.get_players(rarity=rarity)
            for rarity, _ in PACKS
        }

    async def _send_pack_menu(
        self,
        interaction: discord.Interaction,
        *,
        only_rarity: str | None = None,
    ) -> None:
        cards_by_rarity = self._pack_cards()
        visible_packs = [
            (rarity, cost)
            for rarity, cost in PACKS
            if only_rarity is None or rarity == only_rarity
        ]
        embed = discord.Embed(
            title="Available Card Packs" if only_rarity is None else f"{only_rarity} Pack",
            description=(
                f"Your balance: **{format_coins(self.database.get_balance(interaction.user.id))}**\n"
                "Choose a pack button to purchase. Each menu can be used once."
            ),
            color=discord.Color.gold() if only_rarity == "GOLD" else discord.Color.dark_purple(),
        )

        for rarity, cost in visible_packs:
            cards = cards_by_rarity[rarity]
            if not cards:
                contents = "No eligible cards are currently in the catalog."
            else:
                chance = 100 / len(cards)
                listed_cards = [
                    f"• **{card.name}** ({card.overall} OVR) — {chance:.1f}%"
                    for card in cards[:10]
                ]
                if len(cards) > 10:
                    listed_cards.append(
                        f"• {len(cards) - 10} more eligible card(s), same individual odds"
                    )
                contents = "Contents and equal odds:\n" + "\n".join(listed_cards)

            embed.add_field(
                name=f"{rarity} · {format_coins(cost)}",
                value=(
                    f"{contents}\n"
                    f"Requirement: at least {format_coins(cost)} and one {rarity} "
                    "card in the catalog."
                ),
                inline=False,
            )

        view = PackPurchaseView(
            self,
            interaction.user.id,
            cards_by_rarity,
            only_rarity=only_rarity,
        )
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True,
        )

    @app_commands.command(
        name="packs",
        description="View available card packs, odds, and prices.",
    )
    async def packs(self, interaction: discord.Interaction) -> None:
        await self._send_pack_menu(interaction)

    @app_commands.command(
        name="pack",
        description="Choose and purchase a card pack.",
    )
    @app_commands.describe(pack_type="Choose a GOLD or TOTW pack")
    @app_commands.choices(
        pack_type=[
            app_commands.Choice(name="GOLD Pack", value="GOLD"),
            app_commands.Choice(name="TOTW Pack", value="TOTW"),
        ]
    )
    async def pack(
        self,
        interaction: discord.Interaction,
        pack_type: app_commands.Choice[str],
    ) -> None:
        await self._send_pack_menu(interaction, only_rarity=pack_type.value)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(PackCommands(bot.database))
