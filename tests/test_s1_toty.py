from __future__ import annotations

import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

from commands.common import player_card_label, resolve_card_image
from commands.information import InformationCommands
from commands.inventory import InventoryCommands
from commands.lineup_image import OUTPUT_FILENAME, _player_image_uri, build_lineup_svg
from commands.packs import PACKS, PackCommands
from commands.player import PlayerCommands
from commands.team import TeamCommands
from database import (
    S1_TOTY_PACK_COST,
    Database,
    InvalidLineupPosition,
    calculate_pack_odds,
    card_position_role,
)
from main import load_player_seed
from tests.test_commands import FakeFollowup, FakeMessage, FakeResponse


S1_TOTY_CARDS = {
    "Ebs (S1 TOTY 98)": {
        "overall": 98,
        "position": "WI",
        "country": "Ireland 🇮🇪",
        "value": 35_000_000,
        "stats": {"SHO": 97, "DRI": 98, "SAV": 80, "PAS": 95, "DEF": 93, "ACT": 88},
        "image_path": "cards/ebs_s1_toty_98.png",
        "allowed": ("WING1", "WING2"),
        "role": "WING",
    },
    "Hala (S1 TOTY 99)": {
        "overall": 99,
        "position": "WI",
        "country": "Scotland 🏴",
        "value": 50_000_000,
        "stats": {"SHO": 99, "DRI": 99, "SAV": 83, "PAS": 97, "DEF": 87, "ACT": 86},
        "image_path": "cards/hala_s1_toty_99.png",
        "allowed": ("WING1", "WING2"),
        "role": "WING",
    },
    "Cam (S1 TOTY 97)": {
        "overall": 97,
        "position": "W",
        "country": "United States 🇺🇸",
        "value": 30_000_000,
        "stats": {"SHO": 99, "DRI": 98, "SAV": 87, "PAS": 93, "DEF": 87, "ACT": 85},
        "image_path": "cards/cam_s1_toty_97.png",
        "allowed": ("WING1", "WING2"),
        "role": "WING",
    },
    "Road (S1 TOTY 98)": {
        "overall": 98,
        "position": "GK",
        "country": "Malta 🇲🇹",
        "value": 40_000_000,
        "stats": {"SAV": 97, "DIV": 96, "PAS": 93, "CLE": 95, "SWE": 99, "ACT": 99},
        "image_path": "cards/road_s1_toty_98.png",
        "allowed": ("GK",),
        "role": "GK",
    },
    "Matrix (S1 TOTY 97)": {
        "overall": 97,
        "position": "C",
        "country": "Germany 🇩🇪",
        "value": 35_000_000,
        "stats": {"SHO": 99, "DRI": 96, "SAV": 84, "PAS": 95, "DEF": 96, "ACT": 95},
        "image_path": "cards/matrix_s1_toty_97.png",
        "allowed": ("CM1", "CM2"),
        "role": "CM",
    },
    "Lauren (S1 TOTY 97)": {
        "overall": 97,
        "position": "GK",
        "country": "England 🏴",
        "value": 38_000_000,
        "stats": {"SAV": 99, "DIV": 98, "PAS": 94, "SWE": 95, "CLE": 96, "ACT": 95},
        "image_path": "cards/lauren_s1_toty_97.png",
        "allowed": ("GK",),
        "role": "GK",
    },
}

S1_TOTY_SOURCE_ARTWORK = {
    "Ebs (S1 TOTY 98)": "attached_assets/Firefly_(21)_1794057314441.png",
    "Hala (S1 TOTY 99)": "attached_assets/Firefly_(20)_1794057314470.png",
    "Cam (S1 TOTY 97)": "attached_assets/Firefly_(17)_1794057314483.png",
    "Road (S1 TOTY 98)": "attached_assets/Firefly_(11)_1794062387703.png",
    "Matrix (S1 TOTY 97)": "attached_assets/Firefly_(16)_1794062387649.png",
    "Lauren (S1 TOTY 97)": "attached_assets/Firefly_(3)_1794062387721.png",
}


