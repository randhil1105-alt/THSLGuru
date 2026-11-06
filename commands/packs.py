"""Pack catalog and confirmed, single-use pack purchases."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import (
    format_coins,
    player_embed,
    resolve_card_image,
    send_player_card,
)
from database import (
    GOLD_PACK_COST,
    MAX_PACK_QUANTITY,
    TOTW_PACK_COST,
    Database,
    InsufficientFunds,
    InvalidAmount,
    NoEligibleCards,
    Player,
)

LOGGER = logging.getLogger("thsl_guru.packs")

PACKS = (
    ("GOLD", GOLD_PACK_COST),
    ("TOTW", TOTW_PACK_COST),
)


class PackBuyButton(discord.ui.Button["PackPurchaseView"]):
    def __init__(
        self,
        rarity: str,
        cost: int,
        quantity: int = 1,
        *,
        disabled: bool = False,
    ) -> None:
        label = f"Buy {rarity} · {cost:,}"
        if quantity > 1:
            label = f"Buy {rarity} ×{quantity} · {cost * quantity:,}"
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            disabled=disabled,
        )
        self.rarity = rarity
        self.cost = cost
        self.quantity = quantity

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            await interaction.response.send_message(
                "This pack menu is no longer available.",
                ephemeral=False,
            )
            return
        await self.view.buy(interaction, rarity=self.rarity, cost=self.cost)


class PackPurchaseView(discord.ui.View):
    """A user-bound, single-use menu with an idempotent pack purchase."""

    def __init__(
        self,
        command: "PackCommands",
        user_id: str | int,
        cards_by_rarity: dict[str, list[Player]],
        *,
        only_rarity: str | None = None,
        quantity: int = 1,
    ) -> None:
        super().__init__(timeout=180)
        self.command = command
        self.user_id = str(user_id)
        self.cards_by_rarity = cards_by_rarity
        self.quantity = quantity
        self._purchase_lock = asyncio.Lock()
        self._used = False

        for rarity, cost in PACKS:
            if only_rarity is not None and rarity != only_rarity:
                continue
            self.add_item(
                PackBuyButton(
                    rarity,
                    cost,
                    quantity,
                    disabled=not bool(cards_by_rarity.get(rarity)),
                )
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "Only the member who opened this pack menu can use it.",
                ephemeral=False,
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
                    ephemeral=False,
                )
                return

            self._used = True
            try:
                await self.command._open(
                    interaction,
                    rarity=rarity,
                    cost=cost,
                    purchase_id=interaction.id,
                    quantity=self.quantity,
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
        quantity: int = 1,
    ) -> None:
        try:
            if quantity == 1:
                player, balance = self.database.open_pack(
                    interaction.user.id,
                    rarity,
                    cost,
                    purchase_id=purchase_id,
                )
                players = [player]
            else:
                players, balance = self.database.open_packs(
                    interaction.user.id,
                    rarity,
                    cost,
                    quantity,
                    purchase_id=purchase_id,
                )
        except NoEligibleCards:
            await interaction.response.send_message(
                f"No **{rarity}** cards are available yet. Your balance was not charged.",
                ephemeral=False,
            )
            return
        except InsufficientFunds:
            await interaction.response.send_message(
                f"These packs cost {format_coins(cost * quantity)} in total. "
                "Your balance was not charged.",
                ephemeral=False,
            )
            return
        except InvalidAmount as exc:
            await interaction.response.send_message(
                str(exc),
                ephemeral=False,
            )
            return

        if quantity == 1:
            await send_player_card(
                interaction,
                players[0],
                ephemeral=False,
                content=(
                    f"You opened a **{rarity} pack** for {format_coins(cost)}. "
                    f"Remaining balance: {format_coins(balance)}."
                ),
            )
            return

        card_type = "Gold" if rarity == "GOLD" else "TOTW"
        embeds: list[discord.Embed] = []
        files: list[discord.File] = []
        for index, player in enumerate(players, start=1):
            embed = player_embed(player)
            image_path = resolve_card_image(player)
            if image_path:
                filename = f"pack-{index:02d}-{image_path.name}"
                files.append(discord.File(image_path, filename=filename))
                embed.set_image(url=f"attachment://{filename}")
            elif player.image_path:
                embed.add_field(
                    name="Card image",
                    value=f"Image not found. Upload it to `{player.image_path}`.",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Card image",
                    value="No image is assigned to this card yet.",
                    inline=False,
                )
            embeds.append(embed)

        await interaction.response.send_message(
            content=(
                f"You opened **{quantity} {card_type} packs** for "
                f"{format_coins(cost * quantity)}. "
                f"Remaining balance: {format_coins(balance)}."
            ),
            embeds=embeds,
            files=files or None,
            ephemeral=False,
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
        quantity: int = 1,
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
                f"Choose a pack button to buy {quantity} pack"
                f"{'' if quantity == 1 else 's'} at once. This menu can be used once.\n"
                "Each pack contains one random player card of its listed rarity."
            ),
            color=discord.Color.gold() if only_rarity == "GOLD" else discord.Color.dark_purple(),
        )

        for rarity, cost in visible_packs:
            cards = cards_by_rarity[rarity]
            if not cards:
                contents = f"No eligible cards are currently available in the {rarity} catalog."
            else:
                card_type = "Gold" if rarity == "GOLD" else "TOTW"
                contents = (
                    f"Contains one random {card_type} player card."
                    if quantity == 1
                    else f"Contains one random {card_type} player card per pack."
                )

            total_cost = cost * quantity
            embed.add_field(
                name=f"{rarity} · {format_coins(cost)}",
                value=(
                    f"{contents}\n"
                    f"Price: {format_coins(cost)}. Requires an eligible {rarity} "
                    f"card in the catalog.\nTotal for {quantity}: "
                    f"{format_coins(total_cost)}."
                ),
                inline=False,
            )

        view = PackPurchaseView(
            self,
            interaction.user.id,
            cards_by_rarity,
            only_rarity=only_rarity,
            quantity=quantity,
        )
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=False,
        )

    @app_commands.command(
        name="packs",
        description="View available card packs and prices.",
    )
    @app_commands.describe(
        quantity=f"Number of packs to buy at once (1–{MAX_PACK_QUANTITY})"
    )
    async def packs(
        self,
        interaction: discord.Interaction,
        quantity: app_commands.Range[int, 1, MAX_PACK_QUANTITY] = 1,
    ) -> None:
        await self._send_pack_menu(interaction, quantity=quantity)

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
