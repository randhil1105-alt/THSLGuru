"""Shared daily card listings and exact-value purchases."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import RARITY_COLORS, format_coins, resolve_card_image
from database import (
    DAILY_SHOP_CARD_COUNT,
    Database,
    EconomyError,
    NoEligibleCards,
    Player,
    ShopError,
    ShopInsufficientFunds,
)
from database import format_card_type

LOGGER = logging.getLogger("thsl_guru.shop")
SHOP_VIEW_TIMEOUT_SECONDS = 300


def _shop_embeds(
    players: Sequence[Player],
    *,
    shop_day: str,
    balance: int,
    attach_images: bool,
) -> tuple[list[discord.Embed], list[discord.File]]:
    embeds: list[discord.Embed] = []
    files: list[discord.File] = []
    for index, player in enumerate(players, start=1):
        embed = discord.Embed(
            title=f"Daily Shop · {index}/{DAILY_SHOP_CARD_COUNT} · {player.name}",
            description=(
                f"**{player.overall} OVR** · {format_card_type(player.rarity)}\n"
                f"Position: **{player.position}**\n"
                f"Price: **{format_coins(player.value)}**"
            ),
            color=RARITY_COLORS.get(player.rarity, discord.Color.blurple()),
        )
        image_path = resolve_card_image(player)
        if image_path is None:
            raise NoEligibleCards(
                f"The shop card **{player.name}** does not have available artwork."
            )
        filename = f"daily-shop-{player.id}{image_path.suffix.lower()}"
        embed.set_image(url=f"attachment://{filename}")
        embed.set_footer(
            text=(
                f"Shared shop · {shop_day} UTC · "
                f"Your balance: {format_coins(balance)}"
            )
        )
        embeds.append(embed)
        if attach_images:
            files.append(discord.File(image_path, filename=filename))
    return embeds, files


class DailyShopBuyButton(discord.ui.Button["DailyShopView"]):
    def __init__(self, player: Player) -> None:
        price_text = f"{player.value:,}"
        label_prefix = f"Buy · {price_text} coins"
        max_name_length = max(0, 80 - len(label_prefix) - 3)
        label = f"Buy {player.name[:max_name_length]} · {price_text}"
        super().__init__(
            label=label[:80],
            style=discord.ButtonStyle.primary,
        )
        self.player_id = player.id

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            await interaction.response.send_message(
                "This shop button is no longer available. Run `/shop` to reopen today's shop.",
                ephemeral=True,
            )
            return
        await self.view.buy(interaction, self.player_id)


class DailyShopView(discord.ui.View):
    """A user's view of the shared daily listings."""

    def __init__(
        self,
        command: "ShopCommands",
        user_id: str | int,
        *,
        shop_day: str,
        players: Sequence[Player],
        balance: int,
    ) -> None:
        super().__init__(timeout=SHOP_VIEW_TIMEOUT_SECONDS)
        self.command = command
        self.user_id = str(user_id)
        self.shop_day = str(shop_day)
        self.players = tuple(players)
        self.balance = int(balance)
        self.message: discord.Message | None = None
        self._purchase_lock = asyncio.Lock()

        for player in self.players:
            self.add_item(DailyShopBuyButton(player))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "Only the player who opened this shop can buy from it.",
                ephemeral=True,
            )
            return False
        return True

    async def buy(
        self,
        interaction: discord.Interaction,
        player_id: int,
    ) -> None:
        async with self._purchase_lock:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                LOGGER.info("A shop button interaction expired before processing.")
                return

            try:
                purchase = self.command.database.buy_daily_shop_card(
                    interaction.user.id,
                    player_id,
                    interaction.id,
                    self.shop_day,
                )
            except ShopInsufficientFunds as exc:
                self.balance = exc.balance
                await self._refresh_after_purchase(interaction)
                await self._followup(interaction, str(exc))
                return
            except (ShopError, EconomyError) as exc:
                await self._followup(interaction, str(exc))
                return
            except Exception:
                LOGGER.exception(
                    "Daily shop purchase failed for user %s, card %s.",
                    interaction.user.id,
                    player_id,
                )
                await self._followup(
                    interaction,
                    "The purchase could not be completed. Your balance and inventory "
                    "were not changed.",
                )
                return

            player = purchase["player"]
            self.balance = int(purchase["balance_after"])
            await self._refresh_after_purchase(interaction)
            if purchase["already_processed"]:
                confirmation = (
                    f"Your purchase of **{player.name}** was already processed; "
                    "no additional charge was made. "
                )
            else:
                confirmation = f"Purchased **{player.name}** for "
                confirmation += f"**{format_coins(int(purchase['price']))}**. "
            confirmation += f"Your balance is now **{format_coins(self.balance)}**."
            await self._followup(interaction, confirmation)

    async def _refresh_after_purchase(
        self,
        interaction: discord.Interaction,
    ) -> None:
        embeds, _files = _shop_embeds(
            self.players,
            shop_day=self.shop_day,
            balance=self.balance,
            attach_images=False,
        )
        refreshed = DailyShopView(
            self.command,
            self.user_id,
            shop_day=self.shop_day,
            players=self.players,
            balance=self.balance,
        )
        edit_kwargs: dict[str, Any] = {
            "embeds": embeds,
            "view": refreshed,
        }
        message = getattr(interaction, "message", None)
        attachments = getattr(message, "attachments", None)
        if attachments is not None:
            edit_kwargs["attachments"] = attachments
        try:
            updated_message = await interaction.edit_original_response(**edit_kwargs)
            refreshed.bind_message(updated_message or message)
            self.stop()
        except (AttributeError, discord.HTTPException):
            LOGGER.warning(
                "Could not refresh daily shop for user %s; purchase is saved.",
                interaction.user.id,
                exc_info=True,
            )

    @staticmethod
    async def _followup(
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        try:
            await interaction.followup.send(message, ephemeral=True)
        except discord.HTTPException:
            LOGGER.info("A daily shop follow-up expired before it could be sent.")

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                LOGGER.debug("Could not disable an expired daily shop view.")

    def bind_message(self, message: discord.Message | None) -> None:
        self.message = message


class ShopCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(
        name="shop",
        description="View today's shared player card shop.",
    )
    async def shop(self, interaction: discord.Interaction) -> None:
        try:
            catalog = self.database.get_players()
            illustrated_ids = [
                player.id
                for player in catalog
                if resolve_card_image(player) is not None
            ]
            daily_shop = self.database.get_daily_shop(
                eligible_player_ids=illustrated_ids,
            )
            balance = self.database.get_balance(interaction.user.id)
            embeds, files = _shop_embeds(
                daily_shop["players"],
                shop_day=daily_shop["shop_day"],
                balance=balance,
                attach_images=True,
            )
        except NoEligibleCards as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        view = DailyShopView(
            self,
            interaction.user.id,
            shop_day=daily_shop["shop_day"],
            players=daily_shop["players"],
            balance=balance,
        )
        await interaction.response.send_message(
            embeds=embeds,
            files=files,
            view=view,
            ephemeral=True,
        )
        try:
            view.bind_message(await interaction.original_response())
        except discord.HTTPException:
            LOGGER.debug("Could not bind the daily shop message for timeout cleanup.")


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(ShopCommands(bot.database))
