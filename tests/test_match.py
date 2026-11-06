from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from commands.init import load_command_modules
from commands.match import MatchCommands, MatchInvitationView, _completed_embed
from database import (
    InvalidMatchResult,
    MATCH_REWARD,
    Database,
    InvalidMatchParticipant,
    MatchActionUnauthorized,
    MatchAlreadyActive,
    card_position_role,
)
from main import create_bot, load_player_seed
from match_simulation import (
    GOAL_CHANCE_PER_TICK,
    MATCH_DURATION_SECONDS,
    MATCH_TICK_SECONDS,
    MatchSide,
    MatchStarter,
    match_timeline,
    play_match_clock,
    simulate_match,
)


class FixedRandom:
    def __init__(self, values: list[float], fallback: float = 1.0) -> None:
        self.values = iter(values)
        self.fallback = fallback

    def random(self) -> float:
        return next(self.values, self.fallback)

    @staticmethod
    def choice(values):
        return values[0]


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, content=None, **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs) -> None:
        self.messages.append(kwargs)


class FakeMessage:
    id = 555

    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []

    async def edit(self, **kwargs) -> None:
        self.edits.append(kwargs)


class MatchSimulationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.home = MatchSide(
            user_id="home",
            username="Home",
            formation="Balanced",
            starters=(
                MatchStarter("WING1", "Home Wing", 88, "WI"),
                MatchStarter("CM1", "Home Center", 87, "C"),
                MatchStarter("GK", "Home Goalie", 90, "GK"),
            ),
        )
        self.away = MatchSide(
            user_id="away",
            username="Away",
            formation="Offensive",
            starters=(
                MatchStarter("WING1", "Away Left", 86, "WI"),
                MatchStarter("WING2", "Away Right", 85, "W"),
                MatchStarter("GK", "Away Goalie", 89, "GK"),
            ),
        )

    def test_goal_generation_uses_a_starting_scorer_and_updates_score(self) -> None:
        result = simulate_match(
            self.home,
            self.away,
            rng=FixedRandom([0.0, GOAL_CHANCE_PER_TICK]),
        )
        first_goal = result.goals[0]
        self.assertEqual(first_goal.kind, "goal")
        self.assertEqual(first_goal.player_name, "Home Wing")
        self.assertEqual(first_goal.team_id, "home")
        self.assertEqual((first_goal.home_score, first_goal.away_score), (1, 0))
        self.assertEqual(result.winner_id, "home")

    def test_own_goal_is_marked_and_awarded_to_the_other_team(self) -> None:
        result = simulate_match(
            self.home,
            self.away,
            rng=FixedRandom([0.0, 0.0]),
        )
        own_goal = result.goals[0]
        self.assertEqual(own_goal.kind, "own_goal")
        self.assertTrue(own_goal.own_goal)
        self.assertEqual(own_goal.player_name, "Home Wing")
        self.assertEqual(own_goal.player_team_id, "home")
        self.assertEqual(own_goal.team_id, "away")
        self.assertEqual(result.winner_id, "away")

    def test_match_always_has_a_goal_and_a_winner(self) -> None:
        result = simulate_match(self.home, self.away, rng=FixedRandom([]))
        self.assertGreaterEqual(len(result.goals), 1)
        self.assertNotEqual(result.home_score, result.away_score)
        self.assertEqual(result.winner_id, "home")
        self.assertEqual(result.duration_seconds, MATCH_DURATION_SECONDS)
        self.assertEqual(result.goals[-1].minute, 90)

    async def test_clock_uses_exact_45_second_schedule_and_four_minute_steps(self) -> None:
        current_time = 0.0
        requested_sleeps: list[float] = []
        updates: list[tuple[float, int]] = []

        async def fake_sleep(duration: float) -> None:
            nonlocal current_time
            requested_sleeps.append(duration)
            current_time += duration

        async def on_tick(elapsed: float, minute: int) -> None:
            updates.append((elapsed, minute))

        await play_match_clock(
            on_tick,
            sleep=fake_sleep,
            monotonic=lambda: current_time,
        )

        self.assertEqual(current_time, MATCH_DURATION_SECONDS)
        self.assertEqual(updates, list(match_timeline()))
        self.assertEqual(updates[0], (0.0, 1))
        self.assertEqual(updates[1], (MATCH_TICK_SECONDS, 5))
        self.assertEqual(updates[-2], (44.0, 89))
        self.assertEqual(updates[-1], (45.0, 90))
        self.assertEqual(sum(requested_sleeps), MATCH_DURATION_SECONDS)


class MatchDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "matches.sqlite3"
        self.database = Database(self.path)
        self.database.initialize(load_player_seed())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _assign_balanced_lineup(self, user_id: str) -> dict[str, str]:
        players_by_role = {}
        for player in self.database.get_players():
            role = card_position_role(player.position)
            if role in {"WING", "CM", "GK"}:
                players_by_role.setdefault(role, player)
        self.assertEqual(set(players_by_role), {"WING", "CM", "GK"})
        assigned = {
            "WING1": players_by_role["WING"],
            "CM1": players_by_role["CM"],
            "GK": players_by_role["GK"],
        }
        for slot, player in assigned.items():
            self.database.add_inventory(user_id, player.name)
            self.database.set_lineup_position(user_id, slot, player.id)
        return {slot: player.name for slot, player in assigned.items()}

    def _invite(self, match_id="match-1", *, challenger="u1", opponent="u2", now=100):
        return self.database.create_match_invitation(
            match_id,
            challenger,
            opponent,
            "Challenger",
            "Opponent",
            "guild",
            "channel",
            now=now,
        )

    def test_invitation_reserves_both_players_and_prevents_duplicates(self) -> None:
        match = self._invite()
        self.assertEqual(match["status"], "PENDING")
        self.assertEqual(self.database.get_active_match_for_user("u1")["match_id"], "match-1")
        self.assertEqual(self.database.get_active_match_for_user("u2")["match_id"], "match-1")
        with self.assertRaises(MatchAlreadyActive):
            self._invite("match-2", challenger="u1", opponent="u3")
        with self.assertRaises(InvalidMatchParticipant):
            self._invite("self-match", challenger="self", opponent="self")

    def test_only_challenged_player_can_accept_or_decline(self) -> None:
        self._invite()
        with self.assertRaises(MatchActionUnauthorized):
            self.database.accept_match_invitation("match-1", "u1", now=110)
        with self.assertRaises(MatchActionUnauthorized):
            self.database.decline_match_invitation("match-1", "u3", now=110)

    def test_invitation_expires_and_releases_both_player_reservations(self) -> None:
        self._invite()
        expired = self.database.accept_match_invitation("match-1", "u2", now=160)
        self.assertEqual(expired["status"], "EXPIRED")
        self.assertIsNone(self.database.get_active_match_for_user("u1"))
        self.assertIsNone(self.database.get_active_match_for_user("u2"))

        fresh = self._invite("match-2", now=200)
        self.assertEqual(fresh["status"], "PENDING")
        self.assertIsNotNone(self.database.expire_match_invitation("match-2", now=260))
        self.assertEqual(self.database.get_match("match-2")["status"], "EXPIRED")

    def test_accepting_with_missing_lineup_cancels_without_reserving_players(self) -> None:
        self._invite()
        match = self.database.accept_match_invitation("match-1", "u2", now=110)
        self.assertEqual(match["status"], "CANCELLED")
        self.assertIn("missing valid starter", match["cancel_reason"])
        self.assertIsNone(self.database.get_active_match_for_user("u1"))
        self.assertIsNone(self.database.get_active_match_for_user("u2"))
        self.assertFalse(
            self.database.complete_match(
                "match-1",
                "u1",
                {"home_score": 1, "away_score": 0},
                now=200,
            )
        )
        self.assertEqual(self.database.get_balance("u1"), 0)

    def test_valid_starting_lineups_are_snapshotted_without_substitutes(self) -> None:
        home = self._assign_balanced_lineup("u1")
        away = self._assign_balanced_lineup("u2")
        self._invite()
        match = self.database.accept_match_invitation("match-1", "u2", now=110)
        self.assertEqual(match["status"], "ACTIVE")
        self.assertEqual(
            {starter["slot"] for starter in match["lineups"]["u1"]["starters"]},
            set(home),
        )
        self.assertEqual(
            {starter["slot"] for starter in match["lineups"]["u2"]["starters"]},
            set(away),
        )
        self.assertEqual(len(match["lineups"]["u1"]["starters"]), 3)
        self.assertEqual(
            {item["slot"] for item in self.database.get_starting_lineup("u1").starters},
            {"WING1", "CM1", "GK"},
        )

    def test_declined_invitation_releases_reservations(self) -> None:
        self._invite()
        match = self.database.decline_match_invitation("match-1", "u2", now=110)
        self.assertEqual(match["status"], "DECLINED")
        self.assertIsNone(self.database.get_active_match_for_user("u1"))
        self.assertIsNone(self.database.get_active_match_for_user("u2"))

    def test_reward_is_atomic_persistent_and_idempotent(self) -> None:
        self._assign_balanced_lineup("u1")
        self._assign_balanced_lineup("u2")
        self._invite()
        self.database.accept_match_invitation("match-1", "u2", now=110)
        result = {
            "home_score": 2,
            "away_score": 1,
            "winner_id": "u1",
            "duration_seconds": 45,
            "goals": [{"text": "winner"}],
        }
        self.assertTrue(
            self.database.complete_match("match-1", "u1", result, now=155)
        )
        self.assertFalse(
            self.database.complete_match("match-1", "u1", result, now=156)
        )
        self.assertEqual(self.database.get_balance("u1"), MATCH_REWARD)
        self.assertEqual(self.database.get_balance("u2"), 0)

        reloaded = Database(self.path)
        reloaded.initialize(load_player_seed())
        match = reloaded.get_match("match-1")
        self.assertEqual(match["status"], "COMPLETED")
        self.assertEqual(match["reward_amount"], MATCH_REWARD)
        self.assertEqual(match["result"], result)
        self.assertEqual(reloaded.get_balance("u1"), MATCH_REWARD)

    def test_database_rejects_a_draw_or_goal_less_completed_result(self) -> None:
        self._assign_balanced_lineup("u1")
        self._assign_balanced_lineup("u2")
        self._invite()
        self.database.accept_match_invitation("match-1", "u2", now=110)
        with self.assertRaises(InvalidMatchResult):
            self.database.complete_match(
                "match-1",
                "u1",
                {
                    "home_score": 0,
                    "away_score": 0,
                    "winner_id": "u1",
                    "goals": [],
                },
                now=150,
            )
        self.assertEqual(self.database.get_match("match-1")["status"], "ACTIVE")
        self.assertEqual(self.database.get_balance("u1"), 0)

    def test_restart_and_user_cancellation_never_award_reward(self) -> None:
        self._assign_balanced_lineup("u1")
        self._assign_balanced_lineup("u2")
        self._invite()
        self.database.accept_match_invitation("match-1", "u2", now=110)
        interrupted = self.database.cancel_interrupted_matches(now=120)
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(interrupted[0]["status"], "CANCELLED")
        self.assertFalse(
            self.database.complete_match(
                "match-1",
                "u1",
                {"home_score": 1, "away_score": 0},
                now=130,
            )
        )
        self.assertEqual(self.database.get_balance("u1"), 0)

        self._invite("match-2", now=200)
        cancelled = self.database.cancel_match_for_user(
            "u2",
            "participant left",
            guild_id="guild",
            now=210,
        )
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(self.database.get_balance("u1"), 0)

    def test_simultaneous_invites_for_one_player_are_serialized(self) -> None:
        barrier = Barrier(2)

        def create(match_id: str, opponent: str) -> str:
            barrier.wait()
            try:
                self.database.create_match_invitation(
                    match_id,
                    "shared-player",
                    opponent,
                    "Shared",
                    opponent,
                    "guild",
                    "channel",
                    now=100,
                )
                return "created"
            except MatchAlreadyActive:
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(
                pool.map(
                    lambda args: create(*args),
                    (("m-a", "opponent-a"), ("m-b", "opponent-b")),
                )
            )
        self.assertCountEqual(outcomes, ["created", "blocked"])
        self.assertIsNotNone(self.database.get_active_match_for_user("shared-player"))

    def test_independent_matches_can_run_and_reward_without_interfering(self) -> None:
        for user_id in ("u1", "u2", "u3", "u4"):
            self._assign_balanced_lineup(user_id)

        first = self._invite("match-a", challenger="u1", opponent="u2")
        second = self._invite("match-b", challenger="u3", opponent="u4")
        first = self.database.accept_match_invitation(
            first["match_id"],
            "u2",
            now=110,
        )
        second = self.database.accept_match_invitation(
            second["match_id"],
            "u4",
            now=110,
        )
        self.assertEqual(first["status"], "ACTIVE")
        self.assertEqual(second["status"], "ACTIVE")

        self.assertTrue(
            self.database.complete_match(
                "match-a",
                "u1",
                {
                    "home_score": 1,
                    "away_score": 0,
                    "winner_id": "u1",
                    "goals": [{"text": "first match goal"}],
                },
                now=155,
            )
        )
        self.assertEqual(self.database.get_match("match-b")["status"], "ACTIVE")
        self.assertEqual(
            self.database.get_active_match_for_user("u3")["match_id"],
            "match-b",
        )

        self.assertTrue(
            self.database.complete_match(
                "match-b",
                "u4",
                {
                    "home_score": 0,
                    "away_score": 1,
                    "winner_id": "u4",
                    "goals": [{"text": "second match goal"}],
                },
                now=156,
            )
        )
        self.assertEqual(self.database.get_balance("u1"), MATCH_REWARD)
        self.assertEqual(self.database.get_balance("u4"), MATCH_REWARD)
        self.assertEqual(self.database.get_balance("u2"), 0)
        self.assertEqual(self.database.get_balance("u3"), 0)


class MatchCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "match-commands.sqlite3")
        self.database.initialize(load_player_seed())
        self.bot = create_bot(self.database)
        await load_command_modules(self.bot)
        self.cog: MatchCommands = self.bot.get_cog("MatchCommands")

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        self.temp_dir.cleanup()

    async def _create_invitation(self, challenger_id=11, opponent_id=22):
        challenger = SimpleNamespace(
            id=challenger_id,
            name="Challenger",
            display_name="Challenger",
        )
        opponent = SimpleNamespace(
            id=opponent_id,
            name="Opponent",
            display_name="Opponent",
            bot=False,
        )
        response = FakeResponse()
        message = FakeMessage()
        interaction = SimpleNamespace(
            user=challenger,
            guild=SimpleNamespace(id=33),
            channel=SimpleNamespace(id=44),
            response=response,
            original_response=AsyncMock(return_value=message),
        )
        await MatchCommands.match.callback(self.cog, interaction, opponent)
        match = self.database.get_active_match_for_user(challenger_id)
        self.assertIsNotNone(match)
        return match, response, message, opponent

    async def test_match_command_sends_challenge_with_both_buttons(self) -> None:
        match, response, _message, opponent = await self._create_invitation()
        self.assertEqual(match["status"], "PENDING")
        self.assertIsNotNone(match["invite_message_id"])
        payload = response.messages[0]
        embed = payload["embed"]
        self.assertIn("Challenger", embed.description)
        self.assertIn(str(opponent.id), embed.description)
        self.assertEqual(
            [button.label for button in payload["view"].children],
            ["Accept Match", "Decline Match"],
        )

    async def test_invitation_uses_discord_usernames_not_server_nicknames(self) -> None:
        user = SimpleNamespace(
            id=11,
            name="challenger_handle",
            display_name="Server nickname",
        )
        opponent = SimpleNamespace(
            id=22,
            name="opponent_handle",
            display_name="Opponent nickname",
            bot=False,
        )
        response = FakeResponse()
        interaction = SimpleNamespace(
            user=user,
            guild=SimpleNamespace(id=33),
            channel=SimpleNamespace(id=44),
            response=response,
            original_response=AsyncMock(return_value=FakeMessage()),
        )

        await MatchCommands.match.callback(self.cog, interaction, opponent)

        description = response.messages[0]["embed"].description
        self.assertIn("challenger_handle", description)
        self.assertNotIn("Server nickname", description)

    async def test_self_challenge_is_rejected(self) -> None:
        user = SimpleNamespace(
            id=11,
            name="Same",
            display_name="Same",
        )
        response = FakeResponse()
        interaction = SimpleNamespace(
            user=user,
            guild=SimpleNamespace(id=33),
            channel=SimpleNamespace(id=44),
            response=response,
        )
        opponent = SimpleNamespace(id=11, bot=False, name="Same", display_name="Same")
        await MatchCommands.match.callback(self.cog, interaction, opponent)
        self.assertIn("cannot challenge yourself", response.messages[0]["content"])
        self.assertIsNone(self.database.get_active_match_for_user(11))

    async def test_unauthorized_invitation_button_press_is_rejected(self) -> None:
        match, response, _message, _opponent = await self._create_invitation()
        view = MatchInvitationView(self.cog, match["match_id"], "22")
        unauthorized_response = FakeResponse()
        allowed = await view.interaction_check(
            SimpleNamespace(
                user=SimpleNamespace(id=99),
                response=unauthorized_response,
            )
        )
        self.assertFalse(allowed)
        self.assertTrue(unauthorized_response.messages[0]["ephemeral"])
        self.assertIn("Only the challenged player", unauthorized_response.messages[0]["content"])

    async def test_declining_invitation_updates_message_and_database(self) -> None:
        match, response, message, _opponent = await self._create_invitation()
        view = response.messages[0]["view"]
        button_interaction_response = FakeResponse()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=22),
            message=message,
            response=button_interaction_response,
        )
        await view.decline(interaction)
        self.assertEqual(self.database.get_match(match["match_id"])["status"], "DECLINED")
        self.assertIn("Match declined", button_interaction_response.messages[0]["embed"].title)
        self.assertIsNone(self.database.get_active_match_for_user(11))

    async def test_accepting_without_both_lineups_cancels_and_explains(self) -> None:
        match, response, message, _opponent = await self._create_invitation()
        view = response.messages[0]["view"]
        button_interaction_response = FakeResponse()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=22),
            message=message,
            response=button_interaction_response,
        )
        await view.accept(interaction)
        saved = self.database.get_match(match["match_id"])
        self.assertEqual(saved["status"], "CANCELLED")
        self.assertIn(
            "Invalid starting lineup",
            button_interaction_response.messages[0]["embed"].description,
        )
        self.assertFalse(button_interaction_response.messages[0].get("view"))
        self.assertEqual(self.database.get_balance(11), 0)

    async def test_accepting_valid_lineups_starts_the_live_match(self) -> None:
        for user_id in ("11", "22"):
            by_role = {}
            for player in self.database.get_players():
                role = card_position_role(player.position)
                if role in {"WING", "CM", "GK"}:
                    by_role.setdefault(role, player)
            for slot, role in (("WING1", "WING"), ("CM1", "CM"), ("GK", "GK")):
                player = by_role[role]
                self.database.add_inventory(user_id, player.name)
                self.database.set_lineup_position(user_id, slot, player.id)

        match, response, message, _opponent = await self._create_invitation()
        view = response.messages[0]["view"]
        button_response = FakeResponse()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=22),
            message=message,
            response=button_response,
        )
        with patch.object(self.cog, "start_match_task") as start_task:
            await view.accept(interaction)
        self.assertEqual(self.database.get_match(match["match_id"])["status"], "ACTIVE")
        start_task.assert_called_once()
        self.assertEqual(button_response.messages[0]["embed"].title, "Match in progress · 1'")

    async def test_bot_restart_cancels_live_match_without_reward(self) -> None:
        for user_id in ("11", "22"):
            by_role = {}
            for player in self.database.get_players():
                role = card_position_role(player.position)
                if role in {"WING", "CM", "GK"}:
                    by_role.setdefault(role, player)
            for slot, role in (("WING1", "WING"), ("CM1", "CM"), ("GK", "GK")):
                player = by_role[role]
                self.database.add_inventory(user_id, player.name)
                self.database.set_lineup_position(user_id, slot, player.id)
        match, _response, _message, _opponent = await self._create_invitation()
        active = self.database.accept_match_invitation(
            match["match_id"],
            "22",
        )
        with patch.object(self.cog, "_edit_match_message", new_callable=AsyncMock):
            await self.cog.on_ready()
        saved = self.database.get_match(match["match_id"])
        self.assertEqual(active["status"], "ACTIVE")
        self.assertEqual(saved["status"], "CANCELLED")
        self.assertEqual(self.database.get_balance(11), 0)

    async def test_departed_participant_cancels_without_reward(self) -> None:
        for user_id in ("11", "22"):
            by_role = {}
            for player in self.database.get_players():
                role = card_position_role(player.position)
                if role in {"WING", "CM", "GK"}:
                    by_role.setdefault(role, player)
            for slot, role in (("WING1", "WING"), ("CM1", "CM"), ("GK", "GK")):
                player = by_role[role]
                self.database.add_inventory(user_id, player.name)
                self.database.set_lineup_position(user_id, slot, player.id)
        match, _response, message, _opponent = await self._create_invitation()
        active = self.database.accept_match_invitation(
            match["match_id"],
            "22",
        )

        async def fast_clock(on_tick) -> None:
            for elapsed, minute in match_timeline():
                await on_tick(elapsed, minute)

        with (
            patch("commands.match.play_match_clock", side_effect=fast_clock),
            patch.object(self.cog, "_participants_are_present", new_callable=AsyncMock, return_value=False),
        ):
            await self.cog._run_match(active, message)
        saved = self.database.get_match(match["match_id"])
        self.assertEqual(saved["status"], "CANCELLED")
        self.assertEqual(self.database.get_balance(11), 0)
        self.assertTrue(message.edits)

    async def test_final_result_displays_scorers_lineups_winner_and_reward(self) -> None:
        match = {
            "challenger_id": "11",
            "opponent_id": "22",
            "challenger_name": "Challenger",
            "opponent_name": "Opponent",
            "lineups": {
                "11": {
                    "formation": "Balanced",
                    "starters": [
                        {"slot": "WING1", "name": "Home Wing", "overall": 88, "position": "WI"},
                        {"slot": "CM1", "name": "Home Center", "overall": 87, "position": "C"},
                        {"slot": "GK", "name": "Home Goalie", "overall": 90, "position": "GK"},
                    ],
                },
                "22": {
                    "formation": "Offensive",
                    "starters": [
                        {"slot": "WING1", "name": "Away Left", "overall": 86, "position": "WI"},
                        {"slot": "WING2", "name": "Away Right", "overall": 85, "position": "W"},
                        {"slot": "GK", "name": "Away Goalie", "overall": 89, "position": "GK"},
                    ],
                },
            },
            "winner_id": "22",
            "result": {
                "home_score": 1,
                "away_score": 2,
                "winner_id": "22",
                "duration_seconds": 45,
                "goals": [
                    {
                        "minute": 20,
                        "kind": "goal",
                        "text": "GOAL! Home Wing (Challenger) puts it in the net.",
                        "home_score": 1,
                        "away_score": 0,
                    },
                    {
                        "minute": 90,
                        "kind": "own_goal",
                        "text": "OWN GOAL! Away Goalie (Opponent) scores for Challenger.",
                        "home_score": 1,
                        "away_score": 1,
                    },
                    {
                        "minute": 90,
                        "kind": "sudden_death_goal",
                        "text": "Sudden-death goal! Away Left (Opponent) scores the winner.",
                        "home_score": 1,
                        "away_score": 2,
                    },
                ],
            },
        }
        embed = _completed_embed(match)
        rendered = "\n".join(
            [embed.description or "", *(f"{field.name}\n{field.value}" for field in embed.fields)]
        )
        self.assertIn("Challenger", rendered)
        self.assertIn("Opponent", rendered)
        self.assertIn("Home Wing", rendered)
        self.assertIn("OWN GOAL", rendered)
        self.assertIn("Away Left", rendered)
        self.assertIn("1–2", rendered)
        self.assertIn(f"{MATCH_REWARD:,} coins", rendered)
        self.assertIn("45 seconds", rendered)


if __name__ == "__main__":
    unittest.main()
