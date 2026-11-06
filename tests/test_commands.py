from __future__ import annotations

import asyncio
import tempfile
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from commands.admin import AdminCommands
from commands.economy import EconomyCommands
from commands.init import COMMAND_MODULES, load_command_modules
from commands.common import (
    inventory_embed,
    player_card_label,
    player_embed,
    resolve_card_image,
)
from commands.inventory import InventoryCommands
from commands.lineup_image import (
    OUTPUT_FILENAME,
    STARTER_SLOT_LAYOUTS,
    SUBSTITUTE_SLOT_LAYOUTS,
    build_lineup_svg,
)
from commands.minigame import (
    MINIGAME_DURATION_SECONDS,
    MINIGAME_REWARD,
    GuessAnswerModal,
    IndividualMinigameView,
    MinigameCommands,
)
from commands.packs import PackBuyButton, PackCommands
from commands.player import PlayerCommands
from commands.team import TeamCommands
from commands.information import InformationCommands
from database import (
    Database,
    FORMATION_STARTERS,
    GOLD_PACK_COST,
    InventoryEntry,
    LINEUP_POSITIONS,
    SUBSTITUTE_POSITIONS,
    TOTW_PACK_COST,
    format_card_type,
)
from main import create_bot, load_player_seed

EXPECTED_COMMANDS = {
    "card",
    "cards",
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
    "minigame",
}


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.modals: list[discord.ui.Modal] = []
        self.deferred = False

    async def send_message(
        self,
        content: str | None = None,
        **kwargs: object,
    ) -> None:
        self.messages.append({"content": content, **kwargs})

    async def defer(self, **kwargs: object) -> None:
        self.deferred = True

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.modals.append(modal)


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
        self.assertNotIn("player", names_before)
        self.assertNotIn("players", names_before)
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
        self.assertFalse(self.bot.intents.message_content)

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
        self.assertTrue({"packs", "pack", "inventory", "balance"} <= set(commands))

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
        self.assertIn("`/claim` · Claim a GOLD player card every 24 hours", help_text)
        self.assertNotIn("Claim 5,000 coins", help_text)
        self.assertNotIn("/goldpack", help_text)
        self.assertNotIn("/totwpack", help_text)
        self.assertNotIn("/player", help_text)
        self.assertNotIn("/players", help_text)

    async def test_catalog_alias_commands_display_card_types(self) -> None:
        for command_name in ("cards",):
            response = FakeResponse()
            await getattr(InventoryCommands, command_name).callback(
                InventoryCommands(self.database),
                SimpleNamespace(
                    user=SimpleNamespace(id="catalog-alias-user"),
                    response=response,
                ),
            )
            self.assertTrue(response.messages[0]["ephemeral"])
            self.assertIn(
                "**87 Dryboomrun (Gold)**",
                response.messages[0]["embed"].description,
            )
            self.assertIn(
                "**85 Needy (Gold)**",
                response.messages[0]["embed"].description,
            )
            self.assertIn(
                "**86 Fadi (Gold)**",
                response.messages[0]["embed"].description,
            )
            for name in ("Mason", "Demox", "Cheddar"):
                self.assertIn(
                    f"**84 {name} (Gold)**",
                    response.messages[0]["embed"].description,
                )
            self.assertIn(
                "**87 Dan (Gold)**",
                response.messages[0]["embed"].description,
            )

    async def test_balance_and_inventory_responses_are_public(self) -> None:
        user_id = "public-response-user"
        self.database.set_balance(user_id, 12_345)
        balance_response = FakeResponse()
        await EconomyCommands.balance.callback(
            EconomyCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=balance_response,
            ),
        )
        self.assertFalse(balance_response.messages[0]["ephemeral"])
        self.assertIn("12,345 coins", balance_response.messages[0]["content"])

        inventory_response = FakeResponse()
        await InventoryCommands.inventory.callback(
            InventoryCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=inventory_response,
            ),
        )
        self.assertFalse(inventory_response.messages[0]["ephemeral"])

    async def test_claim_awards_gold_card_image_without_changing_balance(self) -> None:
        user_id = "gold-card-claim-command-user"
        initial_balance = 98_765
        self.database.set_balance(user_id, initial_balance)
        cog = EconomyCommands(self.database)
        response = FakeResponse()
        await EconomyCommands.claim.callback(
            cog,
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=response,
            ),
        )

        message = response.messages[0]
        self.assertTrue(message["ephemeral"])
        self.assertNotIn("coin", message["content"].casefold())
        inventory = self.database.get_inventory(user_id)
        self.assertEqual(len(inventory), 1)
        claimed = inventory[0].player
        self.assertEqual(claimed.rarity, "GOLD")
        self.assertEqual(message["embed"].title, player_card_label(claimed))
        self.assertEqual(
            message["embed"].image.url,
            f"attachment://{Path(claimed.image_path).name}",
        )
        self.assertIn(player_card_label(claimed), message["content"])
        self.assertEqual(self.database.get_balance(user_id), initial_balance)
        image_file = message["file"]
        self.assertTrue(image_file.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
        image_file.close()

        cooldown_response = FakeResponse()
        await EconomyCommands.claim.callback(
            cog,
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=cooldown_response,
            ),
        )
        self.assertIn("claim again", cooldown_response.messages[0]["content"])
        self.assertTrue(cooldown_response.messages[0]["ephemeral"])
        self.assertEqual(self.database.get_balance(user_id), initial_balance)
        self.assertEqual(sum(entry.quantity for entry in self.database.get_inventory(user_id)), 1)

    async def test_claim_without_gold_cards_does_not_award_money(self) -> None:
        user_id = "no-gold-claim-command-user"
        self.database.set_balance(user_id, 45_678)
        with self.database._transaction() as connection:
            connection.execute("DELETE FROM players WHERE rarity = 'GOLD'")

        response = FakeResponse()
        await EconomyCommands.claim.callback(
            EconomyCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=response,
            ),
        )
        self.assertIn(
            "No GOLD card rewards are currently available.",
            response.messages[0]["content"],
        )
        self.assertTrue(response.messages[0]["ephemeral"])
        self.assertEqual(self.database.get_balance(user_id), 45_678)
        self.assertEqual(self.database.get_inventory(user_id), [])

    async def test_public_command_error_handler_keeps_responses_public(self) -> None:
        with patch("main.LOGGER.error"):
            for command_name in ("packs", "pack", "inventory", "balance", "minigame"):
                response = SimpleNamespace(
                    is_done=lambda: False,
                    send_message=AsyncMock(),
                )
                interaction = SimpleNamespace(
                    command=SimpleNamespace(name=command_name),
                    response=response,
                )
                await self.bot.tree.on_error(interaction, RuntimeError("test"))
                response.send_message.assert_awaited_once()
                self.assertFalse(response.send_message.await_args.kwargs["ephemeral"])

        followup = AsyncMock()
        interaction = SimpleNamespace(
            command=SimpleNamespace(name="pack"),
            response=SimpleNamespace(is_done=lambda: True),
            followup=SimpleNamespace(send=followup),
        )
        with patch("main.LOGGER.error"):
            await self.bot.tree.on_error(interaction, RuntimeError("test"))
        followup.assert_awaited_once()
        self.assertFalse(followup.await_args.kwargs["ephemeral"])

        admin_response = SimpleNamespace(
            is_done=lambda: False,
            send_message=AsyncMock(),
        )
        await self.bot.tree.on_error(
            SimpleNamespace(
                command=SimpleNamespace(name="grantcard"),
                response=admin_response,
            ),
            discord.app_commands.MissingPermissions(["administrator"]),
        )
        self.assertTrue(admin_response.send_message.await_args.kwargs["ephemeral"])

    async def test_all_seeded_card_images_resolve(self) -> None:
        for name in (
            "Dan",
            "Licha",
            "Hala",
            "Hala (Gold 90)",
            "XavierCapone9",
            "Gamer",
            "Dryboomrun",
            "Needy",
            "Fadi",
            "Mason",
            "Demox",
            "Cheddar",
        ):
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
        self.assertEqual(self.database.get_player("Hala").overall, 93)

    async def test_mason_demox_and_cheddar_work_across_card_and_lineup_commands(self) -> None:
        expected = {
            "Mason": (
                "England", "Dallas Stars",
                {"SHO": 85, "DRI": 85, "SAV": 79, "PAS": 84, "DEF": 83, "ACT": 83},
                "cards/mason.png",
            ),
            "Demox": (
                "Turkey", "Ottawa Senators",
                {"SHO": 87, "DRI": 86, "SAV": 84, "PAS": 86, "DEF": 85, "ACT": 85},
                "cards/demox.png",
            ),
            "Cheddar": (
                "Scotland", "Vegas Golden Knights",
                {"SHO": 87, "DRI": 80, "SAV": 78, "PAS": 83, "DEF": 85, "ACT": 80},
                "cards/cheddar.png",
            ),
        }
        gold_names = {card.name for card in self.database.get_players(rarity="GOLD")}
        player_cog = PlayerCommands(self.database)
        for name, (country, team, stats, image_name) in expected.items():
            with self.subTest(card=name):
                player = self.database.get_player(name)
                self.assertIsNotNone(player)
                assert player is not None
                self.assertEqual(
                    (
                        player.overall,
                        player.position,
                        player.country,
                        player.team,
                        player.rarity,
                        player.value,
                        player.stats,
                        player.image_path,
                    ),
                    (84, "WI", country, team, "GOLD", 400_000, stats, image_name),
                )
                self.assertIn(name, gold_names)
                image_path = resolve_card_image(player)
                self.assertIsNotNone(image_path)
                assert image_path is not None
                self.assertEqual(image_path.name, image_name.removeprefix("cards/"))
                self.assertTrue(image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

                choices = await PlayerCommands.card_player_autocomplete(
                    player_cog,
                    SimpleNamespace(),
                    name[:3],
                )
                self.assertEqual([choice.value for choice in choices], [name])
                for command_name in ("card",):
                    response = FakeResponse()
                    await getattr(PlayerCommands, command_name).callback(
                        player_cog,
                        SimpleNamespace(response=response),
                        name,
                    )
                    message = response.messages[0]
                    self.assertEqual(message["embed"].title, f"84 {name} (Gold)")
                    details = {field.name: field.value for field in message["embed"].fields}
                    self.assertEqual(details["Card Type"], "Gold")
                    self.assertEqual(details["Value"], "400,000 coins")
                    self.assertEqual(details["Country"], country)
                    self.assertEqual(details["Team"], team)
                    self.assertEqual(
                        message["embed"].image.url,
                        f"attachment://{Path(image_name).name}",
                    )
                    image_file = message["file"]
                    self.assertTrue(image_file.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
                    image_file.close()

        inventory_user = "new-gold-card-inventory-user"
        for name in expected:
            self.database.add_inventory(inventory_user, name)
        for command_name in ("inventory", "collection"):
            response = FakeResponse()
            await getattr(InventoryCommands, command_name).callback(
                InventoryCommands(self.database),
                SimpleNamespace(
                    user=SimpleNamespace(id=inventory_user),
                    response=response,
                ),
            )
            for name in expected:
                self.assertIn(f"**84 {name} (Gold)**", response.messages[0]["embed"].description)

        pack_response = FakeResponse()
        await PackCommands.packs.callback(
            PackCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=inventory_user),
                response=pack_response,
            ),
        )
        pack_text = "\n".join(field.value for field in pack_response.messages[0]["embed"].fields)
        for name in expected:
            self.assertIn(f"84 {name} (Gold)", pack_text)

        team_cog = TeamCommands(self.database)
        self.database.set_formation(inventory_user, "Offensive")
        for name, position in (("Mason", "WING1"), ("Demox", "WING2")):
            await TeamCommands.set_position.callback(
                team_cog,
                SimpleNamespace(user=SimpleNamespace(id=inventory_user), response=FakeResponse()),
                discord.app_commands.Choice(name=position, value=position),
                name,
            )
            self.assertEqual(self.database.get_lineup(inventory_user)[position].name, name)
        cheddar_user = "cheddar-lineup-user"
        self.database.add_inventory(cheddar_user, "Cheddar")
        await TeamCommands.set_position.callback(
            team_cog,
            SimpleNamespace(user=SimpleNamespace(id=cheddar_user), response=FakeResponse()),
            discord.app_commands.Choice(name="WING1", value="WING1"),
            "Cheddar",
        )
        self.assertEqual(self.database.get_lineup(cheddar_user)["WING1"].name, "Cheddar")

        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=inventory_user, display_name="New Cards"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(team_cog, team_interaction)
        team_fields = {
            field.name: field.value
            for field in team_interaction.followup.messages[0]["embed"].fields
        }
        self.assertIn("Mason — 84 OVR (GOLD)", team_fields["WING1 · Wing 1"])
        self.assertIn("Demox — 84 OVR (GOLD)", team_fields["WING2 · Wing 2"])
        team_file = team_interaction.followup.messages[0]["file"]
        self.assertTrue(team_file.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
        team_file.close()

        cheddar_interaction = SimpleNamespace(
            user=SimpleNamespace(id=cheddar_user, display_name="Cheddar"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(team_cog, cheddar_interaction)
        cheddar_fields = {
            field.name: field.value
            for field in cheddar_interaction.followup.messages[0]["embed"].fields
        }
        self.assertIn("Cheddar — 84 OVR (GOLD)", cheddar_fields["WING1 · Wing 1"])
        cheddar_interaction.followup.messages[0]["file"].close()

    async def test_card_command_autocompletes_partial_names_and_shows_full_card(self) -> None:
        await load_command_modules(self.bot)
        cog = PlayerCommands(self.database)
        command = next(
            item for item in self.bot.tree.get_commands() if item.name == "card"
        )
        player_option = next(
            parameter for parameter in command.parameters
            if parameter.name == "player"
        )
        self.assertTrue(player_option.required)
        self.assertTrue(player_option.autocomplete)

        choices = await PlayerCommands.card_player_autocomplete(
            cog,
            SimpleNamespace(),
            "Fad",
        )
        self.assertEqual([(choice.name, choice.value) for choice in choices], [
            ("86 Fadi (Gold)", "Fadi"),
        ])
        all_choices = await PlayerCommands.card_player_autocomplete(
            cog,
            SimpleNamespace(),
            "",
        )
        self.assertEqual(len(all_choices), len(self.database.get_players()))
        self.assertEqual(
            len({choice.value.casefold() for choice in all_choices}),
            len(all_choices),
        )

        response = FakeResponse()
        await PlayerCommands.card.callback(
            cog,
            SimpleNamespace(response=response),
            "Fadi",
        )
        message = response.messages[0]
        self.assertEqual(message["embed"].title, "86 Fadi (Gold)")
        self.assertEqual(message["embed"].image.url, "attachment://fadi.png")
        fields = {field.name: field.value for field in message["embed"].fields}
        self.assertEqual(fields["Card Type"], "Gold")
        self.assertEqual(fields["Position"], "C")
        self.assertEqual(fields["Country"], "Algeria")
        self.assertEqual(fields["Team"], "New York Rangers")
        self.assertEqual(fields["Value"], "780,000 coins")
        for stat, value in {
            "SHO": 88,
            "DRI": 86,
            "SAV": 72,
            "PAS": 85,
            "DEF": 85,
            "ACT": 82,
        }.items():
            self.assertIn(f"**{stat}** {value}", fields["Stats"])
        self.assertTrue(message["file"].fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
        message["file"].close()
        card = self.database.get_player("Fadi")
        self.assertIsNotNone(card)
        assert card is not None
        self.assertIn(card, self.database.get_players(rarity="GOLD"))

        user_id = "fadi-lineup-user"
        packs_response = FakeResponse()
        await PackCommands.packs.callback(
            PackCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=packs_response,
            ),
        )
        gold_field = {
            field.name: field.value
            for field in packs_response.messages[0]["embed"].fields
        }["GOLD · 5,000,000 coins"]
        self.assertIn("86 Fadi (Gold)", gold_field)

        self.database.add_inventory(user_id, "Fadi")
        for command_name in ("inventory", "collection"):
            inventory_response = FakeResponse()
            await getattr(InventoryCommands, command_name).callback(
                InventoryCommands(self.database),
                SimpleNamespace(
                    user=SimpleNamespace(id=user_id),
                    response=inventory_response,
                ),
            )
            self.assertIn(
                "86 Fadi (Gold)",
                inventory_response.messages[0]["embed"].description,
            )

        set_response = FakeResponse()
        team_cog = TeamCommands(self.database)
        await TeamCommands.set_position.callback(
            team_cog,
            SimpleNamespace(user=SimpleNamespace(id=user_id), response=set_response),
            discord.app_commands.Choice(name="CM 1", value="CM1"),
            "Fadi",
        )
        self.assertIn(
            "Assigned **Fadi — 86 OVR (GOLD)** to **CM1**",
            set_response.messages[0]["content"],
        )
        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="Fadi"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(team_cog, team_interaction)
        team_message = team_interaction.followup.messages[0]
        team_fields = {
            field.name: field.value for field in team_message["embed"].fields
        }
        self.assertIn("Fadi — 86 OVR (GOLD)", team_fields["CM1 · CM 1"])
        self.assertTrue(
            team_message["file"].fp.read().startswith(b"\x89PNG\r\n\x1a\n")
        )
        team_message["file"].close()

        unknown_response = FakeResponse()
        await PlayerCommands.card.callback(
            cog,
            SimpleNamespace(response=unknown_response),
            "Not in catalog",
        )
        self.assertTrue(unknown_response.messages[0]["ephemeral"])
        self.assertIn("No player named", unknown_response.messages[0]["content"])

    async def test_totw_card_lookup_shows_updated_value(self) -> None:
        for name in ("Licha", "Hala"):
            with self.subTest(card=name):
                response = FakeResponse()
                await PlayerCommands.card.callback(
                    PlayerCommands(self.database),
                    SimpleNamespace(response=response),
                    name,
                )
                fields = {
                    field.name: field.value
                    for field in response.messages[0]["embed"].fields
                }
                self.assertEqual(fields["Card Type"], "TOTW")
                self.assertEqual(fields["Value"], "12,000,000 coins")
                response.messages[0]["file"].close()

    async def test_card_type_is_displayed_for_every_catalog_card(self) -> None:
        for player in self.database.get_players():
            with self.subTest(card=player.name):
                label = player_card_label(player)
                self.assertTrue(player.rarity)
                display_name = {
                    "Hala (Gold 90)": "Hala",
                }.get(player.name, player.name)
                self.assertEqual(
                    label,
                    f"{player.overall} {display_name} "
                    f"({format_card_type(player.rarity)})",
                )
                detail_embed = player_embed(player)
                self.assertEqual(detail_embed.title, label)
                detail_fields = {
                    field.name: field.value for field in detail_embed.fields
                }
                self.assertEqual(
                    detail_fields["Card Type"],
                    format_card_type(player.rarity),
                )
                inventory = inventory_embed(
                    "Inventory",
                    [InventoryEntry(player, 1)],
                )
                collection = inventory_embed(
                    "Collection",
                    [InventoryEntry(player, 1)],
                    collection=True,
                )
                self.assertIn(f"**{label}**", inventory.description)
                self.assertIn(f"**{label}**", collection.description)

    async def test_dryboomrun_card_is_displayed_in_all_card_flows(self) -> None:
        await load_command_modules(self.bot)
        card = self.database.get_player("Dryboomrun")
        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(
            (
                card.name,
                card.overall,
                card.position,
                card.country,
                card.team,
                card.rarity,
                card.value,
                card.image_path,
            ),
            (
                "Dryboomrun",
                87,
                "C",
                "Guatemala",
                "Dallas Stars",
                "GOLD",
                1_100_000,
                "cards/dryboomrun.png",
            ),
        )
        self.assertEqual(
            card.stats,
            {"SHO": 86, "DRI": 85, "SAV": 78, "PAS": 88, "DEF": 89, "ACT": 84},
        )
        image_path = resolve_card_image(card)
        self.assertIsNotNone(image_path)
        assert image_path is not None
        self.assertEqual(image_path, Path("cards/dryboomrun.png").resolve())
        self.assertTrue(image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(card, self.database.get_players(rarity="GOLD"))
        label = "87 Dryboomrun (Gold)"

        for command_name in ("card",):
            response = FakeResponse()
            await getattr(PlayerCommands, command_name).callback(
                PlayerCommands(self.database),
                SimpleNamespace(response=response),
                "Dryboomrun",
            )
            self.assertEqual(response.messages[0]["embed"].title, label)
            self.assertEqual(
                {
                    field.name: field.value
                    for field in response.messages[0]["embed"].fields
                }["Card Type"],
                "Gold",
            )
            self.assertEqual(
                response.messages[0]["embed"].image.url,
                "attachment://dryboomrun.png",
            )
            response.messages[0]["file"].close()

        user_id = "dryboomrun-display-user"
        grant_response = FakeResponse()
        await AdminCommands.grantcard.callback(
            AdminCommands(self.database),
            SimpleNamespace(response=grant_response),
            SimpleNamespace(id=user_id, mention="<@dryboomrun-display-user>"),
            "Dryboomrun",
            1,
        )
        self.assertIn(label, grant_response.messages[0]["content"])
        inventory_cog = InventoryCommands(self.database)
        for command_name in ("inventory", "collection"):
            response = FakeResponse()
            await getattr(InventoryCommands, command_name).callback(
                inventory_cog,
                SimpleNamespace(user=SimpleNamespace(id=user_id), response=response),
            )
            self.assertIn(f"**{label}**", response.messages[0]["embed"].description)

        user_without_card = "dryboomrun-not-owned-user"
        not_owned_response = FakeResponse()
        await TeamCommands.set_position.callback(
            TeamCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=user_without_card),
                response=not_owned_response,
            ),
            discord.app_commands.Choice(name="SUB1", value="SUB1"),
            "Dryboomrun",
        )
        self.assertIn(label, not_owned_response.messages[0]["content"])

        pack_response = FakeResponse()
        await PackCommands.packs.callback(
            PackCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=pack_response,
            ),
        )
        gold_field = {
            field.name: field.value for field in pack_response.messages[0]["embed"].fields
        }["GOLD · 5,000,000 coins"]
        self.assertIn(label, gold_field)

        self.assertIn("CM1", LINEUP_POSITIONS)
        set_command = {
            command.name: command for command in self.bot.tree.get_commands()
        }["set"]
        position_parameter = next(
            parameter for parameter in set_command.parameters
            if parameter.name == "position"
        )
        self.assertIn("CM1", [choice.value for choice in position_parameter.choices])

        team_cog = TeamCommands(self.database)
        assign_response = FakeResponse()
        await TeamCommands.set_position.callback(
            team_cog,
            SimpleNamespace(user=SimpleNamespace(id=user_id), response=assign_response),
            discord.app_commands.Choice(name="CM 1", value="CM1"),
            "Dryboomrun",
        )
        self.assertIn(
            "Dryboomrun — 87 OVR (GOLD)",
            assign_response.messages[0]["content"],
        )
        lineup = self.database.get_lineup(user_id)
        self.assertEqual(lineup["CM1"].name, "Dryboomrun")
        duplicate_response = FakeResponse()
        await TeamCommands.set_position.callback(
            team_cog,
            SimpleNamespace(user=SimpleNamespace(id=user_id), response=duplicate_response),
            discord.app_commands.Choice(name="SUB1", value="SUB1"),
            "Dryboomrun",
        )
        self.assertIn(label, duplicate_response.messages[0]["content"])
        svg = build_lineup_svg(lineup, "Balanced", "87 OVR average")
        self.assertIn("Dryboomrun", svg)
        self.assertIn("87 OVR · GOLD", svg)
        self.assertIn("data:image/png;base64,", svg)
        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="Dryboomrun"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(team_cog, team_interaction)
        team_message = team_interaction.followup.messages[0]
        fields = {field.name: field.value for field in team_message["embed"].fields}
        self.assertIn("Dryboomrun — 87 OVR (GOLD)", fields["CM1 · CM 1"])
        team_image = team_message["file"]
        self.assertTrue(team_image.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
        team_image.close()

    async def test_needy_card_is_displayed_in_all_requested_flows(self) -> None:
        card = self.database.get_player("Needy")
        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(
            (
                card.overall,
                card.rarity,
                card.position,
                card.country,
                card.team,
                card.value,
                card.stats,
                card.image_path,
            ),
            (
                85,
                "GOLD",
                "C",
                "Mexico",
                "Vegas Golden Knights",
                700_000,
                {"SHO": 85, "DRI": 84, "SAV": 85, "PAS": 84, "DEF": 86, "ACT": 86},
                "cards/needy.png",
            ),
        )
        label = "85 Needy (Gold)"
        image_path = resolve_card_image(card)
        self.assertIsNotNone(image_path)
        assert image_path is not None
        self.assertEqual(image_path, Path("cards/needy.png").resolve())
        self.assertTrue(image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(card, self.database.get_players(rarity="GOLD"))

        for command_name in ("card",):
            response = FakeResponse()
            await getattr(PlayerCommands, command_name).callback(
                PlayerCommands(self.database),
                SimpleNamespace(response=response),
                "Needy",
            )
            message = response.messages[0]
            self.assertEqual(message["embed"].title, label)
            self.assertEqual(message["embed"].image.url, "attachment://needy.png")
            details = {field.name: field.value for field in message["embed"].fields}
            self.assertEqual(details["Position"], "C")
            self.assertEqual(details["Country"], "Mexico")
            self.assertEqual(details["Team"], "Vegas Golden Knights")
            self.assertEqual(details["Card Type"], "Gold")
            self.assertEqual(details["Value"], "700,000 coins")
            for stat, value in {
                "SHO": 85,
                "DRI": 84,
                "SAV": 85,
                "PAS": 84,
                "DEF": 86,
                "ACT": 86,
            }.items():
                self.assertIn(f"**{stat}** {value}", details["Stats"])
            message["file"].close()

        user_id = "needy-card-flow-user"
        grant_response = FakeResponse()
        await AdminCommands.grantcard.callback(
            AdminCommands(self.database),
            SimpleNamespace(response=grant_response),
            SimpleNamespace(id=user_id, mention="<@needy-card-flow-user>"),
            "Needy",
            1,
        )
        self.assertIn(label, grant_response.messages[0]["content"])

        inventory_cog = InventoryCommands(self.database)
        for command_name in ("inventory", "collection", "cards"):
            response = FakeResponse()
            await getattr(InventoryCommands, command_name).callback(
                inventory_cog,
                SimpleNamespace(user=SimpleNamespace(id=user_id), response=response),
            )
            self.assertIn(f"**{label}**", response.messages[0]["embed"].description)

        pack_response = FakeResponse()
        await PackCommands.packs.callback(
            PackCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=pack_response,
            ),
        )
        gold_field = {
            field.name: field.value
            for field in pack_response.messages[0]["embed"].fields
        }["GOLD · 5,000,000 coins"]
        self.assertIn(label, gold_field)
        self.assertIn("16.67%", gold_field)

        set_response = FakeResponse()
        team_cog = TeamCommands(self.database)
        await TeamCommands.set_position.callback(
            team_cog,
            SimpleNamespace(user=SimpleNamespace(id=user_id), response=set_response),
            discord.app_commands.Choice(name="CM1", value="CM1"),
            "Needy",
        )
        self.assertIn("Needy — 85 OVR (GOLD)", set_response.messages[0]["content"])

        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="Needy"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(team_cog, team_interaction)
        team_message = team_interaction.followup.messages[0]
        fields = {field.name: field.value for field in team_message["embed"].fields}
        self.assertIn("Needy — 85 OVR (GOLD)", fields["CM1 · CM 1"])
        team_file = team_message["file"]
        self.assertTrue(team_file.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
        team_file.close()

    async def test_sell_card_displays_rating_name_and_stored_type(self) -> None:
        user_id = "dryboomrun-seller"
        self.database.add_inventory(user_id, "Dryboomrun")
        command = InventoryCommands(self.database)

        success_response = FakeResponse()
        await InventoryCommands.sell.callback(
            command,
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=success_response,
            ),
            "Dryboomrun",
            1,
        )
        self.assertIn(
            "Sold **1× 87 Dryboomrun (Gold)** for 1,100,000 coins",
            success_response.messages[0]["content"],
        )

        insufficient_response = FakeResponse()
        await InventoryCommands.sell.callback(
            command,
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=insufficient_response,
            ),
            "Dryboomrun",
            1,
        )
        self.assertIn(
            "You own 0 copy/copies of 87 Dryboomrun (Gold).",
            insufficient_response.messages[0]["content"],
        )

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
        for command_name in ("card",):
            response = FakeResponse()
            await getattr(PlayerCommands, command_name).callback(
                player_cog,
                SimpleNamespace(response=response),
                "Gamer",
            )
            message = response.messages[0]
            self.assertEqual(message["embed"].title, "89 Gamer (Gold)")
            self.assertEqual(message["embed"].image.url, "attachment://gamer.png")
            details = {field.name: field.value for field in message["embed"].fields}
            self.assertEqual(details["Card Type"], "Gold")
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
            self.assertIn(
                "**89 Gamer (Gold)**",
                response.messages[0]["embed"].description,
            )
            self.assertIn("×1", response.messages[0]["embed"].description)

        team_cog = TeamCommands(self.database)
        self.database.set_formation(user_id, "Offensive")
        for position in ("WING1", "WING2"):
            response = FakeResponse()
            await TeamCommands.set_position.callback(
                team_cog,
                SimpleNamespace(user=SimpleNamespace(id=user_id), response=response),
                discord.app_commands.Choice(name=position, value=position),
                "Gamer",
            )
            self.assertEqual(self.database.get_lineup(user_id)[position].name, "Gamer")
            if position == "WING1":
                await TeamCommands.set_position.callback(
                    team_cog,
                    SimpleNamespace(user=SimpleNamespace(id=user_id), response=FakeResponse()),
                    discord.app_commands.Choice(name="WING1", value="WING1"),
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
            "Gamer — 89 OVR (GOLD)",
            {field.name: field.value for field in team_message["embed"].fields}[
                "WING2 · Wing 2"
            ],
        )
        lineup_file = team_message["file"]
        lineup_bytes = lineup_file.fp.read()
        lineup_file.close()
        self.assertTrue(lineup_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", lineup_bytes[16:24]), (1600, 1640))

    async def test_hala_gold_card_is_catalogued_grantable_and_lineup_ready(self) -> None:
        player = self.database.get_player("Hala (Gold 90)")
        self.assertIsNotNone(player)
        assert player is not None
        self.assertEqual(
            (
                player.overall,
                player.position,
                player.country,
                player.team,
                player.rarity,
                player.value,
                player.image_path,
            ),
            (90, "WI", "Scotland", "Colorado Avalanche", "GOLD", 3_000_000, "cards/hala.png"),
        )
        self.assertEqual(
            player.stats,
            {"SHO": 88, "DRI": 93, "SAV": 85, "PAS": 90, "DEF": 86, "ACT": 87},
        )
        image_path = resolve_card_image(player)
        self.assertIsNotNone(image_path)
        assert image_path is not None
        self.assertEqual(image_path.name, "hala.png")
        self.assertTrue(image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

        for command_name in ("card",):
            response = FakeResponse()
            await getattr(PlayerCommands, command_name).callback(
                PlayerCommands(self.database),
                SimpleNamespace(response=response),
                "Hala (Gold 90)",
            )
            message = response.messages[0]
            self.assertEqual(
                message["embed"].title,
                "90 Hala (Gold)",
            )
            self.assertEqual(message["embed"].image.url, "attachment://hala.png")
            details = {field.name: field.value for field in message["embed"].fields}
            self.assertEqual(details["Value"], "3,000,000 coins")
            for stat, value in {
                "SHO": 88,
                "DRI": 93,
                "SAV": 85,
                "PAS": 90,
                "DEF": 86,
                "ACT": 87,
            }.items():
                self.assertIn(f"**{stat}** {value}", details["Stats"])
            card_file = message["file"]
            self.assertTrue(card_file.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
            card_file.close()

        user_id = "hala-gold-card-user"
        await AdminCommands.grantcard.callback(
            AdminCommands(self.database),
            SimpleNamespace(response=FakeResponse()),
            SimpleNamespace(id=user_id, mention="<@hala-gold-card-user>"),
            "Hala (Gold 90)",
            1,
        )
        inventory_cog = InventoryCommands(self.database)
        for command_name in ("inventory", "collection"):
            response = FakeResponse()
            await getattr(InventoryCommands, command_name).callback(
                inventory_cog,
                SimpleNamespace(user=SimpleNamespace(id=user_id), response=response),
            )
            self.assertIn(
                "**90 Hala (Gold)**",
                response.messages[0]["embed"].description,
            )
            if command_name == "inventory":
                self.assertFalse(response.messages[0]["ephemeral"])

        team_cog = TeamCommands(self.database)
        self.database.set_formation(user_id, "Offensive")
        for position in ("WING1", "WING2"):
            await TeamCommands.set_position.callback(
                team_cog,
                SimpleNamespace(user=SimpleNamespace(id=user_id), response=FakeResponse()),
                discord.app_commands.Choice(name=position, value=position),
                "Hala (Gold 90)",
            )
            self.assertEqual(
                self.database.get_lineup(user_id)[position].name,
                "Hala (Gold 90)",
            )
            if position == "WING1":
                await TeamCommands.set_position.callback(
                    team_cog,
                    SimpleNamespace(
                        user=SimpleNamespace(id=user_id),
                        response=FakeResponse(),
                    ),
                    discord.app_commands.Choice(name="WING1", value="WING1"),
                    "CLEAR",
                )

        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="Hala"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(team_cog, team_interaction)
        team_message = team_interaction.followup.messages[0]
        self.assertIn(
            "Hala — 90 OVR (GOLD)",
            {field.name: field.value for field in team_message["embed"].fields}[
                "WING2 · Wing 2"
            ],
        )
        lineup_file = team_message["file"]
        lineup_bytes = lineup_file.fp.read()
        lineup_file.close()
        self.assertTrue(lineup_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", lineup_bytes[16:24]), (1600, 1640))

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
        for command_name in ("card",):
            response = FakeResponse()
            interaction = SimpleNamespace(response=response)
            command = getattr(PlayerCommands, command_name)
            await command.callback(player_cog, interaction, "XavierCapone9")
            message = response.messages[0]
            embed = message["embed"]
            self.assertEqual(embed.title, "91 XavierCapone9 (Gold)")
            self.assertEqual(embed.image.url, "attachment://xaviercapone9.png")
            details = {field.name: field.value for field in embed.fields}
            self.assertEqual(details["Position"], "WI")
            self.assertEqual(details["Country"], "England")
            self.assertEqual(details["Team"], "Vegas Golden Knights")
            self.assertEqual(details["Card Type"], "Gold")
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
        self.assertIn(
            "Granted **1× 91 XavierCapone9 (Gold)**",
            admin_response.messages[0]["content"],
        )

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
            self.assertIn(
                "**91 XavierCapone9 (Gold)**",
                response.messages[0]["embed"].description,
            )
            self.assertIn("×1", response.messages[0]["embed"].description)

        team_cog = TeamCommands(self.database)
        self.database.set_formation(user_id, "Offensive")
        for position in ("WING1", "WING2"):
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
            self.assertIn(
                "Assigned **XavierCapone9 — 91 OVR (GOLD)**",
                response.messages[0]["content"],
            )
            self.assertEqual(self.database.get_lineup(user_id)[position].name, "XavierCapone9")
            if position == "WING1":
                await TeamCommands.set_position.callback(
                    team_cog,
                    SimpleNamespace(
                        user=SimpleNamespace(id=user_id),
                        response=FakeResponse(),
                    ),
                    discord.app_commands.Choice(name="WING1", value="WING1"),
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
            "XavierCapone9 — 91 OVR (GOLD)",
            {field.name: field.value for field in team_message["embed"].fields}[
                "WING2 · Wing 2"
            ],
        )
        lineup_file = team_message["file"]
        lineup_bytes = lineup_file.fp.read()
        lineup_file.close()
        self.assertTrue(lineup_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", lineup_bytes[16:24]), (1600, 1640))

    async def test_lineup_rink_svg_uses_ice_and_hockey_markings(self) -> None:
        svg = build_lineup_svg({}, "Balanced", "Not rated yet")

        self.assertIn('stop-color="#f7fafc"', svg)
        self.assertIn('stop-color="#e5edf2"', svg)
        self.assertIn('stroke="#d52b3e"', svg)
        self.assertEqual(svg.count('stroke="#2867a6"'), 2)
        self.assertIn('x1="800" y1="165" x2="800" y2="1025"', svg)
        self.assertIn('cx="800" cy="575" r="112"', svg)
        self.assertEqual(svg.count('r="68" fill="none"'), 6)
        self.assertIn('cx="360" cy="380" r="68"', svg)
        self.assertIn('cx="1240" cy="740" r="68"', svg)
        self.assertIn('x1="152" y1="165" x2="152" y2="1025"', svg)
        self.assertIn('fill-opacity="0.12" stroke="#d52b3e"', svg)
        self.assertIn('width="1484" height="900" rx="90"', svg)
        self.assertIn(">THSL STARTING LINEUP<", svg)
        self.assertIn('fill="#fff" stroke="#d52b3e"', svg)
        for position in FORMATION_STARTERS["Balanced"]:
            layout = STARTER_SLOT_LAYOUTS[position]
            self.assertGreaterEqual(layout.y, 163)
            self.assertLessEqual(layout.y + layout.height, 1027)

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
        self.assertFalse(menu["ephemeral"])
        field_values = {field.name: field.value for field in menu["embed"].fields}
        self.assertIn("Dan", field_values["GOLD · 5,000,000 coins"])
        self.assertIn("XavierCapone9", field_values["GOLD · 5,000,000 coins"])
        self.assertIn("Gamer", field_values["GOLD · 5,000,000 coins"])
        self.assertIn("90 Hala (Gold)", field_values["GOLD · 5,000,000 coins"])
        for label in (
            "87 Dan (Gold)",
            "91 XavierCapone9 (Gold)",
            "89 Gamer (Gold)",
            "90 Hala (Gold)",
            "87 Dryboomrun (Gold)",
            "85 Needy (Gold)",
            "86 Fadi (Gold)",
        ):
            self.assertIn(label, field_values["GOLD · 5,000,000 coins"])
        self.assertIn("16.67%", field_values["GOLD · 5,000,000 coins"])
        self.assertIn("8.33%", field_values["GOLD · 5,000,000 coins"])
        self.assertIn("Very rare ×0.25", menu["embed"].description)
        self.assertIn("93 Licha (TOTW)", field_values["TOTW · 20,000,000 coins"])
        self.assertIn("93 Hala (TOTW)", field_values["TOTW · 20,000,000 coins"])
        self.assertIn("50.00%", field_values["TOTW · 20,000,000 coins"])

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

        self.assertFalse(purchase_response.messages[0]["ephemeral"])
        self.assertIn("opened a **GOLD pack**", purchase_response.messages[0]["content"])
        self.assertEqual(self.database.get_balance(user_id), 250)
        purchased_inventory = self.database.get_inventory(user_id)
        self.assertEqual(len(purchased_inventory), 1)
        self.assertIn(
            purchased_inventory[0].player.name,
            {
                "Dan",
                "XavierCapone9",
                "Gamer",
                "Hala (Gold 90)",
                "Dryboomrun",
                "Needy",
                "Fadi",
                "Mason",
                "Demox",
                "Cheddar",
            },
        )
        self.assertEqual(purchased_inventory[0].player.rarity, "GOLD")
        self.assertEqual(
            purchase_response.messages[0]["embed"].title,
            player_card_label(purchased_inventory[0].player),
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
        self.assertFalse(duplicate_response.messages[0]["ephemeral"])
        self.assertIn("already been used", duplicate_response.messages[0]["content"])
        self.assertEqual(self.database.get_balance(user_id), 250)
        self.assertEqual(self.database.get_inventory(user_id)[0].quantity, 1)
        uploaded_file.close()

    async def test_pack_command_selects_and_purchases_both_pack_types(self) -> None:
        pack_cases = (
            (
                "GOLD",
                GOLD_PACK_COST,
                {
                    "Dan",
                    "XavierCapone9",
                    "Gamer",
                    "90 Hala (Gold)",
                    "Dryboomrun",
                    "Needy",
                    "Fadi",
                },
                "16.67%",
            ),
            ("TOTW", TOTW_PACK_COST, {"Licha", "Hala"}, "50.00%"),
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
                self.assertFalse(menu["ephemeral"])
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
                self.assertFalse(purchase_response.messages[0]["ephemeral"])
                self.assertEqual(self.database.get_balance(user_id), 321)
                inventory = self.database.get_inventory(user_id)
                self.assertEqual(len(inventory), 1)
                self.assertEqual(inventory[0].player.rarity, rarity)
                self.assertEqual(inventory[0].quantity, 1)
                if rarity == "TOTW":
                    card_fields = {
                        field.name: field.value
                        for field in purchase_response.messages[0]["embed"].fields
                    }
                    self.assertEqual(card_fields["Value"], "12,000,000 coins")
                self.assertEqual(
                    purchase_response.messages[0]["embed"].title,
                    player_card_label(inventory[0].player),
                )
                purchase_response.messages[0]["file"].close()

    async def test_pack_error_paths_are_public(self) -> None:
        detached_button = PackBuyButton("GOLD", GOLD_PACK_COST)
        detached_response = FakeResponse()
        await detached_button.callback(
            SimpleNamespace(response=detached_response)
        )
        self.assertFalse(detached_response.messages[0]["ephemeral"])

        menu_response = FakeResponse()
        await PackCommands.packs.callback(
            PackCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id="menu-owner"),
                response=menu_response,
            ),
        )
        view = menu_response.messages[0]["view"]
        other_response = FakeResponse()
        allowed = await view.interaction_check(
            SimpleNamespace(
                user=SimpleNamespace(id="different-user"),
                response=other_response,
            )
        )
        self.assertFalse(allowed)
        self.assertFalse(other_response.messages[0]["ephemeral"])

        empty_catalog = Database(Path(self.temp_dir.name) / "public-empty-pack.sqlite3")
        empty_catalog.initialize()
        no_card_response = FakeResponse()
        await PackCommands(empty_catalog)._open(
            SimpleNamespace(
                user=SimpleNamespace(id="public-no-card-user"),
                response=no_card_response,
            ),
            rarity="TOTW",
            cost=TOTW_PACK_COST,
            purchase_id=12001,
        )
        self.assertFalse(no_card_response.messages[0]["ephemeral"])

        poor_response = FakeResponse()
        await PackCommands(self.database)._open(
            SimpleNamespace(
                user=SimpleNamespace(id="public-poor-user"),
                response=poor_response,
            ),
            rarity="GOLD",
            cost=GOLD_PACK_COST,
            purchase_id=12002,
        )
        self.assertFalse(poor_response.messages[0]["ephemeral"])

    async def test_set_autocomplete_selects_owned_hala_versions_by_exact_id(self) -> None:
        user_id = "hala-set-user"
        gold_hala = self.database.get_player("Hala (Gold 90)")
        totw_hala = self.database.get_player("Hala")
        assert gold_hala is not None and totw_hala is not None
        self.database.add_inventory(user_id, gold_hala.name)
        self.database.add_inventory(user_id, totw_hala.name)
        cog = TeamCommands(self.database)

        choices = await TeamCommands.set_player_autocomplete(
            cog,
            SimpleNamespace(user=SimpleNamespace(id=user_id)),
            "Hala",
        )
        self.assertEqual(
            [(choice.name, choice.value) for choice in choices],
            [
                ("Hala — 90 OVR (GOLD)", str(gold_hala.id)),
                ("Hala — 93 OVR (TOTW)", str(totw_hala.id)),
            ],
        )

        for position, card in (("WING1", gold_hala), ("CM1", totw_hala)):
            response = FakeResponse()
            await TeamCommands.set_position.callback(
                cog,
                SimpleNamespace(user=SimpleNamespace(id=user_id), response=response),
                discord.app_commands.Choice(name=position, value=position),
                str(card.id),
            )
            self.assertIn("Assigned", response.messages[0]["content"])
        lineup = self.database.get_lineup(user_id)
        self.assertEqual(lineup["WING1"].id, gold_hala.id)
        self.assertEqual(lineup["WING1"].rarity, "GOLD")
        self.assertEqual(lineup["CM1"].id, totw_hala.id)
        self.assertEqual(lineup["CM1"].rarity, "TOTW")

        not_owned_response = FakeResponse()
        await TeamCommands.set_position.callback(
            cog,
            SimpleNamespace(user=SimpleNamespace(id="other-user"), response=not_owned_response),
            discord.app_commands.Choice(name="CM 1", value="CM1"),
            str(totw_hala.id),
        )
        self.assertTrue(not_owned_response.messages[0]["ephemeral"])
        self.assertIn("must own", not_owned_response.messages[0]["content"])

        invalid_response = FakeResponse()
        await TeamCommands.set_position.callback(
            cog,
            SimpleNamespace(user=SimpleNamespace(id=user_id), response=invalid_response),
            discord.app_commands.Choice(name="GK", value="GK"),
            str(gold_hala.id),
        )
        self.assertTrue(invalid_response.messages[0]["ephemeral"])
        self.assertIn("Only GK cards", invalid_response.messages[0]["content"])

        duplicate_response = FakeResponse()
        await TeamCommands.set_position.callback(
            cog,
            SimpleNamespace(user=SimpleNamespace(id=user_id), response=duplicate_response),
            discord.app_commands.Choice(name="SUB1", value="SUB1"),
            str(gold_hala.id),
        )
        self.assertIn("already assigned", duplicate_response.messages[0]["content"])

    async def test_set_formation_and_team_commands_work_together(self) -> None:
        user_id = "team-command-user"
        all_players = (
            "XavierCapone9",
            "Gamer",
            "Dan",
            "Fadi",
            "Licha",
            "Dryboomrun",
            "Needy",
        )
        for name in all_players:
            self.database.add_inventory(user_id, name)
        cog = TeamCommands(self.database)
        assignments = (
            ("WING1", "XavierCapone9"),
            ("CM1", "Dan"),
            ("GK", "Licha"),
            ("SUB1", "Dryboomrun"),
            ("SUB2", "Needy"),
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

        offensive_response = FakeResponse()
        await TeamCommands.formation.callback(
            cog,
            SimpleNamespace(user=SimpleNamespace(id=user_id), response=offensive_response),
            discord.app_commands.Choice(name="Offensive", value="Offensive"),
        )
        self.assertIn("(2 Wings · 1 GK)", offensive_response.messages[0]["content"])
        self.assertIn("Dan", offensive_response.messages[0]["content"])
        self.assertNotIn("Dan", self.database.get_lineup(user_id))

        await TeamCommands.set_position.callback(
            cog,
            SimpleNamespace(user=SimpleNamespace(id=user_id), response=FakeResponse()),
            discord.app_commands.Choice(name="Wing 2", value="WING2"),
            "Gamer",
        )
        defensive_response = FakeResponse()
        await TeamCommands.formation.callback(
            cog,
            SimpleNamespace(user=SimpleNamespace(id=user_id), response=defensive_response),
            discord.app_commands.Choice(name="Defensive", value="Defensive"),
        )
        self.assertIn("2 CMs · 1 GK", defensive_response.messages[0]["content"])
        self.assertIn("XavierCapone9", defensive_response.messages[0]["content"])
        self.assertIn("Gamer", defensive_response.messages[0]["content"])
        self.assertIn("were not moved to another role", defensive_response.messages[0]["content"])

        for position, player in (("CM1", "Dan"), ("CM2", "Fadi")):
            await TeamCommands.set_position.callback(
                cog,
                SimpleNamespace(user=SimpleNamespace(id=user_id), response=FakeResponse()),
                discord.app_commands.Choice(name=position, value=position),
                player,
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
        self.assertIn("89 OVR", fields["Starting lineup OVR"])
        self.assertIn("Defensive", fields["Formation"])
        self.assertEqual(fields["Team value"], "**15,680,000 coins** · 3 starters and up to 2 substitutes")
        self.assertEqual(
            {name.split(" · ", 1)[0] for name in fields if " · " in name},
            set(FORMATION_STARTERS["Defensive"] + SUBSTITUTE_POSITIONS),
        )
        self.assertEqual(embed.image.url, f"attachment://{OUTPUT_FILENAME}")
        attachment = team_message["file"]
        self.assertEqual(attachment.filename, OUTPUT_FILENAME)
        image_bytes = attachment.fp.read()
        attachment.close()
        self.assertTrue(image_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", image_bytes[16:24]), (1600, 1640))
        svg = build_lineup_svg(self.database.get_lineup(user_id), "Defensive", "89 OVR")
        self.assertEqual(svg.count("<image href="), 5)
        for position in FORMATION_STARTERS["Defensive"]:
            layout = STARTER_SLOT_LAYOUTS[position]
            self.assertGreaterEqual(layout.x, 76)
            self.assertGreaterEqual(layout.y, 163)
            self.assertLessEqual(layout.x + layout.width, 1524)
            self.assertLessEqual(layout.y + layout.height, 1027)
        for layout in SUBSTITUTE_SLOT_LAYOUTS:
            self.assertGreater(layout.y, 1045)

    async def test_team_image_renders_empty_starting_substitute_and_mixed_lineups(self) -> None:
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

        assignments_by_case = {
            "empty": (),
            "starting": (
                ("WING1", "XavierCapone9"),
                ("CM1", "Fadi"),
                ("GK", "Licha"),
            ),
            "substitutes": (("SUB1", "Dryboomrun"), ("SUB2", "Needy")),
            "mixed": (
                ("WING1", "XavierCapone9"),
                ("CM1", "Fadi"),
                ("GK", "Missing Art"),
                ("SUB2", "Needy"),
            ),
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
                self.assertEqual(struct.unpack(">II", image_bytes[16:24]), (1600, 1640))
                image_outputs[case_name] = image_bytes

                fields = {field.name: field.value for field in message["embed"].fields}
                for position in FORMATION_STARTERS["Balanced"] + SUBSTITUTE_POSITIONS:
                    self.assertIn(position + " · ", " ".join(fields))
                for position, player_name in assignments:
                    self.assertIn(player_name, fields[f"{position} · " + {
                        "WING1": "Wing 1",
                        "WING2": "Wing 2",
                        "CM1": "CM 1",
                        "CM2": "CM 2",
                        "GK": "Goalie",
                        "SUB1": "Substitute 1",
                        "SUB2": "Substitute 2",
                    }[position]])

        self.assertEqual(len(set(image_outputs.values())), len(assignments_by_case))
        empty_svg = build_lineup_svg({}, "Balanced", "Not rated yet")
        self.assertEqual(empty_svg.count('id="slot-'), 5)
        for position in FORMATION_STARTERS["Balanced"] + SUBSTITUTE_POSITIONS:
            slot_start = empty_svg.index(f'id="slot-{position.lower()}"')
            slot_end = empty_svg.index("</g>", slot_start)
            self.assertIn('fill="none" stroke="#8b9ba3"', empty_svg[slot_start:slot_end])

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
        self.assertIn("NO ART", svg)

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
        self.assertEqual(
            response.messages[0]["content"],
            "Added **93 Test Licha (TOTW)** to the catalog.",
        )

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

        self.assertFalse(response.messages[0]["ephemeral"])
        embed = response.messages[0]["embed"]
        self.assertIn("No eligible cards", embed.fields[0].value)
        totw_button = response.messages[0]["view"].children[0]
        self.assertTrue(totw_button.disabled)
        self.assertEqual(empty_catalog.get_balance(user_id), starting_balance)
        self.assertEqual(empty_catalog.get_inventory(user_id), [])


class FakeGameChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.messages: list[dict[str, object]] = []

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        self.messages.append({"content": content, **kwargs})


class MinigameBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "minigame.sqlite3")
        self.database.initialize(load_player_seed())
        self.cog = MinigameCommands(self.database)
        self.views: list[IndividualMinigameView] = []

    async def asyncTearDown(self) -> None:
        tasks = []
        for view in self.views:
            view.stop()
            view.cancel_deadline()
            if view._deadline_task is not None:
                tasks.append(view._deadline_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.temp_dir.cleanup()

    @staticmethod
    def make_user(user_id: int, username: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=user_id,
            name=username,
            display_name=username,
            mention=f"<@{user_id}>",
        )

    def make_interaction(
        self,
        *,
        user_id: int = 1000,
        username: str = "GameOwner",
        guild_id: int = 123,
        channel: FakeGameChannel | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            user=self.make_user(user_id, username),
            guild=SimpleNamespace(id=guild_id),
            channel=channel or FakeGameChannel(guild_id * 10),
            response=FakeResponse(),
            original_response=AsyncMock(return_value=FakeMessage()),
        )

    async def start_game(
        self,
        *,
        user_id: int = 1000,
        username: str = "GameOwner",
        guild_id: int = 123,
        selected: object | None = None,
        channel: FakeGameChannel | None = None,
    ) -> tuple[SimpleNamespace, FakeGameChannel, object, IndividualMinigameView]:
        player = selected if selected is not None else self.database.get_players()[0]
        game_channel = channel or FakeGameChannel(guild_id * 10)
        interaction = self.make_interaction(
            user_id=user_id,
            username=username,
            guild_id=guild_id,
            channel=game_channel,
        )
        with patch("commands.minigame.random.choice", return_value=player):
            await MinigameCommands.minigame.callback(self.cog, interaction)
        view = interaction.response.messages[0]["view"]
        self.views.append(view)
        return interaction, game_channel, player, view

    async def submit_answer(
        self,
        view: IndividualMinigameView,
        user: SimpleNamespace,
        answer: str,
    ) -> FakeResponse:
        response = FakeResponse()
        await view.submit_answer(
            SimpleNamespace(user=user, response=response),
            answer,
        )
        return response

    async def open_answer_modal(
        self,
        view: IndividualMinigameView,
        user: SimpleNamespace,
    ) -> GuessAnswerModal:
        response = FakeResponse()
        await view.answer_button.callback(
            SimpleNamespace(user=user, response=response)
        )
        self.assertEqual(len(response.modals), 1)
        modal = response.modals[0]
        self.assertIsInstance(modal, GuessAnswerModal)
        return modal

    async def test_start_is_public_and_shows_three_clues_with_answer_button(self) -> None:
        interaction, _, player, view = await self.start_game()
        response = interaction.response.messages[0]
        self.assertFalse(response["ephemeral"])
        self.assertEqual(response["view"], view)
        self.assertEqual(view.answer_button.label, "Answer")
        self.assertEqual(view.timeout, MINIGAME_DURATION_SECONDS)
        embed = response["embed"]
        self.assertEqual(embed.title, "🏒 GUESS WHO?")
        self.assertEqual(
            [field.name for field in embed.fields],
            ["🌍 Nationality", "⭐ Rating", "🏒 Position"],
        )
        self.assertEqual(embed.fields[0].value, player.country)
        self.assertEqual(embed.fields[1].value, f"{player.overall} OVR")
        self.assertEqual(embed.fields[2].value, player.position)
        self.assertNotIn(player.name.casefold(), embed.description.casefold())
        self.assertIsNone(embed.image.url)
        self.assertEqual(view.owner_id, interaction.user.id)

    async def test_two_members_get_independent_games_and_player_selections(self) -> None:
        players = self.database.get_players()
        _, first_channel, first_player, first_view = await self.start_game(
            user_id=1101,
            username="Alice",
            selected=players[0],
        )
        _, second_channel, second_player, second_view = await self.start_game(
            user_id=1102,
            username="Bob",
            selected=players[1],
        )
        self.assertIsNot(first_view, second_view)
        self.assertNotEqual(first_player.id, second_player.id)
        self.assertEqual(first_view.player.id, players[0].id)
        self.assertEqual(second_view.player.id, players[1].id)
        self.assertEqual(first_channel.messages, [])
        self.assertEqual(second_channel.messages, [])

        await self.submit_answer(first_view, self.make_user(1101, "Alice"), first_player.name)
        self.assertTrue(first_view.finished)
        self.assertFalse(second_view.finished)
        self.assertEqual(self.database.get_balance(1101), MINIGAME_REWARD)
        self.assertEqual(self.database.get_balance(1102), 0)

        await self.submit_answer(second_view, self.make_user(1102, "Bob"), second_player.name)
        self.assertTrue(second_view.finished)
        self.assertEqual(self.database.get_balance(1102), MINIGAME_REWARD)

    async def test_only_game_owner_can_open_or_submit_an_answer(self) -> None:
        _, channel, player, view = await self.start_game(
            user_id=1201,
            username="Owner",
        )
        stranger = self.make_user(1202, "Stranger")
        response = FakeResponse()
        await view.answer_button.callback(
            SimpleNamespace(user=stranger, response=response)
        )
        self.assertTrue(response.messages[0]["ephemeral"])
        self.assertEqual(response.modals, [])

        response = await self.submit_answer(view, stranger, player.name)
        self.assertTrue(response.messages[0]["ephemeral"])
        self.assertFalse(view.finished)
        self.assertEqual(self.database.get_balance(1202), 0)
        self.assertEqual(channel.messages, [])

    async def test_incorrect_answer_is_private_and_owner_can_retry(self) -> None:
        _, channel, player, view = await self.start_game(
            user_id=1301,
            username="RetryPlayer",
        )
        owner = self.make_user(1301, "RetryPlayer")
        incorrect = await self.submit_answer(view, owner, "not the answer")
        self.assertTrue(incorrect.messages[0]["ephemeral"])
        self.assertIn("incorrect", incorrect.messages[0]["content"].lower())
        self.assertFalse(view.finished)
        self.assertEqual(self.database.get_balance(1301), 0)
        self.assertEqual(channel.messages, [])

        modal = await self.open_answer_modal(view, owner)
        modal.player_name._value = f"  {player.name.swapcase()}   "
        response = FakeResponse()
        await modal.on_submit(SimpleNamespace(user=owner, response=response))
        self.assertTrue(response.messages[0]["ephemeral"])
        self.assertIn("Correct", response.messages[0]["content"])
        self.assertEqual(self.database.get_balance(1301), MINIGAME_REWARD)

    async def test_correct_answer_pays_exactly_once_and_reveals_full_card(self) -> None:
        _, channel, player, view = await self.start_game(
            user_id=1401,
            username="Winner",
        )
        owner = self.make_user(1401, "Winner")
        modal = await self.open_answer_modal(view, owner)
        modal.player_name._value = player.name
        response = FakeResponse()
        await modal.on_submit(SimpleNamespace(user=owner, response=response))

        self.assertTrue(view.finished)
        self.assertTrue(response.messages[0]["ephemeral"])
        self.assertEqual(
            Database(self.database.path).get_balance(1401),
            500_000,
        )
        self.assertEqual(len(channel.messages), 1)
        reveal = channel.messages[0]
        embed = reveal["embed"]
        self.assertIn("Winner", embed.description)
        self.assertIn("500,000 coins", embed.description)
        self.assertIn(player.name, embed.title)
        self.assertIn("attachment://", embed.image.url)
        self.assertTrue({"Position", "Country", "Team", "Card Type", "Stats"} <= {
            field.name for field in embed.fields
        })
        upload = reveal["file"]
        self.assertEqual(upload.filename, Path(player.image_path).name)
        self.assertTrue(upload.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
        upload.close()

    async def test_duplicate_correct_submissions_cannot_pay_twice(self) -> None:
        _, channel, player, view = await self.start_game(
            user_id=1501,
            username="OneWinner",
        )
        owner = self.make_user(1501, "OneWinner")
        responses = [FakeResponse(), FakeResponse()]
        await asyncio.gather(
            view.submit_answer(
                SimpleNamespace(user=owner, response=responses[0]),
                player.name,
            ),
            view.submit_answer(
                SimpleNamespace(user=owner, response=responses[1]),
                player.name,
            ),
        )
        self.assertEqual(self.database.get_balance(1501), 500_000)
        self.assertEqual(len(channel.messages), 1)
        self.assertTrue(view.finished)
        self.assertTrue(all(response.messages[0]["ephemeral"] for response in responses))

    async def test_timeout_expires_game_without_reward_and_reveals_card(self) -> None:
        self.assertEqual(MINIGAME_DURATION_SECONDS, 60)
        self.cog.duration_seconds = 0.01
        _, channel, player, view = await self.start_game(
            user_id=1601,
            username="ExpiredPlayer",
        )
        self.assertEqual(view.timeout, 0.01)
        assert view._deadline_task is not None
        await asyncio.wait_for(view._deadline_task, timeout=1)

        self.assertTrue(view.finished)
        self.assertTrue(view.answer_button.disabled)
        self.assertEqual(self.database.get_balance(1601), 0)
        self.assertEqual(len(channel.messages), 1)
        reveal = channel.messages[0]
        self.assertIn("Time is up", reveal["embed"].description)
        self.assertIn(player.name, reveal["embed"].title)
        self.assertIn("attachment://", reveal["embed"].image.url)
        upload = reveal["file"]
        self.assertEqual(upload.filename, Path(player.image_path).name)
        upload.close()

    async def test_empty_catalog_is_handled_without_starting_a_game(self) -> None:
        empty_database = Database(Path(self.temp_dir.name) / "empty.sqlite3")
        empty_database.initialize()
        empty_cog = MinigameCommands(empty_database)
        interaction = self.make_interaction(user_id=1701, guild_id=601)
        await MinigameCommands.minigame.callback(empty_cog, interaction)
        response = interaction.response.messages[0]
        self.assertFalse(response["ephemeral"])
        self.assertIn("no player cards", response["content"].lower())
        self.assertNotIn("view", response)

    def test_reward_is_fixed_at_500000(self) -> None:
        self.assertEqual(MINIGAME_REWARD, 500_000)


if __name__ == "__main__":
    unittest.main()
