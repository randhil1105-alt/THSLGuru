from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from threading import Barrier
from typing import Any
from unittest.mock import AsyncMock

import discord

from commands.shop import ShopCommands, _format_reset_countdown
from database import (
    DAILY_SHOP_ALLOWED_RARITIES,
    DAILY_SHOP_CARD_COUNT,
    FORMATION_STARTERS,
    POSITION_ROLE_BY_SLOT,
    Database,
    NoEligibleCards,
    ShopInsufficientFunds,
    ShopPurchaseAlreadyProcessed,
    ShopSelectionExpired,
    card_position_role,
    format_card_type,
)
from commands.common import format_coins
from main import load_player_seed


class FixedSample:
    def __init__(self, *, take_last: bool = False) -> None:
        self.take_last = take_last

    def sample(self, population, count: int):
        return list(population[-count:] if self.take_last else population[:count])


class DailyShopDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "daily-shop.sqlite3"
        self.database = Database(self.path)
        self.seed = load_player_seed()
        self.database.initialize(self.seed)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_five_shared_cards_persist_and_rotate_on_the_utc_date(self) -> None:
        before_midnight = 1_704_067_199  # 2023-12-31 23:59:59 UTC
        next_day = before_midnight + 1
        first = self.database.get_daily_shop(
            now=before_midnight,
            rng=FixedSample(),
        )
        same_day = self.database.get_daily_shop(
            now=before_midnight,
            rng=FixedSample(take_last=True),
        )
        self.assertEqual(len(first["players"]), DAILY_SHOP_CARD_COUNT)
        self.assertEqual(len(set(first["player_ids"])), DAILY_SHOP_CARD_COUNT)
        self.assertEqual(first["shop_day"], "2023-12-31")
        self.assertEqual(first["player_ids"], same_day["player_ids"])

        second = self.database.get_daily_shop(
            now=next_day,
            rng=FixedSample(take_last=True),
        )
        self.assertEqual(second["shop_day"], "2024-01-01")
        self.assertNotEqual(first["player_ids"], second["player_ids"])

        reopened = Database(self.path)
        reopened.initialize(self.seed)
        saved = reopened.get_daily_shop(
            now=before_midnight,
            rng=FixedSample(take_last=True),
        )
        self.assertEqual(first["player_ids"], saved["player_ids"])
        self.assertEqual(reopened.get_daily_shop(now=next_day)["player_ids"], second["player_ids"])

    def test_daily_shop_only_selects_gold_and_silver_cards(self) -> None:
        shop = self.database.get_daily_shop(
            now=1_704_067_199,
            rng=FixedSample(),
        )

        self.assertEqual(len(shop["players"]), DAILY_SHOP_CARD_COUNT)
        self.assertTrue(
            all(
                player.rarity.upper() in DAILY_SHOP_ALLOWED_RARITIES
                for player in shop["players"]
            )
        )

    def test_legacy_selection_is_replaced_and_special_cards_cannot_be_bought(self) -> None:
        now = 1_704_067_199
        shop_day = "2023-12-31"
        legacy_ids = [
            player.id for player in self.database.get_players(rarity="TOTW")[:5]
        ]
        self.assertEqual(len(legacy_ids), DAILY_SHOP_CARD_COUNT)
        with self.database._transaction() as connection:
            connection.execute(
                """
                INSERT INTO daily_shop_selections(day_utc, player_ids_json, created_at)
                VALUES (?, ?, ?)
                """,
                (shop_day, json.dumps(legacy_ids), int(now)),
            )

        self.database.set_balance("legacy-shop-buyer", 500_000_000)
        with self.assertRaises(ShopSelectionExpired):
            self.database.buy_daily_shop_card(
                "legacy-shop-buyer",
                legacy_ids[0],
                "legacy-special-card-purchase",
                shop_day,
                now=now,
            )

        refreshed = self.database.get_daily_shop(now=now, rng=FixedSample())
        self.assertTrue(
            all(
                player.rarity.upper() in DAILY_SHOP_ALLOWED_RARITIES
                for player in refreshed["players"]
            )
        )
        self.assertNotEqual(set(refreshed["player_ids"]), set(legacy_ids))

    def test_countdown_matches_the_utc_midnight_rotation_without_negative_values(self) -> None:
        before_midnight = 1_704_067_199
        midnight = before_midnight + 1
        after_midnight = midnight + 10

        self.assertEqual(_format_reset_countdown(before_midnight), "0h 0m 1s")
        self.assertEqual(_format_reset_countdown(midnight), "24h 0m 0s")
        self.assertEqual(_format_reset_countdown(after_midnight), "23h 59m 50s")
        self.assertNotIn("-", _format_reset_countdown(after_midnight))

    def test_buy_charges_exact_catalog_value_and_duplicate_receipt_is_idempotent(self) -> None:
        now = 1_704_067_199
        shop = self.database.get_daily_shop(now=now)
        card = shop["players"][0]
        self.database.set_balance("buyer", card.value + 123)

        purchase = self.database.buy_daily_shop_card(
            "buyer",
            card.id,
            "interaction-1",
            shop["shop_day"],
            now=now,
        )
        self.assertEqual(purchase["price"], card.value)
        self.assertEqual(purchase["balance_after"], 123)
        self.assertFalse(purchase["already_processed"])
        self.assertEqual(self.database.get_balance("buyer"), 123)
        self.assertEqual(
            {entry.player.id: entry.quantity for entry in self.database.get_inventory("buyer")},
            {card.id: 1},
        )

        retry = self.database.buy_daily_shop_card(
            "buyer",
            card.id,
            "interaction-1",
            shop["shop_day"],
            now=now,
        )
        self.assertTrue(retry["already_processed"])
        self.assertEqual(retry["balance_after"], 123)
        self.assertEqual(self.database.get_balance("buyer"), 123)
        self.assertEqual(self.database.get_inventory("buyer")[0].quantity, 1)
        self.assertEqual(self.database.get_player_by_id(card.id).value, card.value)
        self.assertEqual(
            self.database.get_daily_shop(now=now)["player_ids"],
            shop["player_ids"],
        )
        other_card = next(item for item in shop["players"] if item.id != card.id)
        with self.assertRaises(ShopPurchaseAlreadyProcessed):
            self.database.buy_daily_shop_card(
                "buyer",
                other_card.id,
                "interaction-1",
                shop["shop_day"],
                now=now,
            )
        self.assertEqual(self.database.get_balance("buyer"), 123)

    def test_insufficient_balance_changes_neither_inventory_nor_balance(self) -> None:
        now = 1_704_067_199
        shop = self.database.get_daily_shop(now=now)
        card = shop["players"][0]
        self.assertGreater(card.value, 0)
        self.database.set_balance("poor-buyer", card.value - 1)

        with self.assertRaises(ShopInsufficientFunds) as raised:
            self.database.buy_daily_shop_card(
                "poor-buyer",
                card.id,
                "insufficient-interaction",
                shop["shop_day"],
                now=now,
            )
        self.assertEqual(raised.exception.required, card.value)
        self.assertEqual(raised.exception.balance, card.value - 1)
        self.assertEqual(self.database.get_balance("poor-buyer"), card.value - 1)
        self.assertEqual(self.database.get_inventory("poor-buyer"), [])

    def test_concurrent_retry_of_one_interaction_charges_and_grants_once(self) -> None:
        now = 1_704_067_199
        shop = self.database.get_daily_shop(now=now)
        card = next(player for player in shop["players"] if player.value > 0)
        self.database.set_balance("concurrent-buyer", card.value)
        barrier = Barrier(2)

        def buy_once():
            barrier.wait()
            return self.database.buy_daily_shop_card(
                "concurrent-buyer",
                card.id,
                "same-concurrent-interaction",
                shop["shop_day"],
                now=now,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            purchases = list(executor.map(lambda _index: buy_once(), range(2)))

        self.assertEqual(sum(not purchase["already_processed"] for purchase in purchases), 1)
        self.assertEqual(sum(bool(purchase["already_processed"]) for purchase in purchases), 1)
        self.assertEqual(self.database.get_balance("concurrent-buyer"), 0)
        self.assertEqual(self.database.get_inventory("concurrent-buyer")[0].quantity, 1)

    def test_stale_button_cannot_buy_after_the_utc_shop_rotation(self) -> None:
        before_midnight = 1_704_067_199
        shop = self.database.get_daily_shop(now=before_midnight)
        card = shop["players"][0]
        self.database.set_balance("late-buyer", card.value + 50)

        with self.assertRaises(ShopSelectionExpired):
            self.database.buy_daily_shop_card(
                "late-buyer",
                card.id,
                "late-interaction",
                shop["shop_day"],
                now=before_midnight + 1,
            )
        self.assertEqual(self.database.get_balance("late-buyer"), card.value + 50)
        self.assertEqual(self.database.get_inventory("late-buyer"), [])

    def test_purchased_cards_work_with_inventory_set_sell_and_trade(self) -> None:
        now = 1_704_067_199
        shop = self.database.get_daily_shop(now=now)
        card = shop["players"][0]
        buyer = "shop-buyer"
        receiver = "shop-receiver"
        self.database.set_balance(buyer, card.value * 4 + 100)
        self.database.buy_daily_shop_card(
            buyer,
            card.id,
            "compatibility-first-buy",
            shop["shop_day"],
            now=now,
        )
        self.assertEqual(self.database.get_inventory(buyer)[0].player.id, card.id)

        role = card_position_role(card.position)
        selected_slot = None
        selected_formation = None
        for formation, slots in FORMATION_STARTERS.items():
            for slot in slots:
                if (
                    POSITION_ROLE_BY_SLOT[slot] == role
                    and (
                        card.allowed_lineup_positions is None
                        or slot in card.allowed_lineup_positions
                    )
                ):
                    selected_slot = slot
                    selected_formation = formation
                    break
            if selected_slot:
                break
        self.assertIsNotNone(selected_slot)
        self.database.set_formation(buyer, selected_formation)
        self.database.set_lineup_position(buyer, selected_slot, card.id)
        self.assertEqual(self.database.get_lineup(buyer)[selected_slot].id, card.id)
        self.database.clear_lineup_position(buyer, selected_slot)
        proceeds, _balance = self.database.sell_card(buyer, card.name, 1)
        self.assertEqual(proceeds, card.value * 80 // 100)
        self.assertEqual(self.database.get_inventory(buyer), [])

        self.database.buy_daily_shop_card(
            buyer,
            card.id,
            "compatibility-second-buy",
            shop["shop_day"],
            now=now,
        )
        self.database.create_trade_request(
            "shop-compatibility-trade",
            buyer,
            receiver,
            "Shop Buyer",
            "Shop Receiver",
            "guild",
            "channel",
            now=now,
        )
        self.database.accept_trade_request(
            "shop-compatibility-trade",
            receiver,
            now=now + 1,
        )
        self.database.add_trade_card(
            "shop-compatibility-trade",
            buyer,
            card.id,
            now=now + 2,
        )
        self.database.confirm_trade(
            "shop-compatibility-trade",
            buyer,
            now=now + 3,
        )
        completed = self.database.confirm_trade(
            "shop-compatibility-trade",
            receiver,
            now=now + 4,
        )
        self.assertTrue(completed["completed"])
        self.assertEqual(self.database.get_inventory(buyer), [])
        self.assertEqual(
            {
                entry.player.id: entry.quantity
                for entry in self.database.get_inventory(receiver)
            },
            {card.id: 1},
        )

    def test_reset_clears_player_receipts_but_preserves_the_shared_daily_selection(self) -> None:
        now = 1_704_067_199
        shop = self.database.get_daily_shop(now=now)
        card = shop["players"][0]
        self.database.set_balance("reset-buyer", card.value)
        self.database.buy_daily_shop_card(
            "reset-buyer",
            card.id,
            "reset-purchase",
            shop["shop_day"],
            now=now,
        )
        self.database.reset_all_player_progress("admin", "guild", now=int(now))
        self.assertEqual(self.database.get_balance("reset-buyer"), 0)
        self.assertEqual(self.database.get_inventory("reset-buyer"), [])
        self.assertEqual(
            self.database.get_daily_shop(now=now)["player_ids"],
            shop["player_ids"],
        )
        with self.database._read_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM daily_shop_purchase_receipts"
                ).fetchone()[0],
                0,
            )

    def test_schema_initialization_preserves_existing_player_progress(self) -> None:
        self.database.set_balance("preserved-user", 765_432)
        self.database.add_inventory("preserved-user", "Dan", 2)
        self.database.award_xp("preserved-user", "preserved-shop-xp", 450, "test")
        before = self.database.get_xp_profile("preserved-user")

        reopened = Database(self.path)
        reopened.initialize(self.seed)

        self.assertEqual(reopened.get_balance("preserved-user"), 765_432)
        self.assertEqual(
            {entry.player.name: entry.quantity for entry in reopened.get_inventory("preserved-user")},
            {"Dan": 2},
        )
        after = reopened.get_xp_profile("preserved-user")
        self.assertEqual(after.total_xp, before.total_xp)
        self.assertEqual(after.level, before.level)


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.deferred = False
        self.defer_kwargs: dict[str, Any] = {}

    async def send_message(self, content=None, **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})

    async def defer(self, **kwargs) -> None:
        self.deferred = True
        self.defer_kwargs = kwargs

    def is_done(self) -> bool:
        return self.deferred or bool(self.messages)


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, content=None, **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})


