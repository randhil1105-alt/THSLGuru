from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from database import (
    GUESS_WHO_COOLDOWN_SECONDS,
    Database,
    GuessWhoCooldownActive,
)
from main import load_player_seed


class GuessWhoCooldownDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "guess-who.sqlite3"
        self.database = Database(self.path)
        self.database.initialize(load_player_seed())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_correct_and_incorrect_completions_start_five_minute_cooldowns(self) -> None:
        for user_id, game_id, correct in (
            ("winner", "correct-game", True),
            ("loser", "incorrect-game", False),
        ):
            with self.subTest(correct=correct):
                self.database.complete_guess_who(
                    user_id,
                    game_id,
                    correct=correct,
                    coin_reward=500_000,
                    now=1_000,
                )
                self.assertEqual(
                    self.database.get_guess_who_cooldown_seconds(
                        user_id,
                        now=1_000,
                    ),
                    GUESS_WHO_COOLDOWN_SECONDS,
                )

    def test_timeout_completion_is_recorded_once(self) -> None:
        self.assertTrue(
            self.database.finish_guess_who_timeout(
                "timeout-player",
                "timeout-game",
                now=2_000,
            )
        )
        self.assertFalse(
            self.database.finish_guess_who_timeout(
                "timeout-player",
                "timeout-game",
                now=2_100,
            )
        )
        self.assertEqual(
            self.database.get_guess_who_cooldown_seconds(
                "timeout-player",
                now=2_020,
            ),
            280,
        )

    def test_cooldown_survives_reopen_and_expires_at_five_minutes(self) -> None:
        self.database.complete_guess_who(
            "returning-player",
            "finished-game",
            correct=False,
            coin_reward=500_000,
            now=1_000,
        )
        reopened = Database(self.path)
        reopened.initialize(load_player_seed())

        with self.assertRaises(GuessWhoCooldownActive) as caught:
            reopened.check_guess_who_cooldown("returning-player", now=1_028)
        self.assertEqual(caught.exception.remaining_seconds, 272)
        self.assertEqual(
            reopened.get_guess_who_cooldown_seconds(
                "returning-player",
                now=1_299,
            ),
            1,
        )
        self.assertEqual(
            reopened.get_guess_who_cooldown_seconds(
                "returning-player",
                now=1_300,
            ),
            0,
        )
        reopened.check_guess_who_cooldown("returning-player", now=1_300)

    def test_cooldowns_are_per_player_and_concurrent_checks_cannot_bypass_them(self) -> None:
        self.database.finish_guess_who_timeout(
            "cooling-player",
            "completed-game",
            now=3_000,
        )
        self.database.check_guess_who_cooldown("other-player", now=3_001)

        barrier = Barrier(2)

        def check() -> str:
            barrier.wait()
            try:
                self.database.check_guess_who_cooldown(
                    "cooling-player",
                    now=3_001,
                )
            except GuessWhoCooldownActive:
                return "blocked"
            return "allowed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: check(), range(2)))
        self.assertEqual(results, ["blocked", "blocked"])


if __name__ == "__main__":
    unittest.main()
