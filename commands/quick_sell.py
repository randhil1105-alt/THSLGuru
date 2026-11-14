"""Owner-bound Quick Sell confirmations for newly awarded cards."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import discord

from commands.common import format_coins, player_card_label
from database import (
    Database,
    EconomyError,
    Player,
    calculate_sell_payout,
)

LOGGER = logging.getLogger("thsl_guru.quick_sell")
QUICK_SELL_TIMEOUT_SECONDS = 180
QUICK_SELL_CONFIRM_TIMEOUT_SECONDS = 120


class QuickSellButton(discord.ui.Button["QuickSellView"]):
    def __init__(
        self,
        reward_index: int,
        player: Player,
        *,
        row: int,
    ) -> None:
        name = player.name.split(" (", 1)[0]
        super().__init__(
            label=f"Quick Sell · {name}"[:80],
            style=discord.ButtonStyle.danger,
            row=row,
        )
        self.reward_index = reward_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            await interaction.response.send_message(
                "This Quick Sell button is no longer available.",
                ephemeral=True,
            )
            return
        await self.view.request_quick_sell(interaction, self.reward_index)


class KeepCardButton(discord.ui.Button["QuickSellView"]):
    def __init__(self, reward_index: int) -> None:
        super().__init__(
            label="Keep Card",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        self.reward_index = reward_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            await interaction.response.send_message(
                "This card choice is no longer available.",
                ephemeral=True,
            )
            return
        await self.view.keep_card(interaction, self.reward_index)


class QuickSellView(discord.ui.View):
    """Shows a sell action for each awarded copy; claim rewards can be kept."""

    def __init__(
        self,
        database: Database,
        user_id: str | int,
        players: Sequence[Player],
        *,
        allow_keep: bool = False,
    ) -> None:
        super().__init__(timeout=QUICK_SELL_TIMEOUT_SECONDS)
        self.database = database
        self.user_id = str(user_id)
        self.players = list(players)
        self.allow_keep = allow_keep
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._pending: set[int] = set()
        self._sold: set[int] = set()
        self._kept: set[int] = set()
        self._expired = False

        for index, player in enumerate(self.players):
            row = min(index // 5, 4)
            self.add_item(QuickSellButton(index, player, row=row))
            if allow_keep:
                self.add_item(KeepCardButton(index))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "Only the player who received these cards can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    async def bind_message(self, interaction: discord.Interaction) -> None:
        try:
            self.message = await interaction.original_response()
        except (AttributeError, discord.HTTPException):
            # Tests and alternate interaction implementations may not expose it.
            self.message = getattr(interaction, "message", None)

    async def request_quick_sell(
        self,
        interaction: discord.Interaction,
        reward_index: int,
    ) -> None:
        async with self._lock:
            if self._expired or not 0 <= reward_index < len(self.players):
                await interaction.response.send_message(
                    "This Quick Sell button has expired.",
                    ephemeral=True,
                )
                return
            if reward_index in self._sold or reward_index in self._kept:
                await interaction.response.send_message(
                    "This card reward has already been handled.",
                    ephemeral=True,
                )
                return
            if reward_index in self._pending:
                await interaction.response.send_message(
                    "A Quick Sell confirmation is already open for this card.",
                    ephemeral=True,
                )
                return

            player = self.database.get_player_by_id(
                self.players[reward_index].id
            )
            if player is None:
                await interaction.response.send_message(
                    "This card is no longer in the catalog.",
                    ephemeral=True,
                )
                return
            self.players[reward_index] = player
            self._pending.add(reward_index)
            self._set_reward_buttons_disabled(reward_index, True)
            confirmation = QuickSellConfirmationView(
                self,
                reward_index,
                player,
            )
            payout = calculate_sell_payout(player.value)
            try:
                await interaction.response.send_message(
                    f"Quick sell **{player_card_label(player)}** for "
                    f"**{format_coins(payout)}**? One copy will be removed "
                    "from your inventory.",
                    view=confirmation,
                    ephemeral=True,
                )
                await confirmation.bind_message(interaction)
            except Exception:
                self._pending.discard(reward_index)
                self._set_reward_buttons_disabled(reward_index, False)
                await self._sync_message()
                raise
            await self._sync_message()

    async def keep_card(
        self,
        interaction: discord.Interaction,
        reward_index: int,
    ) -> None:
        async with self._lock:
            if self._expired or not 0 <= reward_index < len(self.players):
                await interaction.response.send_message(
                    "This card choice has expired.",
                    ephemeral=True,
                )
                return
            if reward_index in self._pending:
                await interaction.response.send_message(
                    "Finish or cancel the Quick Sell confirmation first.",
                    ephemeral=True,
                )
                return
            if reward_index in self._sold:
                await interaction.response.send_message(
                    "This card has already been quick sold.",
                    ephemeral=True,
                )
                return
            if reward_index in self._kept:
                await interaction.response.send_message(
                    "This card is already kept in your inventory.",
                    ephemeral=True,
                )
                return
            self._kept.add(reward_index)
            self._set_reward_buttons_disabled(reward_index, True)
            await interaction.response.send_message(
                f"Kept **{player_card_label(self.players[reward_index])}** "
                "in your inventory.",
                ephemeral=True,
            )
            await self._sync_message()

    def _set_reward_buttons_disabled(
        self,
        reward_index: int,
        disabled: bool,
    ) -> None:
        for item in self.children:
            if getattr(item, "reward_index", None) == reward_index:
                item.disabled = disabled

    def _complete_sale(self, reward_index: int) -> None:
        self._pending.discard(reward_index)
        self._sold.add(reward_index)
        self._set_reward_buttons_disabled(reward_index, True)

    def _cancel_sale(self, reward_index: int) -> None:
        self._pending.discard(reward_index)
        if reward_index not in self._sold and reward_index not in self._kept:
            self._set_reward_buttons_disabled(reward_index, False)

    async def _sync_message(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except (AttributeError, discord.HTTPException):
            LOGGER.debug("Could not refresh a Quick Sell message.")

    async def on_timeout(self) -> None:
        self._expired = True
        for item in self.children:
            item.disabled = True
        await self._sync_message()


class QuickSellConfirmButton(
    discord.ui.Button["QuickSellConfirmationView"]
):
    def __init__(self) -> None:
        super().__init__(
            label="Confirm Quick Sell",
            style=discord.ButtonStyle.danger,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            await interaction.response.send_message(
                "This Quick Sell confirmation has expired.",
                ephemeral=True,
            )
            return
        await self.view.confirm(interaction)


class CancelQuickSellButton(
    discord.ui.Button["QuickSellConfirmationView"]
):
    def __init__(self) -> None:
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            await interaction.response.send_message(
                "This Quick Sell confirmation has expired.",
                ephemeral=True,
            )
            return
        await self.view.cancel(interaction)


class QuickSellConfirmationView(discord.ui.View):
    def __init__(
        self,
        source_view: QuickSellView,
        reward_index: int,
        player: Player,
    ) -> None:
        super().__init__(timeout=QUICK_SELL_CONFIRM_TIMEOUT_SECONDS)
        self.source_view = source_view
        self.reward_index = reward_index
        self.player = player
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._processed = False
        self.add_item(QuickSellConfirmButton())
        self.add_item(CancelQuickSellButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.source_view.user_id:
            await interaction.response.send_message(
                "Only the player who received this card can confirm its sale.",
                ephemeral=True,
            )
            return False
        return True

    async def bind_message(self, interaction: discord.Interaction) -> None:
        try:
            self.message = await interaction.original_response()
        except (AttributeError, discord.HTTPException):
            self.message = getattr(interaction, "message", None)

    async def confirm(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            if self._processed:
                await interaction.response.send_message(
                    "This Quick Sell confirmation has already been processed.",
                    ephemeral=True,
                )
                return
            self._processed = True
            if self.source_view._expired:
                self.source_view._cancel_sale(self.reward_index)
                message = "This Quick Sell confirmation expired. The card remains in your inventory."
                await interaction.response.send_message(message, ephemeral=True)
                await self._finish_message()
                return
            try:
                proceeds, balance = self.source_view.database.sell_card(
                    self.source_view.user_id,
                    self.player.name,
                    1,
                    expected_value=self.player.value,
                )
            except EconomyError as exc:
                self.source_view._cancel_sale(self.reward_index)
                await interaction.response.send_message(
                    f"Quick Sell was not completed: {exc}",
                    ephemeral=True,
                )
            else:
                self.source_view._complete_sale(self.reward_index)
                await interaction.response.send_message(
                    f"Quick sold **{player_card_label(self.player)}** for "
                    f"**{format_coins(proceeds)}**. Your balance is now "
                    f"**{format_coins(balance)}**.",
                    ephemeral=True,
                )
            await self.source_view._sync_message()
            await self._finish_message()

    async def cancel(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            if self._processed:
                await interaction.response.send_message(
                    "This Quick Sell confirmation has already been processed.",
                    ephemeral=True,
                )
                return
            self._processed = True
            self.source_view._cancel_sale(self.reward_index)
            await interaction.response.send_message(
                f"Canceled. **{player_card_label(self.player)}** remains "
                "in your inventory.",
                ephemeral=True,
            )
            await self.source_view._sync_message()
            await self._finish_message()

    async def _finish_message(self) -> None:
        for item in self.children:
            item.disabled = True
        await self._sync_message()
        self.stop()

    async def _sync_message(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except (AttributeError, discord.HTTPException):
            LOGGER.debug("Could not refresh a Quick Sell confirmation.")

    async def on_timeout(self) -> None:
        async with self._lock:
            if not self._processed:
                self._processed = True
                self.source_view._cancel_sale(self.reward_index)
                await self.source_view._sync_message()
            await self._finish_message()
