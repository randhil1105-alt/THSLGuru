from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from commands.admin import AdminCommands
from database import AUCTION_DURATIONS_SECONDS, Database, TOTW_PACK_COST
from main import load_player_seed


CARD_DATA = {
    "Jaycob (TOTW 91)": {
        "overall": 91,
        "position": "GK",
        "country": "United States 🇺🇸",
        "team": "New York Rangers",
        "rarity": "TOTW",
        "value": 8_000_000,
        "stats": {
            "SAV": 93,
            "DIV": 90,
            "PAS": 90,
            "CLE": 90,
            "SWE": 93,
            "ACT": 91,
        },
        "image_path": "cards/jaycob_totw_91.png",
        "image_size": (279, 378),
        "allowed_lineup_positions": ("GK",),
        "display_name": "Jaycob",
    },
    "Dark (TOTW 88)": {
        "overall": 88,
        "position": "C",
        "country": "Canada",
        "team": "New York Rangers",
        "rarity": "TOTW",
        "value": 5_400_000,
        "stats": {
            "SHO": 88,
            "DRI": 88,
            "SAV": 82,
            "PAS": 87,
            "DEF": 90,
            "ACT": 90,
        },
        "image_path": "cards/dark_totw_88.png",
        "image_size": (275, 357),
        "allowed_lineup_positions": None,
        "display_name": "Dark",
    },
    "XavierCapone9 (TOTW 93)": {
        "overall": 93,
        "position": "WI",
        "country": "England 🏴",
        "team": "Vegas Golden Knights",
        "rarity": "TOTW",
        "value": 12_000_000,
        "stats": {
            "SHO": 92,
            "DRI": 95,
            "SAV": 79,
            "PAS": 92,
            "DEF": 87,
            "ACT": 88,
        },
        "image_path": "cards/xaviercapone9_totw_93.png",
        "image_size": (276, 356),
        "allowed_lineup_positions": None,
        "display_name": "XavierCapone9",
    },
}


class _SelectCardByName:
    def __init__(self, player_name: str) -> None:
        self.player_name = player_name
        self.population_names: set[str] = set()

    def choices(self, population, *, weights, k):
        del weights
        self.population_names = {str(player["name"]) for player in population}
        selected = next(
            player
            for player in population
            if str(player["name"]) == self.player_name
        )
        return [selected][:k]


class TOTWCardAdditionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "totw-cards.sqlite3")
        self.seed = load_player_seed()
        self.database.initialize(self.seed)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _cards(self):
        cards = {}
        for name in CARD_DATA:
            card = self.database.get_player(name)
            self.assertIsNotNone(card, name)
            assert card is not None
            cards[name] = card
        return cards

    def test_seeded_cards_match_spec_and_keep_individual_cropped_artwork(self) -> None:
        cards = self._cards()
        image_paths: set[str] = set()
        for name, expected in CARD_DATA.items():
            with self.subTest(card=name):
                card = cards[name]
                self.assertEqual(card.name, name)
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
                    ),
                    (
                        expected["overall"],
                        expected["position"],
                        expected["country"],
                        expected["team"],
                        expected["rarity"],
                        expected["value"],
                        expected["stats"],
                        expected["allowed_lineup_positions"],
                    ),
                )
                self.assertEqual(card.image_path, expected["image_path"])
                image_paths.add(str(card.image_path))
                image = Path(str(card.image_path))
                self.assertTrue(image.is_file(), image)
                image_bytes = image.read_bytes()
                self.assertEqual(image_bytes[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(
                    struct.unpack(">II", image_bytes[16:24]),
                    expected["image_size"],
                )
                self.assertIn(card, self.database.get_players(rarity="TOTW"))

        self.assertEqual(len(image_paths), 3)

        # Reinitializing from the seed must preserve each edition exactly once.
        self.database.initialize(self.seed)
        all_players = self.database.get_players()
        for name in CARD_DATA:
            self.assertEqual(
                sum(player.name.casefold() == name.casefold() for player in all_players),
                1,
            )

    def test_each_new_card_is_eligible_for_totw_pack_purchase(self) -> None:
        expected_pool = {
            "Licha",
            "Hala",
            *CARD_DATA.keys(),
        }
        for index, name in enumerate(CARD_DATA):
            with self.subTest(card=name):
                user_id = f"totw-pack-user-{index}"
                self.database.set_balance(user_id, TOTW_PACK_COST)
                chooser = _SelectCardByName(name)
                card, balance = self.database.open_pack(
                    user_id,
                    "TOTW",
                    TOTW_PACK_COST,
                    rng=chooser,
                    purchase_id=f"totw-pack-{index}",
                )
                self.assertEqual(chooser.population_names, expected_pool)
                self.assertEqual(card.name, name)
                self.assertEqual(card.rarity, "TOTW")
                self.assertEqual(balance, 0)
                owned = self.database.get_inventory(user_id)
                self.assertEqual(
                    [(entry.player.id, entry.quantity) for entry in owned],
                    [(card.id, 1)],
                )

    def test_inventory_and_selling_flows_use_the_configured_payouts(self) -> None:
        cards = self._cards()
        for index, (name, card) in enumerate(cards.items()):
            with self.subTest(card=name):
                user_id = f"totw-seller-{index}"
                self.database.add_inventory(user_id, card.id, 3)

                proceeds, balance = self.database.sell_card(user_id, card.name, 1)
                quick_sell_value = card.value * 80 // 100
                self.assertEqual(proceeds, quick_sell_value)
                self.assertEqual(balance, quick_sell_value)

                preview = self.database.get_duplicate_sale_preview(user_id)
                self.assertEqual(preview.total_cards, 1)
                self.assertEqual(preview.total_earnings, card.value)
                self.assertEqual(preview.items[0].unit_value, card.value)
                result = self.database.sell_duplicate_cards(
                    user_id,
                    f"totw-duplicate-sale-{index}",
                    preview.items,
                )
                self.assertEqual(result.total_earnings, card.value)
                self.assertEqual(
                    result.new_balance,
                    quick_sell_value + card.value,
                )
                self.assertEqual(
                    [(entry.player.id, entry.quantity)
                     for entry in self.database.get_inventory(user_id)],
                    [(card.id, 1)],
                )

    def test_trade_transfer_and_auction_listing_accept_each_new_card(self) -> None:
        cards = self._cards()
        seller_id = "totw-transfer-seller"
        buyer_id = "totw-transfer-buyer"
        for card in cards.values():
            self.database.add_inventory(seller_id, card.id, 2)

        self.database.create_trade_request(
            "totw-card-transfer",
            seller_id,
            buyer_id,
            "Seller",
            "Buyer",
            "totw-guild",
            "totw-channel",
            now=100,
        )
        self.database.accept_trade_request("totw-card-transfer", buyer_id, now=101)
        for card in cards.values():
            self.database.add_trade_card(
                "totw-card-transfer",
                seller_id,
                card.id,
                now=102,
            )
        self.database.confirm_trade("totw-card-transfer", seller_id, now=103)
        result = self.database.confirm_trade(
            "totw-card-transfer",
            buyer_id,
            now=104,
        )
        self.assertTrue(result["completed"])

        for index, (name, card) in enumerate(cards.items()):
            with self.subTest(card=name):
                buyer_cards = {
                    entry.player.id: entry.quantity
                    for entry in self.database.get_inventory(buyer_id)
                }
                self.assertEqual(buyer_cards[card.id], 1)
                seller_cards = {
                    entry.player.id: entry.quantity
                    for entry in self.database.get_inventory(seller_id)
                }
                self.assertEqual(seller_cards[card.id], 1)

                auction = self.database.create_auction(
                    seller_id,
                    "Seller",
                    card.id,
                    card.value,
                    AUCTION_DURATIONS_SECONDS[0],
                    f"totw-auction-{index}",
                    "totw-channel",
                    now=200,
                )
                self.assertEqual(auction.player.id, card.id)
                self.assertEqual(auction.player.rarity, "TOTW")
                self.assertEqual(auction.status, "ACTIVE")
                self.assertNotIn(
                    card.id,
                    {
                        entry.player.id
                        for entry in self.database.get_inventory(seller_id)
                    },
                )

    def test_each_card_fits_its_starter_role_and_bench_slots(self) -> None:
        cards = self._cards()
        starter_user = "totw-starter-squad"
        starter_positions = {
            "Jaycob (TOTW 91)": "GK",
            "Dark (TOTW 88)": "CM1",
            "XavierCapone9 (TOTW 93)": "WING1",
        }
        for name, position in starter_positions.items():
            card = cards[name]
            self.database.add_inventory(starter_user, card.id)
            self.database.set_lineup_position(starter_user, position, str(card.id))
            self.assertEqual(
                self.database.get_lineup(starter_user)[position].id,
                card.id,
            )

        for index, (name, card) in enumerate(cards.items()):
            with self.subTest(bench_card=name):
                bench_user = f"totw-bench-{index}"
                self.database.add_inventory(bench_user, card.id)
                self.database.set_lineup_position(bench_user, "SUB1", str(card.id))
                self.assertEqual(
                    self.database.get_lineup(bench_user)["SUB1"].id,
                    card.id,
                )

    async def test_grantcard_autocomplete_returns_each_exact_totw_edition(self) -> None:
        cards = self._cards()
        cog = AdminCommands(self.database)
        for name, card in cards.items():
            with self.subTest(card=name):
                expected = CARD_DATA[name]
                choices = await AdminCommands.grantcard_player_autocomplete(
                    cog,
                    SimpleNamespace(),
                    str(expected["display_name"]).casefold(),
                )
                choice = next(
                    (item for item in choices if item.value == str(card.id)),
                    None,
                )
                self.assertIsNotNone(choice)
                assert choice is not None
                self.assertEqual(
                    choice.name,
                    f"{card.overall} OVR {expected['display_name']} (TOTW)",
                )
