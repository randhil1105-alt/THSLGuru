"""Player-confirmed sales of duplicate, unassigned hockey cards."""

from __future__ import annotations

import logging
import uuid

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import format_coins
from commands.xp import mark_successful_command
from database import (
    Database,
    DuplicateSaleItem,
    DuplicateSalePreview,
    DuplicateSalePreviewChanged,
    DuplicateSaleResult,
    EconomyError,
    format_card_label,
)

LOGGER = logging.getLogger("thsl_guru.duplicate_sales")


def _item_line(item: DuplicateSaleItem) -> str:
    label = format_card_label(item.player_name, item.overall, item.rarity)
    return (
        f"**{label}** — {item.quantity} × {format_coins(item.unit_value)} "
        f"= **{format_coins(item.total_value)}**"
    )


def _add_lines(embed: discord.Embed, title: str, lines: list[str]) -> None:
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > 1000 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    for index, chunk in enumerate(chunks):
        embed.add_field(
            name=title if index == 0 else f"{title} · continued",
            value=chunk,
            inline=False,
        )


def _preview_embed(preview: DuplicateSalePreview) -> discord.Embed:
    embed = discord.Embed(
        title="Confirm Duplicate Card Sale",
        description=(
            f"Cards to sell: **{preview.total_cards}**\n"
            f"You will receive **{format_coins(preview.total_earnings)}**."
        ),
        color=discord.Color.gold(),
    )
    _add_lines(embed, "Cards and selling values", [_item_line(item) for item in preview.items])
    if preview.protected:
        protected_count = sum(item.extra_copies for item in preview.protected)
        embed.add_field(
            name="Kept safe",
            value=(
                f"{protected_count} duplicate card(s) across "
                f"{len(preview.protected)} card type(s) are protected because they "
                "are assigned to `/team` or listed in an active auction."
            ),
            inline=False,
        )
    embed.set_footer(text="One copy of every card is always kept.")
    return embed


def _result_embed(result: DuplicateSaleResult) -> discord.Embed:
    embed = discord.Embed(
        title=(
            "Duplicate Sale Already Processed"
            if result.already_processed
            else "Duplicate Sale Complete"
        ),
        description=(
            f"Cards sold: **{result.total_cards}**\n"
            f"Coins earned: **{format_coins(result.total_earnings)}**\n"
            f"Your new balance: **{format_coins(result.new_balance)}**"
        ),
        color=discord.Color.green(),
    )
    if result.already_processed:
        embed.description = (
            "This confirmation was already processed; no additional cards or coins "
            "were transferred.\n\n"
            + embed.description
        )
    _add_lines(embed, "Sale receipt", [_item_line(item) for item in result.items])
    return embed


class DuplicateSaleConfirmationView(discord.ui.View):
    def __init__(
        self,
        database: Database,
        *,
        owner_id: int,
        sale_id: str,
        preview: DuplicateSalePreview,
    ) -> None:
        super().__init__(timeout=180)
        self.database = database
        self.owner_id = int(owner_id)
        self.sale_id = sale_id
        self.preview = preview
        self.message: discord.Message | None = None

        self.confirm_button = discord.ui.Button(
            label="Confirm Sale",
            style=discord.ButtonStyle.success,
        )
        self.cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
        )
        self.confirm_button.callback = self.confirm_sale
        self.cancel_button.callback = self.cancel_sale
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the player who started this sale can confirm or cancel it.",
            ephemeral=True,
        )
        return False

    def _disable_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()

    async def confirm_sale(self, interaction: discord.Interaction) -> None:
        try:
            result = self.database.sell_duplicate_cards(
                self.owner_id,
                self.sale_id,
                self.preview.items,
            )
        except DuplicateSalePreviewChanged as exc:
            self._disable_buttons()
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="Sale Preview Changed",
                    description=str(exc),
                    color=discord.Color.orange(),
                ),
                view=None,
            )
            return
        except EconomyError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception:
            LOGGER.exception("Duplicate card sale confirmation failed.")
            await interaction.response.send_message(
                "The sale could not be completed. No cards were sold.",
                ephemeral=True,
            )
            return

        self._disable_buttons()
        await interaction.response.edit_message(
            embed=_result_embed(result),
            view=None,
        )

    async def cancel_sale(self, interaction: discord.Interaction) -> None:
        self._disable_buttons()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Sale Cancelled",
                description="No cards were sold and your balance was not changed.",
                color=discord.Color.blurple(),
            ),
            view=None,
        )

    async def on_timeout(self) -> None:
        self._disable_buttons()
        if self.message is None:
            return
        try:
            await self.message.edit(
                embed=discord.Embed(
                    title="Sale Confirmation Expired",
                    description="No cards were sold.",
                    color=discord.Color.blurple(),
                ),
                view=None,
            )
        except discord.HTTPException:
            LOGGER.debug("Could not update an expired duplicate-sale preview.")


class DuplicateSaleCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(
        name="sellduplicates",
        description="Review and sell extra copies of your unassigned cards.",
    )
    async def sellduplicates(self, interaction: discord.Interaction) -> None:
        try:
            preview = self.database.get_duplicate_sale_preview(interaction.user.id)
        except EconomyError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        if not preview.items:
            if preview.protected:
                message = (
                    "No duplicate cards are currently safe to sell. Extra copies "
                    "assigned to `/team` or listed in active auctions were kept."
                )
            else:
                message = (
                    "You have no duplicate cards to sell. Your last copy of every "
                    "card will always be kept."
                )
            await interaction.response.send_message(message, ephemeral=True)
            mark_successful_command(interaction)
            return

        sale_id = str(getattr(interaction, "id", None) or uuid.uuid4().hex)
        view = DuplicateSaleConfirmationView(
            self.database,
            owner_id=interaction.user.id,
            sale_id=sale_id,
            preview=preview,
        )
        await interaction.response.send_message(
            embed=_preview_embed(preview),
            view=view,
            ephemeral=True,
        )
        try:
            view.message = await interaction.original_response()
        except (AttributeError, discord.HTTPException):
            LOGGER.debug("Could not save the duplicate-sale preview message.")
        mark_successful_command(interaction)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(DuplicateSaleCommands(bot.database))
