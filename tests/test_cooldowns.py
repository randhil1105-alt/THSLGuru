from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

from commands.cooldowns import CooldownCommands, format_cooldown_time
from database import (
    CLAIM_COOLDOWN_SECONDS,
    DRAFT_COOLDOWN_SECONDS,
    Database,
)
from main import load_player_seed


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, content: str | None = None, **kwargs: object) -> None:
        self.messages.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(self, user_id: int) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()


class CooldownDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "cooldowns.sqlite3"
        self.database = Database(self.database_path)
        self.database.initialize(load_player_seed())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_never_used_cooldowns_are_ready_without_creating_a_user(self) -> None:
        status = self.database.get_cooldown_status("new-user", now=10_000)

        self.assertIsNone(status.claim_last_used_at)
        self.assertIsNone(status.draft_last_started_at)
        self.assertEqual(status.claim_remaining_seconds, 0)
        self.assertEqual(status.draft_remaining_seconds, 0)
        self.assertFalse(status.draft_active)
        self.assertEqual(self.database.get_balance("new-user"), 0)

    def test_active_claim_and_draft_show_exact_remaining_seconds(self) -> None:
        now = 100_000
        self.database.claim_gold_card("timer-user", now=now - 300)
        self.database.start_draft_session("timer-user", "timer-draft", now=now - 360)

        status = self.database.get_cooldown_status("timer-user", now=now)

        self.assertEqual(
            status.claim_remaining_seconds,
            CLAIM_COOLDOWN_SECONDS - 300,
        )
        self.assertEqual(
            status.draft_remaining_seconds,
            DRAFT_COOLDOWN_SECONDS - 360,
        )
        self.assertTrue(status.draft_active)

    def test_saved_timestamps_survive_database_reopen(self) -> None:
        now = 150_000
        self.database.claim_gold_card("persistent-user", now=now - 1_000)
        self.database.start_draft_session(
            "persistent-user",
            "persistent-draft",
            now=now - 240,
        )

        reopened = Database(self.database_path)
        status = reopened.get_cooldown_status("persistent-user", now=now)

        self.assertEqual(status.claim_last_used_at, now - 1_000)
        self.assertEqual(status.draft_last_started_at, now - 240)
        self.assertEqual(status.claim_remaining_seconds, CLAIM_COOLDOWN_SECONDS - 1_000)
        self.assertEqual(status.draft_remaining_seconds, DRAFT_COOLDOWN_SECONDS - 240)
        self.assertTrue(status.draft_active)

    def test_cooldowns_are_separated_by_user(self) -> None:
        now = 200_000
        self.database.claim_gold_card("claim-user", now=now - 120)
        self.database.start_draft_session("draft-user", "only-draft", now=now - 60)

        claim_status = self.database.get_cooldown_status("claim-user", now=now)
        draft_status = self.database.get_cooldown_status("draft-user", now=now)

        self.assertEqual(
            claim_status.claim_remaining_seconds,
            CLAIM_COOLDOWN_SECONDS - 120,
        )
        self.assertIsNone(claim_status.draft_last_started_at)
        self.assertEqual(draft_status.claim_remaining_seconds, 0)
        self.assertEqual(
            draft_status.draft_remaining_seconds,
            DRAFT_COOLDOWN_SECONDS - 60,
        )

    def test_duration_format_includes_hours_minutes_and_seconds(self) -> None:
        self.assertEqual(format_cooldown_time(0), "0s")
        self.assertEqual(format_cooldown_time(73), "1m 13s")
        self.assertEqual(format_cooldown_time(3_661), "1h 1m 1s")


class CooldownCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "cooldown-command.sqlite3")
        self.database.initialize(load_player_seed())

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_command_displays_saved_and_active_game_timers_for_this_user(self) -> None:
        user_id = 42
        now = int(time.time())
        self.database.claim_gold_card(user_id, now=now - 300)
        self.database.start_draft_session(user_id, "display-draft", now=now - 360)
        guess_game = SimpleNamespace(finished=False, remaining_seconds=73)
        minigame_cog = SimpleNamespace(
            _active_games_by_owner={user_id: guess_game},
            _active_drafts_by_owner={},
        )
        bot = SimpleNamespace(get_cog=lambda name: minigame_cog)
        command = CooldownCommands(bot, self.database)  # type: ignore[arg-type]
        interaction = FakeInteraction(user_id)

        await command.cooldowns.callback(command, interaction)  # type: ignore[arg-type]

        message = interaction.response.messages[0]
        self.assertTrue(message["ephemeral"])
        embed = message["embed"]
        assert isinstance(embed, discord.Embed)
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Gold Claim"], "25m 0s remaining")
        self.assertEqual(fields["Guess Who"], "In progress · 1m 13s remaining")
        self.assertEqual(fields["Draft"], "4m 0s remaining")
        self.assertEqual(set(fields), {"Gold Claim", "Guess Who", "Draft"})

    async def test_never_used_user_sees_ready_for_every_timer(self) -> None:
        bot = SimpleNamespace(get_cog=lambda name: None)
        command = CooldownCommands(bot, self.database)  # type: ignore[arg-type]
        interaction = FakeInteraction(999)

        await command.cooldowns.callback(command, interaction)  # type: ignore[arg-type]

        embed = interaction.response.messages[0]["embed"]
        assert isinstance(embed, discord.Embed)
        self.assertEqual(
            {field.name: field.value for field in embed.fields},
            {
                "Gold Claim": "Ready",
                "Guess Who": "Ready",
                "Draft": "Ready",
            },
        )
