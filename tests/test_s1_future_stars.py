from __future__ import annotations

import copy
import random
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

from commands.admin import AdminCommands
from commands.common import player_card_label
from commands.inventory import InventoryCommands
from commands.packs import PACKS, PackCommands
from commands.player import PlayerCommands
from database import (
    AUCTION_DURATIONS_SECONDS,
    S1_FUTURE_STARS_PACK_COST,
    Database,
    InsufficientFunds,
    calculate_pack_odds,
    pack_card_rating_weight,
)
from main import load_player_seed
from tests.test_commands import FakeMessage, FakeResponse


FUTURE_STARS_CARDS = {
    "Ebs (S1 FUTURE STARS 94)": {
        "overall": 94,
        "position": "WI",
        "country": "Ireland 🇮🇪",
        "team": "Buffalo Sabres",
        "rarity": "S1 FUTURE STARS",
        "value": 40_000_000,
        "stats": {"SHO": 95, "DRI": 89, "SAV": 81, "PAS": 93, "DEF": 84, "ACT": 87},
        "image_path": "cards/ebs_s1_future_stars_94.png",
        "allowed": ("WING1", "WING2"),
    },
    "Dan (S1 FUTURE STARS 90)": {
        "overall": 90,
        "position": "WI",
        "country": "Italy 🇮🇹",
        "team": "Dallas Stars",
        "rarity": "S1 FUTURE STARS",
        "value": 10_000_000,
        "stats": {"SHO": 90, "DRI": 93, "SAV": 80, "PAS": 86, "DEF": 84, "ACT": 83},
        "image_path": "cards/dan_s1_future_stars_90.png",
        "allowed": ("WING1", "WING2"),
    },
    "Ryuko (S1 FUTURE STARS 91)": {
        "overall": 91,
        "position": "WI",
        "country": "United States 🇺🇸",
        "team": "Buffalo Sabres",
        "rarity": "S1 FUTURE STARS",
        "value": 13_000_000,
        "stats": {"SHO": 92, "DRI": 94, "SAV": 81, "PAS": 89, "DEF": 84, "ACT": 87},
        "image_path": "cards/ryuko_s1_future_stars_91.png",
        "allowed": ("WING1", "WING2"),
    },
    "Gamer (S1 FUTURE STARS 93)": {
        "overall": 93,
        "position": "WI",
        "country": "United States 🇺🇸",
        "team": "Buffalo Sabres",
        "rarity": "S1 FUTURE STARS",
        "value": 30_000_000,
        "stats": {"SHO": 93, "DRI": 91, "SAV": 78, "PAS": 85, "DEF": 85, "ACT": 89},
        "image_path": "cards/gamer_s1_future_stars_93.png",
        "allowed": ("WING1", "WING2"),
    },
    "KaruWRLD (S1 FUTURE STARS 88)": {
        "overall": 88,
        "position": "C",
        "country": "Scotland 🏴",
        "team": "Anaheim Ducks",
        "rarity": "S1 FUTURE STARS",
        "value": 10_000_000,
        "stats": {"SHO": 87, "DRI": 86, "SAV": 83, "PAS": 90, "DEF": 86, "ACT": 84},
        "image_path": "cards/karuwrld_s1_future_stars_88.png",
        "allowed": ("CM1", "CM2"),
    },
    "HHH (S1 FUTURE STARS 89)": {
        "overall": 89,
        "position": "WI",
        "country": "England 🇬🇧",
        "team": "Anaheim Ducks",
        "rarity": "S1 FUTURE STARS",
        "value": 12_000_000,
        "stats": {"SHO": 92, "DRI": 84, "SAV": 79, "PAS": 85, "DEF": 80, "ACT": 84},
        "image_path": "cards/hhh_s1_future_stars_89.png",
        "allowed": ("WING1", "WING2"),
    },
    "Lauren (S1 FUTURE STARS 93)": {
        "overall": 93,
        "position": "GK",
        "country": "England 🇬🇧",
        "team": "New York Rangers",
        "rarity": "S1 FUTURE STARS",
        "value": 30_000_000,
        "stats": {"SAV": 93, "DIV": 95, "PAS": 88, "SWE": 90, "CLE": 91, "ACT": 93},
        "image_path": "cards/lauren_s1_future_stars_93.png",
        "allowed": ("GK",),
    },
    "Guzzinga (S1 FUTURE STARS 92)": {
        "overall": 92,
        "position": "WI",
        "country": "England 🇬🇧",
        "team": "New York Rangers",
        "rarity": "S1 FUTURE STARS",
        "value": 24_000_000,
        "stats": {"SHO": 93, "DRI": 95, "SAV": 84, "PAS": 92, "DEF": 90, "ACT": 87},
        "image_path": "cards/guzzinga_s1_future_stars_92.png",
        "allowed": ("WING1", "WING2"),
    },
    "Zizou (S1 FUTURE STARS 87)": {
        "overall": 87,
        "position": "C",
        "country": "England 🇬🇧",
        "team": "Russia",
        "rarity": "S1 FUTURE STARS",
        "value": 8_000_000,
        "stats": {"SHO": 86, "DRI": 87, "SAV": 85, "PAS": 88, "DEF": 83, "ACT": 86},
        "image_path": "cards/zizou_s1_future_stars_87.png",
        "allowed": ("CM1", "CM2"),
    },
    "Fadi (S1 FUTURE STARS 89)": {
        "overall": 89,
        "position": "WI",
        "country": "Algeria 🇩🇿",
        "team": "Pittsburgh Penguins",
        "rarity": "S1 FUTURE STARS",
        "value": 12_000_000,
        "stats": {"SHO": 89, "DRI": 90, "SAV": 80, "PAS": 91, "DEF": 88, "ACT": 85},
        "image_path": "cards/fadi_s1_future_stars_89.png",
        "allowed": ("WING1", "WING2"),
    },
}
NEW_CARDS = (
    "Ebs (S1 FUTURE STARS 94)",
    "Gamer (S1 FUTURE STARS 93)",
    "KaruWRLD (S1 FUTURE STARS 88)",
    "HHH (S1 FUTURE STARS 89)",
    "Lauren (S1 FUTURE STARS 93)",
    "Guzzinga (S1 FUTURE STARS 92)",
    "Zizou (S1 FUTURE STARS 87)",
    "Fadi (S1 FUTURE STARS 89)",
)


