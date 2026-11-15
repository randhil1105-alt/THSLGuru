from __future__ import annotations

import random
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from commands.init import load_command_modules
from commands.tournament import TournamentCommands
from database import (
    FORMATION_STARTERS,
    POSITION_ROLE_BY_SLOT,
    TOURNAMENT_COMPLETION_BONUS,
    TOURNAMENT_COOLDOWN_SECONDS,
    TOURNAMENT_ROUND_REWARD,
    Database,
    TournamentCooldownActive,
    TournamentInvalidLineup,
    TournamentLineupChanged,
    TournamentNotActive,
    card_position_role,
)
from main import create_bot, load_player_seed
from match_simulation import simulate_match as run_match
from tournament_simulation import (
    TournamentAIUnavailable,
    generate_ai_lineup,
    tournament_scoring_probability,
)


def fill_lineup(database: Database, user_id: int | str, formation: str) -> None:
    database.set_formation(user_id, formation)
    catalog = database.get_players()
    used_ids: set[int] = set()
    for slot in FORMATION_STARTERS[formation]:
        role = POSITION_ROLE_BY_SLOT[slot]
        player = next(
            item
            for item in catalog
            if item.id not in used_ids
            and card_position_role(item.position) == role
            and (
                item.allowed_lineup_positions is None
                or slot in item.allowed_lineup_positions
            )
        )
        database.add_inventory(user_id, player.id)
        database.set_lineup_position(user_id, slot, player.id)
        used_ids.add(player.id)


class TournamentDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "tournament.sqlite3"
        self.database = Database(self.path)
        self.seed = load_player_seed()
        self.database.initialize(self.seed)
        fill_lineup(self.database, 101, "Balanced")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _play_round(
        self,
        tournament: dict,
        round_number: int,
        *,
        won: bool = True,
        user_id: int | str = 101,
    ) -> dict:
        context = self.database.get_tournament_match_context(
            user_id,
            tournament["tournament_id"],
        )
        self.assertEqual(context["tournament"]["current_round"], round_number)
        opponent = generate_ai_lineup(
            context["catalog"],
            context["formation"],
            round_number,
            rng=random.Random(round_number),
        )
        score = (
            {"home_score": 2, "away_score": 1, "winner_id": "player"}
            if won
            else {"home_score": 1, "away_score": 2, "winner_id": "ai"}
        )
        return self.database.complete_tournament_round(
            user_id,
            tournament["tournament_id"],
            round_number,
            player_lineup=context["player_lineup"],
            opponent_lineup=opponent,
            result=score,
            now=1_800_000_000 + round_number,
        )

    def test_all_formations_get_distinct_valid_ai_lineups_for_each_stage(self) -> None:
        catalog = self.database.get_players()
        for formation, slots in FORMATION_STARTERS.items():
            for round_number, (minimum, maximum) in {
                1: (70, 85),
                2: (86, 95),
                3: (95, 99),
            }.items():
                with self.subTest(formation=formation, round=round_number):
                    lineup = generate_ai_lineup(
                        catalog,
                        formation,
                        round_number,
                        rng=random.Random(round_number),
                    )
                    self.assertEqual([item["slot"] for item in lineup], list(slots))
                    self.assertEqual(len({item["player_id"] for item in lineup}), 3)
                    for starter in lineup:
                        self.assertGreaterEqual(starter["overall"], minimum)
                        self.assertLessEqual(starter["overall"], maximum)
                        self.assertEqual(
                            card_position_role(starter["position"]),
                            POSITION_ROLE_BY_SLOT[starter["slot"]],
                        )

    def test_winning_all_rounds_pays_1600000_once_and_survives_reopen(self) -> None:
        tournament = self.database.start_tournament(101, now=1_800_000_000)
        for round_number in range(1, 4):
            tournament = self._play_round(tournament, round_number)
            expected = round_number * TOURNAMENT_ROUND_REWARD
            if round_number == 3:
                expected += TOURNAMENT_COMPLETION_BONUS
            self.assertEqual(self.database.get_balance(101), expected)

            latest_round = tournament["rounds"][-1]
            duplicate = self.database.complete_tournament_round(
                101,
                tournament["tournament_id"],
                round_number,
                player_lineup=latest_round["player_lineup"],
                opponent_lineup=latest_round["opponent_lineup"],
                result=latest_round["result"],
                now=1_800_000_100 + round_number,
            )
            self.assertEqual(self.database.get_balance(101), expected)
            self.assertEqual(len(duplicate["rounds"]), round_number)

        self.assertEqual(tournament["status"], "COMPLETED")
        self.assertEqual(self.database.get_balance(101), 1_600_000)
        self.assertEqual(
            self.database.get_tournament_cooldown_seconds(
                101,
                now=1_800_000_003,
            ),
            TOURNAMENT_COOLDOWN_SECONDS,
        )
        reopened = Database(self.path)
        reopened.initialize(self.seed)
        saved = reopened.get_tournament_for_user(101)
        self.assertEqual(saved["status"], "COMPLETED")
        self.assertEqual([item["stage"] for item in saved["rounds"]], [
            "Quarterfinals",
            "Semifinals",
            "Finals",
        ])
        self.assertEqual(reopened.get_balance(101), 1_600_000)
        with self.assertRaises(TournamentCooldownActive) as caught:
            reopened.start_tournament(101, now=1_800_000_103)
        self.assertEqual(caught.exception.remaining_seconds, 500)

    def test_each_elimination_round_starts_a_persistent_cooldown(self) -> None:
        base_time = 1_800_000_000
        for loss_round, user_id in ((1, 201), (2, 202), (3, 203)):
            with self.subTest(loss_round=loss_round):
                fill_lineup(self.database, user_id, "Balanced")
                tournament = self.database.start_tournament(
                    user_id,
                    now=base_time,
                )
                for round_number in range(1, loss_round + 1):
                    tournament = self._play_round(
                        tournament,
                        round_number,
                        won=round_number != loss_round,
                        user_id=user_id,
                    )
                ended_at = base_time + loss_round
                self.assertEqual(tournament["status"], "ELIMINATED")
                self.assertEqual(
                    self.database.get_tournament_cooldown_seconds(
                        user_id,
                        now=ended_at,
                    ),
                    TOURNAMENT_COOLDOWN_SECONDS,
                )
                with self.assertRaises(TournamentCooldownActive) as caught:
                    self.database.start_tournament(user_id, now=ended_at + 100)
                self.assertEqual(caught.exception.remaining_seconds, 500)

                reopened = Database(self.path)
                reopened.initialize(self.seed)
                with self.assertRaises(TournamentCooldownActive) as after_restart:
                    reopened.start_tournament(user_id, now=ended_at + 200)
                self.assertEqual(after_restart.exception.remaining_seconds, 400)

    def test_tournament_cooldowns_are_independent_between_players(self) -> None:
        base_time = 1_800_000_000
        tournament = self.database.start_tournament(101, now=base_time)
        tournament = self._play_round(tournament, 1, won=False)
        self.assertEqual(tournament["status"], "ELIMINATED")

        fill_lineup(self.database, 202, "Balanced")
        other_tournament = self.database.start_tournament(
            202,
            now=base_time + 10,
        )
        self.assertEqual(other_tournament["status"], "ACTIVE")
        self.assertEqual(
            self.database.get_tournament_cooldown_seconds(101, now=base_time + 10),
            TOURNAMENT_COOLDOWN_SECONDS - 9,
        )
        self.assertEqual(
            self.database.get_tournament_cooldown_seconds(202, now=base_time + 10),
            0,
        )

    def test_losing_ends_tournament_without_later_rewards(self) -> None:
        tournament = self.database.start_tournament(101)
        tournament = self._play_round(tournament, 1, won=False)
        self.assertEqual(tournament["status"], "ELIMINATED")
        self.assertEqual(self.database.get_balance(101), 0)
        self.assertEqual(len(tournament["rounds"]), 1)
        with self.assertRaises(TournamentNotActive):
            self.database.get_tournament_match_context(
                101,
                tournament["tournament_id"],
            )

    def test_only_one_active_tournament_is_created_under_concurrent_starts(self) -> None:
        barrier = Barrier(2)

        def start() -> dict:
            barrier.wait()
            return self.database.start_tournament(101)

        with ThreadPoolExecutor(max_workers=2) as executor:
            tournaments = list(executor.map(lambda _index: start(), range(2)))
        self.assertEqual(
            tournaments[0]["tournament_id"],
            tournaments[1]["tournament_id"],
        )
        self.assertEqual(self.database.get_tournament_for_user(101)["status"], "ACTIVE")

    def test_start_requires_a_complete_owned_lineup(self) -> None:
        with self.assertRaises(TournamentInvalidLineup):
            self.database.start_tournament(202)
        self.assertIsNone(self.database.get_tournament_for_user(202))

    def test_round_rejects_a_lineup_that_was_no_longer_owned(self) -> None:
        tournament = self.database.start_tournament(101)
        context = self.database.get_tournament_match_context(
            101,
            tournament["tournament_id"],
        )
        opponent = generate_ai_lineup(
            context["catalog"],
            context["formation"],
            1,
            rng=random.Random(1),
        )
        old_card_id = context["player_lineup"][0]["player_id"]
        with self.database._transaction() as connection:
            connection.execute(
                "DELETE FROM inventory WHERE user_id = ? AND player_id = ?",
                ("101", old_card_id),
            )
        with self.assertRaises((TournamentInvalidLineup, TournamentLineupChanged)):
            self.database.complete_tournament_round(
                101,
                tournament["tournament_id"],
                1,
                player_lineup=context["player_lineup"],
                opponent_lineup=opponent,
                result={"home_score": 2, "away_score": 1, "winner_id": "player"},
            )
        self.assertEqual(self.database.get_balance(101), 0)
        self.assertEqual(
            self.database.get_tournament_for_user(101)["rounds"],
            [],
        )

    def test_tournament_difficulty_decreases_each_round(self) -> None:
        probabilities = [
            tournament_scoring_probability(number, 90, 90)
            for number in (1, 2, 3)
        ]
        self.assertGreater(probabilities[0], probabilities[1])
        self.assertGreater(probabilities[1], probabilities[2])
        self.assertEqual(probabilities, [0.62, 0.5, 0.38])

    def test_ai_generation_fails_cleanly_if_a_stage_has_no_cards(self) -> None:
        with self.assertRaises(TournamentAIUnavailable):
            generate_ai_lineup([], "Balanced", 1)


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.done = False
        self.defer_kwargs: dict[str, object] | None = None

    async def send_message(self, content=None, **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})
        self.done = True

    async def edit_message(self, **kwargs) -> None:
        self.messages.append(kwargs)
        self.done = True

    async def defer(self, **kwargs) -> None:
        self.defer_kwargs = kwargs
        self.done = True

    def is_done(self) -> bool:
        return self.done