class FakeMessage:
    def __init__(self, attachments=None) -> None:
        self.attachments = list(attachments or [])
        self.edits: list[dict[str, Any]] = []

    async def edit(self, **kwargs) -> None:
        self.edits.append(kwargs)


class DailyShopCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "shop-command.sqlite3")
        self.database.initialize(load_player_seed())
        self.cog = ShopCommands(self.database)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def make_slash_interaction(self, user_id: int) -> SimpleNamespace:
        response = FakeResponse()
        message = FakeMessage()
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=response,
            original_response=AsyncMock(return_value=message),
        )

    def make_component_interaction(
        self,
        *,
        user_id: int,
        interaction_id: str,
        message: FakeMessage,
    ) -> SimpleNamespace:
        response = FakeResponse()
        followup = FakeFollowup()

        async def edit_original_response(**kwargs):
            message.edits.append(kwargs)
            return message

        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            id=interaction_id,
            response=response,
            followup=followup,
            message=message,
            edit_original_response=edit_original_response,
        )

    async def test_shop_command_displays_five_illustrated_listings_shared_by_all_users(self) -> None:
        self.database.set_balance(501, 25_000_000)
        self.database.set_balance(502, 123_456)
        first = self.make_slash_interaction(501)
        second = self.make_slash_interaction(502)
        await ShopCommands.shop.callback(self.cog, first)
        await ShopCommands.shop.callback(self.cog, second)

        first_payload = first.response.messages[0]
        second_payload = second.response.messages[0]
        self.assertFalse(first_payload["ephemeral"])
        self.assertFalse(second_payload["ephemeral"])
        self.assertEqual(
            first_payload["embeds"][0].title,
            "🛒 DAILY SHOP",
        )
        self.assertEqual(
            len(first_payload["embeds"]),
            DAILY_SHOP_CARD_COUNT + 1,
        )
        self.assertEqual(len(first_payload["files"]), DAILY_SHOP_CARD_COUNT)
        self.assertEqual(len(first_payload["view"].children), DAILY_SHOP_CARD_COUNT)
        self.assertEqual(len({button.player_id for button in first_payload["view"].children}), 5)
        self.assertIn("**🌐 Time till reset:**", first_payload["embeds"][0].description)
        self.assertIn("**💼 Balance:** 25,000,000", first_payload["embeds"][0].description)
        self.assertIn("**💼 Balance:** 123,456", second_payload["embeds"][0].description)
        self.assertRegex(
            first_payload["embeds"][0].description,
            r"\*\*🌐 Time till reset:\*\* \d+h \d+m \d+s",
        )
        self.assertIn("UTC", first_payload["embeds"][0].footer.text)

        expected_players = self.database.get_daily_shop()["players"]
        description_lines = first_payload["embeds"][0].description.splitlines()
        self.assertEqual(
            description_lines[:DAILY_SHOP_CARD_COUNT],
            second_payload["embeds"][0].description.splitlines()[
                :DAILY_SHOP_CARD_COUNT
            ],
        )
        self.assertEqual(
            description_lines[:DAILY_SHOP_CARD_COUNT],
            [
                f"**{player.overall} {player.name}** "
                f"({format_card_type(player.rarity)}) - "
                f"**{format_coins(player.value)}**"
                for player in expected_players
            ],
        )
        self.assertTrue(
            all(embed.image.url for embed in first_payload["embeds"][1:])
        )
        self.assertEqual(
            [embed.description for embed in first_payload["embeds"][1:]],
            [
                f"**{player.name}** · Position: **{player.position}**"
                for player in expected_players
            ],
        )
        self.assertEqual(
            [button.player_id for button in first_payload["view"].children],
            [player.id for player in expected_players],
        )

        for payload in (first_payload, second_payload):
            for artwork in payload["files"]:
                artwork.close()

    async def test_buy_button_charges_once_confirms_and_refreshes_shop_balance(self) -> None:
        slash = self.make_slash_interaction(601)
        await ShopCommands.shop.callback(self.cog, slash)
        payload = slash.response.messages[0]
        button = payload["view"].children[0]
        player = next(
            card
            for card in self.database.get_daily_shop()["players"]
            if card.id == button.player_id
        )
        self.database.set_balance(601, player.value + 10)
        message = FakeMessage(attachments=["existing-shop-images"])
        view = payload["view"]

        first_click = self.make_component_interaction(
            user_id=601,
            interaction_id="shop-click-once",
            message=message,
        )
        await view.buy(first_click, player.id)
        self.assertEqual(self.database.get_balance(601), 10)
        self.assertEqual(self.database.get_inventory(601)[0].quantity, 1)
        self.assertIn("Purchased", first_click.followup.messages[0]["content"])
        self.assertIn(player.name, first_click.followup.messages[0]["content"])
        self.assertEqual(
            len(message.edits[-1]["embeds"]),
            DAILY_SHOP_CARD_COUNT + 1,
        )
        self.assertIn(
            "**💼 Balance:** 10",
            message.edits[-1]["embeds"][0].description,
        )
        self.assertEqual(
            message.edits[-1]["attachments"],
            ["existing-shop-images"],
        )

        retry = self.make_component_interaction(
            user_id=601,
            interaction_id="shop-click-once",
            message=message,
        )
        await view.buy(retry, player.id)
        self.assertEqual(self.database.get_balance(601), 10)
        self.assertEqual(self.database.get_inventory(601)[0].quantity, 1)
        self.assertIn("already processed", retry.followup.messages[0]["content"])

        for artwork in payload["files"]:
            artwork.close()

    async def test_any_player_can_buy_publicly_listed_cards_without_replacing_opener_balance(self) -> None:
        opener_id = 650
        buyer_id = 651
        self.database.set_balance(opener_id, 99_000)
        slash = self.make_slash_interaction(opener_id)
        await ShopCommands.shop.callback(self.cog, slash)
        payload = slash.response.messages[0]
        button = payload["view"].children[0]
        player = next(
            card
            for card in self.database.get_daily_shop()["players"]
            if card.id == button.player_id
        )
        self.database.set_balance(buyer_id, player.value + 77)
        message = FakeMessage(attachments=["shared-shop-images"])
        click = self.make_component_interaction(
            user_id=buyer_id,
            interaction_id="public-shop-buy",
            message=message,
        )

        await payload["view"].buy(click, player.id)

        self.assertEqual(click.response.defer_kwargs, {"ephemeral": True, "thinking": True})
        self.assertEqual(self.database.get_balance(buyer_id), 77)
        self.assertEqual(self.database.get_inventory(buyer_id)[0].player.id, player.id)
        self.assertEqual(self.database.get_balance(opener_id), 99_000)
        self.assertEqual(message.edits, [])
        self.assertTrue(click.followup.messages[0]["ephemeral"])
        self.assertIn(
            "Your balance is now **77 coins**",
            click.followup.messages[0]["content"],
        )
        for artwork in payload["files"]:
            artwork.close()

    async def test_insufficient_funds_shows_error_without_purchase(self) -> None:
        slash = self.make_slash_interaction(701)
        await ShopCommands.shop.callback(self.cog, slash)
        payload = slash.response.messages[0]
        button = payload["view"].children[0]
        player = next(
            card
            for card in self.database.get_daily_shop()["players"]
            if card.id == button.player_id
        )
        self.database.set_balance(701, player.value - 1)
        message = FakeMessage(attachments=["existing-shop-images"])
        click = self.make_component_interaction(
            user_id=701,
            interaction_id="shop-insufficient",
            message=message,
        )

        await payload["view"].buy(click, player.id)
        self.assertIn("balance is", click.followup.messages[0]["content"])
        self.assertEqual(self.database.get_balance(701), player.value - 1)
        self.assertEqual(self.database.get_inventory(701), [])
        self.assertIn(
            f"**💼 Balance:** {player.value - 1:,}",
            message.edits[-1]["embeds"][0].description,
        )
        for artwork in payload["files"]:
            artwork.close()

    async def test_expired_shop_view_disables_all_buttons(self) -> None:
        slash = self.make_slash_interaction(801)
        await ShopCommands.shop.callback(self.cog, slash)
        view = slash.response.messages[0]["view"]
        message = FakeMessage()
        view.bind_message(message)

        await view.on_timeout()

        self.assertTrue(all(button.disabled for button in view.children))
        self.assertEqual(len(message.edits), 1)
        for artwork in slash.response.messages[0]["files"]:
            artwork.close()


if __name__ == "__main__":
    unittest.main()
