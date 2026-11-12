"""Persistent player-to-player card and coin trading."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import (
    autocomplete_card_label,
    resolve_card_image,
)
from database import (
    TRADE_REQUEST_SECONDS,
    Database,
    EconomyError,
    InvalidAmount,
    Player,
    TradeAlreadyActive,
    TradeError,
    format_card_type,
)

LOGGER = logging.getLogger("thsl_guru.trade")


def _participant_name(user: discord.abc.User) -> str:
    return str(
        getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or "Player"
    )


def _trade_offer_lines(trade: dict[str, Any], user_id: str) -> list[str]:
    cards = [
        card
        for card in trade["cards"]
        if str(card["user_id"]) == user_id
    ]
    lines = [
        f"**{int(card['quantity']):,}× {card['player'].name}** — "
        f"{card['player'].overall} OVR · "
        f"{format_card_type(card['player'].rarity)}"
        for card in cards
    ]
    prefix = "initiator" if user_id == str(trade["initiator_id"]) else "invitee"
    coins = int(trade[f"{prefix}_coins"])
    lines.append(f"**{coins:,} coins**")
    return lines


def _chunk_lines(lines: list[str], limit: int = 900) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks or ["No cards or coins offered."]


def _trade_embeds_and_files(
    trade: dict[str, Any],
) -> tuple[list[discord.Embed], list[discord.File]]:
    status = str(trade["status"])
    if status == "REQUESTED":
        embed = discord.Embed(
            title="🤝 TRADE REQUEST",
            description=(
                f"From: <@{trade['initiator_id']}>\n\n"
                "Would you like to trade with this player?"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="This request expires after 2 minutes.")
        return [embed], []

    status_titles = {
        "ACTIVE": "🤝 TRADE OFFER",
        "COMPLETED": "🤝 TRADE COMPLETED",
        "DECLINED": "TRADE DECLINED",
        "CANCELLED": "TRADE CANCELLED",
        "EXPIRED": "TRADE EXPIRED",
    }
    description = (
        "Both players must confirm this exact offer. Any change resets both "
        "confirmations. Use `/trade card` to search owned cards by name, OVR, "
        "or rarity."
        if status == "ACTIVE"
        else ""
    )
    if status == "COMPLETED":
        description = "Both players confirmed the offer and all assets transferred."
    elif status in {"DECLINED", "CANCELLED", "EXPIRED"}:
        description = "This trade is closed. No cards or coins were transferred."

    summary = discord.Embed(
        title=status_titles.get(status, "🤝 TRADE OFFER"),
        description=description,
        color=(
            discord.Color.green()
            if status == "COMPLETED"
            else discord.Color.blurple()
            if status == "ACTIVE"
            else discord.Color.dark_grey()
        ),
    )
    if status == "ACTIVE":
        first_confirmed = "Confirmed" if trade["initiator_confirmed"] else "Waiting"
        second_confirmed = "Confirmed" if trade["invitee_confirmed"] else "Waiting"
        summary.add_field(
            name="Confirmations",
            value=(
                f"<@{trade['initiator_id']}>: **{first_confirmed}**\n"
                f"<@{trade['invitee_id']}>: **{second_confirmed}**"
            ),
            inline=False,
        )

    embeds = [summary]
    files: list[discord.File] = []
    for prefix, user_id, name in (
        ("initiator", str(trade["initiator_id"]), str(trade["initiator_name"])),
        ("invitee", str(trade["invitee_id"]), str(trade["invitee_name"])),
    ):
        chunks = _chunk_lines(_trade_offer_lines(trade, user_id))
        for index, chunk in enumerate(chunks):
            offer = discord.Embed(
                title=f"{name}'s offer" + (f" · {index + 1}/{len(chunks)}" if len(chunks) > 1 else ""),
                description=chunk,
                color=discord.Color.dark_teal(),
            )
            offer.set_author(name=name)
            offer.set_footer(
                text=(
                    "Coin offers are included in the list."
                    if index == 0
                    else "More cards in this offer."
                )
            )
            embeds.append(offer)

    for card in trade["cards"]:
        if len(embeds) >= 10:
            break
        player: Player = card["player"]
        image_path = resolve_card_image(player)
        if image_path is None:
            continue
        filename = f"trade-{str(trade['trade_id'])[:8]}-{player.id}{image_path.suffix}"
        artwork = discord.Embed(
            title=f"{int(card['quantity']):,}× {player.name}",
            description=(
                f"Offered by <@{card['user_id']}> · {player.overall} OVR · "
                f"{format_card_type(player.rarity)}"
            ),
            color=discord.Color.dark_teal(),
        )
        artwork.set_image(url=f"attachment://{filename}")
        embeds.append(artwork)
        files.append(discord.File(image_path, filename=filename))

    return embeds[:10], files[:10]


def _trade_message_content(trade: dict[str, Any]) -> str | None:
    if trade["status"] == "COMPLETED":
        return (
            f"<@{trade['initiator_id']}> successfully traded with "
            f"<@{trade['invitee_id']}>!"
        )
    if trade["status"] == "REQUESTED":
        return f"<@{trade['invitee_id']}>"
    return None


def _trade_allowed_mentions(trade: dict[str, Any]) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        users=[
            discord.Object(id=int(trade["initiator_id"])),
            discord.Object(id=int(trade["invitee_id"])),
        ],
        roles=False,
        everyone=False,
        replied_user=False,
    )


class TradeView(discord.ui.View):
    """Persistent request and offer controls for one stored trade."""

    def __init__(
        self,
        cog: TradeCommands,
        trade_id: str,
        status: str,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.trade_id = str(trade_id)
        self.status = status
        self.message: discord.Message | None = None

        if status == "REQUESTED":
            accept = discord.ui.Button(
                label="Accept",
                style=discord.ButtonStyle.success,
                custom_id=f"thsl:trade:{self.trade_id}:accept",
            )
            decline = discord.ui.Button(
                label="Decline",
                style=discord.ButtonStyle.danger,
                custom_id=f"thsl:trade:{self.trade_id}:decline",
            )
            accept.callback = self._accept
            decline.callback = self._decline
            self.add_item(accept)
            self.add_item(decline)
        elif status == "ACTIVE":
            controls = (
                ("Add Card", discord.ButtonStyle.primary, "add"),
                ("Remove Card", discord.ButtonStyle.secondary, "remove"),
                ("Set Coins", discord.ButtonStyle.secondary, "coins"),
                ("Confirm Trade", discord.ButtonStyle.success, "confirm"),
                ("Cancel Trade", discord.ButtonStyle.danger, "cancel"),
            )
            for label, style, action in controls:
                button = discord.ui.Button(
                    label=label,
                    style=style,
                    custom_id=f"thsl:trade:{self.trade_id}:{action}",
                )
                button.callback = getattr(self, f"_{action}")
                self.add_item(button)

    async def _deny_if_not_participant(
        self,
        interaction: discord.Interaction,
        trade: dict[str, Any] | None = None,
    ) -> bool:
        trade = trade or self.cog.database.get_trade(self.trade_id)
        if trade is None:
            await interaction.response.send_message(
                "This trade could not be found.",
                ephemeral=True,
            )
            return True
        if str(interaction.user.id) not in {
            str(trade["initiator_id"]),
            str(trade["invitee_id"]),
        }:
            await interaction.response.send_message(
                "Only the two players in this trade can use these buttons.",
                ephemeral=True,
            )
            return True
        return False

    async def _accept(self, interaction: discord.Interaction) -> None:
        try:
            trade = self.cog.database.accept_trade_request(
                self.trade_id,
                interaction.user.id,
            )
        except TradeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if trade["status"] == "ACTIVE":
            self.cog.schedule_expiry(trade)
        await self.cog.refresh_trade_message(
            self.trade_id,
            interaction=interaction,
        )

    async def _decline(self, interaction: discord.Interaction) -> None:
        try:
            trade = self.cog.database.decline_trade_request(
                self.trade_id,
                interaction.user.id,
            )
        except TradeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.cog.refresh_trade_message(
            self.trade_id,
            interaction=interaction,
        )

    async def _add(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_participant(interaction):
            return
        trade = self.cog.database.get_trade(self.trade_id)
        if trade is None or trade["status"] != "ACTIVE":
            await interaction.response.send_message(
                "This trade is no longer active.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Use `/trade card`, choose **Add card**, then search for the exact "
            "edition you want to offer.",
            ephemeral=True,
        )

    async def _remove(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_participant(interaction):
            return
        trade = self.cog.database.get_trade(self.trade_id)
        if trade is None or trade["status"] != "ACTIVE":
            await interaction.response.send_message(
                "This trade is no longer active.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Use `/trade card`, choose **Remove card**, then search for the "
            "exact edition to remove one copy.",
            ephemeral=True,
        )

    async def _coins(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_participant(interaction):
            return
        trade = self.cog.database.get_trade(self.trade_id)
        if trade is None or trade["status"] != "ACTIVE":
            await interaction.response.send_message(
                "This trade is no longer active.",
                ephemeral=True,
            )
            return
        prefix = (
            "initiator"
            if str(interaction.user.id) == str(trade["initiator_id"])
            else "invitee"
        )
        await interaction.response.send_modal(
            TradeCoinsModal(
                self.cog,
                self.trade_id,
                str(interaction.user.id),
                int(trade[f"{prefix}_coins"]),
            )
        )

    async def _confirm(self, interaction: discord.Interaction) -> None:
        try:
            result = self.cog.database.confirm_trade(
                self.trade_id,
                interaction.user.id,
            )
        except TradeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        trade = result["trade"]
        if result["completed"]:
            await self.cog.refresh_trade_message(
                self.trade_id,
                interaction=interaction,
            )
            self.cog.cancel_expiry(self.trade_id)
            return

        error_messages = {
            "empty_offer": "Add cards or coins before confirming this trade.",
            "insufficient_coins": (
                "A coin offer is no longer covered by that player's balance. "
                "Both confirmations were reset."
            ),
            "missing_cards": (
                "An offered card is no longer owned in the required quantity. "
                "Both confirmations were reset."
            ),
            "inventory_limit": (
                "The receiving inventory would exceed its supported limit. "
                "Both confirmations were reset."
            ),
            "balance_limit": (
                "The receiving balance would exceed its supported limit. "
                "Both confirmations were reset."
            ),
        }
        await interaction.response.send_message(
            error_messages.get(
                result["error"],
                "Your confirmation was recorded. The other player must confirm too.",
            ),
            ephemeral=True,
        )
        await self.cog.refresh_trade_message(self.trade_id)
        self.cog.schedule_expiry(trade)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_participant(interaction):
            return
        try:
            self.cog.database.cancel_trade(self.trade_id, interaction.user.id)
        except TradeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.cog.refresh_trade_message(
            self.trade_id,
            interaction=interaction,
        )
        self.cog.cancel_expiry(self.trade_id)


class TradeCoinsModal(discord.ui.Modal, title="Set Coin Offer"):
    def __init__(
        self,
        cog: TradeCommands,
        trade_id: str,
        user_id: str,
        current_amount: int,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.trade_id = trade_id
        self.user_id = user_id
        self.amount = discord.ui.TextInput(
            label="Coins to offer",
            placeholder="Enter a whole-number amount",
            default=str(current_amount),
            max_length=19,
            required=True,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "Only the player who opened this form can submit it.",
                ephemeral=True,
            )
            return
        value = self.amount.value.strip().replace(",", "")
        if not value.isdigit():
            await interaction.response.send_message(
                "Enter a non-negative whole number of coins.",
                ephemeral=True,
            )
            return
        try:
            trade = self.cog.database.set_trade_coins(
                self.trade_id,
                self.user_id,
                int(value),
            )
        except (InvalidAmount, TradeError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Your offer is now **{int(value):,} coins**. "
            "Both players must confirm again.",
            ephemeral=True,
        )
        await self.cog.refresh_trade_message(self.trade_id)
        self.cog.schedule_expiry(trade)


class TradeCommands(discord_commands.Cog):
    trade = app_commands.Group(
        name="trade",
        description="Request a trade or manage card offers.",
    )

    def __init__(self, bot: discord_commands.Bot, database: Database) -> None:
        self.bot = bot
        self.database = database
        self._views: dict[str, TradeView] = {}
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}
        self._recovery_done = False

    @discord_commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._recovery_done:
            return
        self._recovery_done = True
        for trade in self.database.get_recoverable_trades():
            expired = self.database.expire_trade(str(trade["trade_id"]))
            if expired is not None:
                await self._edit_stored_trade_message(expired)
                continue
            view = self._get_view(trade)
            message_id = trade.get("message_id")
            if message_id:
                self.bot.add_view(view, message_id=int(message_id))
            self.schedule_expiry(trade)

    def cog_unload(self) -> None:
        for task in self._expiry_tasks.values():
            task.cancel()
        self._expiry_tasks.clear()

    def cancel_expiry(self, trade_id: str) -> None:
        task = self._expiry_tasks.pop(str(trade_id), None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def schedule_expiry(self, trade: dict[str, Any]) -> None:
        trade_id = str(trade["trade_id"])
        if trade["status"] not in {"REQUESTED", "ACTIVE"}:
            self.cancel_expiry(trade_id)
            return
        self.cancel_expiry(trade_id)
        delay = max(0.0, float(trade["expires_at"]) - time.time())
        self._expiry_tasks[trade_id] = asyncio.create_task(
            self._expire_after(trade_id, delay)
        )

    async def _expire_after(self, trade_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            expired = self.database.expire_trade(trade_id)
            if expired is not None:
                await self._edit_stored_trade_message(expired)
                self._views.pop(trade_id, None)
                return
            current = self.database.get_trade(trade_id)
            if current is not None:
                self.schedule_expiry(current)
        except asyncio.CancelledError:
            return
        except Exception:
            LOGGER.exception("Could not expire trade %s.", trade_id)
        finally:
            if self._expiry_tasks.get(trade_id) is asyncio.current_task():
                self._expiry_tasks.pop(trade_id, None)

    def _get_view(self, trade: dict[str, Any]) -> TradeView | None:
        if trade["status"] not in {"REQUESTED", "ACTIVE"}:
            current = self._views.pop(str(trade["trade_id"]), None)
            if current is not None:
                current.stop()
            return None
        trade_id = str(trade["trade_id"])
        status = str(trade["status"])
        current = self._views.get(trade_id)
        if current is not None and current.status == status:
            return current
        if current is not None:
            current.stop()
        view = TradeView(self, trade_id, status)
        self._views[trade_id] = view
        return view

    async def _edit_stored_trade_message(
        self,
        trade: dict[str, Any],
    ) -> None:
        message_id = trade.get("message_id")
        if not message_id:
            return
        channel_id = int(trade["channel_id"])
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.warning(
                    "Could not find channel for saved trade %s.",
                    trade["trade_id"],
                )
                return
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return
        try:
            message = await fetch_message(int(message_id))
            embeds, files = _trade_embeds_and_files(trade)
            await message.edit(
                content=_trade_message_content(trade),
                embeds=embeds,
                attachments=files,
                view=self._get_view(trade),
                allowed_mentions=_trade_allowed_mentions(trade),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Could not update saved trade message %s.",
                trade["trade_id"],
            )

    async def refresh_trade_message(
        self,
        trade_id: str,
        *,
        interaction: discord.Interaction | None = None,
    ) -> None:
        trade = self.database.get_trade(trade_id)
        if trade is None:
            return
        embeds, files = _trade_embeds_and_files(trade)
        view = self._get_view(trade)
        if view is not None:
            view.message = interaction.message if interaction is not None else None
            self.schedule_expiry(trade)

        content = _trade_message_content(trade)
        allowed_mentions = _trade_allowed_mentions(trade)
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(
                content=content,
                embeds=embeds,
                attachments=files,
                view=view,
                allowed_mentions=allowed_mentions,
            )
            if interaction.message is not None:
                message_id = str(interaction.message.id)
                try:
                    self.database.set_trade_message(trade_id, message_id)
                except TradeError:
                    pass
            return

        message_id = trade.get("message_id")
        if not message_id:
            return
        channel = self.bot.get_channel(int(trade["channel_id"]))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(trade["channel_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        try:
            message = await channel.fetch_message(int(message_id))
            if view is not None:
                view.message = message
            await message.edit(
                content=content,
                embeds=embeds,
                attachments=files,
                view=view,
                allowed_mentions=allowed_mentions,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Could not refresh trade message %s.", trade_id)

    async def _apply_trade_card(
        self,
        interaction: discord.Interaction,
        action: str,
        card: str,
    ) -> None:
        user_id = interaction.user.id
        trade = self.database.get_active_trade_for_user(user_id)
        if trade is None:
            await interaction.response.send_message(
                "You need an active trade before changing a card offer. "
                "Start one with `/trade request` and have the other player accept.",
                ephemeral=True,
            )
            return
        lookup = card.strip()
        if not lookup.isdecimal():
            await interaction.response.send_message(
                "Select a card from the autocomplete suggestions.",
                ephemeral=True,
            )
            return
        player_id = int(lookup)
        try:
            if action == "add":
                player = self.database.add_trade_card(
                    str(trade["trade_id"]),
                    user_id,
                    player_id,
                )
                message = (
                    f"Added one **{autocomplete_card_label(player)}** to your offer. "
                    "Both players must confirm again."
                )
            elif action == "remove":
                self.database.remove_trade_card(
                    str(trade["trade_id"]),
                    user_id,
                    player_id,
                )
                player = self.database.get_player_by_id(player_id)
                if player is None:
                    raise TradeError("That card is no longer in the catalog.")
                message = (
                    f"Removed one **{autocomplete_card_label(player)}** from your "
                    "offer. Both players must confirm again."
                )
            else:
                await interaction.response.send_message(
                    "Choose Add card or Remove card.",
                    ephemeral=True,
                )
                return
        except (EconomyError, TradeError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(message, ephemeral=True)
        await self.refresh_trade_message(str(trade["trade_id"]))

    async def _trade_card_choices(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        user_id = interaction.user.id
        trade = self.database.get_active_trade_for_user(user_id)

        action = getattr(
            getattr(interaction, "namespace", None),
            "action",
            "add",
        )
        action = str(getattr(action, "value", action)).casefold()
        offered_by_id = {
            int(card["player"].id): int(card["quantity"])
            for card in trade["cards"]
            if str(card["user_id"]) == str(user_id)
        } if trade is not None else {}
        if action == "remove":
            cards = [
                (card["player"], int(card["quantity"]))
                for card in (trade["cards"] if trade is not None else [])
                if str(card["user_id"]) == str(user_id)
            ]
        else:
            cards = [
                (
                    entry.player,
                    entry.quantity - offered_by_id.get(entry.player.id, 0),
                )
                for entry in self.database.get_inventory(user_id)
                if entry.quantity > offered_by_id.get(entry.player.id, 0)
            ]

        query = current.strip().casefold()
        choices: list[app_commands.Choice[str]] = []
        for player, quantity in cards:
            quantity_suffix = f" · {quantity} available"
            if action == "remove":
                quantity_suffix = f" · {quantity} offered"
            label = autocomplete_card_label(
                player,
                max_length=100 - len(quantity_suffix),
            )
            search_text = (
                f"{player.overall} OVR {player.name} "
                f"{format_card_type(player.rarity)}"
            ).casefold()
            if query and query not in search_text:
                continue
            choices.append(
                app_commands.Choice(
                    name=f"{label}{quantity_suffix}",
                    value=str(player.id),
                )
            )
            if len(choices) == 25:
                break
        return choices

    @trade.command(
        name="card",
        description="Search owned cards to add to or remove from a trade.",
    )
    @app_commands.describe(
        action="Add a card to your offer or remove one",
        card="Search for an exact card edition you own",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Add card", value="add"),
            app_commands.Choice(name="Remove card", value="remove"),
        ]
    )
    async def trade_card(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        card: str,
    ) -> None:
        await self._apply_trade_card(interaction, action.value, card)

    @trade_card.autocomplete("card")
    async def trade_card_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._trade_card_choices(interaction, current)

    @app_commands.command(
        name="tradecard",
        description="Search owned cards to add to or remove from your active trade.",
    )
    @app_commands.describe(
        action="Add a card to your offer or remove one",
        card="Search for an exact card edition you own",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Add card", value="add"),
            app_commands.Choice(name="Remove card", value="remove"),
        ]
    )
    async def tradecard(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        card: str,
    ) -> None:
        await self._apply_trade_card(interaction, action.value, card)

    @tradecard.autocomplete("card")
    async def tradecard_card_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._trade_card_choices(interaction, current)

    @trade.command(
        name="request",
        description="Request a card and coin trade with another player.",
    )
    @app_commands.describe(player="The player you want to trade with")
    async def trade_request(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
    ) -> None:
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "Trades can only be started in a server channel.",
                ephemeral=True,
            )
            return
        if player.bot:
            await interaction.response.send_message(
                "You can only trade with another player, not a bot.",
                ephemeral=True,
            )
            return
        if player.id == interaction.user.id:
            await interaction.response.send_message(
                "You cannot trade with yourself.",
                ephemeral=True,
            )
            return

        trade_key = uuid.uuid4().hex
        try:
            trade = self.database.create_trade_request(
                trade_key,
                interaction.user.id,
                player.id,
                _participant_name(interaction.user),
                _participant_name(player),
                interaction.guild.id,
                interaction.channel.id,
                request_seconds=TRADE_REQUEST_SECONDS,
            )
        except TradeAlreadyActive as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except EconomyError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        view = self._get_view(trade)
        embeds, files = _trade_embeds_and_files(trade)
        try:
            await interaction.response.send_message(
                content=player.mention,
                embeds=embeds,
                files=files,
                view=view,
                allowed_mentions=discord.AllowedMentions(
                    users=[player],
                    roles=False,
                    everyone=False,
                    replied_user=False,
                ),
            )
            message = await interaction.original_response()
            view.message = message
            self.database.set_trade_message(trade_key, message.id)
            self.schedule_expiry(trade)
        except Exception:
            try:
                self.database.cancel_trade(trade_key, interaction.user.id)
            except TradeError:
                pass
            self._views.pop(trade_key, None)
            self.cancel_expiry(trade_key)
            LOGGER.exception("Could not send trade request %s.", trade_key)
            raise


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(TradeCommands(bot, bot.database))
