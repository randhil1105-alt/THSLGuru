from __future__ import annotations

import random
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from database import (
    CLAIM_COOLDOWN_SECONDS,
    DEFAULT_FORMATION,
    FORMATION_STARTERS,
    GOLD_PACK_COST,
    LINEUP_POSITIONS,
    S1_TOTS_PACK_COST,
    S1_TOTY_PACK_COST,
    S2_TOTS_PACK_COST,
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
    calculate_pack_odds,
    card_rating_weight,
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

    def test_global_reset_clears_progress_and_preserves_catalog_and_user_ids(self) -> None:
        first_user = "reset-player-one"
        second_user = "reset-player-two"
        self.database.set_balance(first_user, GOLD_PACK_COST + 500)
        self.database.set_balance(second_user, 2_000)
        self.database.add_inventory(first_user, "Dan")
        self.database.add_inventory(first_user, "Fadi")
        self.database.add_inventory(second_user, "Gamer")
        self.database.set_lineup_position(first_user, "CM1", "Dan")
        self.database.claim_gold_card(first_user, now=1_000)
        self.database.award_xp(first_user, "reset-xp", 11_700, "test")
        self.database.start_draft_session(first_user, "reset-draft", now=1_000)
        self.database.create_match_invitation(
            "reset-match",
            first_user,
            second_user,
            "Player One",
            "Player Two",
            "123",
            "456",
            now=1_000,
            expires_at=1_100,
        )

        fadi = self.database.get_player("Fadi")
        assert fadi is not None
        self.database.create_auction(
            first_user,
            "Player One",
            fadi.id,
            100,
            1_800,
            "reset-auction",
            "456",
            now=1_000,
        )
        self.database.place_auction_bid(
            "reset-auction",
            second_user,
            "Player Two",
            100,
            "reset-bid",
            now=1_001,
        )
        self.database.open_pack(
            first_user,
            "GOLD",
            GOLD_PACK_COST,
            purchase_id="reset-pack-purchase",
        )

        catalog_before = self.database.get_players()
        reset_count = self.database.reset_all_player_progress(
            "administrator",
            "123",
            now=5_000_000,
        )

        self.assertEqual(reset_count, 2)
        self.assertEqual(self.database.get_balance(first_user), 0)
        self.assertEqual(self.database.get_balance(second_user), 0)
        self.assertEqual(self.database.get_inventory(first_user), [])
        self.assertEqual(self.database.get_inventory(second_user), [])
        self.assertEqual(self.database.get_lineup(first_user), {})
        self.assertEqual(self.database.get_xp_profile(first_user).total_xp, 0)
        self.assertEqual(self.database.get_xp_profile(first_user).level, 1)
        self.assertEqual(
            self.database.get_free_pack_inventory(first_user),
            {"GOLD": 0, "TOTW": 0},
        )
        cooldowns = self.database.get_cooldown_status(first_user, now=5_000_000)
        self.assertIsNone(cooldowns.claim_last_used_at)
        self.assertIsNone(cooldowns.draft_last_started_at)
        self.assertFalse(cooldowns.draft_active)
        self.assertEqual(self.database.get_auction("reset-auction").status, "CANCELLED")
        self.assertEqual(self.database.get_match("reset-match")["status"], "CANCELLED")
        with self.database._read_connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM active_match_users").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM auction_bids").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM draft_sessions").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM pack_purchases").fetchone()[0],
                0,
            )
            audit = connection.execute(
                """
                SELECT administrator_id, guild_id, affected_players, created_at
                FROM admin_reset_audit
                """
            ).fetchone()
            self.assertEqual(
                tuple(audit),
                ("administrator", "123", 2, 5_000_000),
            )
            user_ids = {
                str(row["user_id"])
                for row in connection.execute("SELECT user_id FROM users")
            }
        self.assertEqual(user_ids, {first_user, second_user})
        self.assertEqual(self.database.get_players(), catalog_before)

    def test_global_reset_rolls_back_every_table_on_failure(self) -> None:
        user_id = "atomic-reset-player"
        self.database.set_balance(user_id, 321)
        self.database.add_inventory(user_id, "Dan")
        self.database.award_xp(user_id, "atomic-reset-xp", 500, "test")
        with self.database._transaction() as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_global_reset
                BEFORE DELETE ON inventory
                BEGIN
                    SELECT RAISE(ABORT, 'forced reset failure');
                END
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.database.reset_all_player_progress(
                "administrator",
                "123",
                now=9_000,
            )

        self.assertEqual(self.database.get_balance(user_id), 3_000_321)
        self.assertEqual(len(self.database.get_inventory(user_id)), 1)
        self.assertEqual(self.database.get_xp_profile(user_id).total_xp, 500)
        with self.database._read_connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM admin_reset_audit").fetchone()[0],
                0,
            )

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
            {
                "Dan",
                "XavierCapone9",
                "Gamer",
                "Hala (Gold 90)",
                "Dryboomrun",
                "Needy",
                "Fadi",
                "Mason",
                "Demox",
                "Cheddar",
                "Mr P",
                "Dark",
                "Road",
                "Ebs",
                "Cam",
                "Licha (Gold 90)",
                "Ryuko",
                "Rj",
                "Criss",
                "Megan",
                "Jaycob",
                "Karuwrld",
                "HHH",
                "Apex",
                "R4ze",
                "Chicken",
                "Sluckz",
                "Frozend",
            },
        )

    def test_s2_tots_cards_have_exact_data_correct_lineup_roles_and_stable_seed(self) -> None:
        expected = {
            "EBS (S2 TOTS 99)": (
                99,
                "W",
                "Ireland 🇮🇪",
                "Ottawa Senators",
                34_000_000,
                {"SHO": 99, "DRI": 90, "SAV": 80, "PAS": 92, "DEF": 80, "ACT": 93},
                "cards/ebs_s2_tots_99.png",
                "WING1",
                "GK",
            ),
            "Jaycob (S2 TOTS 96)": (
                96,
                "GK",
                "United States 🇺🇸",
                "New York Rangers",
                24_000_000,
                {"SAV": 99, "DIV": 98, "PAS": 89, "CLE": 87, "SWE": 92, "ACT": 98},
                "cards/jaycob_s2_tots_96.png",
                "GK",
                "WING1",
            ),
            "Licha (S2 TOTS 96)": (
                96,
                "GK",
                "England 🏴",
                "Ottawa Senators",
                25_000_000,
                {"SAV": 97, "DIV": 99, "PAS": 88, "CLE": 87, "SWE": 92, "ACT": 95},
                "cards/licha_s2_tots_96.png",
                "GK",
                "WING1",
            ),
        }
        initial_count = len(self.database.get_players())
        for index, (name, details) in enumerate(expected.items()):
            with self.subTest(card=name):
                player = self.database.get_player(name)
                self.assertIsNotNone(player)
                assert player is not None
                (
                    overall,
                    position,
                    country,
                    team,
                    value,
                    stats,
                    image_path,
                    allowed_slot,
                    forbidden_slot,
                ) = details
                self.assertEqual(
                    (
                        player.overall,
                        player.position,
                        player.country,
                        player.team,
                        player.rarity,
                        player.value,
                        player.stats,
                        player.image_path,
                    ),
                    (
                        overall,
                        position,
                        country,
                        team,
                        "S2 TOTS",
                        value,
                        stats,
                        image_path,
                    ),
                )
                self.assertTrue(Path(image_path).is_file())
                self.assertIn(player, self.database.get_players(rarity="S2 TOTS"))

                user_id = f"s2-lineup-user-{index}"
                self.database.add_inventory(user_id, name)
                self.database.set_lineup_position(user_id, allowed_slot, name)
                self.assertEqual(
                    self.database.get_lineup(user_id)[allowed_slot],
                    player,
                )
                with self.assertRaises(InvalidLineupPosition):
                    self.database.set_lineup_position(
                        user_id,
                        forbidden_slot,
                        name,
                    )

        self.assertEqual(self.database.get_player("Ebs").overall, 90)
        self.assertEqual(self.database.get_player("Jaycob").overall, 89)
        self.assertEqual(self.database.get_player("Licha").overall, 93)
        self.database.initialize(load_player_seed())
        self.assertEqual(len(self.database.get_players()), initial_count)
        for name in expected:
            self.assertEqual(
                sum(player.name == name for player in self.database.get_players()),
                1,
            )

    def test_s2_tots_pack_can_award_each_new_card(self) -> None:
        class SelectNamedCard:
            def __init__(self, name: str) -> None:
                self.name = name

            def choices(self, population, *, weights, k):
                del weights, k
                return [next(row for row in population if row["name"] == self.name)]

        for index, name in enumerate(
            (
                "EBS (S2 TOTS 99)",
                "Jaycob (S2 TOTS 96)",
                "Licha (S2 TOTS 96)",
            )
        ):
            user_id = f"s2-pack-user-{index}"
            self.database.set_balance(user_id, S2_TOTS_PACK_COST)
            awarded, remaining_balance = self.database.open_pack(
                user_id,
                "S2 TOTS",
                S2_TOTS_PACK_COST,
                rng=SelectNamedCard(name),
                purchase_id=f"s2-new-card-pack-{index}",
            )
            self.assertEqual(awarded.name, name)
            self.assertEqual(awarded.rarity, "S2 TOTS")
            self.assertEqual(remaining_balance, 0)
            self.assertEqual(self.database.get_inventory(user_id)[0].player, awarded)

    def test_megan_jaycob_and_karuwrld_have_exact_gold_data_and_slot_rules(self) -> None:
        expected = {
            "Megan": (
                84,
                "GK",
                "Canada",
                "Colorado Avalanche",
                {"SAV": 85, "DIV": 85, "PAS": 82, "CLE": 82, "SWE": 83, "ACT": 85},
                "cards/megan_gold.png",
                ("GK",),
                560_000,
            ),
            "Jaycob": (
                89,
                "GK",
                "England",
                "New York Rangers",
                {"SAV": 90, "DIV": 90, "PAS": 85, "CLE": 85, "SWE": 87, "ACT": 89},
                "cards/jaycob_gold.png",
                ("GK",),
                1_300_000,
            ),
            "Karuwrld": (
                82,
                "WI",
                "Scotland",
                "Colorado Avalanche",
                {"SHO": 84, "DRI": 78, "SAV": 77, "PAS": 80, "DEF": 82, "ACT": 76},
                "cards/karuwrld_gold.png",
                ("WING1", "WING2"),
                460_000,
            ),
        }
        gold_cards = self.database.get_players(rarity="GOLD")
        for name, details in expected.items():
            with self.subTest(card=name):
                player = self.database.get_player(name)
                self.assertIsNotNone(player)
                assert player is not None
                (
                    overall,
                    position,
                    country,
                    team,
                    stats,
                    image_path,
                    allowed_slots,
                    value,
                ) = details
                self.assertEqual(
                    (
                        player.overall,
                        player.position,
                        player.country,
                        player.team,
                        player.rarity,
                        player.value,
                        player.stats,
                        player.image_path,
                        player.allowed_lineup_positions,
                    ),
                    (
                        overall,
                        position,
                        country,
                        team,
                        "GOLD",
                        value,
                        stats,
                        image_path,
                        allowed_slots,
                    ),
                )
                self.assertIn(player, gold_cards)

    def test_hhh_apex_and_r4ze_have_exact_gold_data_and_slot_rules(self) -> None:
        expected = {
            "HHH": (
                84,
                "WI",
                "England",
                "Anaheim Ducks",
                {"SHO": 89, "DRI": 85, "SAV": 77, "PAS": 84, "DEF": 78, "ACT": 82},
                "cards/hhh_gold.png",
                ("WING1", "WING2"),
                450_000,
            ),
            "Apex": (
                88,
                "WI",
                "England",
                "Buffalo Sabres",
                {"SHO": 91, "DRI": 88, "SAV": 76, "PAS": 86, "DEF": 80, "ACT": 82},
                "cards/apex_gold.png",
                ("WING1", "WING2"),
                1_500_000,
            ),
            "R4ze": (
                84,
                "GK",
                "Georgia",
                "Florida Panthers",
                {"SAV": 87, "DIV": 85, "PAS": 81, "CLE": 83, "SWE": 80, "ACT": 84},
                "cards/r4ze_gold.png",
                ("GK",),
                430_000,
            ),
        }
        gold_cards = self.database.get_players(rarity="GOLD")
        for name, details in expected.items():
            with self.subTest(card=name):
                card = self.database.get_player(name)
                self.assertIsNotNone(card)
                assert card is not None
                overall, position, country, team, stats, image_path, allowed, value = details
                self.assertEqual(
                    (
                        card.overall,
                        card.position,
                        card.country,
                        card.team,
                        card.rarity,
                        card.value,
                        card.stats,
                        card.image_path,
                        card.allowed_lineup_positions,
                    ),
                    (
                        overall,
                        position,
                        country,
                        team,
                        "GOLD",
                        value,
                        stats,
                        image_path,
                        allowed,
                    ),
                )
                self.assertIn(card, gold_cards)

    def test_new_gold_cards_keep_starter_restrictions_but_can_use_the_bench(self) -> None:
        for name, allowed_positions, forbidden in (
            ("Megan", ("GK",), ("WING1", "CM1")),
            ("Jaycob", ("GK",), ("WING1", "CM1")),
            ("Karuwrld", ("WING1", "WING2"), ("GK", "CM1")),
            ("HHH", ("WING1", "WING2"), ("GK", "CM1")),
            ("Apex", ("WING1", "WING2"), ("GK", "CM1")),
            ("R4ze", ("GK",), ("WING1", "CM1")),
            (
                "Chicken",
                ("CM1", "CM2"),
                ("WING1", "WING2", "GK"),
            ),
            (
                "Sluckz",
                ("WING1", "WING2"),
                ("CM1", "CM2", "GK"),
            ),
            (
                "Frozend",
                ("GK",),
                ("WING1", "WING2", "CM1", "CM2"),
            ),
        ):
            user_id = f"lineup-rule-invalid-{name.lower()}"
            self.database.add_inventory(user_id, name)
            for position in forbidden:
                with self.subTest(card=name, position=position):
                    with self.assertRaises(InvalidLineupPosition):
                        self.database.set_lineup_position(user_id, position, name)
            for index, allowed in enumerate(allowed_positions):
                allowed_user_id = f"lineup-rule-valid-{name.lower()}-{index}"
                self.database.add_inventory(allowed_user_id, name)
                formation = "Defensive" if allowed in {"CM1", "CM2"} else "Offensive"
                self.database.set_formation(allowed_user_id, formation)
                self.assertEqual(
                    self.database.set_lineup_position(
                        allowed_user_id,
                        allowed,
                        name,
                    ).name,
                    name,
                )
            for index, substitute in enumerate(SUBSTITUTE_POSITIONS):
                bench_user_id = f"lineup-bench-{name.lower()}-{index}"
                self.database.add_inventory(bench_user_id, name)
                assigned = self.database.set_lineup_position(
                    bench_user_id,
                    substitute,
                    name,
                )
                self.assertEqual(assigned.name, name)
                self.assertEqual(
                    self.database.get_lineup(bench_user_id)[substitute].name,
                    name,
                )

    def test_all_rarities_and_future_rarities_are_allowed_on_bench(self) -> None:
        for rarity in ("GOLD", "TOTW", "S1 TOTS", "S2 TOTS", "S1 TOTY"):
            card = next(
                player for player in self.database.get_players(rarity=rarity)
            )
            user_id = f"bench-rarity-{rarity.lower().replace(' ', '-')}"
            self.database.add_inventory(user_id, card.name)
            assigned = self.database.set_lineup_position(
                user_id,
                "SUB1",
                str(card.id),
            )
            self.assertEqual(assigned.rarity, rarity)
            self.assertEqual(self.database.get_lineup(user_id)["SUB1"].id, card.id)

        future = self.database.add_player(
            {
                "name": "Future Rarity Card",
                "overall": 88,
                "position": "WI",
                "country": "Italy",
                "team": "Future Team",
                "rarity": "FUTURE SPECIAL",
                "value": 1_000,
                "stats": {},
                "allowed_lineup_positions": ["WING1"],
            }
        )
        self.database.add_inventory("future-bench-user", future.name)
        self.database.set_lineup_position(
            "future-bench-user",
            "SUB1",
            str(future.id),
        )
        self.assertEqual(
            self.database.get_lineup("future-bench-user")["SUB1"].rarity,
            "FUTURE SPECIAL",
        )
        with self.assertRaises(InvalidLineupPosition):
            self.database.set_lineup_position(
                "future-bench-user",
                "WING2",
                str(future.id),
            )

    def test_chicken_sluckz_and_frozend_have_exact_seed_data(self) -> None:
        expected = {
            "Chicken": (
                84,
                "C",
                "Czech Republic 🇨🇿",
                "Florida Panthers",
                500_000,
                {"SHO": 85, "DRI": 83, "SAV": 86, "PAS": 85, "DEF": 84, "ACT": 86},
                "cards/chicken_gold_supplied.png",
                ("CM1", "CM2"),
            ),
            "Sluckz": (
                83,
                "WI",
                "Sweden 🇸🇪",
                "Anaheim Ducks",
                430_000,
                {"SHO": 84, "DRI": 85, "SAV": 78, "PAS": 84, "DEF": 79, "ACT": 82},
                "cards/sluckz_gold_supplied.png",
                ("WING1", "WING2"),
            ),
            "Frozend": (
                90,
                "GK",
                "United States 🇺🇸",
                "Colorado Avalanche",
                1_200_000,
                {"SAV": 91, "DIV": 91, "PAS": 86, "CLE": 88, "SWE": 90, "ACT": 91},
                "cards/frozend_gold_supplied.png",
                ("GK",),
            ),
        }
        gold_cards = self.database.get_players(rarity="GOLD")
        for name, details in expected.items():
            with self.subTest(card=name):
                card = self.database.get_player(name)
                self.assertIsNotNone(card)
                assert card is not None
                self.assertEqual(
                    (
                        card.overall,
                        card.position,
                        card.country,
                        card.team,
                        card.rarity,
                        card.value,
                        card.stats,
                        card.image_path,
                        card.allowed_lineup_positions,
                    ),
                    (*details[:4], "GOLD", *details[4:]),
                )
                self.assertIn(card, gold_cards)
                self.assertTrue(Path(card.image_path).is_file())

    def test_mr_p_dark_and_road_seed_cards_have_exact_gold_data(self) -> None:
        expected = {
            "Mr P": (
                86,
                "WI",
                "USA",
                "New York Rangers",
                {"SHO": 89, "DRI": 84, "SAV": 81, "PAS": 85, "DEF": 87, "ACT": 86},
                "cards/mr_p_gold.png",
                650_000,
            ),
            "Dark": (
                84,
                "WI",
                "Canada",
                "New York Rangers",
                {"SHO": 86, "DRI": 84, "SAV": 78, "PAS": 85, "DEF": 80, "ACT": 83},
                "cards/dark_gold.png",
                400_000,
            ),
            "Road": (
                86,
                "GK",
                "Malta",
                "Anaheim Ducks",
                {"SAV": 89, "DIV": 87, "PAS": 83, "CLE": 83, "SWE": 88, "ACT": 85},
                "cards/road_gold.png",
                650_000,
            ),
        }
        gold_cards = self.database.get_players(rarity="GOLD")
        for name, details in expected.items():
            with self.subTest(card=name):
                card = self.database.get_player(name)
                self.assertIsNotNone(card)
                assert card is not None
                overall, position, country, team, stats, image_path, value = details
                self.assertEqual(
                    (
                        card.overall,
                        card.position,
                        card.country,
                        card.team,
                        card.rarity,
                        card.value,
                        card.stats,
                        card.image_path,
                    ),
                    (
                        overall,
                        position,
                        country,
                        team,
                        "GOLD",
                        value,
                        stats,
                        image_path,
                    ),
                )
                self.assertIn(card, gold_cards)

    def test_ebs_cam_and_gold_licha_seed_cards_have_exact_data(self) -> None:
        expected = {
            "Ebs": (
                90,
                "WI",
                "Ireland",
                "Ottawa Senators",
                {"SHO": 93, "DRI": 90, "SAV": 74, "PAS": 88, "DEF": 85, "ACT": 83},
                "cards/ebs_gold.png",
            ),
            "Cam": (
                90,
                "WI",
                "USA",
                "Ottawa Senators",
                {"SHO": 93, "DRI": 90, "SAV": 76, "PAS": 88, "DEF": 83, "ACT": 78},
                "cards/cam_gold.png",
            ),
            "Licha (Gold 90)": (
                90,
                "GK",
                "England",
                "Ottawa Senators",
                {"SAV": 92, "DIV": 90, "PAS": 89, "CLE": 87, "SWE": 86, "ACT": 92},
                "cards/licha_gold_90.png",
            ),
        }
        gold_cards = self.database.get_players(rarity="GOLD")
        for name, details in expected.items():
            with self.subTest(card=name):
                card = self.database.get_player(name)
                self.assertIsNotNone(card)
                assert card is not None
                overall, position, country, team, stats, image_path = details
                self.assertEqual(
                    (
                        card.overall,
                        card.position,
                        card.country,
                        card.team,
                        card.rarity,
                        card.value,
                        card.stats,
                        card.image_path,
                    ),
                    (
                        overall,
                        position,
                        country,
                        team,
                        "GOLD",
                        2_600_000,
                        stats,
                        image_path,
                    ),
                )
                self.assertIn(card, gold_cards)

        original_licha = self.database.get_player("Licha")
        self.assertIsNotNone(original_licha)
        assert original_licha is not None
        self.assertEqual(
            (original_licha.overall, original_licha.rarity, original_licha.value, original_licha.image_path),
            (93, "TOTW", 12_000_000, "cards/licha.webp"),
        )

    def test_ryuko_rj_and_criss_seed_cards_have_exact_data(self) -> None:
        expected = {
            "Ryuko": (
                89,
                "WI",
                "USA",
                "Colorado Avalanche",
                {"SHO": 91, "DRI": 89, "SAV": 78, "PAS": 87, "DEF": 85, "ACT": 80},
                "cards/ryuko_gold.png",
            ),
            "Rj": (
                85,
                "WI",
                "England",
                "Vegas Golden Knights",
                {"SHO": 87, "DRI": 85, "SAV": 79, "PAS": 85, "DEF": 83, "ACT": 82},
                "cards/rj_gold.png",
            ),
            "Criss": (
                86,
                "C",
                "Italy",
                "Anaheim Ducks",
                {"SHO": 88, "DRI": 87, "SAV": 83, "PAS": 86, "DEF": 85, "ACT": 84},
                "cards/criss_gold.png",
            ),
        }
        gold_cards = self.database.get_players(rarity="GOLD")
        for name, details in expected.items():
            with self.subTest(card=name):
                card = self.database.get_player(name)
                self.assertIsNotNone(card)
                assert card is not None
                overall, position, country, team, stats, image_path = details
                self.assertEqual(
                    (
                        card.overall,
                        card.position,
                        card.country,
                        card.team,
                        card.rarity,
                        card.value,
                        card.stats,
                        card.image_path,
                    ),
                    (
                        overall,
                        position,
                        country,
                        team,
                        "GOLD",
                        2_600_000,
                        stats,
                        image_path,
                    ),
                )
                self.assertIn(card, gold_cards)

    def test_new_seed_cards_are_added_without_overwriting_existing_user_data(self) -> None:
        legacy_database = Database(Path(self.temp_dir.name) / "legacy.sqlite3")
        old_catalog = [
            record
            for record in load_player_seed()
            if record["name"]
            not in {
                "Mr P",
                "Dark",
                "Road",
                "Ebs",
                "Cam",
                "Licha (Gold 90)",
                "Ryuko",
                "Rj",
                "Criss",
                "Megan",
                "Jaycob",
                "Karuwrld",
            }
        ]
        legacy_database.initialize(old_catalog)
        original_players = {
            player.name: player.id for player in legacy_database.get_players()
        }
        legacy_database.set_balance("preserved-user", 123_456)
        legacy_database.add_inventory("preserved-user", "Dan", 2)

        legacy_database.initialize(load_player_seed())
        legacy_database.initialize(load_player_seed())

        self.assertEqual(len(legacy_database.get_players()), len(original_players) + 12)
        self.assertTrue(
            all(
                legacy_database.get_player(name).id == player_id
                for name, player_id in original_players.items()
            )
        )
        self.assertEqual(legacy_database.get_balance("preserved-user"), 123_456)
        inventory = legacy_database.get_inventory("preserved-user")
        self.assertEqual([(entry.player.name, entry.quantity) for entry in inventory], [("Dan", 2)])
        for name in (
            "Mr P",
            "Dark",
            "Road",
            "Ebs",
            "Cam",
            "Licha (Gold 90)",
            "Ryuko",
            "Rj",
            "Criss",
            "Megan",
            "Jaycob",
            "Karuwrld",
        ):
            self.assertEqual(
                len(
                    [
                        player
                        for player in legacy_database.get_players()
                        if player.name == name
                    ]
                ),
                1,
            )

    def test_each_new_gold_card_can_be_selected_from_a_gold_pack(self) -> None:
        class SelectNamedCard:
            def __init__(self, name: str) -> None:
                self.name = name

            def choices(self, population: list[sqlite3.Row], weights: list[float], k: int):
                del weights, k
                return [next(card for card in population if card["name"] == self.name)]

        for index, name in enumerate(
            (
                "Mr P",
                "Dark",
                "Road",
                "Ebs",
                "Cam",
                "Licha (Gold 90)",
                "Ryuko",
                "Rj",
                "Criss",
                "Megan",
                "Jaycob",
                "Karuwrld",
                "Chicken",
                "Sluckz",
                "Frozend",
            )
        ):
            with self.subTest(card=name):
                user_id = f"gold-pack-card-{index}"
                self.database.set_balance(user_id, GOLD_PACK_COST)
                awarded, balance = self.database.open_pack(
                    user_id,
                    "GOLD",
                    GOLD_PACK_COST,
                    rng=SelectNamedCard(name),
                )
                self.assertEqual(awarded.name, name)
                self.assertEqual(balance, 0)
                self.assertEqual(
                    [(entry.player.name, entry.quantity) for entry in self.database.get_inventory(user_id)],
                    [(name, 1)],
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
            (93, "TOTW", 12_000_000),
        )
        self.assertEqual(existing_hala.image_path, "cards/hala.webp")

    def test_fadi_seed_card_has_exact_data_and_gold_pack_eligibility(self) -> None:
        card = self.database.get_player("Fadi")
        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.overall, 86)
        self.assertEqual(card.position, "C")
        self.assertEqual(card.country, "Algeria")
        self.assertEqual(card.team, "New York Rangers")
        self.assertEqual(card.rarity, "GOLD")
        self.assertEqual(card.value, 780_000)
        self.assertEqual(
            card.stats,
            {"SHO": 88, "DRI": 86, "SAV": 72, "PAS": 85, "DEF": 85, "ACT": 82},
        )
        self.assertEqual(card.image_path, "cards/fadi.png")
        self.assertIn(card, self.database.get_players(rarity="GOLD"))

    def test_mason_demox_and_cheddar_seed_cards_have_exact_gold_data(self) -> None:
        expected = {
            "Mason": (
                84, "WI", "England", "Dallas Stars",
                {"SHO": 85, "DRI": 85, "SAV": 79, "PAS": 84, "DEF": 83, "ACT": 83},
                "cards/mason.png",
            ),
            "Demox": (
                84, "WI", "Turkey", "Ottawa Senators",
                {"SHO": 87, "DRI": 86, "SAV": 84, "PAS": 86, "DEF": 85, "ACT": 85},
                "cards/demox.png",
            ),
            "Cheddar": (
                84, "WI", "Scotland", "Vegas Golden Knights",
                {"SHO": 87, "DRI": 80, "SAV": 78, "PAS": 83, "DEF": 85, "ACT": 80},
                "cards/cheddar.png",
            ),
        }
        gold_cards = self.database.get_players(rarity="GOLD")
        for name, details in expected.items():
            with self.subTest(card=name):
                card = self.database.get_player(name)
                self.assertIsNotNone(card)
                assert card is not None
                overall, position, country, team, stats, image_path = details
                self.assertEqual(
                    (
                        card.overall,
                        card.position,
                        card.country,
                        card.team,
                        card.rarity,
                        card.value,
                        card.stats,
                        card.image_path,
                    ),
                    (
                        overall,
                        position,
                        country,
                        team,
                        "GOLD",
                        400_000,
                        stats,
                        image_path,
                    ),
                )
                self.assertIn(card, gold_cards)

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

    def test_weighted_pack_odds_favor_lower_ratings_and_recalculate(self) -> None:
        gold_cards = self.database.get_players(rarity="GOLD")
        odds_before = calculate_pack_odds(gold_cards)
        self.assertTrue(all(odds > 0 for odds in odds_before.values()))
        self.assertAlmostEqual(sum(odds_before.values()), 100.0)
        by_name = {card.name: odds_before[card.id] for card in gold_cards}
        self.assertGreater(by_name["Fadi"], by_name["Hala (Gold 90)"])
        self.assertEqual(by_name["Dan"], by_name["Dryboomrun"])
        self.assertAlmostEqual(by_name["Needy"], 2 / 70 * 100)
        self.assertAlmostEqual(by_name["Megan"], 4 / 70 * 100)
        self.assertAlmostEqual(by_name["Hala (Gold 90)"], 1 / 70 * 100)
        self.assertAlmostEqual(by_name["Chicken"], 4 / 70 * 100)
        self.assertAlmostEqual(by_name["Sluckz"], 4 / 70 * 100)
        self.assertAlmostEqual(by_name["Frozend"], 1 / 70 * 100)

        totw_cards = self.database.get_players(rarity="TOTW")
        totw_odds = calculate_pack_odds(totw_cards)
        self.assertEqual(len(totw_cards), 2)
        self.assertAlmostEqual(sum(totw_odds.values()), 100.0)
        self.assertEqual(set(totw_odds.values()), {50.0})
        s2_tots_cards = self.database.get_players(rarity="S2 TOTS")
        s2_tots_odds = calculate_pack_odds(s2_tots_cards)
        self.assertEqual(
            {card.name for card in s2_tots_cards},
            {
                "Cryptic",
                "Hala (S2 TOTS 98)",
                "Xavier",
                "EBS (S2 TOTS 99)",
                "Jaycob (S2 TOTS 96)",
                "Licha (S2 TOTS 96)",
            },
        )
        self.assertAlmostEqual(sum(s2_tots_odds.values()), 100.0)

        self.database.add_player(
            {
                "name": "Test Very Rare",
                "overall": 96,
                "position": "C",
                "country": "Canada",
                "team": "Test Team",
                "rarity": "GOLD",
                "value": 1,
                "stats": {},
                "image_path": None,
            }
        )
        updated_cards = self.database.get_players(rarity="GOLD")
        odds_after = calculate_pack_odds(updated_cards)
        self.assertGreater(odds_after[self.database.get_player("Test Very Rare").id], 0)
        self.assertLess(
            odds_after[self.database.get_player("Needy").id],
            odds_before[self.database.get_player("Needy").id],
        )
        self.assertGreater(card_rating_weight(84), card_rating_weight(85))
        self.assertGreater(card_rating_weight(85), card_rating_weight(90))
        self.assertGreater(card_rating_weight(90), card_rating_weight(95))
        self.assertGreater(card_rating_weight(95), 0)

    def test_dryboomrun_seed_card_has_exact_data_and_gold_pack_eligibility(self) -> None:
        card = self.database.get_player("Dryboomrun")
        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.overall, 87)
        self.assertEqual(card.position, "C")
        self.assertEqual(card.country, "Guatemala")
        self.assertEqual(card.team, "Dallas Stars")
        self.assertEqual(card.rarity, "GOLD")
        self.assertEqual(card.value, 1_100_000)
        self.assertEqual(
            card.stats,
            {"SHO": 86, "DRI": 85, "SAV": 78, "PAS": 88, "DEF": 89, "ACT": 84},
        )
        self.assertEqual(card.image_path, "cards/dryboomrun.png")
        self.assertIn(card, self.database.get_players(rarity="GOLD"))

    def test_needy_seed_card_has_exact_data_and_gold_pack_eligibility(self) -> None:
        card = self.database.get_player("Needy")
        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.overall, 85)
        self.assertEqual(card.position, "C")
        self.assertEqual(card.country, "Mexico")
        self.assertEqual(card.team, "Vegas Golden Knights")
        self.assertEqual(card.rarity, "GOLD")
        self.assertEqual(card.value, 700_000)
        self.assertEqual(
            card.stats,
            {"SHO": 85, "DRI": 84, "SAV": 85, "PAS": 84, "DEF": 86, "ACT": 86},
        )
        self.assertEqual(card.image_path, "cards/needy.png")
        self.assertIn(card, self.database.get_players(rarity="GOLD"))

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

    def test_inventory_and_card_claim_cooldown_persist_after_reopen(self) -> None:
        claim_time = 3_000_000
        self.database.add_inventory("persistent-user", "Dan", 2)
        claimed_card, _ = self.database.claim_gold_card(
            "persistent-user",
            now=claim_time,
            rng=random.Random(1),
        )

        reloaded = Database(self.database.path)
        reloaded.initialize(load_player_seed())
        inventory = reloaded.get_inventory("persistent-user")
        inventory_by_name = {entry.player.name: entry.quantity for entry in inventory}
        self.assertEqual(sum(inventory_by_name.values()), 3)
        self.assertEqual(
            inventory_by_name.get("Dan", 0),
            2 + (1 if claimed_card.name == "Dan" else 0),
        )
        self.assertEqual(reloaded.get_balance("persistent-user"), 0)
        with self.assertRaises(CooldownActive):
            reloaded.claim_gold_card(
                "persistent-user",
                now=claim_time + 60,
                rng=random.Random(2),
            )

    def test_claim_awards_weighted_gold_without_coins_and_enforces_cooldown(self) -> None:
        self.assertEqual(CLAIM_COOLDOWN_SECONDS, 30 * 60)
        initial_balance = 123_456
        self.database.set_balance("test-user", initial_balance)
        player, next_claim = self.database.claim_gold_card(
            "test-user",
            now=1_000_000,
            rng=random.Random(3),
        )
        self.assertEqual(player.rarity, "GOLD")
        self.assertEqual(next_claim, 1_000_000 + CLAIM_COOLDOWN_SECONDS)
        self.assertEqual(self.database.get_balance("test-user"), initial_balance)
        self.assertEqual(
            [(entry.player.name, entry.quantity) for entry in self.database.get_inventory("test-user")],
            [(player.name, 1)],
        )
        with self.assertRaises(CooldownActive) as raised:
            self.database.claim_gold_card(
                "test-user",
                now=1_000_001,
                rng=random.Random(4),
            )
        self.assertEqual(
            raised.exception.remaining_seconds,
            CLAIM_COOLDOWN_SECONDS - 1,
        )
        next_player, _ = self.database.claim_gold_card(
            "test-user",
            now=1_000_000 + CLAIM_COOLDOWN_SECONDS,
            rng=random.Random(5),
        )
        self.assertEqual(next_player.rarity, "GOLD")
        self.assertEqual(self.database.get_balance("test-user"), initial_balance)
        self.assertEqual(
            sum(entry.quantity for entry in self.database.get_inventory("test-user")),
            2,
        )

    def test_claim_uses_gold_rating_weights_and_never_selects_totw(self) -> None:
        class WeightedChooser:
            def __init__(self) -> None:
                self.population = []
                self.weights = []

            def choices(self, population, *, weights, k):
                self.population = list(population)
                self.weights = list(weights)
                return [population[weights.index(max(weights))]]

        chooser = WeightedChooser()
        player, _ = self.database.claim_gold_card(
            "weighted-claim-user",
            now=4_000_000,
            rng=chooser,
        )
        gold_cards = self.database.get_players(rarity="GOLD")
        self.assertTrue(
            all(str(card["rarity"]) == "GOLD" for card in chooser.population)
        )
        self.assertEqual(
            chooser.weights,
            [card_rating_weight(int(card["overall"])) for card in chooser.population],
        )
        self.assertEqual(player.overall, 84)
        self.assertGreater(card_rating_weight(player.overall), card_rating_weight(85))
        self.assertEqual(self.database.get_balance("weighted-claim-user"), 0)
        self.assertIn(player, [entry.player for entry in self.database.get_inventory("weighted-claim-user")])
        self.assertEqual(
            {int(card["id"]) for card in chooser.population},
            {card.id for card in gold_cards},
        )

    def test_claim_with_no_gold_cards_does_not_award_or_set_cooldown(self) -> None:
        self.database.set_balance("empty-gold-claim-user", 64_321)
        with self.database._transaction() as connection:
            connection.execute("DELETE FROM players WHERE rarity = 'GOLD'")

        with self.assertRaises(NoEligibleCards):
            self.database.claim_gold_card("empty-gold-claim-user", now=5_000_000)

        self.assertEqual(self.database.get_balance("empty-gold-claim-user"), 64_321)
        self.assertEqual(self.database.get_inventory("empty-gold-claim-user"), [])
        self.database.add_player(
            {
                "name": "Temporary Gold",
                "overall": 84,
                "position": "WI",
                "country": "Canada",
                "team": "Test Team",
                "rarity": "GOLD",
                "value": 1,
                "stats": {},
                "image_path": None,
            }
        )
        claimed_card, _ = self.database.claim_gold_card(
            "empty-gold-claim-user",
            now=5_000_001,
            rng=random.Random(7),
        )
        self.assertEqual(claimed_card.name, "Temporary Gold")
        self.assertEqual(self.database.get_balance("empty-gold-claim-user"), 64_321)

    def test_concurrent_claims_cannot_duplicate_a_reward(self) -> None:
        start = Barrier(3)

        def attempt_claim() -> str:
            start.wait()
            try:
                self.database.claim_gold_card(
                    "race-user",
                    now=2_000_000,
                    rng=random.Random(6),
                )
                return "claimed"
            except CooldownActive:
                return "cooldown"

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(attempt_claim) for _ in range(2)]
            start.wait()
            outcomes = [job.result(timeout=20) for job in jobs]
        self.assertCountEqual(outcomes, ["claimed", "cooldown"])
        self.assertEqual(self.database.get_balance("race-user"), 0)
        self.assertEqual(
            sum(entry.quantity for entry in self.database.get_inventory("race-user")),
            1,
        )

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

    def test_multi_pack_purchase_awards_every_pack_and_is_idempotent(self) -> None:
        class SelectCards:
            def __init__(self, names: list[str]) -> None:
                self.names = iter(names)

            def choices(self, population, *, weights, k):
                del weights, k
                name = next(self.names)
                return [next(card for card in population if card["name"] == name)]

        class NoSelection:
            def choices(self, population, *, weights, k):
                raise AssertionError("An idempotent replay must not select new cards.")

        user_id = "multi-pack-user"
        purchase_id = "multi-pack-interaction"
        self.database.set_balance(user_id, 3 * GOLD_PACK_COST + 234)
        cards, balance = self.database.open_packs(
            user_id,
            "GOLD",
            GOLD_PACK_COST,
            3,
            rng=SelectCards(["Chicken", "Sluckz", "Chicken"]),
            purchase_id=purchase_id,
        )
        self.assertEqual([card.name for card in cards], ["Chicken", "Sluckz", "Chicken"])
        self.assertEqual(balance, 234)
        self.assertEqual(self.database.get_balance(user_id), 234)
        self.assertEqual(
            {entry.player.name: entry.quantity for entry in self.database.get_inventory(user_id)},
            {"Chicken": 2, "Sluckz": 1},
        )

        replayed_cards, replayed_balance = self.database.open_packs(
            user_id,
            "GOLD",
            GOLD_PACK_COST,
            3,
            rng=NoSelection(),
            purchase_id=purchase_id,
        )
        self.assertEqual([card.name for card in replayed_cards], ["Chicken", "Sluckz", "Chicken"])
        self.assertEqual(replayed_balance, 234)
        self.assertEqual(self.database.get_balance(user_id), 234)
        self.assertEqual(
            {entry.player.name: entry.quantity for entry in self.database.get_inventory(user_id)},
            {"Chicken": 2, "Sluckz": 1},
        )

    def test_multi_pack_purchase_with_insufficient_funds_changes_nothing(self) -> None:
        user_id = "multi-pack-insufficient-user"
        self.database.set_balance(user_id, 2 * GOLD_PACK_COST)
        with self.assertRaises(InsufficientFunds):
            self.database.open_packs(
                user_id,
                "GOLD",
                GOLD_PACK_COST,
                3,
                purchase_id="insufficient-multi-pack-interaction",
            )
        self.assertEqual(self.database.get_balance(user_id), 2 * GOLD_PACK_COST)
        self.assertEqual(self.database.get_inventory(user_id), [])

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

    def test_s2_tots_pack_awards_only_s2_tots_cards_and_is_idempotent(self) -> None:
        user_id = "s2-tots-user"
        self.database.set_balance(user_id, S2_TOTS_PACK_COST + 17)
        first, balance = self.database.open_pack(
            user_id,
            "S2 TOTS",
            S2_TOTS_PACK_COST,
            rng=random.Random(8),
            purchase_id="s2-tots-single",
        )
        replayed, replayed_balance = self.database.open_pack(
            user_id,
            "S2 TOTS",
            S2_TOTS_PACK_COST,
            rng=random.Random(9),
            purchase_id="s2-tots-single",
        )
        self.assertEqual(first.rarity, "S2 TOTS")
        self.assertIn(first, self.database.get_players(rarity="S2 TOTS"))
        self.assertEqual(replayed.id, first.id)
        self.assertEqual(balance, 17)
        self.assertEqual(replayed_balance, 17)
        self.assertEqual(self.database.get_balance(user_id), 17)
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 1)

        batch_user = "s2-tots-batch-user"
        self.database.set_balance(batch_user, 2 * S2_TOTS_PACK_COST + 23)
        cards, batch_balance = self.database.open_packs(
            batch_user,
            "S2 TOTS",
            S2_TOTS_PACK_COST,
            2,
            rng=random.Random(10),
            purchase_id="s2-tots-batch",
        )
        self.assertEqual(len(cards), 2)
        self.assertTrue(all(card.rarity == "S2 TOTS" for card in cards))
        self.assertEqual(batch_balance, 23)
        self.assertEqual(self.database.get_balance(batch_user), 23)

    def test_s1_tots_pack_awards_s1_tots_cards_and_is_idempotent(self) -> None:
        user_id = "s1-tots-user"
        self.database.set_balance(user_id, S1_TOTS_PACK_COST + 17)
        first, balance = self.database.open_pack(
            user_id,
            "S1 TOTS",
            S1_TOTS_PACK_COST,
            rng=random.Random(18),
            purchase_id="s1-tots-single",
        )
        replayed, replayed_balance = self.database.open_pack(
            user_id,
            "S1 TOTS",
            S1_TOTS_PACK_COST,
            rng=random.Random(19),
            purchase_id="s1-tots-single",
        )
        self.assertEqual(first.rarity, "S1 TOTS")
        self.assertIn(first, self.database.get_players(rarity="S1 TOTS"))
        self.assertEqual(replayed.id, first.id)
        self.assertEqual(balance, 17)
        self.assertEqual(replayed_balance, 17)
        self.assertEqual(self.database.get_balance(user_id), 17)
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 1)

        batch_user = "s1-tots-batch-user"
        self.database.set_balance(batch_user, 2 * S1_TOTS_PACK_COST + 23)
        cards, batch_balance = self.database.open_packs(
            batch_user,
            "S1 TOTS",
            S1_TOTS_PACK_COST,
            2,
            rng=random.Random(20),
            purchase_id="s1-tots-batch",
        )
        self.assertEqual(len(cards), 2)
        self.assertTrue(all(card.rarity == "S1 TOTS" for card in cards))
        self.assertEqual(batch_balance, 23)
        self.assertEqual(self.database.get_balance(batch_user), 23)

    def test_s1_tots_cards_are_available_to_their_lineup_roles(self) -> None:
        expected = {
            "Dan (S1 TOTS 96)": "CM1",
            "Hala (S1 TOTS 99)": "WING1",
            "Ebs (S1 TOTS 98)": "WING1",
        }
        for index, (name, allowed_position) in enumerate(expected.items()):
            user_id = f"s1-lineup-user-{index}"
            self.database.add_inventory(user_id, name)
            self.database.set_lineup_position(user_id, allowed_position, name)
            self.assertEqual(
                self.database.get_lineup(user_id)[allowed_position].name,
                name,
            )

        self.database.add_inventory("s1-invalid-wing-user", "Dan (S1 TOTS 96)")
        with self.assertRaises(InvalidLineupPosition):
            self.database.set_lineup_position(
                "s1-invalid-wing-user",
                "WING1",
                "Dan (S1 TOTS 96)",
            )
        self.database.add_inventory("s1-invalid-center-user", "Hala (S1 TOTS 99)")
        with self.assertRaises(InvalidLineupPosition):
            self.database.set_lineup_position(
                "s1-invalid-center-user",
                "CM1",
                "Hala (S1 TOTS 99)",
            )

    def test_gamer_road_and_lauren_s1_tots_cards_have_exact_catalog_data(self) -> None:
        expected = {
            "Gamer (S1 TOTS 97)": (
                97,
                "C",
                "United States 🇺🇸",
                27_000_000,
                {
                    "SHO": 96,
                    "DRI": 98,
                    "SAV": 76,
                    "PAS": 95,
                    "DEF": 93,
                    "ACT": 85,
                },
                "cards/gamer_s1_tots_97.png",
                ("CM1", "CM2"),
            ),
            "Road (S1 TOTS 98)": (
                98,
                "GK",
                "France 🇫🇷",
                40_000_000,
                {
                    "SAV": 97,
                    "DIV": 96,
                    "PAS": 93,
                    "CLE": 95,
                    "SWE": 99,
                    "ACT": 99,
                },
                "cards/road_s1_tots_98.png",
                ("GK",),
            ),
            "Lauren (S1 TOTS 97)": (
                97,
                "GK",
                "England 🏴",
                35_000_000,
                {
                    "SAV": 99,
                    "DIV": 98,
                    "PAS": 94,
                    "SWE": 95,
                    "CLE": 96,
                    "ACT": 95,
                },
                "cards/lauren_s1_tots_97.png",
                ("GK",),
            ),
        }
        for name, details in expected.items():
            with self.subTest(card=name):
                player = self.database.get_player(name)
                self.assertIsNotNone(player)
                assert player is not None
                self.assertEqual(
                    (
                        player.overall,
                        player.position,
                        player.country,
                        player.value,
                        player.stats,
                        player.image_path,
                    player.rarity,
                        player.allowed_lineup_positions,
                    ),
                (*details[:6], "S1 TOTS", details[6]),
                )
                image_path = Path(__file__).resolve().parents[1] / details[5]
                self.assertTrue(image_path.is_file())
                self.assertEqual(
                    sum(card.name == name for card in self.database.get_players()),
                    1,
                )

    def test_pack_receipt_migration_preserves_existing_rows(self) -> None:
        user_id = "legacy-pack-user"
        card = self.database.get_player("Dan")
        self.database.set_balance(user_id, 100)
        connection = sqlite3.connect(self.database.path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DROP TABLE pack_purchases")
            connection.execute(
                """
                CREATE TABLE pack_purchases (
                    interaction_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    player_id INTEGER NOT NULL REFERENCES players(id),
                    rarity TEXT NOT NULL CHECK (rarity IN ('GOLD', 'TOTW')),
                    cost INTEGER NOT NULL CHECK (cost > 0),
                    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
                    created_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO pack_purchases
                    (interaction_id, user_id, player_id, rarity, cost,
                     balance_after, created_at)
                VALUES (?, ?, ?, 'GOLD', ?, ?, ?)
                """,
                ("legacy-pack-receipt", user_id, card.id, GOLD_PACK_COST, 100, 1),
            )
            connection.commit()
        finally:
            connection.close()

        self.database.ensure_pack_purchase_schema()
        connection = sqlite3.connect(self.database.path)
        try:
            preserved = connection.execute(
                "SELECT user_id, player_id, rarity FROM pack_purchases "
                "WHERE interaction_id = ?",
                ("legacy-pack-receipt",),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(preserved, (user_id, card.id, "GOLD"))
        connection = sqlite3.connect(self.database.path)
        try:
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'pack_purchases'"
            ).fetchone()[0]
            self.assertIn("'S1 TOTS'", table_sql)
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM pack_purchases"
            ).fetchone()[0]
            self.assertEqual(receipt_count, 1)
        finally:
            connection.close()

        self.database.set_balance(user_id, S2_TOTS_PACK_COST + 10)
        awarded, new_balance = self.database.open_pack(
            user_id,
            "S2 TOTS",
            S2_TOTS_PACK_COST,
            rng=random.Random(11),
            purchase_id="new-s2-tots-receipt",
        )
        self.assertEqual(awarded.rarity, "S2 TOTS")
        self.assertEqual(new_balance, 10)

        self.database.set_balance(user_id, S1_TOTS_PACK_COST + 3)
        s1_awarded, s1_balance = self.database.open_pack(
            user_id,
            "S1 TOTS",
            S1_TOTS_PACK_COST,
            rng=random.Random(21),
            purchase_id="new-s1-tots-receipt",
        )
        self.assertEqual(s1_awarded.rarity, "S1 TOTS")
        self.assertEqual(s1_balance, 3)

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

    def test_formation_slots_and_strict_owned_position_validation(self) -> None:
        user_id = "lineup-user"
        self.assertEqual(TEAM_FORMATIONS, ("Balanced", "Offensive", "Defensive"))
        self.assertEqual(FORMATION_STARTERS["Balanced"], ("WING1", "CM1", "GK"))
        self.assertEqual(FORMATION_STARTERS["Offensive"], ("WING1", "WING2", "GK"))
        self.assertEqual(FORMATION_STARTERS["Defensive"], ("CM1", "CM2", "GK"))
        self.assertEqual(SUBSTITUTE_POSITIONS, ("SUB1", "SUB2"))
        self.assertEqual(
            LINEUP_POSITIONS,
            ("WING1", "WING2", "CM1", "CM2", "GK", "SUB1", "SUB2"),
        )

        for player_name in ("XavierCapone9", "Dan", "Licha", "Gamer", "Fadi"):
            self.database.add_inventory(user_id, player_name)
        with self.assertRaises(CardNotOwned):
            self.database.set_lineup_position("unowned-user", "WING1", "XavierCapone9")

        for position, player_name in (
            ("WING1", "XavierCapone9"),
            ("CM1", "Dan"),
            ("GK", "Licha"),
        ):
            self.database.set_lineup_position(user_id, position, player_name)

        for position, player_name in (
            ("GK", "XavierCapone9"),
            ("CM1", "Licha"),
            ("WING1", "Dan"),
        ):
            with self.subTest(position=position, player=player_name):
                with self.assertRaises(InvalidLineupPosition):
                    self.database.set_lineup_position(user_id, position, player_name)
        with self.assertRaises(InvalidLineupPosition):
            self.database.set_lineup_position(user_id, "WING2", "Gamer")
        with self.assertRaises(DuplicateLineupPlayer):
            self.database.set_lineup_position(user_id, "SUB1", "XavierCapone9")

        lineup = self.database.get_lineup(user_id)
        self.assertEqual(set(lineup), {"WING1", "CM1", "GK"})
        self.assertEqual(lineup["WING1"].name, "XavierCapone9")
        self.assertEqual(lineup["CM1"].name, "Dan")
        self.assertEqual(lineup["GK"].name, "Licha")
        self.assertTrue(self.database.clear_lineup_position(user_id, "WING1"))
        self.assertFalse(self.database.clear_lineup_position(user_id, "WING1"))

    def test_card_versions_are_selected_by_exact_owned_card_id(self) -> None:
        user_id = "hala-version-user"
        gold_hala = self.database.get_player("Hala (Gold 90)")
        totw_hala = self.database.get_player("Hala")
        assert gold_hala is not None and totw_hala is not None
        self.database.add_inventory(user_id, gold_hala.name)
        self.database.add_inventory(user_id, totw_hala.name)

        selected_gold = self.database.set_lineup_position(
            user_id,
            "WING1",
            str(gold_hala.id),
        )
        selected_totw = self.database.set_lineup_position(
            user_id,
            "CM1",
            str(totw_hala.id),
        )
        self.assertEqual((selected_gold.id, selected_gold.rarity), (gold_hala.id, "GOLD"))
        self.assertEqual((selected_totw.id, selected_totw.rarity), (totw_hala.id, "TOTW"))
        lineup = self.database.get_lineup(user_id)
        self.assertEqual(lineup["WING1"].id, gold_hala.id)
        self.assertEqual(lineup["CM1"].id, totw_hala.id)
        with self.assertRaises(CardNotOwned):
            self.database.set_lineup_position(
                "different-owner",
                "CM1",
                str(totw_hala.id),
            )

    def test_formation_switch_preserves_roles_substitutes_and_reports_unassigned_cards(self) -> None:
        user_id = "formation-user"
        card_names = (
            "XavierCapone9",
            "Gamer",
            "Dan",
            "Fadi",
            "Licha",
            "Dryboomrun",
            "Needy",
        )
        for name in card_names:
            self.database.add_inventory(user_id, name)
        self.database.set_lineup_position(user_id, "WING1", "XavierCapone9")
        self.database.set_lineup_position(user_id, "CM1", "Dan")
        self.database.set_lineup_position(user_id, "GK", "Licha")
        self.database.set_lineup_position(user_id, "SUB1", "Dryboomrun")
        self.database.set_lineup_position(user_id, "SUB2", "Needy")

        offensive = self.database.set_formation_with_report(user_id, "Offensive")
        self.assertEqual(offensive.formation, "Offensive")
        self.assertEqual(
            [(slot, player.name) for slot, player in offensive.unassigned],
            [("CM1", "Dan")],
        )
        lineup = self.database.get_lineup(user_id)
        self.assertEqual(set(lineup), {"WING1", "GK", "SUB1", "SUB2"})
        self.assertEqual(lineup["WING1"].name, "XavierCapone9")
        self.assertEqual(lineup["SUB1"].name, "Dryboomrun")
        self.assertEqual(lineup["SUB2"].name, "Needy")

        self.database.set_lineup_position(user_id, "WING2", "Gamer")
        self.database.clear_lineup_position(user_id, "WING1")
        back_to_balanced = self.database.set_formation_with_report(user_id, "Balanced")
        self.assertEqual(
            [(old, new, player.name) for old, new, player in back_to_balanced.moved],
            [("WING2", "WING1", "Gamer")],
        )
        lineup = self.database.get_lineup(user_id)
        self.assertEqual(lineup["WING1"].name, "Gamer")
        self.assertNotIn("WING2", lineup)
        self.assertEqual(lineup["SUB1"].name, "Dryboomrun")
        self.assertEqual(lineup["SUB2"].name, "Needy")

        defensive = self.database.set_formation_with_report(user_id, "Defensive")
        self.assertEqual(defensive.formation, "Defensive")
        self.assertIn(("WING1", "Gamer"), [
            (slot, player.name) for slot, player in defensive.unassigned
        ])
        lineup = self.database.get_lineup(user_id)
        self.assertEqual(set(lineup), {"GK", "SUB1", "SUB2"})
        self.assertEqual(lineup["GK"].name, "Licha")

        with self.assertRaises(InvalidFormation):
            self.database.set_formation(user_id, "Attack")

    def test_legacy_lineups_migrate_additively_without_touching_ownership_or_balances(self) -> None:
        user_id = "legacy-team-user"
        for name in ("XavierCapone9", "Hala", "Dan", "Licha", "Fadi"):
            self.database.add_inventory(user_id, name)
        self.database.set_balance(user_id, 12345)
        original_inventory = [
            (entry.player.id, entry.quantity)
            for entry in self.database.get_inventory(user_id)
        ]
        cards = {player.name: player for player in self.database.get_players()}
        with self.database._transaction() as connection:
            connection.execute(
                "INSERT INTO team_settings(user_id, formation) VALUES (?, 'Attack')",
                (user_id,),
            )
            connection.executemany(
                "INSERT INTO lineup_slots(user_id, position, player_id) VALUES (?, ?, ?)",
                (
                    (user_id, "LW", cards["XavierCapone9"].id),
                    (user_id, "RW", cards["Hala"].id),
                    (user_id, "GK", cards["Licha"].id),
                ),
            )
            connection.execute(
                "INSERT INTO center_slots(user_id, position, player_id) VALUES (?, 'C', ?)",
                (user_id, cards["Dan"].id),
            )
            connection.execute(
                "INSERT INTO substitute_slots(user_id, position, player_id) VALUES (?, 'SUB1', ?)",
                (user_id, cards["Fadi"].id),
            )
            connection.execute("DELETE FROM team_data_migrations")

        self.database._migrate_legacy_team_data()
        self.assertEqual(self.database.get_formation(user_id), "Offensive")
        lineup = self.database.get_lineup(user_id)
        self.assertEqual(
            {slot: player.name for slot, player in lineup.items()},
            {"WING1": "XavierCapone9", "GK": "Licha", "SUB1": "Fadi"},
        )
        notices = self.database.get_lineup_migration_notices(user_id)
        self.assertTrue(any("Hala" in notice and "incompatible" in notice for notice in notices))
        self.assertTrue(any("Dan" in notice and "no additional CM" in notice for notice in notices))
        self.assertEqual(self.database.get_balance(user_id), 12345)
        self.assertEqual(
            [(entry.player.id, entry.quantity) for entry in self.database.get_inventory(user_id)],
            original_inventory,
        )
        with self.database._read_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM lineup_slots WHERE user_id = ?",
                    (user_id,),
                ).fetchone()[0],
                3,
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

    def test_new_gold_card_sales_credit_the_exact_listed_value(self) -> None:
        for name, value in (
            ("Chicken", 500_000),
            ("Sluckz", 430_000),
            ("Frozend", 1_200_000),
        ):
            with self.subTest(card=name):
                user_id = f"new-card-seller-{name.casefold()}"
                self.database.add_inventory(user_id, name)
                proceeds, balance = self.database.sell_card(user_id, name)
                self.assertEqual((proceeds, balance), (value, value))
                self.assertEqual(self.database.get_inventory(user_id), [])

    def test_each_totw_card_sells_for_twelve_million(self) -> None:
        for card_name in ("Licha", "Hala"):
            with self.subTest(card=card_name):
                user_id = f"totw-seller-{card_name.casefold()}"
                card = self.database.get_player(card_name)
                self.assertIsNotNone(card)
                assert card is not None
                self.assertEqual(card.rarity, "TOTW")
                self.assertEqual(card.value, 12_000_000)
                self.database.add_inventory(user_id, card_name)
                proceeds, balance = self.database.sell_card(user_id, card_name)
                self.assertEqual(proceeds, 12_000_000)
                self.assertEqual(balance, 12_000_000)


if __name__ == "__main__":
    unittest.main()
