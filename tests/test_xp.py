from __future__ import annotations

import struct
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from commands.common import player_card_label, resolve_card_image
from commands.profile import ProfileCommands, xp_progress_bar
from commands.xp import award_marked_command_xp, mark_successful_command
from database import Database, InvalidLineupPosition
from main import load_player_seed
from xp_system import (
    MINIGAME_COMPLETION_XP,
    calculate_xp_progress,
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

    def test_card_catalog_artwork_values_and_display_labels(self) -> None:
        expected = {
            "Cryptic": {
                "overall": 97,
                "position": "WI",
                "country": "Canada 🇨🇦",
                "rarity": "TOTW",
                "value": 27_000_000,
                "stats": {
                    "SHO": 97,
                    "DRI": 96,
                    "SAV": 72,
                    "PAS": 90,
                    "DEF": 80,
                    "ACT": 93,
                },
                "image": "cards/cryptic_totw.png",
            },
            "Hala (TOTW 98)": {
                "overall": 98,
                "position": "C",
                "country": "Scotland 🏴",
                "rarity": "TOTW",
                "value": 27_000_000,
                "stats": {
                    "SHO": 91,
                    "DRI": 99,
                    "SAV": 85,
                    "PAS": 93,
                    "DEF": 95,
                    "ACT": 95,
                },
                "image": "cards/hala_totw_98.png",
            },
            "Xavier": {
                "overall": 99,
                "position": "W",
                "country": "England 🏴",
                "rarity": "TOTW",
                "value": 45_000_000,
                "stats": {
                    "SHO": 97,
                    "DRI": 99,
                    "SAV": 82,
                    "PAS": 80,
                    "DEF": 95,
                    "ACT": 96,
                },
                "image": "cards/xavier_totw_99.png",
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

    def test_new_cards_respect_existing_lineup_position_rules(self) -> None:
        self.database.add_inventory("cryptic-user", "Cryptic")
        self.database.add_inventory("hala-user", "Hala (TOTW 98)")
        self.database.add_inventory("xavier-user", "Xavier")
        self.database.set_lineup_position("cryptic-user", "WING1", "Cryptic")
        self.database.set_lineup_position("hala-user", "CM1", "Hala (TOTW 98)")
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
