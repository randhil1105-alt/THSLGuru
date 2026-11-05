from __future__ import annotations

import random
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from database import (
    CLAIM_REWARD,
    DEFAULT_FORMATION,
    GOLD_PACK_COST,
    LINEUP_POSITIONS,
    STARTING_LINEUP_POSITIONS,
    SUBSTITUTE_POSITIONS,
    TEAM_FORMATIONS,
    TOTW_PACK_COST,
    CardNotOwned,
    CooldownActive,
    Database,
    DuplicateLineupPlayer,
    InsufficientFunds,
    InvalidFormation,
    InvalidLineupPosition,
    InvalidAmount,
    NoEligibleCards,
)
from main import load_player_seed


TOTW_PLAYER = {
    "name": "Test Week Star",
    "overall": 90,
    "position": "W",
    "country": "Canada",
    "team": "Test Team",
    "rarity": "TOTW",
    "value": 2_000_000,
    "stats": {"SHO": 90, "DRI": 91},
    "image_path": None,
}

SILVER_PLAYER = {
    "name": "Test Silver",
    "overall": 70,
    "position": "D",
    "country": "USA",
    "team": "Test Team",
    "rarity": "SILVER",
    "value": 25_000,
    "stats": {},
    "image_path": None,
}


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.sqlite3")
        self.database.initialize(load_player_seed())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_database_creation_and_case_insensitive_player_lookup(self) -> None:
        dan = self.database.get_player("dAn")
        self.assertIsNotNone(dan)
        assert dan is not None
        self.assertEqual(dan.name, "Dan")
        self.assertEqual(dan.overall, 87)
        self.assertEqual(dan.position, "C")
        self.assertEqual(dan.country, "Italy")
        self.assertEqual(dan.team, "Colorado Avalanche")
        self.assertEqual(dan.rarity, "GOLD")
        self.assertEqual(dan.value, 1_100_000)
        self.assertEqual(
            dan.stats,
            {"SHO": 83, "DRI": 88, "SAV": 85, "PAS": 85, "DEF": 92, "ACT": 87},
        )
        self.assertEqual(dan.image_path, "cards/dan.png")

    def test_xavier_seed_card_has_exact_data_and_is_gold_pack_eligible(self) -> None:
        xavier = self.database.get_player("XavierCapone9")
        self.assertIsNotNone(xavier)
        assert xavier is not None
        self.assertEqual(xavier.name, "XavierCapone9")
        self.assertEqual(xavier.overall, 91)
        self.assertEqual(xavier.position, "WI")
        self.assertEqual(xavier.country, "England")
        self.assertEqual(xavier.team, "Vegas Golden Knights")
        self.assertEqual(xavier.rarity, "GOLD")
        self.assertEqual(xavier.value, 3_140_000)
        self.assertEqual(
            xavier.stats,
            {"SHO": 92, "DRI": 95, "SAV": 75, "PAS": 88, "DEF": 86, "ACT": 82},
        )
        self.assertEqual(xavier.image_path, "cards/xaviercapone9.png")
        self.assertEqual(
            {player.name for player in self.database.get_players(rarity="GOLD")},
            {"Dan", "XavierCapone9", "Gamer", "Hala (Gold 90)"},
        )

    def test_hala_gold_seed_preserves_the_original_totw_card(self) -> None:
        hala_gold = self.database.get_player("Hala (Gold 90)")
        self.assertIsNotNone(hala_gold)
        assert hala_gold is not None
        self.assertEqual(hala_gold.overall, 90)
        self.assertEqual(hala_gold.position, "WI")
        self.assertEqual(hala_gold.country, "Scotland")
        self.assertEqual(hala_gold.team, "Colorado Avalanche")
        self.assertEqual(hala_gold.rarity, "GOLD")
        self.assertEqual(hala_gold.value, 3_000_000)
        self.assertEqual(
            hala_gold.stats,
            {"SHO": 88, "DRI": 93, "SAV": 85, "PAS": 90, "DEF": 86, "ACT": 87},
        )
        self.assertEqual(hala_gold.image_path, "cards/hala.png")

        existing_hala = self.database.get_player("Hala")
        self.assertIsNotNone(existing_hala)
        assert existing_hala is not None
        self.assertEqual(
            (existing_hala.overall, existing_hala.rarity, existing_hala.value),
            (93, "TOTW", 2_000_000),
        )
        self.assertEqual(existing_hala.image_path, "cards/hala.webp")

    def test_gamer_seed_card_has_exact_data_and_gold_rarity(self) -> None:
        gamer = self.database.get_player("Gamer")
        self.assertIsNotNone(gamer)
        assert gamer is not None
        self.assertEqual(gamer.name, "Gamer")
        self.assertEqual(gamer.overall, 89)
        self.assertEqual(gamer.position, "WI")
        self.assertEqual(gamer.country, "USA")
        self.assertEqual(gamer.team, "Dallas Stars")
        self.assertEqual(gamer.rarity, "GOLD")
        self.assertEqual(gamer.value, 2_000_000)
        self.assertEqual(
            gamer.stats,
            {"SHO": 93, "DRI": 88, "SAV": 78, "PAS": 86, "DEF": 80, "ACT": 76},
        )
        self.assertEqual(gamer.image_path, "cards/gamer.png")

    def test_read_connection_closes_after_the_context(self) -> None:
        with self.database._read_connection() as connection:
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_balance_updates_are_persistent_and_non_negative(self) -> None:
        self.database.set_balance("test-user", 40_000)
        self.database.add_coins("test-user", 5_000)
        reloaded = Database(self.database.path)
        reloaded.initialize()
        self.assertEqual(reloaded.get_balance("test-user"), 45_000)
        with self.assertRaises(InvalidAmount):
            self.database.set_balance("test-user", -1)

    def test_inventory_and_claim_cooldown_persist_after_reopen(self) -> None:
        claim_time = 3_000_000
        self.database.add_inventory("persistent-user", "Dan", 2)
        self.database.claim_daily("persistent-user", now=claim_time)

        reloaded = Database(self.database.path)
        reloaded.initialize(load_player_seed())
        inventory = reloaded.get_inventory("persistent-user")
        self.assertEqual([(entry.player.name, entry.quantity) for entry in inventory], [("Dan", 2)])
        with self.assertRaises(CooldownActive):
            reloaded.claim_daily("persistent-user", now=claim_time + 60)

    def test_claim_awards_once_and_enforces_24_hour_cooldown(self) -> None:
        balance, next_claim = self.database.claim_daily("test-user", now=1_000_000)
        self.assertEqual(balance, CLAIM_REWARD)
        self.assertEqual(next_claim, 1_000_000 + 24 * 60 * 60)
        with self.assertRaises(CooldownActive) as raised:
            self.database.claim_daily("test-user", now=1_000_001)
        self.assertEqual(raised.exception.remaining_seconds, 24 * 60 * 60 - 1)
        balance, _ = self.database.claim_daily(
            "test-user",
            now=1_000_000 + 24 * 60 * 60,
        )
        self.assertEqual(balance, CLAIM_REWARD * 2)

    def test_concurrent_claims_cannot_duplicate_a_reward(self) -> None:
        start = Barrier(3)

        def attempt_claim() -> str:
            start.wait()
            try:
                self.database.claim_daily("race-user", now=2_000_000)
                return "claimed"
            except CooldownActive:
                return "cooldown"

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(attempt_claim) for _ in range(2)]
            start.wait()
            outcomes = [job.result(timeout=20) for job in jobs]
        self.assertCountEqual(outcomes, ["claimed", "cooldown"])
        self.assertEqual(self.database.get_balance("race-user"), CLAIM_REWARD)

    def test_inventory_additions_accumulate_card_copies(self) -> None:
        self.assertEqual(self.database.add_inventory("test-user", "Dan", 2), 2)
        self.assertEqual(self.database.add_inventory("test-user", "Dan", 1), 3)
        inventory = self.database.get_inventory("test-user")
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0].player.name, "Dan")
        self.assertEqual(inventory[0].quantity, 3)

    def test_gold_pack_only_awards_gold_and_charges_exactly_once(self) -> None:
        self.database.add_player(TOTW_PLAYER)
        self.database.add_player(SILVER_PLAYER)
        self.database.set_balance("gold-user", GOLD_PACK_COST + 123)
        player, balance = self.database.open_pack(
            "gold-user",
            "GOLD",
            GOLD_PACK_COST,
            rng=random.Random(2),
        )
        self.assertEqual(player.rarity, "GOLD")
        self.assertEqual(balance, 123)
        inventory = self.database.get_inventory("gold-user")
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0].player.name, player.name)
        self.assertEqual(inventory[0].player.rarity, "GOLD")

    def test_totw_pack_only_awards_totw_and_charges_exactly_once(self) -> None:
        self.database.add_player(TOTW_PLAYER)
        self.database.add_player(SILVER_PLAYER)
        self.database.set_balance("totw-user", TOTW_PACK_COST + 7)
        player, balance = self.database.open_pack(
            "totw-user",
            "TOTW",
            TOTW_PACK_COST,
            rng=random.Random(3),
        )
        self.assertEqual(player.rarity, "TOTW")
        self.assertEqual(balance, 7)
        inventory = self.database.get_inventory("totw-user")
        self.assertEqual(inventory[0].player.rarity, "TOTW")
        self.assertEqual(inventory[0].quantity, 1)

    def test_replayed_pack_purchase_id_does_not_charge_or_award_twice(self) -> None:
        self.database.set_balance("replay-user", GOLD_PACK_COST + 100)
        first_player, first_balance = self.database.open_pack(
            "replay-user",
            "GOLD",
            GOLD_PACK_COST,
            rng=random.Random(4),
            purchase_id="discord-interaction-123",
        )
        replayed_player, replayed_balance = self.database.open_pack(
            "replay-user",
            "GOLD",
            GOLD_PACK_COST,
            rng=random.Random(5),
            purchase_id="discord-interaction-123",
        )

        self.assertEqual(replayed_player.name, first_player.name)
        self.assertEqual(replayed_balance, first_balance)
        self.assertEqual(self.database.get_balance("replay-user"), 100)
        inventory = self.database.get_inventory("replay-user")
        self.assertEqual(
            [(entry.player.name, entry.quantity) for entry in inventory],
            [(first_player.name, 1)],
        )

    def test_lineup_requires_owned_cards_and_rejects_duplicate_slots(self) -> None:
        user_id = "lineup-user"
        self.assertEqual(STARTING_LINEUP_POSITIONS, ("LW", "RW", "GK"))
        self.assertEqual(SUBSTITUTE_POSITIONS, ("SUB1", "SUB2"))
        self.assertEqual(
            LINEUP_POSITIONS,
            ("LW", "RW", "GK", "SUB1", "SUB2"),
        )
        self.database.add_player(TOTW_PLAYER)
        self.database.add_player(SILVER_PLAYER)
        with self.assertRaises(CardNotOwned):
            self.database.set_lineup_position(user_id, "LW", "Dan")

        for player_name in (
            "Dan",
            "Hala",
            "Licha",
            "Test Week Star",
            "Test Silver",
        ):
            self.database.add_inventory(user_id, player_name)

        for position, player_name in (
            ("LW", "Dan"),
            ("RW", "Hala"),
            ("GK", "Licha"),
            ("SUB1", "Test Week Star"),
            ("SUB2", "Test Silver"),
        ):
            self.database.set_lineup_position(user_id, position, player_name)

        with self.assertRaises(DuplicateLineupPlayer):
            self.database.set_lineup_position(user_id, "SUB1", "Dan")
        with self.assertRaises(DuplicateLineupPlayer):
            self.database.set_lineup_position(user_id, "RW", "Test Week Star")
        with self.assertRaises(InvalidLineupPosition):
            self.database.set_lineup_position(user_id, "C", "Hala")

        lineup = self.database.get_lineup(user_id)
        self.assertEqual(set(lineup), set(LINEUP_POSITIONS))
        self.assertEqual(lineup["LW"].name, "Dan")
        self.assertEqual(lineup["RW"].name, "Hala")
        self.assertEqual(lineup["GK"].name, "Licha")
        self.assertEqual(lineup["SUB1"].name, "Test Week Star")
        self.assertEqual(lineup["SUB2"].name, "Test Silver")
        self.assertTrue(self.database.clear_lineup_position(user_id, "LW"))
        self.assertFalse(self.database.clear_lineup_position(user_id, "LW"))
        self.assertTrue(self.database.clear_lineup_position(user_id, "SUB1"))

    def test_formation_options_and_lineup_persist_after_database_reopen(self) -> None:
        user_id = "persistent-team-user"
        self.assertEqual(TEAM_FORMATIONS, ("Balanced", "Attack", "Defensive"))
        self.assertEqual(self.database.get_formation(user_id), DEFAULT_FORMATION)
        for name in ("Dan", "Hala", "Licha"):
            self.database.add_inventory(user_id, name)
        self.database.add_player(TOTW_PLAYER)
        self.database.add_player(SILVER_PLAYER)
        self.database.add_inventory(user_id, "Test Week Star")
        self.database.add_inventory(user_id, "Test Silver")
        self.database.set_lineup_position(user_id, "LW", "Dan")
        self.database.set_lineup_position(user_id, "RW", "Hala")
        self.database.set_lineup_position(user_id, "GK", "Licha")
        self.database.set_lineup_position(user_id, "SUB1", "Test Week Star")
        self.database.set_lineup_position(user_id, "SUB2", "Test Silver")
        self.assertEqual(self.database.set_formation(user_id, "Attack"), "Attack")
        with self.assertRaises(InvalidFormation):
            self.database.set_formation(user_id, "1-2-2")

        reloaded = Database(self.database.path)
        reloaded.initialize(load_player_seed())
        self.assertEqual(reloaded.get_formation(user_id), "Attack")
        lineup = reloaded.get_lineup(user_id)
        self.assertEqual(
            {position: player.name for position, player in lineup.items()},
            {
                "LW": "Dan",
                "RW": "Hala",
                "GK": "Licha",
                "SUB1": "Test Week Star",
                "SUB2": "Test Silver",
            },
        )

    def test_insufficient_funds_do_not_charge_or_award(self) -> None:
        self.database.set_balance("poor-user", GOLD_PACK_COST - 1)
        with self.assertRaises(InsufficientFunds):
            self.database.open_pack("poor-user", "GOLD", GOLD_PACK_COST)
        self.assertEqual(self.database.get_balance("poor-user"), GOLD_PACK_COST - 1)
        self.assertEqual(self.database.get_inventory("poor-user"), [])

    def test_missing_totw_cards_do_not_charge(self) -> None:
        empty_catalog = Database(Path(self.temp_dir.name) / "empty-catalog.sqlite3")
        empty_catalog.initialize()
        empty_catalog.set_balance("totw-user", TOTW_PACK_COST + 100)
        with self.assertRaises(NoEligibleCards):
            empty_catalog.open_pack("totw-user", "TOTW", TOTW_PACK_COST)
        self.assertEqual(
            empty_catalog.get_balance("totw-user"),
            TOTW_PACK_COST + 100,
        )
        self.assertEqual(empty_catalog.get_inventory("totw-user"), [])

    def test_sale_checks_ownership_and_credits_listed_value(self) -> None:
        self.database.add_inventory("seller", "Dan", 2)
        proceeds, balance = self.database.sell_card("seller", "Dan", 1)
        self.assertEqual(proceeds, 1_100_000)
        self.assertEqual(balance, 1_100_000)
        self.assertEqual(self.database.get_inventory("seller")[0].quantity, 1)


if __name__ == "__main__":
    unittest.main()
