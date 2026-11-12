from __future__ import annotations

import tempfile
import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from commands.daily import DailyCommands
from commands.init import load_command_modules
from commands.quests import QuestCommands
from database import (
    DAILY_REWARD_COINS,
    DAILY_REWARD_COOLDOWN_SECONDS,
    GOLD_PACK_COST,
    MATCH_REWARD,
    DailyCooldownActive,
    DailyQuestAlreadyClaimed,
    DailyQuestNotFound,
    Database,
)
from main import create_bot, load_player_seed
from xp_system import LEVEL_UP_COIN_REWARD, xp_required_for_level


class ChooseQuests:
    def __init__(self, keys: tuple[str, ...]) -> None:
        self.keys = keys

    def sample(self, population, *, k):
        choices = {quest.key: quest for quest in population}
        if len(self.keys) != k:
            raise AssertionError("Test quest selection must match the configured set size.")
        return [choices[key] for key in self.keys]


class ChooseFirstCard:
    def choices(self, population, *, weights, k):
        del weights, k
        return [population[0]]


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, content=None, **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs) -> None:
        self.messages.append(kwargs)


class ProgressionDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "progression.sqlite3"
        self.database = Database(self.path)
        self.database.initialize(load_player_seed())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def set_total_xp(self, user_id: str, total_xp: int) -> None:
        with self.database._transaction() as connection:
            self.database._ensure_user(connection, user_id)
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

    def test_level_coins_cover_each_level_and_are_idempotent(self) -> None:
        self.database.set_balance("single-level", 77)
        self.set_total_xp("single-level", xp_required_for_level(2) - 1)
        one_level = self.database.award_xp(
            "single-level",
            "level-two",
            1,
            "test",
            now=100,
        )
        self.assertEqual((one_level.level_before, one_level.level_after), (1, 2))
        self.assertEqual(self.database.get_balance("single-level"), 77 + 3_000_000)

        multi_user = "eight-to-twelve"
        self.database.set_balance(multi_user, 10)
        self.set_total_xp(multi_user, xp_required_for_level(8))
        jump = self.database.award_xp(
            multi_user,
            "skip-four-levels",
            xp_required_for_level(12) - xp_required_for_level(8),
            "test",
            now=101,
        )
        self.assertEqual((jump.level_before, jump.level_after), (8, 12))
        self.assertEqual(
            self.database.get_balance(multi_user),
            10 + 4 * LEVEL_UP_COIN_REWARD,
        )
        self.assertEqual(
            self.database.get_free_pack_inventory(multi_user),
            {"GOLD": 4, "TOTW": 1},
        )
        self.assertFalse(
            self.database.award_xp(
                multi_user,
                "skip-four-levels",
                xp_required_for_level(12) - xp_required_for_level(8),
                "test",
                now=102,
            ).is_new
        )
        self.assertEqual(
            self.database.get_balance(multi_user),
            10 + 4 * LEVEL_UP_COIN_REWARD,
        )
        with self.database._read_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT amount FROM level_up_coin_rewards WHERE event_id = ?",
                    ("skip-four-levels",),
                ).fetchone()["amount"],
                12_000_000,
            )

    def test_daily_claim_awards_coins_and_totw_with_independent_cooldowns(self) -> None:
        now = 1_700_000_000
        self.database.set_balance("daily-a", 25)
        first = self.database.claim_daily_reward("daily-a", "daily-a-1", now=now)
        other = self.database.claim_daily_reward("daily-b", "daily-b-1", now=now + 1)
        self.assertTrue(first["awarded"])
        self.assertEqual(first["coins"], DAILY_REWARD_COINS)
        self.assertEqual(first["totw_packs"], 1)
        self.assertEqual(self.database.get_balance("daily-a"), 25 + DAILY_REWARD_COINS)
        self.assertEqual(
            self.database.get_free_pack_inventory("daily-a"),
            {"GOLD": 0, "TOTW": 1},
        )
        self.assertTrue(other["awarded"])
        with self.assertRaises(DailyCooldownActive) as cooldown:
            self.database.claim_daily_reward(
                "daily-a",
                "daily-a-too-soon",
                now=now + DAILY_REWARD_COOLDOWN_SECONDS - 1,
            )
        self.assertEqual(cooldown.exception.remaining_seconds, 1)

        next_claim = self.database.claim_daily_reward(
            "daily-a",
            "daily-a-2",
            now=now + DAILY_REWARD_COOLDOWN_SECONDS,
        )
        self.assertTrue(next_claim["awarded"])
        replay = self.database.claim_daily_reward(
            "daily-a",
            "daily-a-1",
            now=now + 2 * DAILY_REWARD_COOLDOWN_SECONDS,
        )
        self.assertFalse(replay["awarded"])
        self.assertTrue(replay["duplicate"])
        self.assertEqual(
            self.database.get_balance("daily-a"),
            25 + 2 * DAILY_REWARD_COINS,
        )
        self.assertEqual(
            self.database.get_free_pack_inventory("daily-a"),
            {"GOLD": 0, "TOTW": 2},
        )

    def test_concurrent_daily_claims_award_only_once(self) -> None:
        now = 1_700_000_100

        def claim(index: int):
            try:
                return self.database.claim_daily_reward(
                    "daily-race",
                    f"daily-race-{index}",
                    now=now,
                )
            except DailyCooldownActive:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))
        self.assertEqual(sum(bool(result and result["awarded"]) for result in results), 1)
        self.assertEqual(self.database.get_balance("daily-race"), DAILY_REWARD_COINS)
        self.assertEqual(
            self.database.get_free_pack_inventory("daily-race"),
            {"GOLD": 0, "TOTW": 1},
        )

    def test_random_quest_set_persists_through_reopen_and_resets_at_utc_midnight(self) -> None:
        now = 10_000
        keys = ("packs_open_2", "gold_card_1", "match_win_1")
        quests = self.database.get_daily_quests(
            "quest-packs",
            now=now,
            rng=ChooseQuests(keys),
        )
        self.assertEqual({quest["quest_key"] for quest in quests}, set(keys))
        self.database.set_balance("quest-packs", GOLD_PACK_COST)
        self.database.open_pack(
            "quest-packs",
            "GOLD",
            GOLD_PACK_COST,
            rng=ChooseFirstCard(),
            purchase_id="gold-pack-once",
            now=now,
        )
        progress = {
            quest["quest_key"]: quest
            for quest in self.database.get_daily_quests("quest-packs", now=now)
        }
        self.assertEqual(progress["packs_open_2"]["progress"], 1)
        self.assertEqual(progress["gold_card_1"]["progress"], 1)

        reopened = Database(self.path)
        reopened.initialize(load_player_seed())
        self.assertEqual(
            {
                quest["quest_key"]: quest["progress"]
                for quest in reopened.get_daily_quests("quest-packs", now=now)
            },
            {
                "packs_open_2": 1,
                "gold_card_1": 1,
                "match_win_1": 0,
            },
        )
        new_day = reopened.get_daily_quests(
            "quest-packs",
            now=now + 86_400,
            rng=ChooseQuests(("trade_1", "trade_2", "minigame_play_2")),
        )
        self.assertEqual({quest["day_utc"] for quest in new_day}, {"1970-01-02"})
        self.assertTrue(all(quest["progress"] == 0 for quest in new_day))

    def test_completed_quest_can_be_claimed_once_and_expires_at_midnight(self) -> None:
        user_id = "quest-claim"
        self.database.get_daily_quests(
            user_id,
            now=86_399,
            rng=ChooseQuests(("match_win_1", "match_win_3", "match_goals_5")),
        )
        with self.database._transaction() as connection:
            self.database._record_quest_event_locked(
                connection,
                user_id=user_id,
                event_id="completed-before-midnight",
                metric="match_win",
                amount=1,
                now=86_399,
            )
        result = self.database.claim_daily_quest(
            user_id,
            "match_win_1",
            now=86_399,
        )
        self.assertEqual(result["reward"], 5_000_000)
        self.assertEqual(self.database.get_balance(user_id), 5_000_000)
        with self.assertRaises(DailyQuestAlreadyClaimed):
            self.database.claim_daily_quest(
                user_id,
                "match_win_1",
                now=86_399,
            )
        with self.assertRaises(DailyQuestNotFound):
            self.database.claim_daily_quest(
                user_id,
                "match_win_1",
                now=86_400,
            )

    def test_completed_minigames_automatically_update_play_and_win_quests(self) -> None:
        user_id = "minigame-quest"
        now = 1_700_000_000
        self.database.get_daily_quests(
            user_id,
            now=now,
            rng=ChooseQuests(
                ("minigame_play_2", "minigame_win_1", "trade_1")
            ),
        )
        self.database.complete_guess_who(
            user_id,
            "guess-wrong",
            correct=False,
            coin_reward=500_000,
            now=now,
        )
        self.database.complete_guess_who(
            user_id,
            "guess-correct",
            correct=True,
            coin_reward=500_000,
            now=now + 1,
        )
        progress = {
            quest["quest_key"]: quest["progress"]
            for quest in self.database.get_daily_quests(user_id, now=now + 1)
        }
        self.assertEqual(progress["minigame_play_2"], 2)
        self.assertEqual(progress["minigame_win_1"], 1)

    def test_completed_match_records_winner_and_scored_goals_only_once(self) -> None:
        now = 1_700_000_000
        for user_id, keys in (
            ("match-winner", ("match_win_1", "match_goals_5", "trade_1")),
            ("match-loser", ("match_win_1", "trade_1", "trade_2")),
        ):
            self.database.get_daily_quests(
                user_id,
                now=now,
                rng=ChooseQuests(keys),
            )

        roles = {}
        for player in self.database.get_players():
            role = player.position
            if role == "GK":
                roles.setdefault("GK", player)
            elif role in {"W", "WI"}:
                roles.setdefault("WING", player)
            elif role == "C":
                roles.setdefault("CM", player)
        for user_id in ("match-winner", "match-loser"):
            for slot, role in (("WING1", "WING"), ("CM1", "CM"), ("GK", "GK")):
                player = roles[role]
                self.database.add_inventory(user_id, player.name)
                self.database.set_lineup_position(user_id, slot, player.id)

        self.database.create_match_invitation(
            "quest-match",
            "match-winner",
            "match-loser",
            "Winner",
            "Loser",
            "guild",
            "channel",
            now=now,
        )
        self.database.accept_match_invitation(
            "quest-match",
            "match-loser",
            now=now + 1,
        )
        result = {
            "home_score": 5,
            "away_score": 1,
            "winner_id": "match-winner",
            "duration_seconds": 45,
            "goals": [{"text": "a goal"}],
        }
        self.assertTrue(
            self.database.complete_match(
                "quest-match",
                "match-winner",
                result,
                now=now + 2,
            )
        )
        self.assertFalse(
            self.database.complete_match(
                "quest-match",
                "match-winner",
                result,
                now=now + 3,
            )
        )
        winner_quests = {
            quest["quest_key"]: quest["progress"]
            for quest in self.database.get_daily_quests(
                "match-winner",
                now=now + 3,
            )
        }
        loser_quests = {
            quest["quest_key"]: quest["progress"]
            for quest in self.database.get_daily_quests(
                "match-loser",
                now=now + 3,
            )
        }
        self.assertEqual(winner_quests["match_win_1"], 1)
        self.assertEqual(winner_quests["match_goals_5"], 5)
        self.assertEqual(loser_quests["match_win_1"], 0)
        self.assertEqual(self.database.get_balance("match-winner"), MATCH_REWARD)

    def test_only_completed_trades_advance_both_players_quests(self) -> None:
        now = 1_700_000_000
        for user_id, keys in (
            ("trader-one", ("trade_1", "trade_2", "match_win_1")),
            ("trader-two", ("trade_1", "trade_2", "match_win_1")),
        ):
            self.database.get_daily_quests(
                user_id,
                now=now,
                rng=ChooseQuests(keys),
            )
        self.database.add_inventory("trader-one", "Dan")
        self.database.create_trade_request(
            "quest-trade",
            "trader-one",
            "trader-two",
            "One",
            "Two",
            "guild",
            "channel",
            now=now,
        )
        self.database.accept_trade_request("quest-trade", "trader-two", now=now + 1)
        player = self.database.get_player("Dan")
        assert player is not None
        self.database.add_trade_card("quest-trade", "trader-one", player.id, now=now + 2)
        self.database.confirm_trade("quest-trade", "trader-one", now=now + 3)
        first_user_quests = self.database.get_daily_quests(
            "trader-one",
            now=now + 3,
        )
        self.assertEqual(
            sum(int(quest["progress"]) for quest in first_user_quests),
            0,
        )
        completed = self.database.confirm_trade(
            "quest-trade",
            "trader-two",
            now=now + 4,
        )
        self.assertTrue(completed["completed"])
        for user_id in ("trader-one", "trader-two"):
            progress = {
                quest["quest_key"]: quest["progress"]
                for quest in self.database.get_daily_quests(
                    user_id,
                    now=now + 4,
                )
            }
            self.assertEqual(progress["trade_1"], 1)
            self.assertEqual(progress["trade_2"], 1)

        claimed = self.database.claim_daily_quest(
            "trader-one",
            "trade_1",
            now=now + 4,
        )
        self.assertEqual(claimed["reward"], 5_000_000)
        self.assertEqual(self.database.get_balance("trader-one"), 5_000_000)
        with self.assertRaises(DailyQuestAlreadyClaimed):
            self.database.claim_daily_quest(
                "trader-one",
                "trade_1",
                now=now + 4,
            )

    def test_draft_completion_advances_minigame_quests(self) -> None:
        user_id = "draft-quest"
        now = 1_700_000_000
        self.database.get_daily_quests(
            user_id,
            now=now,
            rng=ChooseQuests(("minigame_play_2", "minigame_win_1", "trade_1")),
        )
        self.database.start_draft_session(user_id, "quest-draft", now=now)
        completed, _balance = self.database.finish_draft_session(
            "quest-draft",
            user_id,
            won=True,
            result={"player_won": True},
            now=now + 1,
        )
        self.assertTrue(completed)
        progress = {
            quest["quest_key"]: quest["progress"]
            for quest in self.database.get_daily_quests(user_id, now=now + 1)
        }
        self.assertEqual(progress["minigame_play_2"], 1)
        self.assertEqual(progress["minigame_win_1"], 1)


class ProgressionCommandRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_and_quests_are_public_slash_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "commands.sqlite3")
            database.initialize(load_player_seed())
            bot = create_bot(database)
            try:
                await load_command_modules(bot)
                for name in ("daily", "quests"):
                    command = bot.tree.get_command(name)
                    self.assertIsNotNone(command)
                    self.assertIsNone(command.default_permissions)
            finally:
                await bot.close()


class ProgressionCommandResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "responses.sqlite3")
        self.database.initialize(load_player_seed())

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_daily_claim_and_cooldown_messages_are_public(self) -> None:
        command = DailyCommands(self.database)
        user = SimpleNamespace(id=501, mention="<@501>")
        first = SimpleNamespace(
            id=10_001,
            user=user,
            response=FakeResponse(),
        )
        await DailyCommands.daily.callback(command, first)
        success = first.response.messages[0]
        self.assertFalse(success["ephemeral"])
        self.assertEqual(success["embed"].title, "🎁 DAILY REWARDS CLAIMED!")
        self.assertIn("5,000,000 coins", success["embed"].description)
        self.assertIn("1x TOTW Pack", success["embed"].description)

        second = SimpleNamespace(
            id=10_002,
            user=user,
            response=FakeResponse(),
        )
        await DailyCommands.daily.callback(command, second)
        cooldown = second.response.messages[0]
        self.assertFalse(cooldown["ephemeral"])
        self.assertIn("claim again in", cooldown["content"])

    async def test_quests_board_and_successful_claim_are_public(self) -> None:
        now = int(time.time())
        user = SimpleNamespace(id=502, mention="<@502>")
        self.database.get_daily_quests(
            user.id,
            now=now,
            rng=ChooseQuests(("match_win_1", "packs_open_2", "trade_1")),
        )
        with self.database._transaction() as connection:
            self.database._record_quest_event_locked(
                connection,
                user_id=str(user.id),
                event_id="quest-ui-win",
                metric="match_win",
                amount=1,
                now=now,
            )

        command = QuestCommands(self.database)
        interaction = SimpleNamespace(
            user=user,
            response=FakeResponse(),
        )
        await QuestCommands.quests.callback(command, interaction)
        board = interaction.response.messages[0]
        self.assertFalse(board["ephemeral"])
        self.assertEqual(board["embed"].title, "📜 DAILY QUESTS")
        self.assertEqual(len(board["view"].children), 1)
        self.assertIn("✅ Completed", board["embed"].fields[0].value)

        button = board["view"].children[0]
        claim_interaction = SimpleNamespace(
            user=user,
            response=FakeResponse(),
        )
        await button.callback(claim_interaction)
        updated = claim_interaction.response.messages[0]
        self.assertIn("claimed!", updated["embed"].description)
        self.assertEqual(updated["view"], None)
        self.assertEqual(self.database.get_balance(user.id), 5_000_000)


if __name__ == "__main__":
    unittest.main()
