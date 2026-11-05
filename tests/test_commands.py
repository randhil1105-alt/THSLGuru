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
from commands.inventory import InventoryCommands
from commands.lineup_image import OUTPUT_FILENAME, build_lineup_svg
from commands.packs import PackCommands
from commands.player import PlayerCommands
from commands.team import TeamCommands
from commands.information import InformationCommands
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
    "pack",
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
        self.assertNotIn("goldpack", names_before)
        self.assertNotIn("totwpack", names_before)

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
        pack_parameters = {
            parameter.name: parameter for parameter in commands["pack"].parameters
        }
        self.assertEqual(
            [choice.value for choice in pack_parameters["pack_type"].choices],
            ["GOLD", "TOTW"],
        )

    async def test_help_lists_the_combined_pack_command(self) -> None:
        response = FakeResponse()
        await InformationCommands.help.callback(
            InformationCommands(),
            SimpleNamespace(response=response),
        )
        help_text = "\n".join(
            field.value for field in response.messages[0]["embed"].fields
        )
        self.assertIn("`/pack pack_type`", help_text)
        self.assertIn("`/packs`", help_text)
        self.assertNotIn("/goldpack", help_text)
        self.assertNotIn("/totwpack", help_text)

    async def test_all_seeded_card_images_resolve(self) -> None:
        for name in ("Dan", "Licha", "Hala", "XavierCapone9", "Gamer"):
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

    async def test_gamer_card_is_catalogued_grantable_and_lineup_ready(self) -> None:
        player = self.database.get_player("Gamer")
        self.assertIsNotNone(player)
        assert player is not None
        self.assertEqual(
            (
                player.name,
                player.overall,
                player.position,
                player.country,
                player.team,
                player.rarity,
                player.value,
            ),
            ("Gamer", 89, "WI", "USA", "Dallas Stars", "GOLD", 2_000_000),
        )
        self.assertEqual(
            player.stats,
            {"SHO": 93, "DRI": 88, "SAV": 78, "PAS": 86, "DEF": 80, "ACT": 76},
        )
        image_path = resolve_card_image(player)
        self.assertIsNotNone(image_path)
        assert image_path is not None
        self.assertEqual(image_path.name, "gamer.png")
        self.assertTrue(image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

        player_cog = PlayerCommands(self.database)
        for command_name in ("card", "player"):
            response = FakeResponse()
            await getattr(PlayerCommands, command_name).callback(
                player_cog,
                SimpleNamespace(response=response),
                "Gamer",
            )
            message = response.messages[0]
            self.assertEqual(message["embed"].title, "Gamer · 89 OVR")
            self.assertEqual(message["embed"].image.url, "attachment://gamer.png")
            details = {field.name: field.value for field in message["embed"].fields}
            self.assertEqual(details["Value"], "2,000,000 coins")
            self.assertIn("**SHO** 93", details["Stats"])
            self.assertIn("**DRI** 88", details["Stats"])
            self.assertIn("**SAV** 78", details["Stats"])
            self.assertIn("**PAS** 86", details["Stats"])
            self.assertIn("**DEF** 80", details["Stats"])
            self.assertIn("**ACT** 76", details["Stats"])
            uploaded = message["file"]
            self.assertTrue(uploaded.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
            uploaded.close()

        user_id = "gamer-card-command-user"
        await AdminCommands.grantcard.callback(
            AdminCommands(self.database),
            SimpleNamespace(response=FakeResponse()),
            SimpleNamespace(id=user_id, mention="<@gamer-card-command-user>"),
            "Gamer",
            1,
        )
        inventory_cog = InventoryCommands(self.database)
        for command_name in ("inventory", "collection"):
            response = FakeResponse()
            await getattr(InventoryCommands, command_name).callback(
                inventory_cog,
                SimpleNamespace(user=SimpleNamespace(id=user_id), response=response),
            )
            self.assertIn("Gamer", response.messages[0]["embed"].description)
            self.assertIn("×1", response.messages[0]["embed"].description)

        team_cog = TeamCommands(self.database)
        for position in ("LW", "RW"):
            response = FakeResponse()
            await TeamCommands.set_position.callback(
                team_cog,
                SimpleNamespace(user=SimpleNamespace(id=user_id), response=response),
                discord.app_commands.Choice(name=position, value=position),
                "Gamer",
            )
            self.assertEqual(self.database.get_lineup(user_id)[position].name, "Gamer")
            if position == "LW":
                await TeamCommands.set_position.callback(
                    team_cog,
                    SimpleNamespace(user=SimpleNamespace(id=user_id), response=FakeResponse()),
                    discord.app_commands.Choice(name="LW", value="LW"),
                    "CLEAR",
                )

        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="Gamer"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(team_cog, team_interaction)
        team_message = team_interaction.followup.messages[0]
        self.assertIn(
            "Gamer",
            {field.name: field.value for field in team_message["embed"].fields}[
                "RW · Right Wing"
            ],
        )
        lineup_file = team_message["file"]
        lineup_bytes = lineup_file.fp.read()
        lineup_file.close()
        self.assertTrue(lineup_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", lineup_bytes[16:24]), (1600, 1810))

    async def test_xavier_card_is_lookup_grant_inventory_and_team_ready(self) -> None:
        player = self.database.get_player("XavierCapone9")
        self.assertIsNotNone(player)
        assert player is not None
        self.assertEqual(
            (
                player.name,
                player.overall,
                player.position,
                player.country,
                player.team,
                player.rarity,
                player.value,
            ),
            (
                "XavierCapone9",
                91,
                "WI",
                "England",
                "Vegas Golden Knights",
                "GOLD",
                3_140_000,
            ),
        )
        self.assertEqual(
            player.stats,
            {"SHO": 92, "DRI": 95, "SAV": 75, "PAS": 88, "DEF": 86, "ACT": 82},
        )
        image_path = resolve_card_image(player)
        self.assertIsNotNone(image_path)
        assert image_path is not None
        self.assertEqual(image_path.name, "xaviercapone9.png")

        player_cog = PlayerCommands(self.database)
        for command_name in ("card", "player"):
            response = FakeResponse()
            interaction = SimpleNamespace(response=response)
            command = getattr(PlayerCommands, command_name)
            await command.callback(player_cog, interaction, "XavierCapone9")
            message = response.messages[0]
            embed = message["embed"]
            self.assertEqual(embed.title, "XavierCapone9 · 91 OVR")
            self.assertEqual(embed.image.url, "attachment://xaviercapone9.png")
            details = {field.name: field.value for field in embed.fields}
            self.assertEqual(details["Position"], "WI")
            self.assertEqual(details["Country"], "England")
            self.assertEqual(details["Team"], "Vegas Golden Knights")
            self.assertEqual(details["Rarity"], "GOLD")
            self.assertEqual(details["Value"], "3,140,000 coins")
            self.assertIn("**SHO** 92", details["Stats"])
            self.assertIn("**DRI** 95", details["Stats"])
            self.assertIn("**SAV** 75", details["Stats"])
            self.assertIn("**PAS** 88", details["Stats"])
            self.assertIn("**DEF** 86", details["Stats"])
            self.assertIn("**ACT** 82", details["Stats"])
            card_file = message["file"]
            self.assertTrue(card_file.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
            card_file.close()

        user_id = "xavier-card-command-user"
        admin_response = FakeResponse()
        member = SimpleNamespace(id=user_id, mention="<@xavier-card-command-user>")
        await AdminCommands.grantcard.callback(
            AdminCommands(self.database),
            SimpleNamespace(response=admin_response),
            member,
            "XavierCapone9",
            1,
        )
        self.assertIn("Granted **1× XavierCapone9**", admin_response.messages[0]["content"])

        inventory_cog = InventoryCommands(self.database)
        for command_name in ("inventory", "collection"):
            response = FakeResponse()
            interaction = SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=response,
            )
            await getattr(InventoryCommands, command_name).callback(
                inventory_cog,
                interaction,
            )
            self.assertIn("XavierCapone9", response.messages[0]["embed"].description)
            self.assertIn("×1", response.messages[0]["embed"].description)

        team_cog = TeamCommands(self.database)
        for position in ("LW", "RW"):
            response = FakeResponse()
            await TeamCommands.set_position.callback(
                team_cog,
                SimpleNamespace(
                    user=SimpleNamespace(id=user_id),
                    response=response,
                ),
                discord.app_commands.Choice(name=position, value=position),
                "XavierCapone9",
            )
            self.assertIn("Assigned **XavierCapone9**", response.messages[0]["content"])
            self.assertEqual(self.database.get_lineup(user_id)[position].name, "XavierCapone9")
            if position == "LW":
                await TeamCommands.set_position.callback(
                    team_cog,
                    SimpleNamespace(
                        user=SimpleNamespace(id=user_id),
                        response=FakeResponse(),
                    ),
                    discord.app_commands.Choice(name="LW", value="LW"),
                    "CLEAR",
                )

        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="Xavier Test"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(team_cog, team_interaction)
        team_message = team_interaction.followup.messages[0]
        self.assertIn(
            "XavierCapone9",
            {field.name: field.value for field in team_message["embed"].fields}[
                "RW · Right Wing"
            ],
        )
        lineup_file = team_message["file"]
        lineup_bytes = lineup_file.fp.read()
        lineup_file.close()
        self.assertTrue(lineup_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", lineup_bytes[16:24]), (1600, 1810))

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
        self.assertIn("XavierCapone9", field_values["GOLD · 5,000,000 coins"])
        self.assertIn("Gamer", field_values["GOLD · 5,000,000 coins"])
        self.assertIn("33.3%", field_values["GOLD · 5,000,000 coins"])
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
        purchased_inventory = self.database.get_inventory(user_id)
        self.assertEqual(len(purchased_inventory), 1)
        self.assertIn(
            purchased_inventory[0].player.name,
            {"Dan", "XavierCapone9", "Gamer"},
        )
        self.assertEqual(purchased_inventory[0].player.rarity, "GOLD")
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

    async def test_pack_command_selects_and_purchases_both_pack_types(self) -> None:
        pack_cases = (
            ("GOLD", GOLD_PACK_COST, {"Dan", "XavierCapone9", "Gamer"}, "33.3%"),
            ("TOTW", TOTW_PACK_COST, {"Licha", "Hala"}, "50.0%"),
        )
        for index, (rarity, cost, expected_names, odds) in enumerate(pack_cases):
            with self.subTest(rarity=rarity):
                user_id = f"single-pack-command-{rarity.lower()}"
                self.database.set_balance(user_id, cost + 321)
                command_response = FakeResponse()
                await PackCommands.pack.callback(
                    PackCommands(self.database),
                    SimpleNamespace(
                        user=SimpleNamespace(id=user_id),
                        response=command_response,
                    ),
                    discord.app_commands.Choice(name=f"{rarity} Pack", value=rarity),
                )

                menu = command_response.messages[0]
                fields = {field.name: field.value for field in menu["embed"].fields}
                pack_field = f"{rarity} · {cost:,} coins"
                self.assertEqual(set(fields), {pack_field})
                for name in expected_names:
                    self.assertIn(name, fields[pack_field])
                self.assertIn(odds, fields[pack_field])

                view = menu["view"]
                self.assertEqual(len(view.children), 1)
                button = view.children[0]
                self.assertEqual(button.rarity, rarity)
                purchase_response = FakeResponse()
                await button.callback(
                    SimpleNamespace(
                        user=SimpleNamespace(id=user_id),
                        response=purchase_response,
                        id=11001 + index,
                        message=FakeMessage(),
                    )
                )
                self.assertIn(
                    f"opened a **{rarity} pack**",
                    purchase_response.messages[0]["content"],
                )
                self.assertEqual(self.database.get_balance(user_id), 321)
                inventory = self.database.get_inventory(user_id)
                self.assertEqual(len(inventory), 1)
                self.assertEqual(inventory[0].player.rarity, rarity)
                self.assertEqual(inventory[0].quantity, 1)
                purchase_response.messages[0]["file"].close()

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

    async def test_pack_command_with_empty_totw_catalog_does_not_charge(self) -> None:
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

        await PackCommands.pack.callback(
            PackCommands(empty_catalog),
            interaction,
            discord.app_commands.Choice(name="TOTW Pack", value="TOTW"),
        )

        embed = response.messages[0]["embed"]
        self.assertIn("No eligible cards", embed.fields[0].value)
        totw_button = response.messages[0]["view"].children[0]
        self.assertTrue(totw_button.disabled)
        self.assertEqual(empty_catalog.get_balance(user_id), starting_balance)
        self.assertEqual(empty_catalog.get_inventory(user_id), [])


if __name__ == "__main__":
    unittest.main()
