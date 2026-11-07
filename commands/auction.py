"""Player-to-player card auctions with SQLite-backed coin reservations."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import (
    format_coins,
    player_card_label,
    resolve_card_image,
)
from commands.xp import mark_successful_command
from database import (
    AUCTION_DURATIONS_SECONDS,
    Auction,
    AuctionBidTooLow,
    AuctionDuplicateBid,
    AuctionError,
    AuctionExpired,
    AuctionHasBids,
    AuctionNotActive,
    AuctionNotFound,
    AuctionNotSeller,
    AuctionSelfBid,
    CardNotOwned,
    Database,
    EconomyError,
    InsufficientFunds,
    InvalidAmount,
)

LOGGER = logging.getLogger("thsl_guru.auction")
MAX_AUCTION_PRICE = 2_000_000_000
AUCTION_CHECK_INTERVAL_SECONDS = 20

DURATION_CHOICES = [
    app_commands.Choice(name="30 minutes", value=AUCTION_DURATIONS_SECONDS[0]),
    app_commands.Choice(name="1 hour", value=AUCTION_DURATIONS_SECONDS[1]),
    app_commands.Choice(name="6 hours", value=AUCTION_DURATIONS_SECONDS[2]),
    app_commands.Choice(name="24 hours", value=AUCTION_DURATIONS_SECONDS[3]),
]


def _auction_embed(auction: Auction) -> tuple[discord.Embed, discord.File | None]:
    card = auction.player
    embed = discord.Embed(
        title=player_card_label(card),
        color=discord.Color.gold(),
        description=f"**{card.rarity}** · {card.overall} OVR",
    )
    embed.add_field(name="Seller", value=auction.seller_name, inline=True)
    embed.add_field(
        name="Starting price",
        value=format_coins(auction.starting_price),
        inline=True,
    )
    embed.add_field(
        name="Current bid",
        value=(
            format_coins(auction.highest_bid)
            if auction.highest_bid is not None
            else "No bids yet"
        ),
        inline=True,
    )
    embed.add_field(
        name="Highest bidder",
        value=auction.highest_bidder_name or "No bids yet",
        inline=True,
    )
    embed.add_field(
        name="Ends",
        value=f"<t:{auction.end_time}:R> · <t:{auction.end_time}:f>",
        inline=True,
    )
    if auction.status == "ACTIVE":
        embed.add_field(name="Status", value="Active", inline=True)
    elif auction.status == "COMPLETED":
        embed.add_field(name="Status", value="Completed", inline=True)
    elif auction.status == "UNSOLD":
        embed.add_field(name="Status", value="Unsold", inline=True)
    elif auction.status == "CANCELLED":
        embed.add_field(name="Status", value="Cancelled", inline=True)
    embed.set_footer(text=f"Auction ID: {auction.auction_id}")

    image_path = resolve_card_image(card)
    upload: discord.File | None = None
    if image_path is not None:
        upload = discord.File(image_path, filename=image_path.name)
        embed.set_image(url=f"attachment://{image_path.name}")
    else:
        embed.add_field(
            name="Card image",
            value="No original card image is available for this player.",
            inline=False,
        )
    return embed, upload


def _name(user: discord.User | discord.Member) -> str:
    return str(getattr(user, "name", None) or getattr(user, "display_name", "Player"))


class AuctionBidModal(discord.ui.Modal, title="Place a bid"):
    def __init__(self, cog: AuctionCommands, auction: Auction) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.auction_id = auction.auction_id
        minimum = (
            auction.highest_bid + 1
            if auction.highest_bid is not None
            else auction.starting_price
        )
        self.bid_amount = discord.ui.TextInput(
            label="Bid amount in coins",
            placeholder=f"Minimum bid: {minimum:,}",
            max_length=20,
        )
        self.add_item(self.bid_amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_amount = self.bid_amount.value.strip().replace(",", "").replace("_", "")
        try:
            amount = int(raw_amount)
            auction = self.cog.database.place_auction_bid(
                self.auction_id,
                interaction.user.id,
                _name(interaction.user),
                amount,
                interaction.id,
            )
        except ValueError:
            await interaction.response.send_message(
                "Enter a whole-number bid using coins.",
                ephemeral=True,
            )
            return
        except (
            AuctionBidTooLow,
            AuctionDuplicateBid,
            AuctionError,
            CardNotOwned,
            EconomyError,
        ) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"Your bid of **{format_coins(amount)}** is now the highest bid for "
            f"**{player_card_label(auction.player)}**.",
            ephemeral=True,
        )
        await self.cog._update_auction_post(auction)


class AuctionBidButton(discord.ui.Button["AuctionBidView"]):
    def __init__(self, cog: AuctionCommands, auction_id: str) -> None:
        super().__init__(
            label="Bid",
            style=discord.ButtonStyle.primary,
            custom_id=f"thsl-auction-bid:{auction_id}",
        )
        self.cog = cog
        self.auction_id = auction_id

    async def callback(self, interaction: discord.Interaction) -> None:
        auction = self.cog.database.get_auction(self.auction_id)
        if auction is None:
            await interaction.response.send_message(
                "This auction could not be found.",
                ephemeral=True,
            )
            return
        if auction.status != "ACTIVE" or int(time.time()) >= auction.end_time:
            await interaction.response.send_message(
                "This auction has ended.",
                ephemeral=True,
            )
            return
        if str(interaction.user.id) == auction.seller_id:
            await interaction.response.send_message(
                "You cannot bid on your own auction.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(AuctionBidModal(self.cog, auction))


class AuctionBidView(discord.ui.View):
    def __init__(self, cog: AuctionCommands, auction_id: str) -> None:
        super().__init__(timeout=None)
        self.add_item(AuctionBidButton(cog, auction_id))


class AuctionBrowseView(discord.ui.View):
    def __init__(
        self,
        cog: AuctionCommands,
        auctions: list[Auction],
        *,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.auctions = auctions
        self.page = page
        self.bid_button = AuctionBidButton(cog, auctions[page].auction_id)
        self.previous_button = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
        )
        self.next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
        )
        self.previous_button.callback = self.previous_page
        self.next_button.callback = self.next_page
        self.add_item(self.previous_button)
        self.add_item(self.bid_button)
        self.add_item(self.next_button)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= len(self.auctions) - 1
        self.bid_button.auction_id = self.auctions[self.page].auction_id
        self.bid_button.custom_id = (
            f"thsl-auction-bid:{self.auctions[self.page].auction_id}"
        )

    async def _show_page(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        auction = self.auctions[self.page]
        embed, upload = _auction_embed(auction)
        embed.set_footer(
            text=(
                f"Auction {self.page + 1} of {len(self.auctions)} · "
                f"ID: {auction.auction_id}"
            )
        )
        try:
            await interaction.response.edit_message(
                embed=embed,
                attachments=[upload] if upload is not None else [],
                view=self,
            )
        finally:
            if upload is not None:
                upload.close()

    async def previous_page(self, interaction: discord.Interaction) -> None:
        if self.page > 0:
            self.page -= 1
        await self._show_page(interaction)

    async def next_page(self, interaction: discord.Interaction) -> None:
        if self.page < len(self.auctions) - 1:
            self.page += 1
        await self._show_page(interaction)


class AuctionCommands(discord_commands.Cog):
    auction = app_commands.Group(
        name="auction",
        description="Sell cards, browse auctions, bid, and manage your listings.",
        guild_only=True,
    )

    def __init__(self, bot: discord_commands.Bot, database: Database) -> None:
        self.bot = bot
        self.database = database
        self._maintenance_task: asyncio.Task[None] | None = None

    async def cog_load(self) -> None:
        for auction in self.database.list_active_auctions():
            if auction.message_id is not None:
                self._register_persistent_view(auction)
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def cog_unload(self) -> None:
        task = self._maintenance_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _register_persistent_view(self, auction: Auction) -> None:
        if auction.message_id is None:
            return
        try:
            self.bot.add_view(
                AuctionBidView(self, auction.auction_id),
                message_id=int(auction.message_id),
            )
        except (ValueError, RuntimeError):
            LOGGER.exception(
                "Could not restore the auction button for %s.",
                auction.auction_id,
            )

    async def _maintenance_loop(self) -> None:
        while not self.bot.is_closed():
            try:
                await self._maintenance_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Auction background processing failed.")
            await asyncio.sleep(AUCTION_CHECK_INTERVAL_SECONDS)

    async def _maintenance_pass(self) -> None:
        for auction in self.database.finalize_expired_auctions():
            await self._update_auction_post(auction)
        await self._publish_unpublished_auctions()
        await self._deliver_pending_notifications()

    async def _fetch_channel(self, channel_id: str | None) -> discord.abc.Messageable | None:
        if channel_id is None:
            return None
        try:
            channel_id_int = int(channel_id)
        except ValueError:
            return None
        channel = self.bot.get_channel(channel_id_int)
        if channel is not None:
            return channel
        try:
            channel = await self.bot.fetch_channel(channel_id_int)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            return None
        return channel if hasattr(channel, "send") else None

    async def _publish_auction(self, auction: Auction) -> Auction | None:
        channel = await self._fetch_channel(auction.channel_id)
        if channel is None:
            return None
        embed, upload = _auction_embed(auction)
        try:
            message = await channel.send(
                embed=embed,
                file=upload,
                view=AuctionBidView(self, auction.auction_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            LOGGER.exception("Could not publish auction %s.", auction.auction_id)
            return None
        finally:
            if upload is not None:
                upload.close()
        saved = self.database.set_auction_message(
            auction.auction_id,
            auction.channel_id or "",
            message.id,
        )
        self._register_persistent_view(saved)
        return saved

    async def _publish_unpublished_auctions(self) -> None:
        for auction in self.database.list_unpublished_auctions():
            await self._publish_auction(auction)

    async def _update_auction_post(self, auction: Auction) -> None:
        if auction.message_id is None or auction.channel_id is None:
            return
        channel = await self._fetch_channel(auction.channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(int(auction.message_id))
            embed, upload = _auction_embed(auction)
            if upload is not None:
                upload.close()
            await message.edit(
                embed=embed,
                view=(
                    AuctionBidView(self, auction.auction_id)
                    if auction.status == "ACTIVE"
                    else None
                ),
            )
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            LOGGER.exception("Could not update auction post %s.", auction.auction_id)

    async def _deliver_pending_notifications(self) -> None:
        for auction in self.database.list_auctions_pending_notification():
            if not auction.seller_notification_attempted:
                await self._notify_user(auction, recipient="seller")
            if (
                auction.status == "COMPLETED"
                and not auction.bidder_notification_attempted
            ):
                await self._notify_user(auction, recipient="bidder")

    async def _notify_user(self, auction: Auction, *, recipient: str) -> None:
        if recipient == "seller":
            user_id = auction.seller_id
            if auction.status == "COMPLETED":
                message = (
                    f"Your auction for **{player_card_label(auction.player)}** "
                    f"finished with a winning bid of "
                    f"**{format_coins(auction.highest_bid or 0)}** from "
                    f"**{auction.highest_bidder_name or 'another player'}**. "
                    "The card and payment have been transferred."
                )
            else:
                message = (
                    f"Your auction for **{player_card_label(auction.player)}** "
                    "ended without any bids. The card has been returned to your inventory."
                )
        else:
            user_id = auction.highest_bidder_id or ""
            message = (
                f"You won **{player_card_label(auction.player)}** for "
                f"**{format_coins(auction.highest_bid or 0)}**. "
                "The card has been added to your inventory."
            )

        delivered = False
        try:
            user = self.bot.get_user(int(user_id))
            if user is None:
                user = await self.bot.fetch_user(int(user_id))
            await user.send(message, allowed_mentions=discord.AllowedMentions.none())
            delivered = True
        except (ValueError, discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass

        if not delivered:
            channel = await self._fetch_channel(auction.channel_id)
            if channel is not None:
                try:
                    await channel.send(
                        content=f"<@{user_id}> {message}",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    delivered = True
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    LOGGER.exception(
                        "Could not notify %s for auction %s.",
                        recipient,
                        auction.auction_id,
                    )
        if not delivered:
            LOGGER.warning(
                "No notification could be delivered to %s for auction %s.",
                recipient,
                auction.auction_id,
            )
        try:
            self.database.mark_auction_notification_attempted(
                auction.auction_id,
                recipient,
            )
        except EconomyError:
            LOGGER.exception(
                "Could not record the %s notification attempt for auction %s.",
                recipient,
                auction.auction_id,
            )

    async def _autocomplete_cards(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        query = current.casefold()
        choices: list[app_commands.Choice[str]] = []
        for entry in self.database.get_inventory(interaction.user.id):
            label = f"{player_card_label(entry.player)} ×{entry.quantity}"
            if query and query not in label.casefold() and query not in str(entry.player.id):
                continue
            choices.append(
                app_commands.Choice(name=label[:100], value=str(entry.player.id))
            )
            if len(choices) >= 25:
                break
        return choices

    async def _autocomplete_user_auctions(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        query = current.casefold()
        choices: list[app_commands.Choice[str]] = []
        for auction in self.database.list_user_auctions(interaction.user.id):
            if auction.status != "ACTIVE":
                continue
            label = f"{player_card_label(auction.player)} · {auction.auction_id}"
            if query and query not in label.casefold():
                continue
            choices.append(
                app_commands.Choice(name=label[:100], value=auction.auction_id)
            )
            if len(choices) >= 25:
                break
        return choices

    @auction.command(name="sell", description="List one owned card for auction.")
    @app_commands.describe(
        card="Choose an available card from your inventory",
        starting_price="Starting price in coins",
        duration="How long the auction will run",
    )
    @app_commands.choices(duration=DURATION_CHOICES)
    @app_commands.autocomplete(card=_autocomplete_cards)
    async def sell(
        self,
        interaction: discord.Interaction,
        card: str,
        starting_price: app_commands.Range[int, 1, MAX_AUCTION_PRICE],
        duration: app_commands.Choice[int],
    ) -> None:
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "Auctions can only be created in a server channel.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            auction = self.database.create_auction(
                interaction.user.id,
                _name(interaction.user),
                int(card),
                int(starting_price),
                int(duration.value),
                uuid.uuid4().hex[:16],
                interaction.channel.id,
            )
        except (ValueError, AuctionError, EconomyError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        try:
            published = await self._publish_auction(auction)
            if published is None:
                await interaction.followup.send(
                    f"Your auction for **{player_card_label(auction.player)}** was "
                    "saved. It will be posted automatically when the channel is available.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"Your auction for **{player_card_label(auction.player)}** is live. "
                    f"It ends <t:{auction.end_time}:R>.",
                    ephemeral=True,
                )
        except Exception:
            LOGGER.exception("Auction %s was saved but could not be published.", auction.auction_id)
            await interaction.followup.send(
                "Your auction was saved and will be posted automatically.",
                ephemeral=True,
            )
        mark_successful_command(interaction)

    @auction.command(name="browse", description="Browse active auctions from other players.")
    async def browse(self, interaction: discord.Interaction) -> None:
        self.database.finalize_expired_auctions()
        auctions = self.database.list_active_auctions(
            exclude_seller_id=interaction.user.id
        )
        if not auctions:
            await interaction.response.send_message(
                "There are no active auctions from other players right now.",
                ephemeral=False,
            )
            mark_successful_command(interaction)
            return
        view = AuctionBrowseView(self, auctions)
        auction = auctions[0]
        embed, upload = _auction_embed(auction)
        embed.set_footer(text=f"Auction 1 of {len(auctions)} · ID: {auction.auction_id}")
        try:
            await interaction.response.send_message(
                embed=embed,
                file=upload,
                view=view,
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        finally:
            if upload is not None:
                upload.close()
        mark_successful_command(interaction)

    @auction.command(name="myauctions", description="View your active and completed auctions.")
    async def myauctions(self, interaction: discord.Interaction) -> None:
        self.database.finalize_expired_auctions()
        auctions = self.database.list_user_auctions(interaction.user.id)
        embed = discord.Embed(
            title="Your Auctions",
            color=discord.Color.gold(),
        )
        if not auctions:
            embed.description = "You have not created any auctions yet."
        else:
            for item in auctions[:25]:
                value = (
                    f"Starting price: {format_coins(item.starting_price)}\n"
                    f"Current bid: "
                    f"{format_coins(item.highest_bid) if item.highest_bid is not None else 'No bids'}\n"
                    f"Ends: <t:{item.end_time}:R>\n"
                    f"Auction ID: `{item.auction_id}`"
                )
                if item.status == "COMPLETED":
                    value += (
                        f"\nWinner: {item.highest_bidder_name or 'Unknown'}"
                    )
                embed.add_field(
                    name=f"{player_card_label(item.player)} · {item.status.title()}",
                    value=value,
                    inline=False,
                )
            if len(auctions) > 25:
                embed.set_footer(text="Showing the 25 most recent of your auctions.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        mark_successful_command(interaction)

    @auction.command(name="cancel", description="Cancel an active auction with no bids.")
    @app_commands.describe(auction_id="Choose an active auction you created")
    @app_commands.autocomplete(auction_id=_autocomplete_user_auctions)
    async def cancel(
        self,
        interaction: discord.Interaction,
        auction_id: str,
    ) -> None:
        try:
            auction = self.database.cancel_auction(
                auction_id,
                interaction.user.id,
            )
        except AuctionExpired:
            await self._maintenance_pass()
            await interaction.response.send_message(
                "That auction had already expired and has been settled.",
                ephemeral=True,
            )
            return
        except (
            AuctionError,
            AuctionHasBids,
            AuctionNotActive,
            AuctionNotFound,
            AuctionNotSeller,
            EconomyError,
        ) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Cancelled the auction for **{player_card_label(auction.player)}**. "
            "The card is back in your inventory.",
            ephemeral=True,
        )
        mark_successful_command(interaction)
        await self._update_auction_post(auction)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(AuctionCommands(bot, bot.database))
