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
from commands.xp import mark_successful_command
from database import (
    GOLD_PACK_COST,
    MAX_PACK_QUANTITY,
    S1_TOTS_PACK_COST,
    S1_TOTY_PACK_COST,
    S2_TOTS_PACK_COST,
    TOTW_PACK_COST,
    Database,
    InsufficientFunds,
    InvalidAmount,
    NoFreeRewardPacks,
    NoEligibleCards,
    Player,
    calculate_pack_odds,
    format_card_type,
)

LOGGER = logging.getLogger("thsl_guru.packs")

PACKS = (
    ("GOLD", GOLD_PACK_COST),
    ("TOTW", TOTW_PACK_COST),
    ("S2 TOTS", S2_TOTS_PACK_COST),
    ("S1 TOTS", S1_TOTS_PACK_COST),
    ("S1 TOTY", S1_TOTY_PACK_COST),
)
PACK_DISPLAY_NAMES = {"S1 TOTY": "S1 TOTY PACK"}


class PackBuyButton(discord.ui.Button["PackPurchaseView"]):
    def __init__(
        self,
        rarity: str,
        cost: int,
        quantity: int = 1,
        *,
        disabled: bool = False,
    ) -> None:
        pack_name = PACK_DISPLAY_NAMES.get(rarity, rarity)
        label = f"Buy {pack_name} · {cost:,}"
        if quantity > 1:
            pack_name = PACK_DISPLAY_NAMES.get(rarity, rarity)
            label = f"Buy {pack_name} ×{quantity} · {cost * quantity:,}"
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


