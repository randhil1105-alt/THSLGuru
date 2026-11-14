from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from commands.economy import EconomyCommands
from commands.packs import PACKS, PackCommands
from commands.quick_sell import (
    KeepCardButton,
    QuickSellButton,
    QuickSellConfirmButton,
    QuickSellConfirmationView,
    QuickSellView,
)
from database import Database, calculate_sell_payout
from main import load_player_seed


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, content: str | None = None, **kwargs: object) -> None:
        self.messages.append({"content": content, **kwargs})


class FakeMessage:
    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []

    async def edit(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


class FakeInteraction:
    def __init__(self, user_id: str, interaction_id: int = 1) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.id = interaction_id
        self.response = FakeResponse()
        self.message = FakeMessage()

    async def original_response(self) -> FakeMessage:
        return self.message


class QuickSellTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "quick-sell.sqlite3")
        self.database.initialize(load_player_seed())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _sell_reward(
        self,
        view: QuickSellView,
        user_id: str,
        reward_index: int = 0,
    ) -> tuple[dict[str, object], dict[str, object], QuickSellConfirmationView]:
        button = next(
            item
            for item in view.children
            if isinstance(item, QuickSellButton)
            and item.reward_index == reward_index
        )
        request = FakeInteraction(user_id, interaction_id=100 + reward_index)
        self.assertTrue(await view.interaction_check(request))
        await button.callback(request)
        prompt = request.response.messages[-1]
        self.assertTrue(prompt["ephemeral"])
        confirmation = prompt["view"]
        self.assertIsInstance(confirmation, QuickSellConfirmationView)
        assert isinstance(confirmation, QuickSellConfirmationView)

        confirm_button = next(
            item
            for item in confirmation.children
            if isinstance(item, QuickSellConfirmButton)
        )
        confirm_interaction = FakeInteraction(
            user_id,
            interaction_id=200 + reward_index,
        )
        self.assertTrue(await confirmation.interaction_check(confirm_interaction))
        await confirm_button.callback(confirm_interaction)
        return prompt, confirm_interaction.response.messages[-1], confirmation

    async def test_every_pack_rarity_can_quick_sell_its_awarded_card(self) -> None:
        user_id = "quick-sell-every-pack"
        self.database.set_balance(
            user_id,
            sum(cost for _rarity, cost in PACKS) + 100,
        )
        command = PackCommands(self.database)

        for index, (rarity, cost) in enumerate(PACKS):
            with self.subTest(rarity=rarity):
                before_balance = self.database.get_balance(user_id)
                before_inventory = {
                    entry.player.id: entry.quantity
                    for entry in self.database.get_inventory(user_id)
                }
                interaction = FakeInteraction(user_id, interaction_id=1000 + index)
                await command._open(
                    interaction,
                    rarity=rarity,
                    cost=cost,
                    purchase_id=f"quick-sell-pack-{rarity}",
                )
                message = interaction.response.messages[-1]
                self.assertEqual(self.database.get_balance(user_id), before_balance - cost)
                view = message["view"]
                self.assertIsInstance(view, QuickSellView)
                assert isinstance(view, QuickSellView)
                quick_sell_buttons = [
                    item for item in view.children if isinstance(item, QuickSellButton)
                ]
                self.assertEqual(len(quick_sell_buttons), 1)
                card_id = view.players[0].id
                card = self.database.get_player_by_id(card_id)
                assert card is not None
                expected_payout = calculate_sell_payout(card.value)
                self.assertEqual(
                    self.database.get_inventory(user_id)[
                        next(
                            index
                            for index, entry in enumerate(
                                self.database.get_inventory(user_id)
                            )
                            if entry.player.id == card_id
                        )
                    ].quantity,
                    before_inventory.get(card_id, 0) + 1,
                )

                prompt, receipt, _confirmation = await self._sell_reward(
                    view,
                    user_id,
                )
                self.assertIn(player_name := card.name.split(" (", 1)[0], prompt["content"])
                self.assertIn(f"{expected_payout:,} coins", prompt["content"])
                self.assertIn(player_name, receipt["content"])
                self.assertIn(f"{expected_payout:,} coins", receipt["content"])
                self.assertEqual(
                    self.database.get_balance(user_id),
                    before_balance - cost + expected_payout,
                )
                after_inventory = {
                    entry.player.id: entry.quantity
                    for entry in self.database.get_inventory(user_id)
                }
                self.assertEqual(
                    after_inventory.get(card_id, 0),
                    before_inventory.get(card_id, 0),
                )
                if message.get("file") is not None:
                    message["file"].close()

    async def test_claim_can_be_kept_or_quick_sold_without_prior_ownership(self) -> None:
        keep_user = "quick-sell-claim-keep"
        before_keep_balance = self.database.get_balance(keep_user)
        keep_interaction = FakeInteraction(keep_user)
        await EconomyCommands(self.database).claim.callback(
            EconomyCommands(self.database),
            keep_interaction,
        )
        keep_message = keep_interaction.response.messages[-1]
        keep_view = keep_message["view"]
        self.assertIsInstance(keep_view, QuickSellView)
        assert isinstance(keep_view, QuickSellView)
        self.assertEqual(len(self.database.get_inventory(keep_user)), 1)
        keep_button = next(
            item for item in keep_view.children if isinstance(item, KeepCardButton)
        )
        keep_choice = FakeInteraction(keep_user)
        self.assertTrue(await keep_view.interaction_check(keep_choice))
        await keep_button.callback(keep_choice)
        self.assertIn("Kept", keep_choice.response.messages[-1]["content"])
        self.assertEqual(self.database.get_balance(keep_user), before_keep_balance)
        self.assertEqual(len(self.database.get_inventory(keep_user)), 1)
        keep_message["file"].close()

        sell_user = "quick-sell-claim-sale"
        self.assertEqual(self.database.get_inventory(sell_user), [])
        sell_interaction = FakeInteraction(sell_user)
        await EconomyCommands(self.database).claim.callback(
            EconomyCommands(self.database),
            sell_interaction,
        )
        sell_message = sell_interaction.response.messages[-1]
        sell_view = sell_message["view"]
        self.assertIsInstance(sell_view, QuickSellView)
        assert isinstance(sell_view, QuickSellView)
        claimed_card = sell_view.players[0]
        expected_payout = calculate_sell_payout(claimed_card.value)
        self.assertEqual(self.database.get_inventory(sell_user)[0].player, claimed_card)

        prompt, receipt, _confirmation = await self._sell_reward(sell_view, sell_user)
        self.assertIn(f"{expected_payout:,} coins", prompt["content"])
        self.assertIn(f"{expected_payout:,} coins", receipt["content"])
        self.assertEqual(self.database.get_balance(sell_user), expected_payout)
        self.assertEqual(self.database.get_inventory(sell_user), [])
        sell_message["file"].close()

    async def test_free_gold_and_totw_packs_also_include_quick_sell(self) -> None:
        user_id = "quick-sell-free-packs"
        self.database.set_balance(user_id, 0)
        command = PackCommands(self.database)
        for index, rarity in enumerate(("GOLD", "TOTW")):
            with self.subTest(rarity=rarity):
                with self.database._transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO free_pack_inventory(user_id, rarity, quantity)
                        VALUES (?, ?, 1)
                        ON CONFLICT(user_id, rarity)
                        DO UPDATE SET quantity = free_pack_inventory.quantity + 1
                        """,
                        (user_id, rarity),
                    )
                interaction = FakeInteraction(user_id, interaction_id=3000 + index)
                await command._open_free_reward(
                    interaction,
                    rarity=rarity,
                    redemption_id=f"quick-sell-free-{rarity}",
                )
                message = interaction.response.messages[-1]
                view = message["view"]
                self.assertIsInstance(view, QuickSellView)
                assert isinstance(view, QuickSellView)
                card = view.players[0]
                balance_before_sale = self.database.get_balance(user_id)
                expected_payout = calculate_sell_payout(card.value)
                _prompt, receipt, _confirmation = await self._sell_reward(
                    view,
                    user_id,
                )
                self.assertIn(f"{expected_payout:,} coins", receipt["content"])
                self.assertEqual(
                    self.database.get_balance(user_id),
                    balance_before_sale + expected_payout,
                )
                self.assertEqual(
                    self.database.get_free_pack_inventory(user_id)[rarity],
                    0,
                )
                if message.get("file") is not None:
                    message["file"].close()

    async def test_duplicate_copies_sell_once_per_button_and_block_double_clicks(self) -> None:
        user_id = "quick-sell-duplicates"
        card = self.database.get_player("Dan")
        assert card is not None
        payout = calculate_sell_payout(card.value)
        self.database.add_inventory(user_id, card.id, 2)
        view = QuickSellView(self.database, user_id, [card, card])
        source = FakeInteraction(user_id)
        await view.bind_message(source)

        _prompt, _receipt, confirmation = await self._sell_reward(
            view,
            user_id,
            reward_index=0,
        )
        self.assertEqual(self.database.get_balance(user_id), payout)
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 1)

        confirm_button = next(
            item
            for item in confirmation.children
            if isinstance(item, QuickSellConfirmButton)
        )
        repeated_confirmation = FakeInteraction(user_id)
        self.assertTrue(
            await confirmation.interaction_check(repeated_confirmation)
        )
        await confirm_button.callback(repeated_confirmation)
        self.assertIn(
            "already been processed",
            repeated_confirmation.response.messages[-1]["content"],
        )
        self.assertEqual(self.database.get_balance(user_id), payout)
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 1)

        first_button = next(
            item
            for item in view.children
            if isinstance(item, QuickSellButton) and item.reward_index == 0
        )
        repeated_click = FakeInteraction(user_id)
        self.assertTrue(await view.interaction_check(repeated_click))
        await first_button.callback(repeated_click)
        self.assertIn("already been handled", repeated_click.response.messages[-1]["content"])
        self.assertEqual(self.database.get_balance(user_id), payout)
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 1)

        await self._sell_reward(view, user_id, reward_index=1)
        self.assertEqual(self.database.get_balance(user_id), payout * 2)
        self.assertEqual(self.database.get_inventory(user_id), [])

    async def test_cancel_and_expired_quick_sell_leave_card_and_balance_unchanged(self) -> None:
        user_id = "quick-sell-cancel"
        card = self.database.get_player("Dan")
        assert card is not None
        self.database.add_inventory(user_id, card.id)
        view = QuickSellView(self.database, user_id, [card])
        source = FakeInteraction(user_id)
        await view.bind_message(source)

        button = next(item for item in view.children if isinstance(item, QuickSellButton))
        request = FakeInteraction(user_id)
        self.assertTrue(await view.interaction_check(request))
        await button.callback(request)
        confirmation = request.response.messages[-1]["view"]
        assert isinstance(confirmation, QuickSellConfirmationView)
        cancel_button = next(
            item
            for item in confirmation.children
            if item.label == "Cancel"
        )
        cancel = FakeInteraction(user_id)
        self.assertTrue(await confirmation.interaction_check(cancel))
        await cancel_button.callback(cancel)
        self.assertIn("Canceled", cancel.response.messages[-1]["content"])
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 1)
        self.assertEqual(self.database.get_balance(user_id), 0)

        await view.on_timeout()
        self.assertTrue(button.disabled)
        expired_click = FakeInteraction(user_id)
        await button.callback(expired_click)
        self.assertIn("expired", expired_click.response.messages[-1]["content"])
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 1)
        self.assertEqual(self.database.get_balance(user_id), 0)


if __name__ == "__main__":
    unittest.main()