class S1TOTYTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "s1-toty.sqlite3")
        self.seed = load_player_seed()
        self.database.initialize(self.seed)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_catalog_data_artwork_positions_and_idempotent_seed(self) -> None:
        cards = self.database.get_players(rarity="S1 TOTY")
        self.assertEqual(
            {card.name for card in cards},
            set(S1_TOTY_CARDS),
        )
        self.assertEqual(len(cards), 6)

        for name, expected in S1_TOTY_CARDS.items():
            with self.subTest(card=name):
                card = self.database.get_player(name)
                self.assertIsNotNone(card)
                assert card is not None
                self.assertEqual(
                    (
                        card.overall,
                        card.position,
                        card.country,
                        card.value,
                        card.stats,
                        card.rarity,
                        card.image_path,
                        card.allowed_lineup_positions,
                    ),
                    (
                        expected["overall"],
                        expected["position"],
                        expected["country"],
                        expected["value"],
                        expected["stats"],
                        "S1 TOTY",
                        expected["image_path"],
                        expected["allowed"],
                    ),
                )
                self.assertEqual(
                    card_position_role(card.position),
                    expected["role"],
                )
                image = resolve_card_image(card)
                self.assertIsNotNone(image)
                assert image is not None
                self.assertEqual(image.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(
                    image.read_bytes(),
                    Path(S1_TOTY_SOURCE_ARTWORK[name]).read_bytes(),
                )

        user_id = "s1-toty-seed-rerun-user"
        self.database.set_balance(user_id, 123)
        self.database.add_inventory(user_id, "Cam (S1 TOTY 97)")
        self.database.initialize(self.seed)
        self.assertEqual(
            sum(card.name in S1_TOTY_CARDS for card in self.database.get_players()),
            6,
        )
        self.assertEqual(self.database.get_balance(user_id), 123)
        self.assertEqual(
            [(entry.player.name, entry.quantity) for entry in self.database.get_inventory(user_id)],
            [("Cam (S1 TOTY 97)", 1)],
        )

    def test_s1_toty_pack_uses_rating_weights_and_preserves_purchase_idempotency(self) -> None:
        cards = self.database.get_players(rarity="S1 TOTY")
        odds = calculate_pack_odds(cards)
        self.assertEqual(set(odds), {card.id for card in cards})
        for probability in odds.values():
            self.assertAlmostEqual(probability, 100 / 6)
        self.assertAlmostEqual(sum(odds.values()), 100)

        class CaptureChoice:
            candidates = []
            weights = []

            def choices(self, population, weights, k):
                self.candidates = list(population)
                self.weights = list(weights)
                self.assert_k = k
                return [population[-1]]

        user_id = "s1-toty-pack-single-user"
        self.database.set_balance(user_id, S1_TOTY_PACK_COST + 17)
        rng = CaptureChoice()
        card, balance = self.database.open_pack(
            user_id,
            "S1 TOTY",
            S1_TOTY_PACK_COST,
            rng=rng,
            purchase_id="s1-toty-single-receipt",
        )
        replayed, replayed_balance = self.database.open_pack(
            user_id,
            "S1 TOTY",
            S1_TOTY_PACK_COST,
            rng=random.Random(19),
            purchase_id="s1-toty-single-receipt",
        )
        self.assertEqual(card.rarity, "S1 TOTY")
        self.assertEqual(card.name, rng.candidates[-1]["name"])
        self.assertEqual(
            rng.weights,
            [0.25] * 6,
        )
        self.assertEqual(rng.assert_k, 1)
        self.assertEqual(replayed.id, card.id)
        self.assertEqual(balance, 17)
        self.assertEqual(replayed_balance, 17)
        self.assertEqual(self.database.get_balance(user_id), 17)
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 1)

        batch_user = "s1-toty-pack-batch-user"
        self.database.set_balance(batch_user, 2 * S1_TOTY_PACK_COST + 9)
        batch, remaining = self.database.open_packs(
            batch_user,
            "S1 TOTY",
            S1_TOTY_PACK_COST,
            2,
            rng=random.Random(22),
            purchase_id="s1-toty-batch",
        )
        self.assertEqual(len(batch), 2)
        self.assertTrue(all(card.name in S1_TOTY_CARDS for card in batch))
        self.assertTrue(all(card.rarity == "S1 TOTY" for card in batch))
        self.assertEqual(remaining, 9)

    async def test_cards_work_in_card_inventory_set_team_and_sell(self) -> None:
        inventory_user = "s1-toty-command-inventory"
        team_cog = TeamCommands(self.database)

        for index, (name, expected) in enumerate(S1_TOTY_CARDS.items()):
            with self.subTest(card=name):
                card = self.database.get_player(name)
                self.assertIsNotNone(card)
                assert card is not None

                card_response = FakeResponse()
                await PlayerCommands(self.database).card.callback(
                    PlayerCommands(self.database),
                    SimpleNamespace(response=card_response),
                    name,
                )
                card_message = card_response.messages[0]
                self.assertEqual(card_message["embed"].fields[0].value, card.position)
                fields = {
                    field.name: field.value for field in card_message["embed"].fields
                }
                self.assertEqual(fields["Card Type"], "S1 TOTY")
                self.assertEqual(fields["Value"], f"{expected['value']:,} coins")
                self.assertEqual(
                    card_message["embed"].image.url,
                    f"attachment://{Path(expected['image_path']).name}",
                )
                card_message["file"].close()

                self.database.add_inventory(inventory_user, name)
                team_user = f"s1-toty-command-team-{index}"
                self.database.add_inventory(team_user, name)
                set_response = FakeResponse()
                await team_cog.set_position.callback(
                    team_cog,
                    SimpleNamespace(
                        id=2100 + index,
                        user=SimpleNamespace(id=team_user),
                        response=set_response,
                    ),
                    discord.app_commands.Choice(
                        name=expected["allowed"][0],
                        value=expected["allowed"][0],
                    ),
                    name,
                )
                self.assertIn("Assigned", set_response.messages[0]["content"])
                lineup_position = expected["allowed"][0]
                invalid_position = "CM1" if expected["role"] == "GK" else "GK"
                with self.assertRaises(InvalidLineupPosition):
                    self.database.set_lineup_position(
                        team_user,
                        invalid_position,
                        name,
                    )

                lineup = self.database.get_lineup(team_user)
                image_uri = _player_image_uri(lineup[lineup_position])
                self.assertIsNotNone(image_uri)
                self.assertIn(
                    image_uri,
                    build_lineup_svg(lineup, "Balanced", "1/3 starters filled"),
                )
                team_response, team_followup = FakeResponse(), FakeFollowup()
                await team_cog.team.callback(
                    team_cog,
                    SimpleNamespace(
                        id=2120 + index,
                        user=SimpleNamespace(id=team_user, display_name=name),
                        response=team_response,
                        followup=team_followup,
                    ),
                )
                team_message = team_followup.messages[0]
                self.assertEqual(
                    team_message["embed"].image.url,
                    f"attachment://{OUTPUT_FILENAME}",
                )
                team_message["file"].close()

                seller = f"s1-toty-command-seller-{index}"
                self.database.add_inventory(seller, name)
                sell_response = FakeResponse()
                await InventoryCommands.sell.callback(
                    InventoryCommands(self.database),
                    SimpleNamespace(
                        id=2140 + index,
                        user=SimpleNamespace(id=seller),
                        response=sell_response,
                    ),
                    name,
                    1,
                )
                expected_quick_sell = int(expected["value"]) * 80 // 100
                self.assertIn(
                    f"{expected_quick_sell:,} coins",
                    sell_response.messages[0]["content"],
                )
                self.assertEqual(
                    self.database.get_balance(seller),
                    expected_quick_sell,
                )

        inventory_response = FakeResponse()
        await InventoryCommands.inventory.callback(
            InventoryCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=inventory_user),
                response=inventory_response,
            ),
        )
        inventory_text = inventory_response.messages[0]["embed"].description
        for name in S1_TOTY_CARDS:
            card = self.database.get_player(name)
            self.assertIsNotNone(card)
            assert card is not None
            self.assertIn(player_card_label(card), inventory_text)

    async def test_named_pack_menu_purchase_and_reveal(self) -> None:
        expected_packs = (
            ("GOLD", 5_000_000),
            ("SILVER", 2_000_000),
            ("TOTW", 20_000_000),
            ("S2 TOTS", 80_000_000),
            ("S1 TOTS", 100_000_000),
            ("S1 TOTY", 120_000_000),
            ("S1 FUTURE STARS", 30_000_000),
        )
        self.assertEqual(PACKS, expected_packs)
        self.assertEqual(len({rarity for rarity, _ in PACKS}), len(PACKS))

        user_id = "s1-toty-command-pack"
        self.database.set_balance(user_id, S1_TOTY_PACK_COST)
        packs_response = FakeResponse()
        await PackCommands(self.database).packs.callback(
            PackCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=packs_response,
            ),
        )
        menu_fields = {
            field.name: field.value
            for field in packs_response.messages[0]["embed"].fields
        }
        field_name = "S1 TOTY PACK · 120,000,000 coins"
        self.assertIn(field_name, menu_fields)
        self.assertIn("Rarity: S1 TOTY.", menu_fields[field_name])
        for name in S1_TOTY_CARDS:
            self.assertIn(name, menu_fields[field_name])
        self.assertTrue(
            all(
                f"{name} 16.67%" in menu_fields[field_name]
                for name in S1_TOTY_CARDS
            )
        )

        pack_response = FakeResponse()
        pack_cog = PackCommands(self.database)
        await pack_cog.pack.callback(
            pack_cog,
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=pack_response,
            ),
            discord.app_commands.Choice(
                name="S1 TOTY PACK",
                value="S1 TOTY",
            ),
        )
        pack_menu = pack_response.messages[0]
        self.assertEqual(pack_menu["embed"].title, "S1 TOTY PACK")
        self.assertEqual(len(pack_menu["view"].children), 1)
        self.assertIn("S1 TOTY PACK", pack_menu["view"].children[0].label)

        purchase_response = FakeResponse()
        await pack_menu["view"].children[0].callback(
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=purchase_response,
                id=2155,
                message=FakeMessage(),
            )
        )
        result = purchase_response.messages[0]
        self.assertIn("opened a **S1 TOTY PACK**", result["content"])
        self.assertIn("rarity **S1 TOTY**", result["content"])
        owned = self.database.get_inventory(user_id)
        self.assertEqual(len(owned), 1)
        self.assertIn(owned[0].player.name, S1_TOTY_CARDS)
        self.assertEqual(self.database.get_balance(user_id), 0)
        fields = {field.name: field.value for field in result["embed"].fields}
        self.assertEqual(fields["Card Type"], "S1 TOTY")
        self.assertEqual(
            result["embed"].image.url,
            f"attachment://{Path(owned[0].player.image_path).name}",
        )
        result["file"].close()

    async def test_help_mentions_s1_toty(self) -> None:
        response = FakeResponse()
        await InformationCommands.help.callback(
            InformationCommands(),
            SimpleNamespace(response=response),
        )
        help_text = "\n".join(
            field.value for field in response.messages[0]["embed"].fields
        )
        self.assertIn("S1 TOTY", help_text)