class FreePackOpenButton(discord.ui.Button["PackPurchaseView"]):
    def __init__(self, rarity: str, quantity: int) -> None:
        label = (
            f"Open Free {format_card_type(rarity)} Pack · "
            f"{quantity} available"
        )
        super().__init__(
            label=label[:80],
            style=discord.ButtonStyle.success,
        )
        self.rarity = rarity
        self.quantity = quantity

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            await interaction.response.send_message(
                "This pack menu is no longer available.",
                ephemeral=False,
            )
            return
        await self.view.open_free_pack(interaction, rarity=self.rarity)


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
        balance: int = 0,
        free_pack_counts: dict[str, int] | None = None,
    ) -> None:
        super().__init__(timeout=180)
        self.command = command
        self.user_id = str(user_id)
        self.cards_by_rarity = cards_by_rarity
        self.quantity = quantity
        self.free_pack_counts = free_pack_counts or {}
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
                    disabled=(
                        not bool(cards_by_rarity.get(rarity))
                        or balance < cost * quantity
                    ),
                )
            )
            free_count = self.free_pack_counts.get(rarity, 0)
            if rarity in {"GOLD", "TOTW"} and free_count > 0:
                self.add_item(FreePackOpenButton(rarity, free_count))

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
                await self._disable_completed_menu(interaction)

    async def open_free_pack(
        self,
        interaction: discord.Interaction,
        *,
        rarity: str,
    ) -> None:
        async with self._purchase_lock:
            if self._used:
                await interaction.response.send_message(
                    "This pack menu has already been used. Run `/pack` again to open another pack.",
                    ephemeral=False,
                )
                return

            self._used = True
            try:
                await self.command._open_free_reward(
                    interaction,
                    rarity=rarity,
                    redemption_id=interaction.id,
                )
            finally:
                await self._disable_completed_menu(interaction)

    async def _disable_completed_menu(
        self,
        interaction: discord.Interaction,
    ) -> None:
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
            if rarity in PACK_DISPLAY_NAMES:
                content = (
                    f"You opened a **{PACK_DISPLAY_NAMES[rarity]}** "
                    f"(rarity **{format_card_type(rarity)}**) for "
                    f"{format_coins(cost)}. Remaining balance: "
                    f"{format_coins(balance)}."
                )
            else:
                content = (
                    f"You opened a **{rarity} pack** for {format_coins(cost)}. "
                    f"Remaining balance: {format_coins(balance)}."
                )
            await send_player_card(
                interaction,
                players[0],
                ephemeral=False,
                content=content,
            )
            return

        card_type = format_card_type(rarity)
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
                (
                    f"You opened **{quantity} {PACK_DISPLAY_NAMES[rarity]}S** "
                    f"(rarity **{card_type}**) for {format_coins(cost * quantity)}. "
                    if rarity in PACK_DISPLAY_NAMES
                    else f"You opened **{quantity} {card_type} packs** for "
                    f"{format_coins(cost * quantity)}. "
                )
                + f"Remaining balance: {format_coins(balance)}."
            ),
            embeds=embeds,
            files=files or None,
            ephemeral=False,
        )

    async def _open_free_reward(
        self,
        interaction: discord.Interaction,
        *,
        rarity: str,
        redemption_id: str | int,
    ) -> None:
        try:
            player, remaining = self.database.open_free_reward_pack(
                interaction.user.id,
                rarity,
                redemption_id,
            )
        except NoFreeRewardPacks as exc:
            await interaction.response.send_message(str(exc), ephemeral=False)
            return
        except NoEligibleCards:
            await interaction.response.send_message(
                f"No **{rarity}** cards are available. Your free pack was not used.",
                ephemeral=False,
            )
            return
        except InvalidAmount as exc:
            await interaction.response.send_message(str(exc), ephemeral=False)
            return

        pack_name = f"{format_card_type(rarity)} Pack"
        await send_player_card(
            interaction,
            player,
            ephemeral=False,
            content=(
                f"You opened a **FREE {pack_name}** level-up reward. "
                f"Remaining free {format_card_type(rarity)} packs: **{remaining}**."
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
        quantity: int = 1,
    ) -> None:
        cards_by_rarity = self._pack_cards()
        balance = self.database.get_balance(interaction.user.id)
        free_pack_counts = self.database.get_free_pack_inventory(
            interaction.user.id
        )
        free_pack_lines = "\n".join(
            f"- {format_card_type(rarity)} Packs: "
            f"{free_pack_counts.get(rarity, 0)}"
            for rarity, _cost in PACKS
        )
        visible_packs = [
            (rarity, cost)
            for rarity, cost in PACKS
            if only_rarity is None or rarity == only_rarity
        ]
        embed = discord.Embed(
            title=(
                "Available Card Packs"
                if only_rarity is None
                else PACK_DISPLAY_NAMES.get(only_rarity, f"{only_rarity} Pack")
            ),
            description=(
                f"Your balance: **{format_coins(balance)}**\n"
                f"Choose a pack button to buy {quantity} pack"
                f"{'' if quantity == 1 else 's'} at once. This menu can be used once.\n"
                "Each pack contains one random player card of its listed rarity.\n\n"
                "🎁 **Your Available Free Packs**\n"
                f"{free_pack_lines}\n"
                "A free-pack button opens exactly one pack."
            ),
            color=discord.Color.gold() if only_rarity == "GOLD" else discord.Color.dark_purple(),
        )

        for rarity, cost in visible_packs:
            cards = cards_by_rarity[rarity]
            if not cards:
                contents = f"No eligible cards are currently available in the {rarity} catalog."
                odds_text = "No card odds are available."
            else:
                card_type = format_card_type(rarity)
                contents = (
                    f"Contains one random {card_type} player card."
                    if quantity == 1
                    else f"Contains one random {card_type} player card per pack."
                )
                if rarity in PACK_DISPLAY_NAMES:
                    contents += f" Rarity: {card_type}."
                if rarity in {"GOLD", "TOTW"}:
                    contents += (
                        f" Free level-up packs available: "
                        f"{free_pack_counts.get(rarity, 0)}."
                    )
                odds = calculate_pack_odds(cards)
                odds_text = "Card odds: " + " · ".join(
                    f"{card.name} {odds[card.id]:.2f}%"
                    for card in cards
                )

            total_cost = cost * quantity
            embed.add_field(
                name=(
                    f"{PACK_DISPLAY_NAMES[rarity]} · {format_coins(cost)}"
                    if rarity in PACK_DISPLAY_NAMES
                    else f"{rarity} · {format_coins(cost)}"
                ),
                value=(
                    f"{contents}\n"
                    f"Price: {format_coins(cost)}. Requires an eligible {rarity} "
                    f"card in the catalog and {format_coins(total_cost)} in your "
                    f"balance. Level-up reward packs are free and separate.\n"
                    f"{odds_text}\nTotal for {quantity}: "
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
            balance=balance,
            free_pack_counts=free_pack_counts,
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
        mark_successful_command(interaction)

    @app_commands.command(
        name="pack",
        description="Choose and purchase a card pack.",
    )
    @app_commands.describe(
        pack_type="Choose a GOLD, TOTW, S2 TOTS, S1 TOTS, or S1 TOTY pack"
    )
    @app_commands.choices(
        pack_type=[
            app_commands.Choice(name="GOLD Pack", value="GOLD"),
            app_commands.Choice(name="TOTW Pack", value="TOTW"),
            app_commands.Choice(name="S2 TOTS Pack", value="S2 TOTS"),
            app_commands.Choice(name="S1 TOTS Pack", value="S1 TOTS"),
            app_commands.Choice(name="S1 TOTY PACK", value="S1 TOTY"),
        ]
    )
    async def pack(
        self,
        interaction: discord.Interaction,
        pack_type: app_commands.Choice[str],
    ) -> None:
        await self._send_pack_menu(interaction, only_rarity=pack_type.value)
        mark_successful_command(interaction)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(PackCommands(bot.database))
