from __future__ import annotations

import tempfile
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from commands.admin import AdminCommands
from commands.init import COMMAND_MODULES, load_command_modules
from commands.common import resolve_card_image
from commands.lineup_image import OUTPUT_FILENAME, build_lineup_svg
from commands.packs import PackCommands
from commands.team import TeamCommands
from database import Database, GOLD_PACK_COST, LINEUP_POSITIONS, TOTW_PACK_COST
from main import create_bot, load_player_seed

EXPECTED_COMMANDS = {
    "player",
    "card",
    "claim",
    "balance",
    "leaderboard",
    "inventory",
    "collection",
    "goldpack",
    "totwpack",
    "packs",
    "team",
    "set",
    "formation",
    "sell",
    "help",
    "botinfo",
    "addplayer",
    "setbalance",
    "grantcoins",
    "grantcard",
}


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.deferred = False

    async def send_message(
        self,
        content: str | None = None,
        **kwargs: object,
    ) -> None:
        self.messages.append({"content": content, **kwargs})

    async def defer(self, **kwargs: object) -> None:
        self.deferred = True


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        self.messages.append({"content": content, **kwargs})


class FakeMessage:
    async def edit(self, **kwargs: object) -> None:
        self.edited = kwargs


class SlashCommandLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "commands.sqlite3")
        self.database.initialize(load_player_seed())
        self.bot = create_bot(self.database)

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        self.temp_dir.cleanup()

    async def test_modules_load_once_and_register_expected_commands(self) -> None:
        self.assertEqual(len(COMMAND_MODULES), len(set(COMMAND_MODULES)))
        await load_command_modules(self.bot)
        names_before = [command.name for command in self.bot.tree.get_commands()]
        self.assertEqual(set(names_before), EXPECTED_COMMANDS)
        self.assertEqual(len(names_before), len(set(names_before)))

        loaded_extensions = set(self.bot.extensions)
        await load_command_modules(self.bot)
        names_after = [command.name for command in self.bot.tree.get_commands()]

        self.assertEqual(set(self.bot.extensions), loaded_extensions)
        self.assertEqual(names_after, names_before)

    async def test_setup_hook_syncs_commands_globally(self) -> None:
        with patch.object(
            self.bot.tree,
            "sync",
            new_callable=AsyncMock,
            return_value=[],
        ) as sync:
            await self.bot.setup_hook()

        sync.assert_awaited_once_with()
        self.assertEqual(
            {command.name for command in self.bot.tree.get_commands()},
            EXPECTED_COMMANDS,
        )

    async def test_setup_hook_syncs_to_configured_development_guild(self) -> None:
        guild = discord.Object(id=123456789012345678)
        self.bot.development_guild_id = guild.id
        with (
            patch.object(self.bot.tree, "copy_global_to") as copy_global_to,
            patch.object(
                self.bot.tree,
                "sync",
                new_callable=AsyncMock,
                return_value=[],
            ) as sync,
        ):
            await self.bot.setup_hook()

        copy_global_to.assert_called_once_with(guild=guild)
        sync.assert_awaited_once_with(guild=guild)

    async def test_default_permissions_are_public_except_admin_commands(self) -> None:
        await load_command_modules(self.bot)
        commands = {
            command.name: command for command in self.bot.tree.get_commands()
        }
        admin_only = {"addplayer", "setbalance", "grantcoins", "grantcard"}
        self.assertEqual(set(commands), EXPECTED_COMMANDS)

        for name, command in commands.items():
            with self.subTest(command=name):
                if name in admin_only:
                    self.assertIsNotNone(command.default_permissions)
                    assert command.default_permissions is not None
                    self.assertEqual(
                        command.default_permissions.value,
                        discord.Permissions(administrator=True).value,
                    )
                    self.assertTrue(command.checks)
                else:
                    self.assertIsNone(command.default_permissions)
                    self.assertFalse(command.checks)

        set_parameters = {
            parameter.name: parameter for parameter in commands["set"].parameters
        }
        self.assertEqual(
            [choice.value for choice in set_parameters["position"].choices],
            list(LINEUP_POSITIONS),
        )

    async def test_all_seeded_card_images_resolve(self) -> None:
        for name in ("Dan", "Licha", "Hala"):
            with self.subTest(name=name):
                player = self.database.get_player(name)
                self.assertIsNotNone(player)
                assert player is not None
                image = resolve_card_image(player)
                self.assertIsNotNone(image)
                assert image is not None
                self.assertTrue(image.is_file())

        self.assertEqual(self.database.get_player("Licha").rarity, "TOTW")
        self.assertEqual(self.database.get_player("Hala").rarity, "TOTW")

    async def test_lineup_rink_svg_uses_ice_and_hockey_markings(self) -> None:
        svg = build_lineup_svg({}, "Balanced", "Not rated yet")

        self.assertIn('stop-color="#f7fafc"', svg)
        self.assertIn('stop-color="#e5edf2"', svg)
        self.assertIn('stroke="#d52b3e"', svg)
        self.assertEqual(svg.count('stroke="#2867a6"'), 2)
        self.assertIn('x1="800" y1="165" x2="800" y2="795"', svg)
        self.assertIn('cx="800" cy="480" r="112"', svg)
        self.assertEqual(svg.count('r="68" fill="none"'), 4)
        self.assertIn('cx="360" cy="330" r="68"', svg)
        self.assertIn('cx="1240" cy="630" r="68"', svg)
        self.assertIn('x1="152" y1="165" x2="152" y2="795"', svg)
        self.assertIn('fill-opacity="0.12" stroke="#d52b3e"', svg)
        self.assertIn('width="1484" height="670" rx="90"', svg)

    async def test_packs_menu_shows_odds_and_confirms_once(self) -> None:
        user_id = "packs-menu-user"
        self.database.set_balance(user_id, GOLD_PACK_COST + 250)
        command_response = FakeResponse()
        command_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=command_response,
        )

        await PackCommands.packs.callback(
            PackCommands(self.database),
            command_interaction,
        )

        menu = command_response.messages[0]
        self.assertTrue(menu["ephemeral"])
        field_values = {field.name: field.value for field in menu["embed"].fields}
        self.assertIn("Dan", field_values["GOLD · 5,000,000 coins"])
        self.assertIn("100.0%", field_values["GOLD · 5,000,000 coins"])
        self.assertIn("Licha", field_values["TOTW · 20,000,000 coins"])
        self.assertIn("50.0%", field_values["TOTW · 20,000,000 coins"])

        view = menu["view"]
        gold_button = next(
            button for button in view.children if button.rarity == "GOLD"
        )
        purchase_response = FakeResponse()
        purchase_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=purchase_response,
            id=10001,
            message=FakeMessage(),
        )
        await gold_button.callback(purchase_interaction)

        self.assertIn("opened a **GOLD pack**", purchase_response.messages[0]["content"])
        self.assertEqual(self.database.get_balance(user_id), 250)
        self.assertEqual(
            [(entry.player.name, entry.quantity) for entry in self.database.get_inventory(user_id)],
            [("Dan", 1)],
        )
        uploaded_file = purchase_response.messages[0]["file"]
        self.assertIsNotNone(uploaded_file)

        duplicate_response = FakeResponse()
        duplicate_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=duplicate_response,
            id=10002,
            message=FakeMessage(),
        )
        await gold_button.callback(duplicate_interaction)
        self.assertIn("already been used", duplicate_response.messages[0]["content"])
        self.assertEqual(self.database.get_balance(user_id), 250)
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 1)
        uploaded_file.close()

    async def test_set_formation_and_team_commands_work_together(self) -> None:
        user_id = "team-command-user"
        for name, overall in (("Bench One", 81), ("Bench Two", 79)):
            self.database.add_player(
                {
                    "name": name,
                    "overall": overall,
                    "position": "C",
                    "country": "Canada",
                    "team": "Test Team",
                    "rarity": "TOTW",
                    "value": 1,
                    "stats": {},
                    "image_path": None,
                }
            )
        all_players = ("Dan", "Hala", "Licha", "Bench One", "Bench Two")
        for name in all_players:
            self.database.add_inventory(user_id, name)
        cog = TeamCommands(self.database)
        assignments = (
            ("LW", "Dan"),
            ("RW", "Hala"),
            ("GK", "Licha"),
            ("SUB1", "Bench One"),
            ("SUB2", "Bench Two"),
        )
        for position, player in assignments:
            interaction = SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=FakeResponse(),
            )
            await TeamCommands.set_position.callback(
                cog,
                interaction,
                discord.app_commands.Choice(name=position, value=position),
                player,
            )
            self.assertIn("Assigned", interaction.response.messages[0]["content"])

        lineup_svg = build_lineup_svg(
            self.database.get_lineup(user_id),
            "Defensive",
            "91 OVR",
        )
        self.assertEqual(lineup_svg.count("<image href="), 3)
        for player_name in all_players:
            self.assertIn(player_name, lineup_svg)

        formation_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=FakeResponse(),
        )
        await TeamCommands.formation.callback(
            cog,
            formation_interaction,
            discord.app_commands.Choice(name="Defensive", value="Defensive"),
        )
        self.assertEqual(self.database.get_formation(user_id), "Defensive")

        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="Test Member"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(cog, team_interaction)
        self.assertTrue(team_interaction.response.deferred)
        team_message = team_interaction.followup.messages[0]
        embed = team_message["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("91 OVR", fields["Starting lineup OVR"])
        self.assertIn("Defensive", fields["Formation"])
        self.assertEqual(
            {name.split(" · ", 1)[0] for name in fields if " · " in name},
            set(LINEUP_POSITIONS),
        )
        self.assertEqual(embed.image.url, f"attachment://{OUTPUT_FILENAME}")
        attachment = team_message["file"]
        self.assertEqual(attachment.filename, OUTPUT_FILENAME)
        image_bytes = attachment.fp.read()
        attachment.close()
        self.assertTrue(image_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", image_bytes[16:24]), (1600, 1810))

    async def test_team_image_renders_empty_starting_substitute_and_mixed_lineups(self) -> None:
        for name, overall, image_path in (
            ("Bench One", 81, None),
            ("Bench Two", 79, None),
            ("Missing Art", 75, "cards/not-present.png"),
        ):
            self.database.add_player(
                {
                    "name": name,
                    "overall": overall,
                    "position": "C",
                    "country": "Canada",
                    "team": "Test Team",
                    "rarity": "TOTW",
                    "value": 1,
                    "stats": {},
                    "image_path": image_path,
                }
            )

        assignments_by_case = {
            "empty": (),
            "starting": (("LW", "Dan"), ("RW", "Hala"), ("GK", "Licha")),
            "substitutes": (("SUB1", "Bench One"), ("SUB2", "Bench Two")),
            "mixed": (("LW", "Dan"), ("GK", "Missing Art"), ("SUB2", "Bench Two")),
        }
        image_outputs: dict[str, bytes] = {}
        cog = TeamCommands(self.database)

        for case_name, assignments in assignments_by_case.items():
            with self.subTest(case=case_name):
                user_id = f"visual-{case_name}"
                for position, player_name in assignments:
                    self.database.add_inventory(user_id, player_name)
                    self.database.set_lineup_position(user_id, position, player_name)

                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=user_id, display_name=case_name.title()),
                    response=FakeResponse(),
                    followup=FakeFollowup(),
                )
                await TeamCommands.team.callback(cog, interaction)

                self.assertTrue(interaction.response.deferred)
                message = interaction.followup.messages[0]
                attachment = message["file"]
                self.assertEqual(attachment.filename, OUTPUT_FILENAME)
                image_bytes = attachment.fp.read()
                attachment.close()
                self.assertTrue(image_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertEqual(struct.unpack(">II", image_bytes[16:24]), (1600, 1810))
                image_outputs[case_name] = image_bytes

                fields = {field.name: field.value for field in message["embed"].fields}
                self.assertIn("LW · Left Wing", fields)
                self.assertIn("SUB1 · Substitute 1", fields)
                self.assertIn("SUB2 · Substitute 2", fields)
                for position, player_name in assignments:
                    self.assertIn(player_name, fields[f"{position} · " + {
                        "LW": "Left Wing",
                        "RW": "Right Wing",
                        "GK": "Goalie",
                        "SUB1": "Substitute 1",
                        "SUB2": "Substitute 2",
                    }[position]])

        self.assertEqual(len(set(image_outputs.values())), len(assignments_by_case))
        empty_svg = build_lineup_svg({}, "Balanced", "Not rated yet")
        for position in LINEUP_POSITIONS:
            slot_start = empty_svg.index(f'id="slot-{position.lower()}"')
            slot_end = empty_svg.index("</g>", slot_start)
            self.assertIn('fill="none" stroke="#edf7f5"', empty_svg[slot_start:slot_end])

    async def test_team_renderer_handles_missing_card_art_without_failing(self) -> None:
        self.database.add_player(
            {
                "name": "Missing Art",
                "overall": 75,
                "position": "G",
                "country": "Canada",
                "team": "Test Team",
                "rarity": "TOTW",
                "value": 1,
                "stats": {},
                "image_path": "cards/not-present.png",
            }
        )
        user_id = "missing-art-user"
        self.database.add_inventory(user_id, "Missing Art")
        self.database.set_lineup_position(user_id, "GK", "Missing Art")
        svg = build_lineup_svg(
            self.database.get_lineup(user_id),
            "Balanced",
            "75 OVR average",
        )
        self.assertIn("IMAGE UNAVAILABLE", svg)

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="Missing Art User"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(TeamCommands(self.database), interaction)
        attachment = interaction.followup.messages[0]["file"]
        image_bytes = attachment.fp.read()
        attachment.close()
        self.assertTrue(image_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

    async def test_addplayer_accepts_totw_and_card_specific_stats(self) -> None:
        response = FakeResponse()
        interaction = SimpleNamespace(response=response)
        admin = AdminCommands(self.database)

        await AdminCommands.addplayer.callback(
            admin,
            interaction,
            name="Test Licha",
            overall=93,
            position="GK",
            country="England",
            team="Ottawa Senators",
            rarity="TOTW",
            value=2_000_000,
            stats="SAV=96,DIV=93,PAS=87,CLE=88,SWE=84,ACT=94",
            image_path="cards/licha.webp",
        )

        player = self.database.get_player("Test Licha")
        self.assertIsNotNone(player)
        assert player is not None
        self.assertEqual(player.rarity, "TOTW")
        self.assertEqual(player.stats["DIV"], 93)
        self.assertEqual(player.stats["CLE"], 88)
        self.assertEqual(player.stats["SWE"], 84)
        self.assertEqual(response.messages[0]["content"], "Added **Test Licha** (TOTW, 93 OVR).")

    async def test_totwpack_with_empty_catalog_does_not_charge(self) -> None:
        user_id = "empty-totw-user"
        starting_balance = TOTW_PACK_COST + 500
        empty_catalog = Database(Path(self.temp_dir.name) / "empty-catalog.sqlite3")
        empty_catalog.initialize()
        empty_catalog.set_balance(user_id, starting_balance)
        response = FakeResponse()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=response,
        )

        await PackCommands.totwpack.callback(
            PackCommands(empty_catalog),
            interaction,
        )

        embed = response.messages[0]["embed"]
        self.assertIn("No eligible cards", embed.fields[0].value)
        totw_button = response.messages[0]["view"].children[0]
        self.assertTrue(totw_button.disabled)
        self.assertEqual(empty_catalog.get_balance(user_id), starting_balance)
        self.assertEqual(empty_catalog.get_inventory(user_id), [])


if __name__ == "__main__":
    unittest.main()