class FakeMessage:
    async def edit(self, **kwargs) -> None:
        self.edited = kwargs


class TournamentCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "tournament-command.sqlite3")
        self.database.initialize(load_player_seed())
        fill_lineup(self.database, 303, "Balanced")
        self.bot = create_bot(self.database)
        await load_command_modules(self.bot)
        self.cog: TournamentCommands = self.bot.get_cog("TournamentCommands")

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        self.temp_dir.cleanup()

    def make_interaction(self) -> SimpleNamespace:
        response = FakeResponse()

        async def edit_original_response(**kwargs) -> None:
            response.messages.append({"content": None, **kwargs, "edited": True})

        return SimpleNamespace(
            user=SimpleNamespace(id=303, display_name="Tournament Player"),
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=edit_original_response,
            original_response=AsyncMock(return_value=FakeMessage()),
            message=FakeMessage(),
        )

    async def test_command_and_buttons_complete_tournament_and_show_results(self) -> None:
        def player_always_scores(home, away, **_kwargs):
            return run_match(home, away, home_scoring_probability=1.0)

        interaction = self.make_interaction()
        with patch("commands.tournament.simulate_match", side_effect=player_always_scores):
            await TournamentCommands.tournament.callback(self.cog, interaction)
            self.assertEqual(interaction.response.defer_kwargs, {"ephemeral": False})
            payload = interaction.response.messages[0]
            self.assertEqual(payload["embed"].title, "THSL Tournament · Quarterfinals")
            self.assertIn("Victory", payload["embed"].description)
            self.assertEqual(self.database.get_balance(303), TOURNAMENT_ROUND_REWARD)

            view = payload["view"]
            self.assertEqual(view.children[0].label, "Play Semifinals")
            for expected_stage, expected_balance in (
                ("Semifinals", 400_000),
                ("Finals", 1_600_000),
            ):
                click = self.make_interaction()
                await view.children[0].callback(click)
                self.assertEqual(click.response.defer_kwargs, {"ephemeral": False})
                self.assertEqual(
                    self.database.get_balance(303),
                    expected_balance,
                )
                view = click.response.messages[0]["view"]
                result_embed = click.response.messages[0]["embed"]
                self.assertIn(expected_stage, result_embed.title)

            saved = self.database.get_tournament_for_user(303)
            self.assertEqual(saved["status"], "COMPLETED")
            self.assertEqual(len(saved["rounds"]), 3)
            self.assertGreater(
                self.database.get_tournament_cooldown_seconds(303),
                0,
            )

            cooldown_click = self.make_interaction()
            await view.children[0].callback(cooldown_click)
            self.assertEqual(
                cooldown_click.response.defer_kwargs,
                {"ephemeral": False},
            )
            cooldown_click.followup.send.assert_awaited_once()
            cooldown_error = cooldown_click.followup.send.await_args
            self.assertTrue(cooldown_error.args[0].startswith("❌ You must wait "))
            self.assertFalse(cooldown_error.kwargs["ephemeral"])

            reopen = self.make_interaction()
            await TournamentCommands.tournament.callback(self.cog, reopen)
            self.assertEqual(reopen.response.defer_kwargs, {"ephemeral": False})
            reopen_payload = reopen.response.messages[0]
            self.assertIn("You must wait", reopen_payload["embed"].description)

    async def test_replaying_an_expired_or_stale_round_does_not_pay_twice(self) -> None:
        def player_always_scores(home, away, **_kwargs):
            return run_match(home, away, home_scoring_probability=1.0)

        interaction = self.make_interaction()
        with patch("commands.tournament.simulate_match", side_effect=player_always_scores):
            await TournamentCommands.tournament.callback(self.cog, interaction)
            self.assertEqual(interaction.response.defer_kwargs, {"ephemeral": False})
            first_view = interaction.response.messages[0]["view"]
            self.assertEqual(self.database.get_balance(303), TOURNAMENT_ROUND_REWARD)

            semifinal_click = self.make_interaction()
            await first_view.children[0].callback(semifinal_click)
            self.assertEqual(self.database.get_balance(303), 2 * TOURNAMENT_ROUND_REWARD)
            stale_click = self.make_interaction()
            await first_view.children[0].callback(stale_click)
            self.assertEqual(self.database.get_balance(303), 2 * TOURNAMENT_ROUND_REWARD)
            latest = self.database.get_tournament_for_user(303)
            self.assertEqual(len(latest["rounds"]), 2)

    async def test_expired_view_disables_buttons(self) -> None:
        interaction = self.make_interaction()
        tournament = self.database.start_tournament(303)
        from commands.tournament import TournamentView

        button_view = TournamentView(self.cog, 303, tournament)
        message = FakeMessage()
        button_view.bind_message(message)
        await button_view.on_timeout()
        self.assertTrue(button_view.expired)
        self.assertTrue(button_view.children[0].disabled)
        self.assertIsNotNone(message.edited)