class _ChooseCardByName:
    def __init__(self, name: str) -> None:
        self.name = name
        self.pool_names: set[str] = set()

    def choices(self, population, *, weights, k):
        del weights
        self.pool_names = {str(row["name"]) for row in population}
        selected = next(row for row in population if str(row["name"]) == self.name)
        return [selected][:k]


class S1FutureStarsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "future-stars.sqlite3")
        self.seed = load_player_seed()
        self.database.initialize(self.seed)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _cards(self):
        cards = {}
        for name in FUTURE_STARS_CARDS:
            card = self.database.get_player(name)
            self.assertIsNotNone(card, name)
            assert card is not None
            cards[name] = card
        return cards

    def test_catalog_data_artwork_teams_and_seed_idempotency(self) -> None:
        cards = self._cards()
        self.assertEqual(
            {card.name for card in self.database.get_players(rarity="S1 FUTURE STARS")},
            set(FUTURE_STARS_CARDS),
        )
        image_paths: set[str] = set()
        for name, expected in FUTURE_STARS_CARDS.items():
            with self.subTest(card=name):
                card = cards[name]
                self.assertEqual(
                    (
                        card.overall,
                        card.position,
                        card.country,
                        card.team,
                        card.rarity,
                        card.value,
                        card.stats,
                        card.allowed_lineup_positions,
                        card.image_path,
                    ),
                    (
                        expected["overall"],
                        expected["position"],
                        expected["country"],
                        expected["team"],
                        expected["rarity"],
                        expected["value"],
                        expected["stats"],
                        expected["allowed"],
                        expected["image_path"],
                    ),
                )
                self.assertEqual(
                    player_card_label(card),
                    f"{card.overall} {name.split(' (', 1)[0]} "
                    "(S1 FUTURE STARS)",
                )
                image_paths.add(card.image_path)
                image = Path(card.image_path)
                self.assertTrue(image.is_file(), image)
                image_data = image.read_bytes()
                self.assertEqual(image_data[:8], b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack(">II", image_data[16:24])
                self.assertGreater(width, 250)
                self.assertGreater(height, 300)
                self.assertGreater(len(image_data), 50_000)
        self.assertEqual(len(image_paths), 10)

        user_id = "future-stars-seed-preservation"
        self.database.set_balance(user_id, 987)
        self.database.add_inventory(user_id, "Dan (S1 FUTURE STARS 90)", 2)
        self.database.initialize(self.seed)
        self.assertEqual(self.database.get_balance(user_id), 987)
        self.assertEqual(
            [(entry.player.name, entry.quantity)
             for entry in self.database.get_inventory(user_id)],
            [("Dan (S1 FUTURE STARS 90)", 2)],
        )
        self.assertEqual(
            sum(
                player.rarity == "S1 FUTURE STARS"
                for player in self.database.get_players()
            ),
            10,
        )

    def test_legacy_ebs_and_team_corrections_preserve_ids_inventory_and_receipts(
        self,
    ) -> None:
        migration_db = Database(
            Path(self.temp_dir.name) / "future-stars-migration.sqlite3"
        )
        future_names = set(FUTURE_STARS_CARDS)
        migration_db.initialize(
            [record for record in self.seed if record["name"] not in future_names]
        )
        seed_by_name = {record["name"]: record for record in self.seed}

        old_ebs = {
            "name": "Ebs (TOTW 94)",
            "overall": 94,
            "position": "WI",
            "country": "Ireland 🇮🇪",
            "team": "Ottawa Senators",
            "rarity": "TOTW",
            "value": 12_000_000,
            "stats": {"SHO": 95, "DRI": 92, "SAV": 84, "PAS": 92, "DEF": 83, "ACT": 91},
            "image_path": "cards/ebs_totw_94.png",
            "allowed_lineup_positions": ["WING1", "WING2"],
        }
        migration_db.add_player(old_ebs)
        old_ebs_card = migration_db.get_player(old_ebs["name"])
        assert old_ebs_card is not None
        migration_db.add_inventory("old-ebs-owner", old_ebs_card.id, 2)

        old_dan = copy.deepcopy(seed_by_name["Dan (S1 FUTURE STARS 90)"])
        old_dan["team"] = "Colorado Avalanche"
        old_ryuko = copy.deepcopy(seed_by_name["Ryuko (S1 FUTURE STARS 91)"])
        old_ryuko["team"] = "Colorado Avalanche"
        migration_db.add_player(old_dan)
        migration_db.add_player(old_ryuko)
        old_dan_card = migration_db.get_player(old_dan["name"])
        old_ryuko_card = migration_db.get_player(old_ryuko["name"])
        assert old_dan_card is not None and old_ryuko_card is not None

        purchase_user = "old-future-stars-purchase"
        migration_db.set_balance(
            purchase_user,
            S1_FUTURE_STARS_PACK_COST + 11,
        )
        old_pack_card, _ = migration_db.open_pack(
            purchase_user,
            "S1 FUTURE STARS",
            S1_FUTURE_STARS_PACK_COST,
            rng=_ChooseCardByName(old_dan_card.name),
            purchase_id="existing-future-stars-receipt",
        )
        self.assertEqual(old_pack_card.id, old_dan_card.id)

        migration_db.initialize(self.seed)
        migrated_ebs = migration_db.get_player("Ebs (S1 FUTURE STARS 94)")
        migrated_dan = migration_db.get_player("Dan (S1 FUTURE STARS 90)")
        migrated_ryuko = migration_db.get_player("Ryuko (S1 FUTURE STARS 91)")
        assert migrated_ebs is not None
        assert migrated_dan is not None
        assert migrated_ryuko is not None
        self.assertEqual(migrated_ebs.id, old_ebs_card.id)
        self.assertEqual(migrated_dan.id, old_dan_card.id)
        self.assertEqual(migrated_ryuko.id, old_ryuko_card.id)
        self.assertEqual(migrated_dan.team, "Dallas Stars")
        self.assertEqual(migrated_ryuko.team, "Buffalo Sabres")
        self.assertEqual(
            [(entry.player.id, entry.quantity)
             for entry in migration_db.get_inventory("old-ebs-owner")],
            [(migrated_ebs.id, 2)],
        )
        replayed, remaining = migration_db.open_pack(
            purchase_user,
            "S1 FUTURE STARS",
            S1_FUTURE_STARS_PACK_COST,
            rng=random.Random(4),
            purchase_id="existing-future-stars-receipt",
        )
        self.assertEqual(replayed.id, old_dan_card.id)
        self.assertEqual(remaining, 11)
        migration_db.initialize(self.seed)
        self.assertEqual(
            sum(
                card.name == "Dan (S1 FUTURE STARS 90)"
                for card in migration_db.get_players()
            ),
            1,
        )
        self.assertIsNone(migration_db.get_player("Ebs (TOTW 94)"))

    def test_future_stars_pack_pool_price_random_awards_and_funds(self) -> None:
        cards = self._cards()
        pool = self.database.get_players(rarity="S1 FUTURE STARS")
        self.assertEqual({card.name for card in pool}, set(FUTURE_STARS_CARDS))
        self.assertEqual(PACKS[-1], ("S1 FUTURE STARS", 30_000_000))
        self.assertEqual(S1_FUTURE_STARS_PACK_COST, 30_000_000)
        odds = calculate_pack_odds(pool)
        self.assertEqual(len(odds), 10)
        total_weight = sum(pack_card_rating_weight(card.overall) for card in pool)
        for card in pool:
            self.assertAlmostEqual(
                odds[card.id],
                pack_card_rating_weight(card.overall) / total_weight * 100,
            )
        self.assertGreater(
            odds[cards["KaruWRLD (S1 FUTURE STARS 88)"].id],
            odds[cards["Ebs (S1 FUTURE STARS 94)"].id],
        )

        for index, name in enumerate(FUTURE_STARS_CARDS):
            with self.subTest(card=name):
                user_id = f"future-stars-award-{index}"
                self.database.set_balance(
                    user_id,
                    S1_FUTURE_STARS_PACK_COST + 7,
                )
                chooser = _ChooseCardByName(name)
                awarded, balance = self.database.open_pack(
                    user_id,
                    "S1 FUTURE STARS",
                    S1_FUTURE_STARS_PACK_COST,
                    rng=chooser,
                    purchase_id=f"future-stars-purchase-{index}",
                )
                self.assertEqual(chooser.pool_names, set(FUTURE_STARS_CARDS))
                self.assertEqual(awarded.name, name)
                self.assertEqual(awarded.rarity, "S1 FUTURE STARS")
                self.assertEqual(balance, 7)
                self.assertEqual(
                    self.database.get_balance(user_id),
                    7,
                )

        poor_user = "future-stars-insufficient-funds"
        self.database.set_balance(poor_user, S1_FUTURE_STARS_PACK_COST - 1)
        with self.assertRaises(InsufficientFunds):
            self.database.open_pack(
                poor_user,
                "S1 FUTURE STARS",
                S1_FUTURE_STARS_PACK_COST,
                purchase_id="future-stars-too-expensive",
            )
        self.assertEqual(
            self.database.get_balance(poor_user),
            S1_FUTURE_STARS_PACK_COST - 1,
        )
        self.assertEqual(self.database.get_inventory(poor_user), [])

    async def test_pack_commands_show_and_open_future_stars_with_artwork(self) -> None:
        user_id = "future-stars-pack-command"
        self.database.set_balance(user_id, S1_FUTURE_STARS_PACK_COST)
        command = PackCommands(self.database)
        menu_response = FakeResponse()
        await command.packs.callback(
            command,
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=menu_response,
            ),
        )
        menu = menu_response.messages[0]
        fields = {field.name: field.value for field in menu["embed"].fields}
        field_name = "S1 FUTURE STARS PACK · 30,000,000 coins"
        self.assertIn(field_name, fields)
        self.assertIn("Rarity: S1 FUTURE STARS.", fields[field_name])
        for card_name in FUTURE_STARS_CARDS:
            card = self.database.get_player(card_name)
            assert card is not None
            self.assertIn(card_name, fields[field_name])
        self.assertIn("Price: 30,000,000 coins.", fields[field_name])
        self.assert_pack_menu_odds_sum_to_100(menu["embed"])

        single_response = FakeResponse()
        await command.pack.callback(
            command,
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=single_response,
            ),
            discord.app_commands.Choice(
                name="S1 FUTURE STARS PACK",
                value="S1 FUTURE STARS",
            ),
        )
        single_menu = single_response.messages[0]
        self.assertEqual(single_menu["embed"].title, "S1 FUTURE STARS PACK")
        self.assertEqual(len(single_menu["view"].children), 1)
        button = single_menu["view"].children[0]
        self.assertEqual(button.rarity, "S1 FUTURE STARS")
        self.assertEqual(button.cost, S1_FUTURE_STARS_PACK_COST)
        purchase_response = FakeResponse()
        await button.callback(
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=purchase_response,
                id=30001,
                message=FakeMessage(),
            )
        )
        result = purchase_response.messages[0]
        self.assertIn("S1 FUTURE STARS PACK", result["content"])
        self.assertIn("rarity **S1 FUTURE STARS**", result["content"])
        self.assertEqual(self.database.get_balance(user_id), 0)
        owned = self.database.get_inventory(user_id)
        self.assertEqual(len(owned), 1)
        self.assertIn(owned[0].player.name, FUTURE_STARS_CARDS)
        self.assertEqual(
            {field.name: field.value for field in result["embed"].fields}["Card Type"],
            "S1 FUTURE STARS",
        )
        self.assertEqual(
            result["embed"].image.url,
            f"attachment://{Path(owned[0].player.image_path).name}",
        )
        result["file"].close()

        insufficient_user = "future-stars-pack-disabled"
        self.database.set_balance(
            insufficient_user,
            S1_FUTURE_STARS_PACK_COST - 1,
        )
        insufficient_response = FakeResponse()
        await PackCommands(self.database).packs.callback(
            PackCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=insufficient_user),
                response=insufficient_response,
            ),
        )
        future_button = next(
            item
            for item in insufficient_response.messages[0]["view"].children
            if item.rarity == "S1 FUTURE STARS"
        )
        self.assertTrue(future_button.disabled)

    async def test_new_cards_display_and_grant_with_exact_card_artwork(self) -> None:
        cards = self._cards()
        inventory_user = "future-stars-command-inventory"
        card_cog = PlayerCommands(self.database)
        for index, name in enumerate(NEW_CARDS):
            card = cards[name]
            self.database.add_inventory(inventory_user, card.id)
            with self.subTest(card=name):
                card_response = FakeResponse()
                await card_cog.card.callback(
                    card_cog,
                    SimpleNamespace(
                        id=31000 + index,
                        response=card_response,
                    ),
                    str(card.id),
                )
                card_message = card_response.messages[0]
                self.assertEqual(card_message["embed"].title, player_card_label(card))
                card_fields = {
                    field.name: field.value
                    for field in card_message["embed"].fields
                }
                self.assertEqual(card_fields["Card Type"], "S1 FUTURE STARS")
                self.assertEqual(card_fields["Team"], FUTURE_STARS_CARDS[name]["team"])
                self.assertEqual(
                    card_fields["Position"],
                    FUTURE_STARS_CARDS[name]["position"],
                )
                self.assertEqual(
                    card_fields["Country"],
                    FUTURE_STARS_CARDS[name]["country"],
                )
                self.assertEqual(
                    card_fields["Value"],
                    f"{FUTURE_STARS_CARDS[name]['value']:,} coins",
                )
                for stat, value in FUTURE_STARS_CARDS[name]["stats"].items():
                    self.assertIn(f"**{stat}** {value}", card_fields["Stats"])
                self.assertEqual(
                    card_message["embed"].image.url,
                    f"attachment://{Path(card.image_path).name}",
                )
                self.assertEqual(
                    card_message["file"].fp.read()[:8],
                    b"\x89PNG\r\n\x1a\n",
                )
                card_message["file"].close()

                card_choices = await PlayerCommands.card_player_autocomplete(
                    card_cog,
                    SimpleNamespace(),
                    name.split(" (", 1)[0].casefold(),
                )
                self.assertTrue(
                    any(candidate.value == str(card.id) for candidate in card_choices)
                )

                choices = await AdminCommands.grantcard_player_autocomplete(
                    AdminCommands(self.database),
                    SimpleNamespace(),
                    name.split(" (", 1)[0].casefold(),
                )
                choice = next(
                    candidate
                    for candidate in choices
                    if candidate.value == str(card.id)
                )
                self.assertEqual(
                    choice.name,
                    f"{card.overall} OVR {name.split(' (', 1)[0]} "
                    "(S1 FUTURE STARS)",
                )

                grant_response = FakeResponse()
                await AdminCommands.grantcard.callback(
                    AdminCommands(self.database),
                    SimpleNamespace(
                        id=31100 + index,
                        response=grant_response,
                    ),
                    SimpleNamespace(
                        id=f"future-stars-grant-recipient-{index}",
                        mention=f"<@future-stars-grant-recipient-{index}>",
                    ),
                    str(card.id),
                    1,
                )
                self.assertIn("Granted", grant_response.messages[0]["content"])
                self.assertEqual(
                    self.database.get_inventory(
                        f"future-stars-grant-recipient-{index}"
                    )[0].player.id,
                    card.id,
                )

        inventory_response = FakeResponse()
        inventory_cog = InventoryCommands(self.database)
        await inventory_cog.inventory.callback(
            inventory_cog,
            SimpleNamespace(
                id=31200,
                user=SimpleNamespace(id=inventory_user),
                response=inventory_response,
            ),
        )
        inventory_text = inventory_response.messages[0]["embed"].description
        for name in NEW_CARDS:
            self.assertIn(player_card_label(cards[name]), inventory_text)

    def test_new_cards_can_be_sold_traded_auctioned_and_placed_on_bench(self) -> None:
        cards = self._cards()
        for index, name in enumerate(NEW_CARDS):
            card = cards[name]
            with self.subTest(flow="selling", card=name):
                user_id = f"future-stars-seller-{index}"
                self.database.add_inventory(user_id, card.id, 3)
                proceeds, balance = self.database.sell_card(user_id, card.name, 1)
                expected_quick_sell = card.value * 80 // 100
                self.assertEqual(proceeds, expected_quick_sell)
                self.assertEqual(balance, expected_quick_sell)
                preview = self.database.get_duplicate_sale_preview(user_id)
                self.assertEqual(preview.items[0].unit_value, card.value)
                duplicate_result = self.database.sell_duplicate_cards(
                    user_id,
                    f"future-stars-duplicate-sale-{index}",
                    preview.items,
                )
                self.assertEqual(duplicate_result.total_earnings, card.value)
                self.assertEqual(
                    duplicate_result.new_balance,
                    expected_quick_sell + card.value,
                )
                self.assertEqual(
                    self.database.get_inventory(user_id)[0].quantity,
                    1,
                )

        seller_id = "future-stars-trade-auction-seller"
        buyer_id = "future-stars-trade-auction-buyer"
        for name in NEW_CARDS:
            self.database.add_inventory(seller_id, cards[name].id, 2)
        self.database.create_trade_request(
            "future-stars-new-card-trade",
            seller_id,
            buyer_id,
            "Seller",
            "Buyer",
            "future-stars-guild",
            "future-stars-channel",
            now=100,
        )
        self.database.accept_trade_request(
            "future-stars-new-card-trade",
            buyer_id,
            now=101,
        )
        for name in NEW_CARDS:
            self.database.add_trade_card(
                "future-stars-new-card-trade",
                seller_id,
                cards[name].id,
                now=102,
            )
        self.database.confirm_trade(
            "future-stars-new-card-trade",
            seller_id,
            now=103,
        )
        trade_result = self.database.confirm_trade(
            "future-stars-new-card-trade",
            buyer_id,
            now=104,
        )
        self.assertTrue(trade_result["completed"])

        for index, name in enumerate(NEW_CARDS):
            card = cards[name]
            buyer_inventory = {
                entry.player.id: entry.quantity
                for entry in self.database.get_inventory(buyer_id)
            }
            self.assertEqual(buyer_inventory[card.id], 1)
            auction = self.database.create_auction(
                seller_id,
                "Seller",
                card.id,
                card.value,
                AUCTION_DURATIONS_SECONDS[0],
                f"future-stars-auction-{index}",
                "future-stars-channel",
                now=200,
            )
            self.assertEqual(auction.player.id, card.id)
            self.assertEqual(auction.player.rarity, "S1 FUTURE STARS")
            self.assertEqual(auction.status, "ACTIVE")

        wing_user = "future-stars-wing-starters"
        self.database.set_formation(wing_user, "Offensive")
        for name, position in (
            ("Ebs (S1 FUTURE STARS 94)", "WING1"),
            ("Gamer (S1 FUTURE STARS 93)", "WING2"),
            ("HHH (S1 FUTURE STARS 89)", "WING1"),
            ("Guzzinga (S1 FUTURE STARS 92)", "WING2"),
            ("Lauren (S1 FUTURE STARS 93)", "GK"),
            ("Zizou (S1 FUTURE STARS 87)", "CM1"),
            ("Fadi (S1 FUTURE STARS 89)", "WING1"),
        ):
            card = cards[name]
            owner_id = f"{wing_user}-{name}"
            self.database.set_formation(
                owner_id,
                (
                    "Offensive"
                    if position in {"WING1", "WING2"}
                    else "Defensive"
                    if position == "CM1"
                    else "Balanced"
                ),
            )
            self.database.add_inventory(owner_id, card.id)
            self.database.set_lineup_position(owner_id, position, card.id)
            self.assertEqual(
                self.database.get_lineup(owner_id)[position].id,
                card.id,
            )

        center_user = "future-stars-center-starter"
        self.database.set_formation(center_user, "Defensive")
        center_card = cards["KaruWRLD (S1 FUTURE STARS 88)"]
        self.database.add_inventory(center_user, center_card.id)
        self.database.set_lineup_position(center_user, "CM1", center_card.id)
        self.assertEqual(
            self.database.get_lineup(center_user)["CM1"].id,
            center_card.id,
        )

        for index, name in enumerate(NEW_CARDS):
            bench_user = f"future-stars-bench-{index}"
            card = cards[name]
            position = "SUB1" if index % 2 == 0 else "SUB2"
            self.database.add_inventory(bench_user, card.id)
            self.database.set_lineup_position(bench_user, position, card.id)
            self.assertEqual(
                self.database.get_lineup(bench_user)[position].id,
                card.id,
            )

    @staticmethod
    def assert_pack_menu_odds_sum_to_100(embed) -> None:
        future_field = next(
            field.value
            for field in embed.fields
            if field.name == "S1 FUTURE STARS PACK · 30,000,000 coins"
        )
        odds_text = future_field.split("Card odds: ", 1)[1].split("\n", 1)[0]
        odds = [
            float(item.rsplit(" ", 1)[1].rstrip("%"))
            for item in odds_text.split(" · ")
        ]
        assert len(odds) == 10
        assert abs(sum(odds) - 100.0) < 0.02
