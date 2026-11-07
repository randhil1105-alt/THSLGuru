from __future__ import annotations

import struct
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import discord

from commands.common import player_card_label, resolve_card_image
from commands.profile import ProfileCommands, xp_progress_bar
from commands.packs import FreePackOpenButton, PackCommands
from commands.xp import (
    award_marked_command_xp,
    mark_successful_command,
    xp_level_up_embed,
)
from database import (
    GOLD_PACK_COST,
    TOTW_PACK_COST,
    Database,
    InvalidLineupPosition,
    NoFreeRewardPacks,
)
from main import load_player_seed
from xp_system import (
    MINIGAME_COMPLETION_XP,
    calculate_xp_progress,
    level_up_pack_rewards,
    xp_required_for_level,
)


class _FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, content=None, **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})


class _FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, content=None, **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})


class XPDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "xp.sqlite3"
        self.database = Database(self.path)
        self.database.initialize(load_player_seed())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def set_total_xp(self, user_id: str, total_xp: int) -> None:
        self.database.set_balance(user_id, 0)
        with self.database._transaction() as connection:
            connection.execute(
                """
                INSERT INTO player_xp(user_id, total_xp, updated_at)
                VALUES (?, ?, 1)
                ON CONFLICT(user_id)
                DO UPDATE SET total_xp = excluded.total_xp,
                              updated_at = excluded.updated_at
                """,
                (user_id, total_xp),
            )

    def test_card_catalog_artwork_values_and_display_labels(self) -> None:
        expected = {
            "Cryptic": {
                "overall": 97,
                "position": "WI",
                "country": "Canada 🇨🇦",
                "rarity": "S2 TOTS",
                "value": 27_000_000,
                "stats": {
                    "SHO": 97,
                    "DRI": 96,
                    "SAV": 72,
                    "PAS": 90,
                    "DEF": 80,
                    "ACT": 93,
                },
                "image": "cards/cryptic_s2_tots.png",
            },
            "Hala (S2 TOTS 98)": {
                "overall": 98,
                "position": "C",
                "country": "Scotland 🏴",
                "rarity": "S2 TOTS",
                "value": 27_000_000,
                "stats": {
                    "SHO": 91,
                    "DRI": 99,
                    "SAV": 85,
                    "PAS": 93,
                    "DEF": 95,
                    "ACT": 95,
                },
                "image": "cards/hala_s2_tots_98.png",
            },
            "Xavier": {
                "overall": 99,
                "position": "W",
                "country": "England 🏴",
                "rarity": "S2 TOTS",
                "value": 45_000_000,
                "stats": {
                    "SHO": 97,
                    "DRI": 99,
                    "SAV": 82,
                    "PAS": 80,
                    "DEF": 95,
                    "ACT": 96,
                },
                "image": "cards/xavier_s2_tots_99.png",
            },
            "EBS (S2 TOTS 99)": {
                "overall": 99,
                "position": "W",
                "country": "Ireland 🇮🇪",
                "rarity": "S2 TOTS",
                "value": 34_000_000,
                "stats": {
                    "SHO": 99,
                    "DRI": 90,
                    "SAV": 80,
                    "PAS": 92,
                    "DEF": 80,
                    "ACT": 93,
                },
                "image": "cards/ebs_s2_tots_99.png",
            },
            "Jaycob (S2 TOTS 96)": {
                "overall": 96,
                "position": "GK",
                "country": "United States 🇺🇸",
                "rarity": "S2 TOTS",
                "value": 24_000_000,
                "stats": {
                    "SAV": 99,
                    "DIV": 98,
                    "PAS": 89,
                    "CLE": 87,
                    "SWE": 92,
                    "ACT": 98,
                },
                "image": "cards/jaycob_s2_tots_96.png",
            },
            "Licha (S2 TOTS 96)": {
                "overall": 96,
                "position": "GK",
                "country": "England 🏴",
                "rarity": "S2 TOTS",
                "value": 25_000_000,
                "stats": {
                    "SAV": 97,
                    "DIV": 99,
                    "PAS": 88,
                    "CLE": 87,
                    "SWE": 92,
                    "ACT": 95,
                },
                "image": "cards/licha_s2_tots_96.png",
            },
        }
        for name, values in expected.items():
            with self.subTest(name=name):
                player = self.database.get_player(name)
                self.assertEqual(player.overall, values["overall"])
                self.assertEqual(player.position, values["position"])
                self.assertEqual(player.country, values["country"])
                self.assertEqual(player.rarity, values["rarity"])
                self.assertEqual(player.value, values["value"])
                self.assertEqual(player.stats, values["stats"])
                self.assertEqual(player.image_path, values["image"])
                self.assertTrue(
                    player_card_label(player).startswith(
                        f"{values['overall']} "
                    )
                )
                image_path = resolve_card_image(player)
                self.assertIsNotNone(image_path)
                self.assertTrue(image_path.is_file())
                image_data = image_path.read_bytes()
                self.assertEqual(image_data[:8], b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack(">II", image_data[16:24])
                self.assertGreater(width, 200)
                self.assertGreater(height, 250)

        # Re-initialization must not overwrite or duplicate the new catalog rows.
        self.database.initialize(load_player_seed())
        all_names = [player.name for player in self.database.get_players()]
        for name in expected:
            self.assertEqual(sum(item == name for item in all_names), 1)
        self.assertEqual(
            len(self.database.get_players(rarity="S2 TOTS")),
            6,
        )

    def test_legacy_cards_migrate_in_place_without_losing_owned_copies(self) -> None:
        legacy_seed = load_player_seed()
        legacy_images = {
            "Cryptic": "cards/cryptic_totw.png",
            "Hala (S2 TOTS 98)": "cards/hala_totw_98.png",
            "Xavier": "cards/xavier_totw_99.png",
        }
        for record in legacy_seed:
            if record["name"] in legacy_images:
                record["rarity"] = "TOTW"
                record["image_path"] = legacy_images[str(record["name"])]
                if record["name"] == "Hala (S2 TOTS 98)":
                    record["name"] = "Hala (TOTW 98)"

        legacy_path = Path(self.temp_dir.name) / "legacy-cards.sqlite3"
        legacy_database = Database(legacy_path)
        legacy_database.initialize(legacy_seed)
        legacy_database.set_balance("legacy-owner", 123_456)
        expected_ids = {}
        for name in ("Cryptic", "Hala (TOTW 98)", "Xavier"):
            legacy_database.add_inventory("legacy-owner", name, 2)
            expected_ids[name] = legacy_database.get_player(name).id

        legacy_database.initialize(load_player_seed())

        migrated_names = {
            "Cryptic": "Cryptic",
            "Hala (TOTW 98)": "Hala (S2 TOTS 98)",
            "Xavier": "Xavier",
        }
        for old_name, new_name in migrated_names.items():
            with self.subTest(card=old_name):
                player = legacy_database.get_player(new_name)
                self.assertIsNotNone(player)
                assert player is not None
                self.assertEqual(player.rarity, "S2 TOTS")
                self.assertEqual(player.id, expected_ids[old_name])
                self.assertEqual(
                    sum(
                        item.name == new_name
                        for item in legacy_database.get_players()
                    ),
                    1,
                )
        self.assertEqual(legacy_database.get_balance("legacy-owner"), 123_456)
        self.assertEqual(
            {
                entry.player.name: entry.quantity
                for entry in legacy_database.get_inventory("legacy-owner")
            },
            {
                "Cryptic": 2,
                "Hala (S2 TOTS 98)": 2,
                "Xavier": 2,
            },
        )

    def test_new_cards_respect_existing_lineup_position_rules(self) -> None:
        self.database.add_inventory("cryptic-user", "Cryptic")
        self.database.add_inventory("hala-user", "Hala (S2 TOTS 98)")
        self.database.add_inventory("xavier-user", "Xavier")
        self.database.set_lineup_position("cryptic-user", "WING1", "Cryptic")
        self.database.set_lineup_position("hala-user", "CM1", "Hala (S2 TOTS 98)")
        self.database.set_lineup_position("xavier-user", "WING1", "Xavier")
        with self.assertRaises(InvalidLineupPosition):
            self.database.set_lineup_position("cryptic-user", "CM1", "Cryptic")

    def test_xp_level_thresholds_and_progress_calculation(self) -> None:
        self.assertEqual(
            [xp_required_for_level(level) for level in range(1, 6)],
            [0, 500, 1_200, 2_100, 3_200],
        )
        at_level_three = calculate_xp_progress(1_200)
        self.assertEqual(at_level_three.level, 3)
        self.assertEqual(at_level_three.progress_xp, 0)
        self.assertEqual(at_level_three.xp_to_next_level, 900)
        self.assertEqual(at_level_three.xp_remaining, 900)
        partial = calculate_xp_progress(1_650)
        self.assertEqual(partial.level, 3)
        self.assertEqual(partial.progress_xp, 450)
        self.assertEqual(partial.xp_remaining, 450)
        self.assertEqual(partial.progress_percent, 50)
        self.assertEqual(xp_progress_bar(50), "███████░░░░░░░░")

    def test_level_ups_grant_gold_and_each_tenth_level_grants_totw(self) -> None:
        ordinary_user = "ordinary-level-reward"
        self.set_total_xp(ordinary_user, xp_required_for_level(2) - 1)
        ordinary_grant = self.database.award_xp(
            ordinary_user,
            "ordinary-level-up",
            1,
            "test",
        )
        self.assertEqual((ordinary_grant.level_before, ordinary_grant.level_after), (1, 2))
        self.assertEqual(
            self.database.get_free_pack_inventory(ordinary_user),
            {"GOLD": 1, "TOTW": 0},
        )

        for milestone in range(10, 51, 10):
            with self.subTest(level=milestone):
                user_id = f"milestone-{milestone}"
                self.set_total_xp(user_id, xp_required_for_level(milestone) - 1)
                grant = self.database.award_xp(
                    user_id,
                    f"reach-level-{milestone}",
                    1,
                    "test",
                )
                self.assertEqual(grant.level_after, milestone)
                self.assertEqual(
                    self.database.get_free_pack_inventory(user_id),
                    {"GOLD": 1, "TOTW": 1},
                )

    def test_skipped_levels_grant_each_gold_pack_and_crossed_totw_milestones(self) -> None:
        user_id = "skip-level-rewards"
        previous_xp = xp_required_for_level(8)
        target_xp = xp_required_for_level(12)
        self.set_total_xp(user_id, previous_xp)

        grant = self.database.award_xp(
            user_id,
            "skip-levels-8-to-12",
            target_xp - previous_xp,
            "test",
        )

        self.assertEqual((grant.level_before, grant.level_after), (8, 12))
        self.assertEqual(level_up_pack_rewards(8, 12), (4, 1))
        self.assertEqual(
            self.database.get_free_pack_inventory(user_id),
            {"GOLD": 4, "TOTW": 1},
        )
        embed = xp_level_up_embed(grant, user_id=user_id, username="SkipPlayer")
        self.assertEqual(embed.title, "🎉 LEVEL UP!")
        self.assertIn("<@skip-level-rewards>", embed.description)
        self.assertIn("Level 12", embed.description)
        self.assertEqual(
            embed.fields[0].value,
            "- 4x Gold Packs\n- 1x TOTW Pack",
        )

    def test_level_rewards_and_free_pack_redemption_persist_and_are_idempotent(self) -> None:
        user_id = "persistent-free-packs"
        self.set_total_xp(user_id, xp_required_for_level(2) - 1)
        self.database.set_balance(user_id, 777)
        self.database.award_xp(user_id, "persistent-level-up", 1, "test")

        reopened = Database(self.path)
        reopened.initialize(load_player_seed())
        self.assertEqual(
            reopened.get_free_pack_inventory(user_id),
            {"GOLD": 1, "TOTW": 0},
        )

        class ChooseFirst:
            def choices(self, population, *, weights, k):
                del weights, k
                return [population[0]]

        player, remaining = reopened.open_free_reward_pack(
            user_id,
            "GOLD",
            "free-pack-open-once",
            rng=ChooseFirst(),
        )

        class NoSelection:
            def choices(self, population, *, weights, k):
                del population, weights, k
                raise AssertionError("A replay must return the saved pack result.")

        replayed, replayed_remaining = reopened.open_free_reward_pack(
            user_id,
            "GOLD",
            "free-pack-open-once",
            rng=NoSelection(),
        )
        self.assertEqual(player.id, replayed.id)
        self.assertEqual((remaining, replayed_remaining), (0, 0))
        self.assertEqual(player.rarity, "GOLD")
        self.assertEqual(reopened.get_balance(user_id), 777)
        self.assertEqual(reopened.get_free_pack_inventory(user_id), {"GOLD": 0, "TOTW": 0})
        self.assertEqual(
            [(entry.player.name, entry.quantity) for entry in reopened.get_inventory(user_id)],
            [(player.name, 1)],
        )
        with self.assertRaises(NoFreeRewardPacks):
            reopened.open_free_reward_pack(
                user_id,
                "GOLD",
                "another-free-pack-open",
                rng=ChooseFirst(),
            )

    def test_concurrent_duplicate_level_xp_event_awards_one_reward(self) -> None:
        user_id = "concurrent-level-up"
        self.set_total_xp(user_id, xp_required_for_level(2) - 1)

        def award(_index: int):
            return self.database.award_xp(
                user_id,
                "same-level-up-event",
                1,
                "test",
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            grants = list(pool.map(award, range(20)))

        self.assertEqual(sum(grant.is_new for grant in grants), 1)
        self.assertEqual(self.database.get_xp_profile(user_id).level, 2)
        self.assertEqual(
            self.database.get_free_pack_inventory(user_id),
            {"GOLD": 1, "TOTW": 0},
        )
        with self.database._read_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM xp_level_rewards WHERE user_id = ?",
                    (user_id,),
                ).fetchone()[0],
                1,
            )

    def test_xp_transactions_are_idempotent_and_persist(self) -> None:
        first = self.database.award_xp(
            "user-1", "command:101", 50, "command", now=100
        )
        duplicate = self.database.award_xp(
            "user-1", "command:101", 50, "command", now=101
        )
        self.assertTrue(first.is_new)
        self.assertFalse(duplicate.is_new)
        self.assertEqual(self.database.get_xp_profile("user-1").total_xp, 50)

        reopened = Database(self.path)
        reopened.initialize(load_player_seed())
        self.assertEqual(reopened.get_xp_profile("user-1").total_xp, 50)
        self.assertFalse(
            reopened.award_xp(
                "user-1", "command:101", 50, "command", now=102
            ).is_new
        )

    def test_guess_who_completion_awards_xp_and_correct_prize_once(self) -> None:
        correct_balance, grant = self.database.complete_guess_who(
            "winner",
            "guess-correct",
            correct=True,
            coin_reward=500_000,
            now=100,
        )
        self.assertEqual(correct_balance, 500_000)
        self.assertEqual(grant.amount, MINIGAME_COMPLETION_XP)
        self.assertEqual(self.database.get_balance("winner"), 500_000)
        self.assertEqual(self.database.get_xp_profile("winner").total_xp, 200)

        repeat_balance, repeated = self.database.complete_guess_who(
            "winner",
            "guess-correct",
            correct=True,
            coin_reward=500_000,
            now=101,
        )
        self.assertEqual(repeat_balance, 500_000)
        self.assertFalse(repeated.is_new)
        self.assertEqual(self.database.get_balance("winner"), 500_000)
        self.assertEqual(self.database.get_xp_profile("winner").total_xp, 200)

        wrong_balance, wrong_grant = self.database.complete_guess_who(
            "wrong",
            "guess-wrong",
            correct=False,
            coin_reward=500_000,
            now=100,
        )
        self.assertEqual(wrong_balance, 0)
        self.assertEqual(wrong_grant.amount, 200)
        self.assertEqual(self.database.get_balance("wrong"), 0)
        self.assertEqual(self.database.get_xp_profile("wrong").total_xp, 200)

    def test_cancelled_minigame_does_not_award_completion_xp(self) -> None:
        self.database.start_draft_session("unfinished", "unfinished-game")
        self.assertTrue(
            self.database.cancel_draft_session("unfinished-game", "unfinished")
        )
        self.assertEqual(self.database.get_xp_profile("unfinished").total_xp, 0)

    def test_simultaneous_xp_awards_are_not_lost_or_duplicated(self) -> None:
        def award(index: int):
            return self.database.award_xp(
                "parallel",
                f"parallel:{index}",
                50,
                "command",
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            grants = list(pool.map(award, range(40)))
        self.assertEqual(sum(grant.amount for grant in grants), 2_000)
        self.assertEqual(self.database.get_xp_profile("parallel").total_xp, 2_000)
        self.assertEqual(
            self.database.get_free_pack_inventory("parallel"),
            {"GOLD": 2, "TOTW": 0},
        )

        def repeat_same_event(_index: int):
            return self.database.award_xp(
                "parallel-duplicate",
                "same-interaction",
                50,
                "command",
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            repeated = list(pool.map(repeat_same_event, range(20)))
        self.assertEqual(sum(grant.is_new for grant in repeated), 1)
        self.assertEqual(
            self.database.get_xp_profile("parallel-duplicate").total_xp,
            50,
        )

    def test_additive_xp_migration_preserves_existing_coins_and_cards(self) -> None:
        self.database.set_balance("existing", 123_456)
        self.database.add_inventory("existing", "Gamer", 2)
        before_inventory = self.database.get_inventory("existing")
        self.database.ensure_xp_schema()
        self.database.ensure_xp_schema()
        self.assertEqual(self.database.get_balance("existing"), 123_456)
        self.assertEqual(self.database.get_inventory("existing"), before_inventory)
        self.assertEqual(self.database.get_xp_profile("existing").total_xp, 0)


class XPCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "xp-command.sqlite3"
        self.database = Database(self.path)
        self.database.initialize(load_player_seed())

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_successful_command_marker_awards_once_and_notifies(self) -> None:
        interaction = SimpleNamespace(
            id=5001,
            user=SimpleNamespace(id=42, bot=False),
            extras={},
            followup=_FakeFollowup(),
        )
        self.assertIsNone(await award_marked_command_xp(interaction, self.database))
        mark_successful_command(interaction)
        grant = await award_marked_command_xp(interaction, self.database)
        self.assertTrue(grant.is_new)
        self.assertEqual(self.database.get_xp_profile(42).total_xp, 50)
        self.assertEqual(len(interaction.followup.messages), 1)
        self.assertIn("+50 XP", interaction.followup.messages[0]["content"])

        mark_successful_command(interaction)
        repeated = await award_marked_command_xp(interaction, self.database)
        self.assertFalse(repeated.is_new)
        self.assertEqual(self.database.get_xp_profile(42).total_xp, 50)
        self.assertEqual(len(interaction.followup.messages), 1)

    async def test_profile_shows_updated_progress_and_level_up(self) -> None:
        self.database.award_xp(55, "seed-profile-xp", 450, "command")
        interaction = SimpleNamespace(
            id=6001,
            user=SimpleNamespace(id=55, name="player_handle", bot=False),
            response=_FakeResponse(),
        )
        command = ProfileCommands(self.database)
        await ProfileCommands.profile.callback(command, interaction)

        embed = interaction.response.messages[0]["embed"]
        self.assertEqual(self.database.get_xp_profile(55).total_xp, 500)
        self.assertIn("player\\_handle", embed.description)
        self.assertTrue(any(field.name == "Level" and field.value == "**2**" for field in embed.fields))
        self.assertTrue(any("0 / 700 XP" in field.value for field in embed.fields))
        self.assertIn("LEVEL UP", next(field.value for field in embed.fields if field.name == "XP earned"))

    async def test_bot_users_do_not_receive_command_xp(self) -> None:
        interaction = SimpleNamespace(
            id=7001,
            user=SimpleNamespace(id=77, bot=True),
            extras={},
            followup=_FakeFollowup(),
        )
        mark_successful_command(interaction)
        self.assertIsNone(await award_marked_command_xp(interaction, self.database))
        self.assertEqual(self.database.get_xp_profile(77).total_xp, 0)


if __name__ == "__main__":
    unittest.main()
