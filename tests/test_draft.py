from __future__ import annotations

import random
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from commands.draft import DraftSessionView
from commands.common import player_card_label
from commands.minigame import MinigameCommands
from database import (
    DRAFT_COOLDOWN_SECONDS,
    DRAFT_WIN_REWARD,
    Database,
    DraftAlreadyActive,
    DraftCooldownActive,
    card_position_role,
)
from draft_simulation import (
    InsufficientDraftCatalog,
    calculate_squad_average,
    draft_win_probability,
    generate_ai_squad,
    generate_draft_groups,
    simulate_draft_match,
)
from main import load_player_seed


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, content=None, **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs) -> None:
        self.messages.append(kwargs)


class FakeMessage:
    async def edit(self, **kwargs) -> None:
        self.edited = kwargs


class FixedRandom:
    def __init__(self, roll: float = 0.0) -> None:
        self.roll = roll

    def random(self) -> float:
        return self.roll

    @staticmethod
    def sample(population, count):
        return list(population)[:count]

    @staticmethod
    def choice(population):
        return population[0]

    @staticmethod
    def randint(low: int, high: int) -> int:
        return low


class DraftDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "draft.sqlite3"
        self.database = Database(self.path)
        self.database.initialize(load_player_seed())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cooldown_is_ten_minutes_and_blocks_a_second_active_draft(self) -> None:
        self.assertEqual(DRAFT_COOLDOWN_SECONDS, 600)
        self.database.start_draft_session("player-1", "first", now=100)
        with self.assertRaises(DraftAlreadyActive):
            self.database.start_draft_session("player-1", "second", now=101)

    def test_cooldown_persists_after_sqlite_reopens(self) -> None:
        self.database.start_draft_session("player-1", "first", now=100)
        self.database.finish_draft_session(
            "first",
            "player-1",
            won=False,
            result={"player_won": False},
            now=130,
        )

        reopened = Database(self.path)
        reopened.initialize(load_player_seed())
        with self.assertRaises(DraftCooldownActive) as caught:
            reopened.start_draft_session("player-1", "too-soon", now=200)
        self.assertEqual(caught.exception.remaining_seconds, 500)
        reopened.start_draft_session(
            "player-1",
            "ready",
            now=100 + DRAFT_COOLDOWN_SECONDS,
        )

    def test_different_users_can_start_drafts_during_each_others_cooldown(self) -> None:
        self.database.start_draft_session("player-1", "one", now=100)
        self.database.start_draft_session("player-2", "two", now=101)
        self.assertEqual(
            self.database.get_balance("player-1"),
            0,
        )
        self.assertEqual(
            self.database.get_balance("player-2"),
            0,
        )

    def test_different_users_can_begin_drafts_concurrently(self) -> None:
        barrier = Barrier(2)

        def start(user_id: str, game_id: str) -> bool:
            barrier.wait()
            self.database.start_draft_session(user_id, game_id, now=100)
            return True

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(
                pool.map(
                    lambda args: start(*args),
                    (("concurrent-a", "game-a"), ("concurrent-b", "game-b")),
                )
            )
        self.assertEqual(outcomes, [True, True])

    def test_concurrent_draft_starts_allow_only_one_session_per_user(self) -> None:
        barrier = Barrier(2)

        def start(game_id: str) -> str:
            barrier.wait()
            try:
                self.database.start_draft_session(
                    "same-player",
                    game_id,
                    now=100,
                )
                return "started"
            except (DraftAlreadyActive, DraftCooldownActive):
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(start, ("race-a", "race-b")))
        self.assertCountEqual(outcomes, ["started", "blocked"])

    def test_win_awards_two_hundred_thousand_once_and_preserves_inventory(self) -> None:
        self.database.set_balance("winner", 7_147_502)
        self.database.add_inventory("winner", "Dan")
        before_inventory = self.database.get_inventory("winner")
        self.database.start_draft_session("winner", "win", now=100)

        result = {
            "player_won": True,
            "player_score": 3,
            "ai_score": 1,
        }
        self.assertEqual(
            self.database.finish_draft_session(
                "win",
                "winner",
                won=True,
                result=result,
                now=200,
            ),
            (True, 7_347_502),
        )
        self.assertEqual(
            self.database.finish_draft_session(
                "win",
                "winner",
                won=True,
                result=result,
                now=201,
            ),
            (False, 7_347_502),
        )
        self.assertEqual(DRAFT_WIN_REWARD, 200_000)
        self.assertEqual(self.database.get_balance("winner"), 7_347_502)
        self.assertEqual(self.database.get_inventory("winner"), before_inventory)
        self.assertEqual(self.database.get_xp_profile("winner").total_xp, 200)

        reopened = Database(self.path)
        reopened.initialize(load_player_seed())
        self.assertEqual(reopened.get_balance("winner"), 7_347_502)
        self.assertEqual(reopened.get_xp_profile("winner").total_xp, 200)
        self.assertEqual(
            reopened.finish_draft_session(
                "win",
                "winner",
                won=True,
                result=result,
                now=202,
            ),
            (False, 7_347_502),
        )

    def test_loss_awards_zero_and_cancellation_awards_nothing(self) -> None:
        self.database.set_balance("loser", 12_345)
        self.database.start_draft_session("loser", "loss", now=100)
        completed, balance = self.database.finish_draft_session(
            "loss",
            "loser",
            won=False,
            result={"player_won": False},
            now=200,
        )
        self.assertTrue(completed)
        self.assertEqual(balance, 12_345)
        self.assertEqual(self.database.get_balance("loser"), 12_345)
        self.assertEqual(self.database.get_xp_profile("loser").total_xp, 200)

        self.database.start_draft_session(
            "cancelled",
            "cancel",
            now=100 + DRAFT_COOLDOWN_SECONDS,
        )
        self.assertTrue(
            self.database.cancel_draft_session(
                "cancel",
                "cancelled",
                now=200 + DRAFT_COOLDOWN_SECONDS,
            )
        )
        self.assertEqual(self.database.get_balance("cancelled"), 0)
        self.assertEqual(self.database.get_xp_profile("cancelled").total_xp, 0)
        self.assertFalse(
            self.database.finish_draft_session(
                "cancel",
                "cancelled",
                won=True,
                result={"player_won": True},
                now=201 + DRAFT_COOLDOWN_SECONDS,
            )[0]
        )


class DraftSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "draft-simulation.sqlite3")
        self.database.initialize(load_player_seed())
        self.catalog = self.database.get_players()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_new_s2_tots_cards_are_available_to_draft_by_position(self) -> None:
        class CaptureCandidates:
            def __init__(self) -> None:
                self.populations: list[tuple] = []

            def sample(self, population, count):
                self.populations.append(tuple(population))
                return list(population[:count])

        captured = CaptureCandidates()
        groups = generate_draft_groups(self.catalog, rng=captured)
        self.assertEqual(len(groups), 4)
        goalkeeper_names = {player.name for player in captured.populations[0]}
        winger_names = {player.name for player in captured.populations[2]}
        self.assertIn("Jaycob (S2 TOTS 96)", goalkeeper_names)
        self.assertIn("Licha (S2 TOTS 96)", goalkeeper_names)
        self.assertIn("EBS (S2 TOTS 99)", winger_names)

    def test_draft_generates_exactly_three_cards_per_position_group(self) -> None:
        groups = generate_draft_groups(self.catalog, rng=random.Random(23))
        self.assertEqual(tuple(map(len, groups)), (3, 3, 3, 3))
        self.assertEqual(
            [
                [card_position_role(player.position) for player in group]
                for group in groups
            ],
            [
                ["GK"] * 3,
                ["CM"] * 3,
                ["WING"] * 3,
                ["WING"] * 3,
            ],
        )
        ids = [player.id for group in groups for player in group]
        self.assertEqual(len(ids), len(set(ids)))

    def test_draft_reports_catalogs_missing_required_position_cards(self) -> None:
        only_goalies = [
            player
            for player in self.catalog
            if card_position_role(player.position) == "GK"
        ][:2]
        with self.assertRaises(InsufficientDraftCatalog) as caught:
            generate_draft_groups(only_goalies)
        self.assertIn("does not have enough cards", str(caught.exception))

    def test_ai_squad_has_one_goalie_one_center_and_two_distinct_wingers(self) -> None:
        draft_groups = generate_draft_groups(self.catalog, rng=random.Random(4))
        drafted_ids = {group[0].id for group in draft_groups}
        ai_squad = generate_ai_squad(
            self.catalog,
            excluded_player_ids=drafted_ids,
            rng=random.Random(5),
        )
        self.assertEqual(len(ai_squad), 4)
        self.assertEqual(
            sorted(card_position_role(player.position) for player in ai_squad),
            ["CM", "GK", "WING", "WING"],
        )
        self.assertEqual(len({player.id for player in ai_squad}), 4)
        self.assertFalse({player.id for player in ai_squad} & drafted_ids)

    def test_squad_average_uses_all_four_ratings(self) -> None:
        players = self.catalog[:4]
        self.assertEqual(
            calculate_squad_average(players),
            (sum(player.overall for player in players) + 2) // 4,
        )

    def test_higher_rated_squads_have_better_winning_odds(self) -> None:
        self.assertGreater(
            draft_win_probability(90, 80),
            draft_win_probability(80, 90),
        )
        self.assertEqual(draft_win_probability(80, 80), 0.5)

    def test_lower_rated_squad_still_has_a_random_chance_to_win(self) -> None:
        groups = generate_draft_groups(self.catalog, rng=random.Random(2))
        player_squad = tuple(group[0] for group in groups)
        ai_squad = generate_ai_squad(
            self.catalog,
            excluded_player_ids={player.id for player in player_squad},
            rng=random.Random(3),
        )
        player_squad = tuple(
            player
            for player in player_squad
        )
        low_average = min(
            calculate_squad_average(player_squad),
            calculate_squad_average(ai_squad),
        )
        high_average = max(
            calculate_squad_average(player_squad),
            calculate_squad_average(ai_squad),
        )
        low_team = (
            player_squad
            if calculate_squad_average(player_squad) == low_average
            else ai_squad
        )
        high_team = ai_squad if low_team is player_squad else player_squad
        forced_upset = simulate_draft_match(
            low_team,
            high_team,
            rng=FixedRandom(0.0),
        )
        self.assertTrue(forced_upset.player_won)
        self.assertGreater(forced_upset.player_win_probability, 0.0)
        self.assertLess(forced_upset.player_win_probability, 0.5)


class DraftCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "draft-command.sqlite3")
        self.database.initialize(load_player_seed())
        self.cog = MinigameCommands(self.database)
        self.views: list[DraftSessionView] = []
        self.files: list[discord.File] = []

    async def asyncTearDown(self) -> None:
        for view in self.views:
            view.stop()
            if not view.finished:
                self.database.cancel_draft_session(view.game_id, view.owner_id)
        for file in self.files:
            file.close()
        self.temp_dir.cleanup()

    def make_interaction(self, user_id: int = 1001) -> SimpleNamespace:
        user = SimpleNamespace(
            id=user_id,
            name=f"Player{user_id}",
            display_name=f"Player{user_id}",
        )
        response = FakeResponse()
        channel = SimpleNamespace(id=9001)
        return SimpleNamespace(
            user=user,
            guild=SimpleNamespace(id=9000),
            channel=channel,
            response=response,
            original_response=AsyncMock(return_value=FakeMessage()),
        )

    async def start_draft(self, user_id: int = 1001) -> tuple[SimpleNamespace, DraftSessionView]:
        interaction = self.make_interaction(user_id)
        await MinigameCommands.minigame.callback(
            self.cog,
            interaction,
            discord.app_commands.Choice(name="Draft", value="draft"),
        )
        payload = interaction.response.messages[0]
        self.assertFalse(payload["ephemeral"])
        view = payload["view"]
        self.assertIsInstance(view, DraftSessionView)
        self.views.append(view)
        self.files.extend(payload["files"])
        return interaction, view

    async def test_minigame_exposes_guess_who_and_draft_as_mode_options(self) -> None:
        command = MinigameCommands.minigame
        mode_parameter = next(
            parameter for parameter in command.parameters if parameter.name == "mode"
        )
        self.assertEqual(
            [(choice.name, choice.value) for choice in mode_parameter.choices],
            [("Guess Who", "guess"), ("Draft", "draft")],
        )

    async def test_draft_command_starts_public_owner_controlled_picks(self) -> None:
        interaction, view = await self.start_draft()
        payload = interaction.response.messages[0]
        self.assertEqual(len(view.children), 3)
        self.assertEqual(len(payload["embeds"]), 4)
        self.assertIn("Goalkeeper", payload["embeds"][0].title)
        self.assertEqual(
            [embed.title for embed in payload["embeds"][1:]],
            [player_card_label(player) for player in view.groups[0]],
        )
        self.assertEqual(len(view.selected), 0)
        self.assertIn(view.owner_id, self.cog._active_drafts_by_owner)

    async def test_other_users_cannot_control_an_individual_draft(self) -> None:
        _, view = await self.start_draft()
        response = FakeResponse()
        allowed = await view.interaction_check(
            SimpleNamespace(
                user=SimpleNamespace(id=view.owner_id + 1),
                response=response,
            )
        )
        self.assertFalse(allowed)
        self.assertTrue(response.messages[0]["ephemeral"])
        self.assertEqual(view.selected, [])

        choose_response = FakeResponse()
        await view.choose(
            SimpleNamespace(
                user=SimpleNamespace(id=view.owner_id + 1),
                response=choose_response,
            ),
            view.groups[0][0],
        )
        self.assertTrue(choose_response.messages[0]["ephemeral"])
        self.assertEqual(view.selected, [])

    async def test_four_position_picks_are_required_before_match_and_reward(self) -> None:
        _, view = await self.start_draft()
        for index in range(3):
            response = FakeResponse()
            await view.choose(
                SimpleNamespace(
                    user=SimpleNamespace(id=view.owner_id),
                    response=response,
                ),
                view.groups[index][0],
            )
            self.assertEqual(view.stage_index, index + 1)
            self.assertEqual(self.database.get_balance(view.owner_id), 0)
            prompt_embeds = response.messages[0]["embeds"]
            self.assertEqual(len(prompt_embeds), 4)
            for selected in view.selected:
                self.assertIn(
                    player_card_label(selected),
                    prompt_embeds[0].description,
                )
        self.assertFalse(view.finished)
        self.assertEqual(len(view.selected), 3)

    async def test_duplicate_selection_is_rejected_and_all_four_picks_finish(self) -> None:
        interaction, view = await self.start_draft()
        view.rng = FixedRandom(0.0)

        first_response = FakeResponse()
        await view.choose(
            SimpleNamespace(user=interaction.user, response=first_response),
            view.groups[0][0],
        )
        duplicate_response = FakeResponse()
        await view.choose(
            SimpleNamespace(user=interaction.user, response=duplicate_response),
            view.groups[0][0],
        )
        self.assertIn("same card twice", duplicate_response.messages[0]["content"])
        for group in view.groups[1:]:
            response = FakeResponse()
            await view.choose(
                SimpleNamespace(user=interaction.user, response=response),
                group[0],
            )

        final_message = response.messages[0]
        self.assertTrue(view.finished)
        self.assertEqual(len(view.selected), 4)
        self.assertEqual(len({player.id for player in view.selected}), 4)
        self.assertIsNone(final_message["view"])
        self.assertIn(
            "Final score",
            [field.name for field in final_message["embeds"][0].fields],
        )
        self.assertEqual(self.database.get_balance(view.owner_id), DRAFT_WIN_REWARD)
        reward_field = next(
            field
            for field in final_message["embeds"][0].fields
            if field.name == "Reward"
        )
        self.assertIn(f"{DRAFT_WIN_REWARD:,} coins", reward_field.value)

    async def test_insufficient_catalog_returns_clear_error_without_cooldown(self) -> None:
        empty_database = Database(Path(self.temp_dir.name) / "empty-draft.sqlite3")
        empty_database.initialize()
        empty_cog = MinigameCommands(empty_database)
        interaction = self.make_interaction(user_id=1003)

        await MinigameCommands.minigame.callback(
            empty_cog,
            interaction,
            discord.app_commands.Choice(name="Draft", value="draft"),
        )

        response = interaction.response.messages[0]
        self.assertTrue(response["ephemeral"])
        self.assertIn("does not have enough cards", response["content"])
        with empty_database._read_connection() as connection:
            cooldown_count = connection.execute(
                "SELECT COUNT(*) AS count FROM draft_cooldowns"
            ).fetchone()["count"]
        self.assertEqual(cooldown_count, 0)

    async def test_missing_card_artwork_does_not_break_the_draft_prompt(self) -> None:
        _, view = await self.start_draft(user_id=1002)
        original = view.groups[0]
        view.groups = (
            tuple(
                player.__class__(
                    id=player.id,
                    name=player.name,
                    overall=player.overall,
                    position=player.position,
                    country=player.country,
                    team=player.team,
                    rarity=player.rarity,
                    value=player.value,
                    stats=player.stats,
                    image_path="cards/does-not-exist.png",
                    allowed_lineup_positions=player.allowed_lineup_positions,
                )
                for player in original
            ),
            *view.groups[1:],
        )
        embeds, files = view._option_embeds_and_files()
        self.assertEqual(files, [])
        self.assertTrue(
            all(
                any(field.name == "Card artwork" for field in embed.fields)
                for embed in embeds[1:]
            )
        )


if __name__ == "__main__":
    unittest.main()
