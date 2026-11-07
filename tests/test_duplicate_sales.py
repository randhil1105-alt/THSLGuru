from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

from commands.common import player_card_label
from commands.duplicate_sales import (
    DuplicateSaleCommands,
    DuplicateSaleConfirmationView,
)
from database import (
    AUCTION_DURATIONS_SECONDS,
    Database,
    DuplicateSalePreviewChanged,
)
from main import load_player_seed


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.edited: dict[str, object] | None = None

    async def send_message(self, content: str | None = None, **kwargs: object) -> None:
        self.messages.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs: object) -> None:
        self.edited = kwargs


class FakeInteraction:
    def __init__(self, user_id: int, interaction_id: int = 1234) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()


class DuplicateSaleDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "duplicate-sales.sqlite3")
        self.database.initialize(load_player_seed())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sells_one_duplicate_and_keeps_one_copy(self) -> None:
        user_id = "single-duplicate-seller"
        card = self.database.get_player("Dan")
        assert card is not None
        self.database.add_inventory(user_id, card.name, 2)

        preview = self.database.get_duplicate_sale_preview(user_id)
        self.assertEqual(preview.total_cards, 1)
        self.assertEqual(preview.items[0].quantity, 1)
        self.assertEqual(preview.items[0].unit_value, card.value)

        result = self.database.sell_duplicate_cards(user_id, "single-sale", preview.items)

        self.assertEqual(result.total_earnings, card.value)
        self.assertEqual(result.new_balance, card.value)
        self.assertEqual(
            [(entry.player.name, entry.quantity) for entry in self.database.get_inventory(user_id)],
            [("Dan", 1)],
        )

    def test_sells_multiple_duplicate_types_at_database_values(self) -> None:
        user_id = "multiple-duplicate-seller"
        copies = {"Hala (Gold 90)": 3, "Chicken": 4}
        expected_earnings = 0
        for name, owned_count in copies.items():
            card = self.database.get_player(name)
            assert card is not None
            self.database.add_inventory(user_id, name, owned_count)
            expected_earnings += (owned_count - 1) * card.value

        preview = self.database.get_duplicate_sale_preview(user_id)
        self.assertEqual(preview.total_cards, 5)
        self.assertEqual(preview.total_earnings, expected_earnings)
        self.assertEqual(
            {item.player_name: item.quantity for item in preview.items},
            {"Hala (Gold 90)": 2, "Chicken": 3},
        )

        result = self.database.sell_duplicate_cards(user_id, "multi-sale", preview.items)

        self.assertEqual(result.total_earnings, expected_earnings)
        self.assertEqual(
            {entry.player.name: entry.quantity for entry in self.database.get_inventory(user_id)},
            {"Hala (Gold 90)": 1, "Chicken": 1},
        )

    def test_card_assigned_to_team_is_protected(self) -> None:
        user_id = "team-card-seller"
        self.database.add_inventory(user_id, "Dan", 3)
        self.database.set_lineup_position(user_id, "CM1", "Dan")

        preview = self.database.get_duplicate_sale_preview(user_id)

        self.assertEqual(preview.items, ())
        self.assertEqual(preview.protected[0].player_name, "Dan")
        self.assertIn("assigned to /team", preview.protected[0].reason)
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 3)

    def test_card_listed_in_active_auction_is_protected(self) -> None:
        user_id = "auction-card-seller"
        card = self.database.get_player("Dan")
        assert card is not None
        self.database.add_inventory(user_id, card.name, 3)
        self.database.create_auction(
            user_id,
            "Auction Seller",
            card.id,
            100,
            AUCTION_DURATIONS_SECONDS[0],
            "active-duplicate-test-auction",
            "123456",
            now=5_000,
        )

        preview = self.database.get_duplicate_sale_preview(user_id)

        self.assertEqual(preview.items, ())
        self.assertEqual(preview.protected[0].player_name, card.name)
        self.assertIn("active auction", preview.protected[0].reason)
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 2)

    def test_sale_receipt_prevents_duplicate_payout(self) -> None:
        user_id = "idempotent-duplicate-seller"
        card = self.database.get_player("Chicken")
        assert card is not None
        self.database.add_inventory(user_id, card.name, 2)
        preview = self.database.get_duplicate_sale_preview(user_id)

        first = self.database.sell_duplicate_cards(user_id, "same-sale", preview.items)
        second = self.database.sell_duplicate_cards(user_id, "same-sale", preview.items)

        self.assertFalse(first.already_processed)
        self.assertTrue(second.already_processed)
        self.assertEqual(second.total_earnings, card.value)
        self.assertEqual(second.new_balance, card.value)
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 1)

    def test_changed_inventory_rejects_stale_confirmation(self) -> None:
        user_id = "stale-duplicate-seller"
        self.database.add_inventory(user_id, "Dan", 2)
        preview = self.database.get_duplicate_sale_preview(user_id)
        self.database.add_inventory(user_id, "Dan", 1)

        with self.assertRaises(DuplicateSalePreviewChanged):
            self.database.sell_duplicate_cards(user_id, "stale-sale", preview.items)
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 3)
        self.assertEqual(self.database.get_balance(user_id), 0)

    def test_user_with_no_duplicates_has_empty_preview(self) -> None:
        preview = self.database.get_duplicate_sale_preview("no-duplicate-cards")
        self.assertEqual(preview.items, ())
        self.assertEqual(preview.protected, ())
        self.assertEqual(preview.total_cards, 0)
        self.assertEqual(preview.total_earnings, 0)

    def test_user_cannot_confirm_another_users_duplicate_cards(self) -> None:
        self.database.add_inventory("actual-owner", "Dan", 2)
        other_users_preview = self.database.get_duplicate_sale_preview("actual-owner")

        with self.assertRaises(DuplicateSalePreviewChanged):
            self.database.sell_duplicate_cards(
                "different-user",
                "forged-sale",
                other_users_preview.items,
            )
        self.assertEqual(self.database.get_inventory("actual-owner")[0].quantity, 2)
        self.assertEqual(self.database.get_balance("different-user"), 0)


class DuplicateSaleViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "duplicate-view.sqlite3")
        self.database.initialize(load_player_seed())
        self.database.add_inventory(10, "Dan", 2)
        self.preview = self.database.get_duplicate_sale_preview(10)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def make_view(self) -> DuplicateSaleConfirmationView:
        return DuplicateSaleConfirmationView(
            self.database,
            owner_id=10,
            sale_id="view-sale",
            preview=self.preview,
        )

    async def test_cancel_leaves_inventory_and_balance_unchanged(self) -> None:
        view = self.make_view()
        interaction = FakeInteraction(10)

        await view.cancel_sale(interaction)  # type: ignore[arg-type]

        self.assertEqual(self.database.get_inventory(10)[0].quantity, 2)
        self.assertEqual(self.database.get_balance(10), 0)
        assert interaction.response.edited is not None
        self.assertIsNone(interaction.response.edited["view"])

    async def test_only_initiating_player_can_use_confirmation_controls(self) -> None:
        view = self.make_view()
        stranger = FakeInteraction(11)

        allowed = await view.interaction_check(stranger)  # type: ignore[arg-type]

        self.assertFalse(allowed)
        self.assertEqual(self.database.get_inventory(10)[0].quantity, 2)
        self.assertEqual(self.database.get_balance(10), 0)
        self.assertTrue(stranger.response.messages[0]["ephemeral"])
        self.assertEqual(view.confirm_button.label, "Confirm Sale")
        self.assertEqual(view.cancel_button.label, "Cancel")

    async def test_confirm_button_completes_atomic_sale_and_shows_balance(self) -> None:
        view = self.make_view()
        interaction = FakeInteraction(10)

        await view.confirm_sale(interaction)  # type: ignore[arg-type]

        self.assertEqual(self.database.get_inventory(10)[0].quantity, 1)
        card = self.database.get_player("Dan")
        assert card is not None
        self.assertEqual(self.database.get_balance(10), card.value)
        assert interaction.response.edited is not None
        embed = interaction.response.edited["embed"]
        assert isinstance(embed, discord.Embed)
        self.assertIn(f"{card.value:,} coins", embed.description or "")


class DuplicateSaleCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_duplicates_responds_without_showing_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "command.sqlite3")
            database.initialize(load_player_seed())
            cog = DuplicateSaleCommands(database)
            interaction = FakeInteraction(77)

            await cog.sellduplicates.callback(cog, interaction)  # type: ignore[arg-type]

            self.assertTrue(interaction.response.messages[0]["ephemeral"])
            self.assertIn("no duplicate cards", str(interaction.response.messages[0]["content"]))

    async def test_sellall_alias_sells_new_s1_toty_duplicates_at_catalog_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "sellall.sqlite3")
            database.initialize(load_player_seed())
            card_names = (
                "Road (S1 TOTY 98)",
                "Matrix (S1 TOTY 97)",
                "Lauren (S1 TOTY 97)",
            )
            expected_earnings = 0
            for name in card_names:
                card = database.get_player(name)
                self.assertIsNotNone(card)
                assert card is not None
                database.add_inventory(77, name, 2)
                expected_earnings += card.value

            cog = DuplicateSaleCommands(database)
            interaction = FakeInteraction(77)
            await cog.sellall.callback(cog, interaction)  # type: ignore[arg-type]

            response = interaction.response.messages[0]
            embed = response["embed"]
            self.assertIsInstance(embed, discord.Embed)
            for name in card_names:
                card = database.get_player(name)
                assert card is not None
                self.assertIn(player_card_label(card), str(embed.fields[0].value))
            view = response["view"]
            self.assertIsInstance(view, DuplicateSaleConfirmationView)
            await view.confirm_sale(interaction)  # type: ignore[union-attr,arg-type]

            self.assertEqual(database.get_balance(77), expected_earnings)
            self.assertEqual(
                {entry.player.name: entry.quantity for entry in database.get_inventory(77)},
                {name: 1 for name in card_names},
            )
