from __future__ import annotations

import asyncio
import tempfile
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

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
    FORMATION_SLOT_LAYOUTS,
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    OUTPUT_FILENAME,
    RINK_BOTTOM,
    RINK_LEFT,
    RINK_RIGHT,
    RINK_TOP,
    SUBSTITUTE_SLOT_LAYOUTS,
    _player_image_uri,
    build_lineup_svg,
)
from commands.minigame import (
    MINIGAME_DURATION_SECONDS,
    MINIGAME_REWARD,
    GuessAnswerModal,
    IndividualMinigameView,
    MinigameCommands,
)
from commands.packs import FreePackOpenButton, PackBuyButton, PackCommands
from commands.player import PlayerCommands
from commands.profile import ProfileCommands
from commands.team import TeamCommands, lineup_card_label
from commands.information import InformationCommands
from draft_simulation import generate_draft_groups
from database import (
    Database,
    FORMATION_STARTERS,
    GOLD_PACK_COST,
    InventoryEntry,
    InvalidLineupPosition,
    LINEUP_POSITIONS,
    S1_TOTS_PACK_COST,
    S1_TOTY_PACK_COST,
    S2_TOTS_PACK_COST,
    SUBSTITUTE_POSITIONS,
    TOTW_PACK_COST,
    format_card_type,
)
from main import create_bot, load_player_seed

EXPECTED_COMMANDS = {
    "card",
    "cards",
    "claim",
    "cooldowns",
    "sellduplicates",
    "sellall",
    "profile",
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
    "resetall",
    "minigame",
    "match",
    "auction",
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

    async def edit_message(
        self,
        content: str | None = None,
        **kwargs: object,
    ) -> None:
        self.messages.append({"content": content, **kwargs, "edited": True})

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

    def assert_pack_menu_shows_odds(self, embed: discord.Embed) -> None:
        for field in embed.fields:
            self.assertIn("Card odds:", field.value)
            self.assertRegex(field.value, r"\b\d+\.\d+%")

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
        sync.assert_has_awaits([call(), call(guild=guild)])
        self.assertEqual(sync.await_count, 2)

    async def test_default_permissions_are_public_except_admin_commands(self) -> None:
        await load_command_modules(self.bot)
        commands = {
            command.name: command for command in self.bot.tree.get_commands()
        }
        admin_only = {
            "addplayer",
            "setbalance",
            "grantcoins",
            "grantcard",
            "resetall",
        }
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
                elif isinstance(command, discord.app_commands.Group):
                    self.assertIsNone(command.default_permissions)
                else:
                    self.assertIsNone(command.default_permissions)
                    self.assertFalse(command.checks)

        for name in (
            "claim",
            "minigame",
            "cooldowns",
            "sellduplicates",
            "sellall",
            "profile",
        ):
            with self.subTest(public_command=name):
                self.assertIsNone(commands[name].default_permissions)
                self.assertFalse(commands[name].checks)

        auction_group = commands["auction"]
        self.assertIsNone(auction_group.default_permissions)
        self.assertEqual(
            {command.name for command in auction_group.commands},
            {"sell", "browse", "myauctions", "cancel"},
        )
        for command in auction_group.commands:
            with self.subTest(auction_subcommand=command.name):
                self.assertIsNone(command.default_permissions)
        auction_commands = {command.name: command for command in auction_group.commands}
        sell_parameters = {
            parameter.name: parameter
            for parameter in auction_commands["sell"].parameters
        }
        self.assertEqual(
            [choice.value for choice in sell_parameters["duration"].choices],
            [1_800, 3_600, 21_600, 86_400],
        )

        minigame_parameters = {
            parameter.name: parameter for parameter in commands["minigame"].parameters
        }
        self.assertEqual(
            [choice.value for choice in minigame_parameters["mode"].choices],
            ["guess", "draft"],
        )

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
            ["GOLD", "TOTW", "S2 TOTS", "S1 TOTS", "S1 TOTY"],
        )

    async def test_resetall_denies_non_admin_and_cancel_preserves_progress(self) -> None:
        user_id = "reset-cancel-user"
        self.database.set_balance(user_id, 500)
        self.database.add_inventory(user_id, "Dan")
        cog = AdminCommands(self.database)

        denied_response = FakeResponse()
        await AdminCommands.resetall.callback(
            cog,
            SimpleNamespace(
                user=SimpleNamespace(
                    id=301,
                    guild_permissions=SimpleNamespace(administrator=False),
                ),
                guild=SimpleNamespace(id=401),
                response=denied_response,
            ),
        )
        self.assertIn("administrator permission", denied_response.messages[0]["content"])
        self.assertEqual(self.database.get_balance(user_id), 500)

        admin = SimpleNamespace(
            id=302,
            guild_permissions=SimpleNamespace(administrator=True),
        )
        command_response = FakeResponse()
        await AdminCommands.resetall.callback(
            cog,
            SimpleNamespace(
                user=admin,
                guild=SimpleNamespace(id=401),
                response=command_response,
            ),
        )
        warning = command_response.messages[0]
        self.assertTrue(warning["ephemeral"])
        self.assertIn("⚠️ **RESET ALL PLAYER DATA**", warning["content"])
        view = warning["view"]
        self.assertEqual(
            [button.label for button in view.children],
            ["🔴 Confirm Reset", "🟢 Cancel"],
        )

        outsider_response = FakeResponse()
        allowed = await view.interaction_check(
            SimpleNamespace(
                user=SimpleNamespace(id=999),
                response=outsider_response,
            )
        )
        self.assertFalse(allowed)
        self.assertIn("Only the administrator", outsider_response.messages[0]["content"])

        cancel_interaction = SimpleNamespace(
            user=admin,
            guild=SimpleNamespace(id=401),
            response=FakeResponse(),
        )
        self.assertTrue(await view.interaction_check(cancel_interaction))
        cancel_button = next(button for button in view.children if button.label == "🟢 Cancel")
        await cancel_button.callback(cancel_interaction)
        self.assertIn(
            "Reset cancelled",
            cancel_interaction.response.messages[0]["content"],
        )
        self.assertEqual(self.database.get_balance(user_id), 500)
        self.assertEqual(len(self.database.get_inventory(user_id)), 1)

    async def test_resetall_confirm_button_resets_everyone_and_audits_admin(self) -> None:
        self.database.set_balance("reset-confirm-one", 900)
        self.database.set_balance("reset-confirm-two", 1_200)
        self.database.add_inventory("reset-confirm-one", "Dan")
        self.database.award_xp("reset-confirm-one", "confirm-xp", 500, "test")

        admin = SimpleNamespace(
            id=303,
            guild_permissions=SimpleNamespace(administrator=True),
        )
        command_response = FakeResponse()
        cog = AdminCommands(self.database)
        await AdminCommands.resetall.callback(
            cog,
            SimpleNamespace(
                user=admin,
                guild=SimpleNamespace(id=402),
                response=command_response,
            ),
        )
        view = command_response.messages[0]["view"]
        confirm_interaction = SimpleNamespace(
            user=admin,
            guild=SimpleNamespace(id=402),
            response=FakeResponse(),
        )
        self.assertTrue(await view.interaction_check(confirm_interaction))
        confirm_button = next(
            button for button in view.children if button.label == "🔴 Confirm Reset"
        )
        await confirm_button.callback(confirm_interaction)

        result = confirm_interaction.response.messages[0]
        self.assertIn("Reset complete", result["content"])
        self.assertIn("**2** registered players", result["content"])
        self.assertEqual(self.database.get_balance("reset-confirm-one"), 0)
        self.assertEqual(self.database.get_balance("reset-confirm-two"), 0)
        self.assertEqual(self.database.get_inventory("reset-confirm-one"), [])
        self.assertEqual(self.database.get_xp_profile("reset-confirm-one").level, 1)
        self.assertIsNotNone(self.database.get_player("Dan"))
        with self.database._read_connection() as connection:
            audit = connection.execute(
                "SELECT administrator_id, guild_id, affected_players "
                "FROM admin_reset_audit"
            ).fetchone()
        self.assertEqual(tuple(audit), ("303", "402", 2))

    async def test_free_gold_and_totw_level_packs_open_once_without_coins(self) -> None:
        cog = PackCommands(self.database)
        gold_user = "free-gold-packs"
        self.database.award_xp(gold_user, "gold-pack-levels", 1_200, "test")
        self.assertEqual(self.database.get_free_pack_inventory(gold_user)["GOLD"], 2)
        self.assertEqual(self.database.get_balance(gold_user), 0)

        gold_menu_response = FakeResponse()
        await PackCommands.pack.callback(
            cog,
            SimpleNamespace(
                user=SimpleNamespace(id=gold_user),
                response=gold_menu_response,
            ),
            discord.app_commands.Choice(name="GOLD Pack", value="GOLD"),
        )
        gold_menu = gold_menu_response.messages[0]
        self.assertIn("- Gold Packs: 2", gold_menu["embed"].description)
        gold_button = next(
            button
            for button in gold_menu["view"].children
            if isinstance(button, FreePackOpenButton)
        )
        self.assertEqual(gold_button.quantity, 2)

        first_open = SimpleNamespace(
            user=SimpleNamespace(id=gold_user),
            response=FakeResponse(),
            id=70_001,
            message=FakeMessage(),
        )
        await gold_button.callback(first_open)
        first_result = first_open.response.messages[0]
        self.assertIn("FREE Gold Pack", first_result["content"])
        self.assertEqual(self.database.get_balance(gold_user), 0)
        self.assertEqual(self.database.get_free_pack_inventory(gold_user)["GOLD"], 1)
        self.assertEqual(
            sum(entry.quantity for entry in self.database.get_inventory(gold_user)),
            1,
        )
        first_card = first_result["embed"]
        gold_inventory = self.database.get_inventory(gold_user)
        self.assertIn(str(gold_inventory[0].player.overall), first_card.title)
        card_fields = {field.name: field.value for field in first_card.fields}
        self.assertIn("Country", card_fields)
        self.assertIn("Card Type", card_fields)
        self.assertIn("Stats", card_fields)
        self.assertIsNotNone(first_result.get("file"))
        first_result["file"].close()

        repeat_open = SimpleNamespace(
            user=SimpleNamespace(id=gold_user),
            response=FakeResponse(),
            id=70_002,
            message=FakeMessage(),
        )
        await gold_button.callback(repeat_open)
        self.assertIn("already been used", repeat_open.response.messages[0]["content"])
        self.assertEqual(self.database.get_free_pack_inventory(gold_user)["GOLD"], 1)
        self.assertEqual(
            sum(entry.quantity for entry in self.database.get_inventory(gold_user)),
            1,
        )

        totw_user = "free-totw-packs"
        self.database.award_xp(totw_user, "totw-pack-levels", 11_700, "test")
        self.assertEqual(self.database.get_free_pack_inventory(totw_user)["TOTW"], 1)
        self.assertEqual(self.database.get_balance(totw_user), 0)
        totw_menu_response = FakeResponse()
        await PackCommands.pack.callback(
            cog,
            SimpleNamespace(
                user=SimpleNamespace(id=totw_user),
                response=totw_menu_response,
            ),
            discord.app_commands.Choice(name="TOTW Pack", value="TOTW"),
        )
        totw_button = next(
            button
            for button in totw_menu_response.messages[0]["view"].children
            if isinstance(button, FreePackOpenButton)
        )
        totw_open = SimpleNamespace(
            user=SimpleNamespace(id=totw_user),
            response=FakeResponse(),
            id=70_003,
            message=FakeMessage(),
        )
        await totw_button.callback(totw_open)
        self.assertIn("FREE TOTW Pack", totw_open.response.messages[0]["content"])
        self.assertEqual(self.database.get_balance(totw_user), 0)
        self.assertEqual(self.database.get_free_pack_inventory(totw_user)["TOTW"], 0)
        self.assertEqual(
            sum(entry.quantity for entry in self.database.get_inventory(totw_user)),
            1,
        )
        totw_open.response.messages[0]["file"].close()

    async def test_special_rarity_bench_cards_work_through_set_and_team(self) -> None:
        user_id = "special-rarity-bench-command"
        selected_cards = (
            ("SUB1", self.database.get_players(rarity="TOTW")[0]),
            ("SUB2", self.database.get_players(rarity="S1 TOTY")[0]),
        )
        cog = TeamCommands(self.database)
        for position, card in selected_cards:
            self.database.add_inventory(user_id, card.name)
            choices = await TeamCommands.set_player_autocomplete(
                cog,
                SimpleNamespace(
                    user=SimpleNamespace(id=user_id),
                    namespace=SimpleNamespace(position=position),
                ),
                card.name.split()[0],
            )
            self.assertIn(str(card.id), [choice.value for choice in choices])
            response = FakeResponse()
            await TeamCommands.set_position.callback(
                cog,
                SimpleNamespace(
                    user=SimpleNamespace(id=user_id),
                    response=response,
                ),
                discord.app_commands.Choice(name=position, value=position),
                str(card.id),
            )
            self.assertIn("Assigned", response.messages[0]["content"])

        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="Bench Test"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(cog, team_interaction)
        team_embed = team_interaction.followup.messages[0]["embed"]
        slot_values = {field.name: field.value for field in team_embed.fields}
        self.assertIn(
            lineup_card_label(selected_cards[0][1]),
            slot_values["SUB1 · Substitute 1"],
        )
        self.assertIn(
            lineup_card_label(selected_cards[1][1]),
            slot_values["SUB2 · Substitute 2"],
        )
        team_interaction.followup.messages[0]["file"].close()

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
        self.assertIn("`/packs quantity`", help_text)
        self.assertIn("`/match opponent`", help_text)
        self.assertIn("`/claim` · Claim a GOLD player card every 30 minutes", help_text)
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
            for name, overall in (("Mr P", 86), ("Dark", 84), ("Road", 86)):
                self.assertIn(
                    f"**{overall} {name} (Gold)**",
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

    async def test_profile_response_is_public_and_shows_xp_progress(self) -> None:
        response = FakeResponse()
        interaction = SimpleNamespace(
            id=501,
            user=SimpleNamespace(id="public-profile-user", name="PublicPlayer"),
            response=response,
        )
        await ProfileCommands.profile.callback(
            ProfileCommands(self.database),
            interaction,
        )

        message = response.messages[0]
        self.assertFalse(message["ephemeral"])
        embed = message["embed"]
        self.assertEqual(embed.title, "🏆 PLAYER PROFILE")
        self.assertIn("PublicPlayer", embed.description)
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Level"], "**1**")
        self.assertEqual(fields["Total XP"], "⭐ **50**")
        self.assertIn("XP", fields["Level 2 progress"])

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
        self.assertFalse(message["ephemeral"])
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
        self.assertFalse(cooldown_response.messages[0]["ephemeral"])
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
        self.assertFalse(response.messages[0]["ephemeral"])
        self.assertEqual(self.database.get_balance(user_id), 45_678)
        self.assertEqual(self.database.get_inventory(user_id), [])

    async def test_public_command_error_handler_keeps_responses_public(self) -> None:
        with patch("main.LOGGER.error"):
            for command_name in (
                "packs",
                "pack",
                "inventory",
                "balance",
                "minigame",
                "claim",
                "profile",
            ):
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
            "Mr P",
            "Dark",
            "Road",
            "Ebs",
            "EBS (S2 TOTS 99)",
            "Jaycob (S2 TOTS 96)",
            "Licha (S2 TOTS 96)",
            "Cam",
            "Licha (Gold 90)",
            "Chicken",
            "Sluckz",
            "Frozend",
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

    async def test_s2_tots_cards_work_across_card_inventory_packs_set_and_team_art(self) -> None:
        expected = {
            "EBS (S2 TOTS 99)": (
                99,
                "W",
                "Ireland 🇮🇪",
                34_000_000,
                {"SHO": 99, "DRI": 90, "SAV": 80, "PAS": 92, "DEF": 80, "ACT": 93},
                "cards/ebs_s2_tots_99.png",
                "WING1",
            ),
            "Jaycob (S2 TOTS 96)": (
                96,
                "GK",
                "United States 🇺🇸",
                24_000_000,
                {"SAV": 99, "DIV": 98, "PAS": 89, "CLE": 87, "SWE": 92, "ACT": 98},
                "cards/jaycob_s2_tots_96.png",
                "GK",
            ),
            "Licha (S2 TOTS 96)": (
                96,
                "GK",
                "England 🏴",
                25_000_000,
                {"SAV": 97, "DIV": 99, "PAS": 88, "CLE": 87, "SWE": 92, "ACT": 95},
                "cards/licha_s2_tots_96.png",
                "GK",
            ),
        }
        inventory_user = "s2-command-inventory-user"
        player_cog = PlayerCommands(self.database)
        for index, (name, details) in enumerate(expected.items()):
            with self.subTest(card=name):
                (
                    overall,
                    position,
                    country,
                    value,
                    stats,
                    image_path,
                    lineup_position,
                ) = details
                player = self.database.get_player(name)
                self.assertIsNotNone(player)
                assert player is not None
                self.assertEqual(
                    (
                        player.overall,
                        player.position,
                        player.country,
                        player.value,
                        player.stats,
                        player.rarity,
                        player.image_path,
                    ),
                    (overall, position, country, value, stats, "S2 TOTS", image_path),
                )
                label = player_card_label(player)
                image_uri = _player_image_uri(player)
                self.assertIsNotNone(image_uri)
                assert image_uri is not None
                self.assertTrue(image_uri.startswith("data:image/png;base64,"))

                choices = await PlayerCommands.card_player_autocomplete(
                    player_cog,
                    SimpleNamespace(),
                    name.split(" (", 1)[0],
                )
                choice = next(choice for choice in choices if choice.value == name)
                self.assertEqual(choice.name, label)

                card_response = FakeResponse()
                await PlayerCommands.card.callback(
                    player_cog,
                    SimpleNamespace(response=card_response),
                    name,
                )
                card_message = card_response.messages[0]
                self.assertFalse(card_message["ephemeral"])
                self.assertEqual(card_message["embed"].title, label)
                self.assertEqual(
                    card_message["embed"].image.url,
                    f"attachment://{Path(image_path).name}",
                )
                card_fields = {
                    field.name: field.value
                    for field in card_message["embed"].fields
                }
                self.assertEqual(card_fields["Position"], position)
                self.assertEqual(card_fields["Country"], country)
                self.assertEqual(card_fields["Card Type"], "S2 TOTS")
                self.assertEqual(card_fields["Value"], f"{value:,} coins")
                for stat, stat_value in stats.items():
                    self.assertIn(f"**{stat}** {stat_value}", card_fields["Stats"])
                card_message["file"].close()

                self.database.add_inventory(inventory_user, name)
                lineup_user = f"s2-set-user-{index}"
                self.database.add_inventory(lineup_user, name)
                set_response = FakeResponse()
                await TeamCommands.set_position.callback(
                    TeamCommands(self.database),
                    SimpleNamespace(
                        id=700 + index,
                        user=SimpleNamespace(id=lineup_user),
                        response=set_response,
                    ),
                    discord.app_commands.Choice(
                        name=lineup_position,
                        value=lineup_position,
                    ),
                    name,
                )
                self.assertIn("Assigned", set_response.messages[0]["content"])
                lineup = self.database.get_lineup(lineup_user)
                self.assertEqual(lineup[lineup_position], player)
                svg = build_lineup_svg(
                    lineup,
                    "Balanced",
                    "0/3 starters filled",
                )
                self.assertIn(image_uri, svg)

        inventory_response = FakeResponse()
        await InventoryCommands.inventory.callback(
            InventoryCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=inventory_user),
                response=inventory_response,
            ),
        )
        inventory_description = inventory_response.messages[0]["embed"].description
        for name in expected:
            player = self.database.get_player(name)
            self.assertIn(f"**{player_card_label(player)}** ×1", inventory_description)

        sell_user = "s2-sell-user"
        sell_name = "EBS (S2 TOTS 99)"
        self.database.add_inventory(sell_user, sell_name)
        sell_response = FakeResponse()
        await InventoryCommands.sell.callback(
            InventoryCommands(self.database),
            SimpleNamespace(
                id=712,
                user=SimpleNamespace(id=sell_user),
                response=sell_response,
            ),
            sell_name,
            1,
        )
        self.assertIn("99 EBS (S2 TOTS)", sell_response.messages[0]["content"])
        self.assertIn("34,000,000 coins", sell_response.messages[0]["content"])
        self.assertEqual(self.database.get_balance(sell_user), 34_000_000)

        packs_response = FakeResponse()
        await PackCommands.packs.callback(
            PackCommands(self.database),
            SimpleNamespace(
                id=710,
                user=SimpleNamespace(id=inventory_user),
                response=packs_response,
            ),
        )
        packs_fields = {
            field.name: field.value
            for field in packs_response.messages[0]["embed"].fields
        }
        s2_field_name = "S2 TOTS · 50,000,000 coins"
        self.assertIn(s2_field_name, packs_fields)
        for name in expected:
            self.assertIn(name, packs_fields[s2_field_name])

        pack_response = FakeResponse()
        await PackCommands.pack.callback(
            PackCommands(self.database),
            SimpleNamespace(
                id=711,
                user=SimpleNamespace(id=inventory_user),
                response=pack_response,
            ),
            discord.app_commands.Choice(name="S2 TOTS Pack", value="S2 TOTS"),
        )
        self.assertEqual(
            [button.rarity for button in pack_response.messages[0]["view"].children],
            ["S2 TOTS"],
        )
        self.assertIn(
            "S2 TOTS",
            next(
                field.value
                for field in pack_response.messages[0]["embed"].fields
                if field.name.startswith("S2 TOTS")
            ),
        )

    async def test_s1_tots_cards_work_across_card_inventory_pack_set_team_and_sell(self) -> None:
        expected = {
            "Dan (S1 TOTS 96)": (
                96,
                "C",
                "Italy 🇮🇹",
                25_000_000,
                {"SHO": 96, "DRI": 97, "SAV": 76, "PAS": 93, "DEF": 96, "ACT": 88},
                "cards/dan_s1_tots_96.png",
                "CM1",
                "WING1",
            ),
            "Hala (S1 TOTS 99)": (
                99,
                "WI",
                "Scotland 🏴",
                50_000_000,
                {"SHO": 99, "DRI": 99, "SAV": 83, "PAS": 97, "DEF": 87, "ACT": 86},
                "cards/hala_s1_tots_99.png",
                "WING1",
                "CM1",
            ),
            "Ebs (S1 TOTS 98)": (
                98,
                "WI",
                "Ireland 🇮🇪",
                27_000_000,
                {"SHO": 97, "DRI": 98, "SAV": 80, "PAS": 95, "DEF": 93, "ACT": 88},
                "cards/ebs_s1_tots_98.png",
                "WING1",
                "CM1",
            ),
            "Gamer (S1 TOTS 97)": (
                97,
                "C",
                "United States 🇺🇸",
                27_000_000,
                {"SHO": 96, "DRI": 98, "SAV": 76, "PAS": 95, "DEF": 93, "ACT": 85},
                "cards/gamer_s1_tots_97.png",
                "CM1",
                "WING1",
            ),
            "Road (S1 TOTS 98)": (
                98,
                "GK",
                "France 🇫🇷",
                40_000_000,
                {"SAV": 97, "DIV": 96, "PAS": 93, "CLE": 95, "SWE": 99, "ACT": 99},
                "cards/road_s1_tots_98.png",
                "GK",
                "CM1",
            ),
            "Lauren (S1 TOTS 97)": (
                97,
                "GK",
                "England 🏴",
                35_000_000,
                {"SAV": 99, "DIV": 98, "PAS": 94, "SWE": 95, "CLE": 96, "ACT": 95},
                "cards/lauren_s1_tots_97.png",
                "GK",
                "CM1",
            ),
        }
        originals = {"Dan": 87, "Hala": 93, "Ebs": 90, "Gamer": 89, "Road": 86}
        position_restrictions = {
            "Gamer (S1 TOTS 97)": ("CM1", "CM2"),
            "Road (S1 TOTS 98)": ("GK",),
            "Lauren (S1 TOTS 97)": ("GK",),
        }
        for name, rating in originals.items():
            original = self.database.get_player(name)
            self.assertIsNotNone(original)
            self.assertEqual(original.overall, rating)

        player_cog = PlayerCommands(self.database)
        inventory_user = "s1-command-inventory-user"
        for index, (name, details) in enumerate(expected.items()):
            with self.subTest(card=name):
                (
                    overall,
                    position,
                    country,
                    value,
                    stats,
                    image_path,
                    allowed_position,
                    forbidden_position,
                ) = details
                player = self.database.get_player(name)
                self.assertIsNotNone(player)
                assert player is not None
                self.assertEqual(
                    (
                        player.overall,
                        player.position,
                        player.country,
                        player.value,
                        player.stats,
                        player.rarity,
                        player.image_path,
                    ),
                    (overall, position, country, value, stats, "S1 TOTS", image_path),
                )
                image = resolve_card_image(player)
                self.assertIsNotNone(image)
                assert image is not None
                self.assertEqual(image.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
                image_bytes = image.read_bytes()
                width, height = struct.unpack(">II", image_bytes[16:24])
                self.assertGreater(width, 200)
                self.assertGreater(height, 250)

                card_response = FakeResponse()
                await PlayerCommands.card.callback(
                    player_cog,
                    SimpleNamespace(response=card_response),
                    name,
                )
                card_message = card_response.messages[0]
                self.assertEqual(card_message["embed"].title, player_card_label(player))
                self.assertEqual(
                    card_message["embed"].image.url,
                    f"attachment://{Path(image_path).name}",
                )
                fields = {
                    field.name: field.value for field in card_message["embed"].fields
                }
                self.assertEqual(fields["Position"], position)
                self.assertEqual(fields["Country"], country)
                self.assertEqual(fields["Card Type"], "S1 TOTS")
                self.assertEqual(fields["Value"], f"{value:,} coins")
                for stat, stat_value in stats.items():
                    self.assertIn(f"**{stat}** {stat_value}", fields["Stats"])
                card_message["file"].close()

                self.database.add_inventory(inventory_user, name)
                lineup_user = f"s1-set-user-{index}"
                self.database.add_inventory(lineup_user, name)
                set_response = FakeResponse()
                await TeamCommands.set_position.callback(
                    TeamCommands(self.database),
                    SimpleNamespace(
                        id=800 + index,
                        user=SimpleNamespace(id=lineup_user),
                        response=set_response,
                    ),
                    discord.app_commands.Choice(
                        name=allowed_position,
                        value=allowed_position,
                    ),
                    name,
                )
                self.assertIn("Assigned", set_response.messages[0]["content"])
                self.assertEqual(
                    self.database.get_lineup(lineup_user)[allowed_position].name,
                    name,
                )
                if name in position_restrictions:
                    self.assertEqual(
                        player.allowed_lineup_positions,
                        position_restrictions[name],
                    )
                    testable_positions = ("WING1", "CM1", "GK")
                    for forbidden in (
                        slot
                        for slot in testable_positions
                        if slot not in position_restrictions[name]
                    ):
                        forbidden_response = FakeResponse()
                        await TeamCommands.set_position.callback(
                            TeamCommands(self.database),
                            SimpleNamespace(
                                id=820 + index,
                                user=SimpleNamespace(id=lineup_user),
                                response=forbidden_response,
                            ),
                            discord.app_commands.Choice(
                                name=forbidden,
                                value=forbidden,
                            ),
                            name,
                        )
                        self.assertIn(
                            "can only be assigned to",
                            forbidden_response.messages[0]["content"],
                        )
                    lineup = self.database.get_lineup(lineup_user)
                    image_uri = _player_image_uri(lineup[allowed_position])
                    self.assertIsNotNone(image_uri)
                    self.assertIn(
                        image_uri,
                        build_lineup_svg(
                            lineup,
                            "Balanced",
                            f"{player.overall} OVR",
                        ),
                    )
                    team_response = FakeResponse()
                    team_followup = FakeFollowup()
                    await TeamCommands.team.callback(
                        TeamCommands(self.database),
                        SimpleNamespace(
                            id=830 + index,
                            user=SimpleNamespace(
                                id=lineup_user,
                                display_name=player.name,
                            ),
                            response=team_response,
                            followup=team_followup,
                        ),
                    )
                    team_message = team_followup.messages[0]
                    team_slot = (
                        "GK · Goalie"
                        if allowed_position == "GK"
                        else f"{allowed_position} · CM 1"
                    )
                    team_fields = {
                        field.name: field.value
                        for field in team_message["embed"].fields
                    }
                    self.assertIn(player_card_label(player), team_fields[team_slot])
                    self.assertEqual(
                        team_message["embed"].image.url,
                        f"attachment://{OUTPUT_FILENAME}",
                    )
                    team_message["file"].close()
                with self.assertRaises(InvalidLineupPosition) as error:
                    self.database.set_lineup_position(
                        lineup_user,
                        forbidden_position,
                        name,
                    )
                self.assertIn(
                    (
                        "can only be assigned to"
                        if name in position_restrictions
                        else "can occupy"
                    ),
                    str(error.exception),
                )

        inventory_response = FakeResponse()
        await InventoryCommands.inventory.callback(
            InventoryCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=inventory_user),
                response=inventory_response,
            ),
        )
        inventory_description = inventory_response.messages[0]["embed"].description
        for name in expected:
            self.assertIn(
                f"**{player_card_label(self.database.get_player(name))}** ×1",
                inventory_description,
            )

        packs_response = FakeResponse()
        await PackCommands.packs.callback(
            PackCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=inventory_user),
                response=packs_response,
            ),
        )
        packs_fields = {
            field.name: field.value
            for field in packs_response.messages[0]["embed"].fields
        }
        s1_field = f"S1 TOTS · {S1_TOTS_PACK_COST:,} coins"
        self.assertIn(s1_field, packs_fields)
        for name in expected:
            self.assertIn(name, packs_fields[s1_field])

        pack_user = "s1-pack-command-user"
        self.database.set_balance(pack_user, S1_TOTS_PACK_COST)
        pack_response = FakeResponse()
        await PackCommands.pack.callback(
            PackCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=pack_user),
                response=pack_response,
            ),
            discord.app_commands.Choice(name="S1 TOTS Pack", value="S1 TOTS"),
        )
        pack_menu = pack_response.messages[0]
        self.assertEqual(
            [button.rarity for button in pack_menu["view"].children],
            ["S1 TOTS"],
        )
        purchase_response = FakeResponse()
        await pack_menu["view"].children[0].callback(
            SimpleNamespace(
                user=SimpleNamespace(id=pack_user),
                response=purchase_response,
                id=801,
                message=FakeMessage(),
            )
        )
        purchased = self.database.get_inventory(pack_user)
        self.assertEqual(len(purchased), 1)
        self.assertEqual(purchased[0].player.rarity, "S1 TOTS")
        self.assertEqual(self.database.get_balance(pack_user), 0)
        purchase_response.messages[0]["file"].close()

        for index, (sell_name, details) in enumerate(expected.items()):
            sell_user = f"s1-sell-user-{index}"
            value = details[3]
            self.database.add_inventory(sell_user, sell_name)
            sell_response = FakeResponse()
            await InventoryCommands.sell.callback(
                InventoryCommands(self.database),
                SimpleNamespace(
                    id=802 + index,
                    user=SimpleNamespace(id=sell_user),
                    response=sell_response,
                ),
                sell_name,
                1,
            )
            self.assertIn(
                f"{value:,} coins",
                sell_response.messages[0]["content"],
            )
            self.assertEqual(self.database.get_balance(sell_user), value)

        team_user = "s1-team-user"
        self.database.add_inventory(team_user, "Dan (S1 TOTS 96)")
        self.database.set_lineup_position(team_user, "CM1", "Dan (S1 TOTS 96)")
        lineup = self.database.get_lineup(team_user)
        image_uri = _player_image_uri(lineup["CM1"])
        self.assertIsNotNone(image_uri)
        svg = build_lineup_svg(lineup, "Balanced", "0/3 starters filled")
        self.assertIn(image_uri, svg)
        team_response = FakeResponse()
        team_followup = FakeFollowup()
        await TeamCommands.team.callback(
            TeamCommands(self.database),
            SimpleNamespace(
                id=803,
                user=SimpleNamespace(id=team_user, display_name="S1 Captain"),
                response=team_response,
                followup=team_followup,
            ),
        )
        team_message = team_followup.messages[0]
        team_fields = {field.name: field.value for field in team_message["embed"].fields}
        self.assertIn("96 Dan (S1 TOTS)", team_fields["CM1 · CM 1"])
        self.assertEqual(
            team_message["embed"].image.url,
            f"attachment://{OUTPUT_FILENAME}",
        )
        team_message["file"].close()

    async def test_ebs_cam_and_gold_licha_work_across_card_inventory_pack_set_team_and_sell(self) -> None:
        expected = {
            "Ebs": {
                "overall": 90,
                "position": "WI",
                "country": "Ireland",
                "team": "Ottawa Senators",
                "stats": {"SHO": 93, "DRI": 90, "SAV": 74, "PAS": 88, "DEF": 85, "ACT": 83},
                "image": "ebs_gold.png",
                "label": "90 Ebs (Gold)",
            },
            "Cam": {
                "overall": 90,
                "position": "WI",
                "country": "USA",
                "team": "Ottawa Senators",
                "stats": {"SHO": 93, "DRI": 90, "SAV": 76, "PAS": 88, "DEF": 83, "ACT": 78},
                "image": "cam_gold.png",
                "label": "90 Cam (Gold)",
            },
            "Licha (Gold 90)": {
                "overall": 90,
                "position": "GK",
                "country": "England",
                "team": "Ottawa Senators",
                "stats": {"SAV": 92, "DIV": 90, "PAS": 89, "CLE": 87, "SWE": 86, "ACT": 92},
                "image": "licha_gold_90.png",
                "label": "90 Licha (Gold)",
            },
        }
        user_id = "ebs-cam-gold-licha-flow-user"
        player_cog = PlayerCommands(self.database)

        for name, details in expected.items():
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
                    (
                        details["overall"],
                        details["position"],
                        details["country"],
                        details["team"],
                        "GOLD",
                        2_600_000,
                        details["stats"],
                        f"cards/{details['image']}",
                    ),
                )
                image_path = resolve_card_image(player)
                self.assertIsNotNone(image_path)
                assert image_path is not None
                self.assertEqual(image_path.name, details["image"])
                self.assertTrue(image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

                search_name = details["label"].split(" ", 1)[1].split(" (", 1)[0]
                choices = await PlayerCommands.card_player_autocomplete(
                    player_cog,
                    SimpleNamespace(),
                    search_name,
                )
                choice = next(choice for choice in choices if choice.value == name)
                self.assertEqual(choice.name, details["label"])

                card_response = FakeResponse()
                await PlayerCommands.card.callback(
                    player_cog,
                    SimpleNamespace(response=card_response),
                    name,
                )
                card_message = card_response.messages[0]
                self.assertEqual(card_message["embed"].title, details["label"])
                self.assertEqual(
                    card_message["embed"].image.url,
                    f"attachment://{details['image']}",
                )
                card_fields = {
                    field.name: field.value for field in card_message["embed"].fields
                }
                self.assertEqual(card_fields["Position"], details["position"])
                self.assertEqual(card_fields["Country"], details["country"])
                self.assertEqual(card_fields["Team"], details["team"])
                self.assertEqual(card_fields["Card Type"], "Gold")
                self.assertEqual(card_fields["Value"], "2,600,000 coins")
                for stat, value in details["stats"].items():
                    self.assertIn(f"**{stat}** {value}", card_fields["Stats"])
                card_message["file"].close()

                grant_response = FakeResponse()
                await AdminCommands.grantcard.callback(
                    AdminCommands(self.database),
                    SimpleNamespace(response=grant_response),
                    SimpleNamespace(
                        id=user_id,
                        mention="<@ebs-cam-gold-licha-flow-user>",
                    ),
                    name,
                    1,
                )
                self.assertIn(details["label"], grant_response.messages[0]["content"])

        inventory_response = FakeResponse()
        await InventoryCommands.inventory.callback(
            InventoryCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=inventory_response,
            ),
        )
        inventory_text = inventory_response.messages[0]["embed"].description
        for details in expected.values():
            self.assertIn(f"**{details['label']}** ×1", inventory_text)

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
        self.assertIn("Contains one random Gold player card.", gold_field)
        self.assert_pack_menu_shows_odds(pack_response.messages[0]["embed"])

        team_cog = TeamCommands(self.database)
        self.database.set_formation(user_id, "Offensive")
        for name, wrong_position in (
            ("Ebs", "GK"),
            ("Cam", "GK"),
            ("Licha (Gold 90)", "WING1"),
        ):
            response = FakeResponse()
            await TeamCommands.set_position.callback(
                team_cog,
                SimpleNamespace(
                    user=SimpleNamespace(id=user_id),
                    response=response,
                ),
                discord.app_commands.Choice(
                    name=wrong_position,
                    value=wrong_position,
                ),
                name,
            )
            self.assertTrue(response.messages[0]["ephemeral"])
            self.assertEqual(self.database.get_lineup(user_id), {})

        valid_assignments = (
            ("Ebs", "WING1"),
            ("Cam", "WING2"),
            ("Licha (Gold 90)", "GK"),
        )
        for name, position in valid_assignments:
            response = FakeResponse()
            await TeamCommands.set_position.callback(
                team_cog,
                SimpleNamespace(
                    user=SimpleNamespace(id=user_id),
                    response=response,
                ),
                discord.app_commands.Choice(name=position, value=position),
                name,
            )
            self.assertIn("Assigned", response.messages[0]["content"])
            self.assertEqual(self.database.get_lineup(user_id)[position].name, name)

        lineup = self.database.get_lineup(user_id)
        svg = build_lineup_svg(lineup, "Offensive", "90 OVR average")
        for name, _position in valid_assignments:
            player = self.database.get_player(name)
            self.assertIsNotNone(player)
            assert player is not None
            self.assertIn(_player_image_uri(player), svg)

        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="Ebs, Cam and Licha"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(team_cog, team_interaction)
        team_message = team_interaction.followup.messages[0]
        team_fields = {
            field.name: field.value for field in team_message["embed"].fields
        }
        position_labels = {
            "WING1": "Wing 1",
            "WING2": "Wing 2",
            "GK": "Goalie",
        }
        for name, position in valid_assignments:
            display_name = (
                expected[name]["label"].split(" ", 1)[1].rsplit(" (", 1)[0]
            )
            self.assertIn(
                f"{expected[name]['label']} — 90 OVR",
                team_fields[f"{position} · {position_labels[position]}"],
            )
        team_file = team_message["file"]
        self.assertTrue(team_file.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
        team_file.close()

        for index, (name, details) in enumerate(expected.items()):
            seller_id = f"ebs-cam-gold-licha-seller-{index}"
            self.database.add_inventory(seller_id, name)
            sale_response = FakeResponse()
            await InventoryCommands.sell.callback(
                InventoryCommands(self.database),
                SimpleNamespace(
                    user=SimpleNamespace(id=seller_id),
                    response=sale_response,
                ),
                name,
                1,
            )
            self.assertIn(
                f"Sold **1× {details['label']}** for 2,600,000 coins",
                sale_response.messages[0]["content"],
            )
            self.assertEqual(self.database.get_balance(seller_id), 2_600_000)

    async def test_mr_p_dark_and_road_work_across_card_inventory_pack_and_team(self) -> None:
        expected = {
            "Mr P": (
                86,
                "WI",
                "USA",
                "New York Rangers",
                {"SHO": 89, "DRI": 84, "SAV": 81, "PAS": 85, "DEF": 87, "ACT": 86},
                "mr_p_gold.png",
                650_000,
            ),
            "Dark": (
                84,
                "WI",
                "Canada",
                "New York Rangers",
                {"SHO": 86, "DRI": 84, "SAV": 78, "PAS": 85, "DEF": 80, "ACT": 83},
                "dark_gold.png",
                400_000,
            ),
            "Road": (
                86,
                "GK",
                "Malta",
                "Anaheim Ducks",
                {"SAV": 89, "DIV": 87, "PAS": 83, "CLE": 83, "SWE": 88, "ACT": 85},
                "road_gold.png",
                650_000,
            ),
        }
        user_id = "mr-p-dark-road-flow-user"
        player_cog = PlayerCommands(self.database)

        for name, details in expected.items():
            with self.subTest(card=name):
                overall, position, country, team, stats, image_name, value = details
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
                    (
                        overall,
                        position,
                        country,
                        team,
                        "GOLD",
                        value,
                        stats,
                        f"cards/{image_name}",
                    ),
                )
                image_path = resolve_card_image(player)
                self.assertIsNotNone(image_path)
                assert image_path is not None
                self.assertEqual(image_path.name, image_name)
                self.assertTrue(image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

                choices = await PlayerCommands.card_player_autocomplete(
                    player_cog,
                    SimpleNamespace(),
                    name,
                )
                self.assertIn(name, [choice.value for choice in choices])
                card_response = FakeResponse()
                await PlayerCommands.card.callback(
                    player_cog,
                    SimpleNamespace(response=card_response),
                    name,
                )
                card_message = card_response.messages[0]
                self.assertEqual(
                    card_message["embed"].title,
                    f"{overall} {name} (Gold)",
                )
                self.assertEqual(
                    card_message["embed"].image.url,
                    f"attachment://{image_name}",
                )
                card_fields = {
                    field.name: field.value
                    for field in card_message["embed"].fields
                }
                self.assertEqual(card_fields["Position"], position)
                self.assertEqual(card_fields["Country"], country)
                self.assertEqual(card_fields["Team"], team)
                self.assertEqual(card_fields["Card Type"], "Gold")
                self.assertEqual(card_fields["Value"], f"{value:,} coins")
                for stat, value in stats.items():
                    self.assertIn(f"**{stat}** {value}", card_fields["Stats"])
                card_message["file"].close()

                grant_response = FakeResponse()
                await AdminCommands.grantcard.callback(
                    AdminCommands(self.database),
                    SimpleNamespace(response=grant_response),
                    SimpleNamespace(
                        id=user_id,
                        mention="<@mr-p-dark-road-flow-user>",
                    ),
                    name,
                    1,
                )
                self.assertIn(
                    f"{overall} {name} (Gold)",
                    grant_response.messages[0]["content"],
                )

        inventory_response = FakeResponse()
        await InventoryCommands.inventory.callback(
            InventoryCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=inventory_response,
            ),
        )
        inventory_text = inventory_response.messages[0]["embed"].description
        for name, (overall, *_details) in expected.items():
            self.assertIn(f"**{overall} {name} (Gold)** ×1", inventory_text)

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
        self.assertIn("Contains one random Gold player card.", gold_field)
        self.assert_pack_menu_shows_odds(pack_response.messages[0]["embed"])

        team_cog = TeamCommands(self.database)
        self.database.set_formation(user_id, "Offensive")
        for name, wrong_position in (
            ("Mr P", "GK"),
            ("Dark", "CM1"),
            ("Road", "WING1"),
        ):
            response = FakeResponse()
            await TeamCommands.set_position.callback(
                team_cog,
                SimpleNamespace(
                    user=SimpleNamespace(id=user_id),
                    response=response,
                ),
                discord.app_commands.Choice(
                    name=wrong_position,
                    value=wrong_position,
                ),
                name,
            )
            self.assertTrue(response.messages[0]["ephemeral"])
            self.assertEqual(self.database.get_lineup(user_id), {})

        valid_assignments = (
            ("Mr P", "WING1"),
            ("Dark", "WING2"),
            ("Road", "GK"),
        )
        for name, position in valid_assignments:
            response = FakeResponse()
            await TeamCommands.set_position.callback(
                team_cog,
                SimpleNamespace(
                    user=SimpleNamespace(id=user_id),
                    response=response,
                ),
                discord.app_commands.Choice(name=position, value=position),
                name,
            )
            self.assertIn("Assigned", response.messages[0]["content"])
            self.assertEqual(self.database.get_lineup(user_id)[position].name, name)

        lineup = self.database.get_lineup(user_id)
        svg = build_lineup_svg(lineup, "Offensive", "85 OVR average")
        for name, _position in valid_assignments:
            player = self.database.get_player(name)
            self.assertIsNotNone(player)
            assert player is not None
            self.assertIn(_player_image_uri(player), svg)

        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="New Gold Cards"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(team_cog, team_interaction)
        team_message = team_interaction.followup.messages[0]
        team_fields = {
            field.name: field.value for field in team_message["embed"].fields
        }
        position_labels = {
            "WING1": "Wing 1",
            "WING2": "Wing 2",
            "GK": "Goalie",
        }
        for name, position in valid_assignments:
            self.assertIn(name, team_fields[f"{position} · {position_labels[position]}"])
        team_file = team_message["file"]
        self.assertTrue(team_file.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
        team_file.close()

        for index, (name, (overall, *_details, value)) in enumerate(expected.items()):
            seller_id = f"new-gold-seller-{index}"
            self.database.add_inventory(seller_id, name)
            sale_response = FakeResponse()
            await InventoryCommands.sell.callback(
                InventoryCommands(self.database),
                SimpleNamespace(
                    user=SimpleNamespace(id=seller_id),
                    response=sale_response,
                ),
                name,
                1,
            )
            self.assertIn(
                f"Sold **1× {overall} {name} (Gold)** for {value:,} coins",
                sale_response.messages[0]["content"],
            )
            self.assertEqual(self.database.get_balance(seller_id), value)

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
        self.assertIn("Contains one random Gold player card.", pack_text)
        self.assert_pack_menu_shows_odds(pack_response.messages[0]["embed"])
        for name in expected:
            card = self.database.get_player(name)
            self.assertIsNotNone(card)
            self.assertIn(card, self.database.get_players(rarity="GOLD"))

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
        self.assertIn("84 Mason (Gold) — 84 OVR", team_fields["WING1 · Wing 1"])
        self.assertIn("84 Demox (Gold) — 84 OVR", team_fields["WING2 · Wing 2"])
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
        self.assertIn("84 Cheddar (Gold) — 84 OVR", cheddar_fields["WING1 · Wing 1"])
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
        self.assertEqual(
            len(all_choices),
            min(25, len(self.database.get_players())),
        )
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
        self.assertIn("Contains one random Gold player card.", gold_field)
        self.assert_pack_menu_shows_odds(packs_response.messages[0]["embed"])
        self.assertIn(
            self.database.get_player("Fadi"),
            self.database.get_players(rarity="GOLD"),
        )

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
            "Assigned **86 Fadi (Gold) — 86 OVR** to **CM1**",
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
        self.assertIn("86 Fadi (Gold) — 86 OVR", team_fields["CM1 · CM 1"])
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
                    "Licha (Gold 90)": "Licha",
                    "Hala (S2 TOTS 98)": "Hala",
                    "EBS (S2 TOTS 99)": "EBS",
                    "Jaycob (S2 TOTS 96)": "Jaycob",
                    "Licha (S2 TOTS 96)": "Licha",
                    "Dan (S1 TOTS 96)": "Dan",
                    "Hala (S1 TOTS 99)": "Hala",
                    "Ebs (S1 TOTS 98)": "Ebs",
                    "Gamer (S1 TOTS 97)": "Gamer",
                    "Road (S1 TOTS 98)": "Road",
                    "Lauren (S1 TOTS 97)": "Lauren",
            "Ebs (S1 TOTY 98)": "Ebs",
            "Hala (S1 TOTY 99)": "Hala",
            "Cam (S1 TOTY 97)": "Cam",
            "Road (S1 TOTY 98)": "Road",
            "Matrix (S1 TOTY 97)": "Matrix",
            "Lauren (S1 TOTY 97)": "Lauren",
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
        self.assertIn("Contains one random Gold player card.", gold_field)
        self.assert_pack_menu_shows_odds(pack_response.messages[0]["embed"])

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
            "87 Dryboomrun (Gold) — 87 OVR",
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
        self.assertIn("87 Dryboomrun (Gold) — 87 OVR", fields["CM1 · CM 1"])
        team_image = team_message["file"]
        self.assertTrue(team_image.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
        team_image.close()

    async def test_needy_card_seed_data_and_artwork(self) -> None:
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

    async def test_chicken_sluckz_and_frozend_work_in_card_inventory_sell_set_and_team(self) -> None:
        expected = {
            "Chicken": (
                84,
                "C",
                "Czech Republic 🇨🇿",
                {"SHO": 85, "DRI": 83, "SAV": 86, "PAS": 85, "DEF": 84, "ACT": 86},
                500_000,
                "cards/chicken_gold_supplied.png",
                "attached_assets/Firefly_-_2026-10-09T135533.430_1793976312048.png",
                "CM1",
            ),
            "Sluckz": (
                83,
                "WI",
                "Sweden 🇸🇪",
                {"SHO": 84, "DRI": 85, "SAV": 78, "PAS": 84, "DEF": 79, "ACT": 82},
                430_000,
                "cards/sluckz_gold_supplied.png",
                "attached_assets/Firefly_-_2026-11-06T152319.060_1793976311979.png",
                "WING1",
            ),
            "Frozend": (
                90,
                "GK",
                "United States 🇺🇸",
                {"SAV": 91, "DIV": 91, "PAS": 86, "CLE": 88, "SWE": 90, "ACT": 91},
                1_200_000,
                "cards/frozend_gold_supplied.png",
                "attached_assets/Firefly_-_2026-10-09T135940.009_1793976312031.png",
                "GK",
            ),
        }
        player_cog = PlayerCommands(self.database)
        inventory_cog = InventoryCommands(self.database)
        team_cog = TeamCommands(self.database)
        gold_pack_cards = PackCommands(self.database)._pack_cards()["GOLD"]

        for index, (name, details) in enumerate(expected.items()):
            (
                rating,
                position,
                country,
                stats,
                value,
                image_name,
                supplied_image,
                lineup_position,
            ) = details
            label = f"{rating} {name} (Gold)"
            with self.subTest(card=name):
                player = self.database.get_player(name)
                self.assertIsNotNone(player)
                assert player is not None
                self.assertEqual(
                    (
                        player.overall,
                        player.position,
                        player.country,
                        player.rarity,
                        player.value,
                        player.stats,
                        player.image_path,
                    ),
                    (rating, position, country, "GOLD", value, stats, image_name),
                )
                self.assertIn(player, gold_pack_cards)

                choices = await PlayerCommands.card_player_autocomplete(
                    player_cog,
                    SimpleNamespace(),
                    name,
                )
                self.assertIn((label, name), [(choice.name, choice.value) for choice in choices])

                card_response = FakeResponse()
                await PlayerCommands.card.callback(
                    player_cog,
                    SimpleNamespace(response=card_response),
                    name,
                )
                card_message = card_response.messages[0]
                self.assertEqual(card_message["embed"].title, label)
                card_fields = {
                    field.name: field.value for field in card_message["embed"].fields
                }
                self.assertEqual(card_fields["Position"], position)
                self.assertEqual(card_fields["Country"], country)
                self.assertEqual(card_fields["Card Type"], "Gold")
                for stat, score in stats.items():
                    self.assertIn(f"**{stat}** {score}", card_fields["Stats"])
                artwork = Path(image_name)
                self.assertEqual(artwork.read_bytes(), Path(supplied_image).read_bytes())
                self.assertEqual(card_message["file"].fp.read(), artwork.read_bytes())
                card_message["file"].close()

                inventory_user = f"new-card-inventory-{index}"
                self.database.add_inventory(inventory_user, name)
                inventory_response = FakeResponse()
                await InventoryCommands.inventory.callback(
                    inventory_cog,
                    SimpleNamespace(
                        user=SimpleNamespace(id=inventory_user),
                        response=inventory_response,
                    ),
                )
                self.assertIn(
                    label,
                    inventory_response.messages[0]["embed"].description,
                )

                sell_response = FakeResponse()
                await InventoryCommands.sell.callback(
                    inventory_cog,
                    SimpleNamespace(
                        user=SimpleNamespace(id=inventory_user),
                        response=sell_response,
                    ),
                    name,
                    1,
                )
                self.assertIn(
                    f"Sold **1× {label}** for {value:,} coins",
                    sell_response.messages[0]["content"],
                )
                self.assertEqual(self.database.get_balance(inventory_user), value)

        team_user = "new-gold-cards-team"
        for name, details in expected.items():
            lineup_position = details[-1]
            self.database.add_inventory(team_user, name)
            response = FakeResponse()
            await TeamCommands.set_position.callback(
                team_cog,
                SimpleNamespace(
                    user=SimpleNamespace(id=team_user),
                    response=response,
                ),
                discord.app_commands.Choice(
                    name=lineup_position,
                    value=lineup_position,
                ),
                name,
            )
            self.assertIn(f"{details[0]} {name} (Gold)", response.messages[0]["content"])

        lineup = self.database.get_lineup(team_user)
        team_svg = build_lineup_svg(lineup, "Balanced", "three new Gold cards")
        for name, details in expected.items():
            player = self.database.get_player(name)
            assert player is not None
            self.assertIn(_player_image_uri(player), team_svg)

        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=team_user, display_name="New Gold Cards"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(team_cog, team_interaction)
        team_message = team_interaction.followup.messages[0]
        self.assertTrue(team_message["file"].fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
        team_message["file"].close()

    async def test_needy_card_is_displayed_in_all_requested_flows(self) -> None:
        label = "85 Needy (Gold)"
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
        self.assertIn("Contains one random Gold player card.", gold_field)
        self.assert_pack_menu_shows_odds(pack_response.messages[0]["embed"])

        set_response = FakeResponse()
        team_cog = TeamCommands(self.database)
        await TeamCommands.set_position.callback(
            team_cog,
            SimpleNamespace(user=SimpleNamespace(id=user_id), response=set_response),
            discord.app_commands.Choice(name="CM1", value="CM1"),
            "Needy",
        )
        self.assertIn("85 Needy (Gold) — 85 OVR", set_response.messages[0]["content"])

        team_interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="Needy"),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await TeamCommands.team.callback(team_cog, team_interaction)
        team_message = team_interaction.followup.messages[0]
        fields = {field.name: field.value for field in team_message["embed"].fields}
        self.assertIn("85 Needy (Gold) — 85 OVR", fields["CM1 · CM 1"])
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
            "89 Gamer (Gold) — 89 OVR",
            {field.name: field.value for field in team_message["embed"].fields}[
                "WING2 · Wing 2"
            ],
        )
        lineup_file = team_message["file"]
        lineup_bytes = lineup_file.fp.read()
        lineup_file.close()
        self.assertTrue(lineup_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(
            struct.unpack(">II", lineup_bytes[16:24]),
            (IMAGE_WIDTH, IMAGE_HEIGHT),
        )

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
            "90 Hala (Gold) — 90 OVR",
            {field.name: field.value for field in team_message["embed"].fields}[
                "WING2 · Wing 2"
            ],
        )
        lineup_file = team_message["file"]
        lineup_bytes = lineup_file.fp.read()
        lineup_file.close()
        self.assertTrue(lineup_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(
            struct.unpack(">II", lineup_bytes[16:24]),
            (IMAGE_WIDTH, IMAGE_HEIGHT),
        )

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
                "Assigned **91 XavierCapone9 (Gold) — 91 OVR**",
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
            "91 XavierCapone9 (Gold) — 91 OVR",
            {field.name: field.value for field in team_message["embed"].fields}[
                "WING2 · Wing 2"
            ],
        )
        lineup_file = team_message["file"]
        lineup_bytes = lineup_file.fp.read()
        lineup_file.close()
        self.assertTrue(lineup_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(
            struct.unpack(">II", lineup_bytes[16:24]),
            (IMAGE_WIDTH, IMAGE_HEIGHT),
        )

    async def test_lineup_rink_svg_uses_ice_and_hockey_markings(self) -> None:
        svg = build_lineup_svg({}, "Balanced", "Not rated yet")

        self.assertIn('stop-color="#ffffff"', svg)
        self.assertIn('stop-color="#edf5f9"', svg)
        self.assertIn('stroke="#d52b3e"', svg)
        self.assertEqual(svg.count('stroke="#2867a6"'), 2)
        self.assertIn(f'width="{IMAGE_WIDTH}" height="{IMAGE_HEIGHT}"', svg)
        self.assertIn('x1="155" y1="815" x2="1045" y2="815"', svg)
        self.assertIn('cx="600" cy="815" r="115"', svg)
        self.assertEqual(svg.count('r="58" fill="none"'), 4)
        self.assertIn('cx="410" cy="520" r="58"', svg)
        self.assertIn('cx="790" cy="1220" r="58"', svg)
        self.assertIn('x1="155" y1="610" x2="1045" y2="610"', svg)
        self.assertIn('fill-opacity="0.12" stroke="#d52b3e"', svg)
        self.assertIn('height="1420" rx="160"', svg)
        self.assertIn('viewBox="0 0 1200 1980"', svg)
        self.assertIn(">THSL STARTING LINEUP<", svg)
        self.assertIn('fill="#ffffff" fill-opacity="0.3" stroke="#d52b3e"', svg)
        self.assertIn(">SUBSTITUTES<", svg)
        for position in FORMATION_STARTERS["Balanced"]:
            layout = FORMATION_SLOT_LAYOUTS["Balanced"][position]
            self.assertGreaterEqual(layout.x, RINK_LEFT)
            self.assertGreaterEqual(layout.y, RINK_TOP)
            self.assertLessEqual(layout.x + layout.width, RINK_RIGHT)
            self.assertLessEqual(layout.y + layout.height, RINK_BOTTOM)
        balanced = FORMATION_SLOT_LAYOUTS["Balanced"]
        offensive = FORMATION_SLOT_LAYOUTS["Offensive"]
        defensive = FORMATION_SLOT_LAYOUTS["Defensive"]
        rink_center_x = (RINK_LEFT + RINK_RIGHT) // 2
        self.assertLess(balanced["WING1"].x, rink_center_x)
        self.assertEqual(
            offensive["WING1"].x + offensive["WING1"].width // 2,
            rink_center_x - 340,
        )
        self.assertGreater(offensive["WING2"].x, rink_center_x)
        self.assertLess(
            defensive["CM1"].x + defensive["CM1"].width // 2,
            rink_center_x,
        )
        self.assertGreater(
            defensive["CM2"].x + defensive["CM2"].width // 2,
            rink_center_x,
        )
        for formation in (balanced, offensive, defensive):
            self.assertGreater(formation["GK"].y, min(
                slot.y for key, slot in formation.items() if key != "GK"
            ))

    async def test_packs_menu_shows_prices_and_odds_and_confirms_once(self) -> None:
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
        self.assertEqual(
            set(field_values),
            {
                "GOLD · 5,000,000 coins",
                "TOTW · 20,000,000 coins",
                "S2 TOTS · 50,000,000 coins",
                "S1 TOTS · 35,000,000 coins",
                "S1 TOTY PACK · 60,000,000 coins",
            },
        )
        self.assertIn(
            "Contains one random Gold player card.",
            field_values["GOLD · 5,000,000 coins"],
        )
        self.assertIn(
            "Contains one random TOTW player card.",
            field_values["TOTW · 20,000,000 coins"],
        )
        self.assertIn(
            "Contains one random S2 TOTS player card.",
            field_values["S2 TOTS · 50,000,000 coins"],
        )
        s2_odds = field_values["S2 TOTS · 50,000,000 coins"]
        for name in (
            "Cryptic",
            "Hala (S2 TOTS 98)",
            "Xavier",
            "EBS (S2 TOTS 99)",
            "Jaycob (S2 TOTS 96)",
            "Licha (S2 TOTS 96)",
        ):
            self.assertIn(f"{name} 16.67%", s2_odds)
        self.assertIn("Price: 5,000,000 coins.", field_values["GOLD · 5,000,000 coins"])
        self.assertIn("Price: 20,000,000 coins.", field_values["TOTW · 20,000,000 coins"])
        self.assertIn("50,000,000 coins", field_values["S2 TOTS · 50,000,000 coins"])
        s1_odds = field_values["S1 TOTS · 35,000,000 coins"]
        for name in (
            "Dan (S1 TOTS 96)",
            "Hala (S1 TOTS 99)",
            "Ebs (S1 TOTS 98)",
        ):
            self.assertIn(name, s1_odds)
        self.assertIn("35,000,000 coins", s1_odds)
        s1_toty_field = field_values["S1 TOTY PACK · 60,000,000 coins"]
        self.assertIn("Contains one random S1 TOTY player card.", s1_toty_field)
        self.assertIn("Rarity: S1 TOTY.", s1_toty_field)
        self.assertIn("Price: 60,000,000 coins.", s1_toty_field)
        for name in (
            "Ebs (S1 TOTY 98)",
            "Hala (S1 TOTY 99)",
            "Cam (S1 TOTY 97)",
            "Road (S1 TOTY 98)",
            "Matrix (S1 TOTY 97)",
            "Lauren (S1 TOTY 97)",
        ):
            self.assertIn(f"{name} 16.67%", s1_toty_field)
        self.assert_pack_menu_shows_odds(menu["embed"])

        view = menu["view"]
        s2_tots_button = next(
            button for button in view.children if button.rarity == "S2 TOTS"
        )
        self.assertTrue(s2_tots_button.disabled)
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
            purchased_inventory[0].player,
            self.database.get_players(rarity="GOLD"),
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

    async def test_pack_command_selects_and_purchases_each_pack_type(self) -> None:
        pack_cases = (
            ("GOLD", GOLD_PACK_COST, "Contains one random Gold player card."),
            ("TOTW", TOTW_PACK_COST, "Contains one random TOTW player card."),
            ("S2 TOTS", S2_TOTS_PACK_COST, "Contains one random S2 TOTS player card."),
            ("S1 TOTS", S1_TOTS_PACK_COST, "Contains one random S1 TOTS player card."),
        )
        for index, (rarity, cost, offer) in enumerate(pack_cases):
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
                self.assertIn(offer, fields[pack_field])
                self.assertIn(f"Price: {cost:,} coins.", fields[pack_field])
                self.assert_pack_menu_shows_odds(menu["embed"])

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

    async def test_packs_command_buys_multiple_packs_in_one_purchase(self) -> None:
        user_id = "multi-pack-command-user"
        quantity = 3
        self.database.set_balance(user_id, quantity * GOLD_PACK_COST + 321)
        command_response = FakeResponse()
        await PackCommands.packs.callback(
            PackCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=command_response,
            ),
            quantity,
        )

        menu = command_response.messages[0]
        view = menu["view"]
        self.assertEqual(view.quantity, quantity)
        gold_button = next(button for button in view.children if button.rarity == "GOLD")
        self.assertEqual(gold_button.quantity, quantity)
        self.assertIn(f"×{quantity}", gold_button.label)

        purchase_response = FakeResponse()
        await gold_button.callback(
            SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                response=purchase_response,
                id=13_001,
                message=FakeMessage(),
            )
        )
        result = purchase_response.messages[0]
        self.assertIn(f"{quantity} Gold packs", result["content"])
        self.assertIn(f"{quantity * GOLD_PACK_COST:,} coins", result["content"])
        self.assertEqual(len(result["embeds"]), quantity)
        self.assertTrue(
            all("(Gold)" in embed.title for embed in result["embeds"])
        )
        self.assertEqual(len(result["files"]), quantity)
        for uploaded_file in result["files"]:
            self.assertTrue(uploaded_file.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
            uploaded_file.close()

        self.assertEqual(self.database.get_balance(user_id), 321)
        inventory = self.database.get_inventory(user_id)
        self.assertEqual(sum(entry.quantity for entry in inventory), quantity)
        self.assertTrue(all(entry.player.rarity == "GOLD" for entry in inventory))

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
                ("90 Hala (Gold) — 90 OVR", str(gold_hala.id)),
                ("93 Hala (TOTW) — 93 OVR", str(totw_hala.id)),
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
        self.assertEqual(
            fields["Team value"],
            "**15,680,000 coins** · 3 starter slots and 2 substitute slots",
        )
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
        self.assertEqual(
            struct.unpack(">II", image_bytes[16:24]),
            (IMAGE_WIDTH, IMAGE_HEIGHT),
        )
        svg = build_lineup_svg(self.database.get_lineup(user_id), "Defensive", "89 OVR")
        self.assertEqual(svg.count("<image href="), 5)
        for formation, positions in FORMATION_STARTERS.items():
            with self.subTest(formation=formation):
                formation_svg = build_lineup_svg({}, formation, "Not rated yet")
                starter_slots = [
                    FORMATION_SLOT_LAYOUTS[formation][position]
                    for position in positions
                ]
                self.assertEqual(len(starter_slots), 3)
                self.assertEqual(
                    len(
                        {
                            (slot.x, slot.y, slot.width, slot.height)
                            for slot in starter_slots
                        }
                    ),
                    3,
                )
                for index, layout in enumerate(starter_slots):
                    self.assertGreaterEqual(layout.x, RINK_LEFT)
                    self.assertGreaterEqual(layout.y, RINK_TOP)
                    self.assertLessEqual(layout.x + layout.width, RINK_RIGHT)
                    self.assertLessEqual(layout.y + layout.height, RINK_BOTTOM)
                    for other in starter_slots[index + 1 :]:
                        overlaps = (
                            layout.x < other.x + other.width
                            and layout.x + layout.width > other.x
                            and layout.y < other.y + other.height
                            and layout.y + layout.height > other.y
                        )
                        self.assertFalse(overlaps)
                self.assertEqual(formation_svg.count('id="slot-'), 5)
                self.assertIn('cx="600" cy="815" r="115"', formation_svg)
                self.assertEqual(formation_svg.count('r="58" fill="none"'), 4)
        for layout in SUBSTITUTE_SLOT_LAYOUTS:
            self.assertGreater(layout.y, RINK_BOTTOM)

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
                self.assertEqual(
                    struct.unpack(">II", image_bytes[16:24]),
                    (IMAGE_WIDTH, IMAGE_HEIGHT),
                )
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
        for formation, positions in FORMATION_STARTERS.items():
            with self.subTest(empty_formation=formation):
                empty_svg = build_lineup_svg({}, formation, "Not rated yet")
                self.assertEqual(empty_svg.count('id="slot-'), 5)
                for position in positions + SUBSTITUTE_POSITIONS:
                    slot_start = empty_svg.index(f'id="slot-{position.lower()}"')
                    slot_end = empty_svg.index("</g>", slot_start)
                    self.assertIn(
                        "stroke-dasharray=",
                        empty_svg[slot_start:slot_end],
                    )

    async def test_new_gold_cards_work_across_card_inventory_packs_set_and_team(self) -> None:
        expected = {
            "Megan": {
                "label": "84 Megan (Gold)",
                "position": "GK",
                "country": "Canada",
                "team": "Colorado Avalanche",
                "allowed": ("GK",),
                "forbidden": ("WING1", "CM1"),
                "image": "cards/megan_gold.png",
                "value": 560_000,
                "stats": {"SAV": 85, "DIV": 85, "PAS": 82, "CLE": 82, "SWE": 83, "ACT": 85},
            },
            "Jaycob": {
                "label": "89 Jaycob (Gold)",
                "position": "GK",
                "country": "England",
                "team": "New York Rangers",
                "allowed": ("GK",),
                "forbidden": ("WING1", "CM1"),
                "image": "cards/jaycob_gold.png",
                "value": 1_300_000,
                "stats": {"SAV": 90, "DIV": 90, "PAS": 85, "CLE": 85, "SWE": 87, "ACT": 89},
            },
            "Karuwrld": {
                "label": "82 Karuwrld (Gold)",
                "position": "WI",
                "country": "Scotland",
                "team": "Colorado Avalanche",
                "allowed": ("WING1", "WING2"),
                "forbidden": ("GK", "CM1"),
                "image": "cards/karuwrld_gold.png",
                "value": 460_000,
                "stats": {"SHO": 84, "DRI": 78, "SAV": 77, "PAS": 80, "DEF": 82, "ACT": 76},
            },
            "HHH": {
                "label": "84 HHH (Gold)",
                "position": "WI",
                "country": "England",
                "team": "Anaheim Ducks",
                "allowed": ("WING1", "WING2"),
                "forbidden": ("GK", "CM1"),
                "image": "cards/hhh_gold.png",
                "value": 450_000,
                "stats": {"SHO": 89, "DRI": 85, "SAV": 77, "PAS": 84, "DEF": 78, "ACT": 82},
            },
            "Apex": {
                "label": "88 Apex (Gold)",
                "position": "WI",
                "country": "England",
                "team": "Buffalo Sabres",
                "allowed": ("WING1", "WING2"),
                "forbidden": ("GK", "CM1"),
                "image": "cards/apex_gold.png",
                "value": 1_500_000,
                "stats": {"SHO": 91, "DRI": 88, "SAV": 76, "PAS": 86, "DEF": 80, "ACT": 82},
            },
            "R4ze": {
                "label": "84 R4ze (Gold)",
                "position": "GK",
                "country": "Georgia",
                "team": "Florida Panthers",
                "allowed": ("GK",),
                "forbidden": ("WING1", "CM1"),
                "image": "cards/r4ze_gold.png",
                "value": 430_000,
                "stats": {"SAV": 87, "DIV": 85, "PAS": 81, "CLE": 83, "SWE": 80, "ACT": 84},
            },
        }
        player_cog = PlayerCommands(self.database)
        for name, details in expected.items():
            with self.subTest(card=name):
                player = self.database.get_player(name)
                self.assertIsNotNone(player)
                assert player is not None
                image_path = resolve_card_image(player)
                self.assertIsNotNone(image_path)
                assert image_path is not None
                self.assertEqual(image_path.as_posix().split("/")[-2:], ["cards", details["image"].split("/")[-1]])

                card_response = FakeResponse()
                await PlayerCommands.card.callback(
                    player_cog,
                    SimpleNamespace(response=card_response),
                    name,
                )
                card_message = card_response.messages[0]
                self.assertEqual(card_message["embed"].title, details["label"])
                fields = {
                    field.name: field.value for field in card_message["embed"].fields
                }
                self.assertEqual(fields["Position"], details["position"])
                self.assertEqual(fields["Country"], details["country"])
                self.assertEqual(fields["Team"], details["team"])
                self.assertEqual(fields["Card Type"], "Gold")
                self.assertEqual(fields["Value"], f"{details['value']:,} coins")
                self.assertEqual(
                    fields["Stats"],
                    " · ".join(
                        f"**{stat}** {value}"
                        for stat, value in details["stats"].items()
                    ),
                )
                card_file = card_message["file"]
                self.assertEqual(card_file.filename, image_path.name)
                self.assertEqual(card_file.fp.read(), image_path.read_bytes())
                card_file.close()

        owner_id = "new-gold-cards-inventory"
        for name in expected:
            self.database.add_inventory(owner_id, name)
        inventory_response = FakeResponse()
        await InventoryCommands.inventory.callback(
            InventoryCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=owner_id),
                response=inventory_response,
            ),
        )
        inventory_text = inventory_response.messages[0]["embed"].description
        for details in expected.values():
            self.assertIn(f"**{details['label']}** ×1", inventory_text)

        packs_response = FakeResponse()
        await PackCommands.packs.callback(
            PackCommands(self.database),
            SimpleNamespace(
                user=SimpleNamespace(id=owner_id),
                response=packs_response,
            ),
        )
        gold_pack_text = {
            field.name: field.value
            for field in packs_response.messages[0]["embed"].fields
        }["GOLD · 5,000,000 coins"]
        self.assertIn("Contains one random Gold player card.", gold_pack_text)
        self.assert_pack_menu_shows_odds(packs_response.messages[0]["embed"])
        for name in expected:
            player = self.database.get_player(name)
            self.assertIsNotNone(player)
            self.assertIn(player, self.database.get_players(rarity="GOLD"))

        team_cog = TeamCommands(self.database)
        for index, (name, details) in enumerate(expected.items()):
            user_id = f"new-gold-card-team-{index}"
            self.database.add_inventory(user_id, name)
            player = self.database.get_player(name)
            assert player is not None
            for position in details["forbidden"]:
                with self.subTest(card=name, forbidden_position=position):
                    response = FakeResponse()
                    await TeamCommands.set_position.callback(
                        team_cog,
                        SimpleNamespace(
                            user=SimpleNamespace(id=user_id),
                            response=response,
                        ),
                        discord.app_commands.Choice(name=position, value=position),
                        name,
                    )
                    self.assertIn(
                        "can only be assigned to",
                        response.messages[0]["content"],
                    )

            autocomplete = await TeamCommands.set_player_autocomplete(
                team_cog,
                SimpleNamespace(
                    user=SimpleNamespace(id=owner_id),
                    namespace=SimpleNamespace(position=details["allowed"][0]),
                ),
                "",
            )
            choice_ids = {choice.value for choice in autocomplete}
            self.assertIn(str(player.id), choice_ids)
            for other_name in expected:
                other = self.database.get_player(other_name)
                assert other is not None
                if (
                    other.allowed_lineup_positions is not None
                    and details["allowed"][0] not in other.allowed_lineup_positions
                ):
                    self.assertNotIn(str(other.id), choice_ids)

            response = FakeResponse()
            await TeamCommands.set_position.callback(
                team_cog,
                SimpleNamespace(
                    user=SimpleNamespace(id=user_id),
                    response=response,
                ),
                discord.app_commands.Choice(
                    name=details["allowed"][0],
                    value=details["allowed"][0],
                ),
                name,
            )
            self.assertIn(details["label"], response.messages[0]["content"])
            svg = build_lineup_svg(
                self.database.get_lineup(user_id),
                "Balanced",
                f"{player.overall} OVR",
            )
            image_uri = _player_image_uri(player)
            self.assertIsNotNone(image_uri)
            assert image_uri is not None
            self.assertIn(image_uri, svg)
            self.assertNotIn("ART UNAVAILABLE", svg)

            team_response = FakeResponse()
            team_interaction = SimpleNamespace(
                user=SimpleNamespace(id=user_id, display_name=name),
                response=team_response,
                followup=FakeFollowup(),
            )
            await TeamCommands.team.callback(team_cog, team_interaction)
            team_message = team_interaction.followup.messages[0]
            slot_label = {
                "GK": "GK · Goalie",
                "WING1": "WING1 · Wing 1",
            }[details["allowed"][0]]
            self.assertIn(details["label"], {
                field.name: field.value
                for field in team_message["embed"].fields
            }[slot_label])
            self.assertIn(
                f"Value: {details['value']:,} coins",
                {
                    field.name: field.value
                    for field in team_message["embed"].fields
                }[slot_label],
            )
            team_file = team_message["file"]
            self.assertTrue(team_file.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
            team_file.close()

            seller_id = f"new-gold-card-seller-{index}"
            self.database.add_inventory(seller_id, name)
            sale_response = FakeResponse()
            await InventoryCommands.sell.callback(
                InventoryCommands(self.database),
                SimpleNamespace(
                    user=SimpleNamespace(id=seller_id),
                    response=sale_response,
                ),
                name,
                1,
            )
            self.assertIn(
                f"for {details['value']:,} coins",
                sale_response.messages[0]["content"],
            )
            self.assertEqual(
                self.database.get_balance(seller_id),
                details["value"],
            )
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
        self.assertIn("ART UNAVAILABLE", svg)

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

    async def test_s1_special_cards_can_be_selected_in_minigames_and_drafts(self) -> None:
        for index, name in enumerate(
            (
                "Dan (S1 TOTS 96)",
                "Hala (S1 TOTS 99)",
                "Ebs (S1 TOTS 98)",
                "Gamer (S1 TOTS 97)",
                "Road (S1 TOTS 98)",
                "Lauren (S1 TOTS 97)",
                "Ebs (S1 TOTY 98)",
                "Hala (S1 TOTY 99)",
                "Cam (S1 TOTY 97)",
                "Road (S1 TOTY 98)",
                "Matrix (S1 TOTY 97)",
                "Lauren (S1 TOTY 97)",
            )
        ):
            player = self.database.get_player(name)
            self.assertIsNotNone(player)
            assert player is not None
            interaction, _, selected, _ = await self.start_game(
                user_id=5100 + index,
                guild_id=6100 + index,
                selected=player,
            )
            self.assertEqual(selected.name, name)
            fields = {
                field.name: field.value
                for field in interaction.response.messages[0]["embed"].fields
            }
            self.assertEqual(fields["🌍 Nationality"], player.country)
            self.assertEqual(fields["⭐ Rating"], f"{player.overall} OVR")
            self.assertEqual(fields["🏒 Position"], player.position)
            self.assertEqual(fields["Card Type"], player.rarity)

        class PrioritizeS1Cards:
            def __init__(self):
                self.populations = []

            def sample(self, population, count):
                self.populations.append(tuple(population))
                selected = [
                    player
                    for player in population
                    if player.rarity in {"S1 TOTS", "S1 TOTY"}
                ][:count]
                selected_ids = {player.id for player in selected}
                selected.extend(
                    player for player in population if player.id not in selected_ids
                )
                return selected[:count]

        captured = PrioritizeS1Cards()
        groups = generate_draft_groups(
            self.database.get_players(),
            rng=captured,
        )
        self.assertTrue(
            {"Dan (S1 TOTS 96)", "Gamer (S1 TOTS 97)"}
            <= {player.name for player in groups[1]}
        )
        self.assertTrue(
            {"Road (S1 TOTS 98)", "Lauren (S1 TOTS 97)"}
            <= {player.name for player in groups[0]}
        )
        self.assertTrue(
            {"Hala (S1 TOTS 99)", "Ebs (S1 TOTS 98)"}
            <= {player.name for group in groups[2:] for player in group}
        )
        self.assertTrue(
            {
                "Ebs (S1 TOTY 98)",
                "Hala (S1 TOTY 99)",
                "Cam (S1 TOTY 97)",
            }
            <= {player.name for group in groups[2:] for player in group}
        )
        self.assertIn(
            "Road (S1 TOTY 98)",
            {player.name for player in captured.populations[0]},
        )
        self.assertIn(
            "Lauren (S1 TOTY 97)",
            {player.name for player in captured.populations[0]},
        )
        self.assertIn(
            "Matrix (S1 TOTY 97)",
            {player.name for player in captured.populations[1]},
        )

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

    async def test_start_is_public_and_shows_four_clues_with_answer_button(self) -> None:
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
            ["🌍 Nationality", "⭐ Rating", "🏒 Position", "Card Type"],
        )
        self.assertEqual(embed.fields[0].value, player.country)
        self.assertEqual(embed.fields[1].value, f"{player.overall} OVR")
        self.assertEqual(embed.fields[2].value, player.position)
        self.assertEqual(embed.fields[3].value, format_card_type(player.rarity))
        self.assertNotIn(player.name.casefold(), embed.description.casefold())
        self.assertIsNone(embed.image.url)
        self.assertEqual(view.owner_id, interaction.user.id)

    async def test_new_gold_cards_are_available_in_minigame_with_type_and_original_art(self) -> None:
        for index, name in enumerate(
            (
                "Megan",
                "Jaycob",
                "Karuwrld",
                "HHH",
                "Apex",
                "R4ze",
                "Chicken",
                "Sluckz",
                "Frozend",
            )
        ):
            with self.subTest(card=name):
                player = self.database.get_player(name)
                self.assertIsNotNone(player)
                assert player is not None
                self.assertIn(player, self.database.get_players())

                interaction, channel, _, view = await self.start_game(
                    user_id=6000 + index,
                    username=f"GoldPlayer{index}",
                    selected=player,
                )
                prompt = interaction.response.messages[0]["embed"]
                self.assertEqual(prompt.fields[-1].name, "Card Type")
                self.assertEqual(prompt.fields[-1].value, "Gold")
                self.assertEqual(prompt.fields[0].value, player.country)

                await view.on_timeout()
                reveal_message = channel.messages[0]
                reveal = reveal_message["embed"]
                self.assertEqual(
                    reveal.title,
                    f"🏒 The player was {player_card_label(player)}",
                )
                self.assertEqual(
                    {field.name: field.value for field in reveal.fields}["Card Type"],
                    "Gold",
                )
                reveal_file = reveal_message["file"]
                image_path = resolve_card_image(player)
                self.assertIsNotNone(image_path)
                assert image_path is not None
                self.assertEqual(reveal_file.fp.read(), image_path.read_bytes())
                reveal_file.close()

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

    async def test_mr_p_dark_and_road_are_valid_minigame_answers(self) -> None:
        for index, name in enumerate(("Mr P", "Dark", "Road")):
            with self.subTest(card=name):
                card = self.database.get_player(name)
                self.assertIsNotNone(card)
                assert card is not None
                self.assertIn(card, self.database.get_players())

                interaction, _channel, selected, view = await self.start_game(
                    user_id=1800 + index,
                    username=f"Card{index}",
                    selected=card,
                )
                self.assertEqual(selected.name, name)
                self.assertEqual(view.player.name, name)
                clues = interaction.response.messages[0]["embed"].fields
                self.assertEqual(clues[0].value, card.country)
                self.assertEqual(clues[1].value, f"{card.overall} OVR")
                self.assertEqual(clues[2].value, card.position)

    async def test_ebs_cam_and_gold_licha_are_valid_minigame_answers(self) -> None:
        for index, name in enumerate(("Ebs", "Cam", "Licha (Gold 90)")):
            with self.subTest(card=name):
                card = self.database.get_player(name)
                self.assertIsNotNone(card)
                assert card is not None
                self.assertIn(card, self.database.get_players())

                user_id = 2800 + index
                interaction, channel, selected, view = await self.start_game(
                    user_id=user_id,
                    username=f"NewCard{index}",
                    selected=card,
                )
                self.assertEqual(selected.name, name)
                self.assertEqual(view.player.name, name)
                clues = interaction.response.messages[0]["embed"].fields
                self.assertEqual(clues[0].value, card.country)
                self.assertEqual(clues[1].value, f"{card.overall} OVR")
                self.assertEqual(clues[2].value, card.position)

                response = await self.submit_answer(
                    view,
                    self.make_user(user_id, f"NewCard{index}"),
                    card.name,
                )
                self.assertIn("Correct", response.messages[0]["content"])
                self.assertEqual(self.database.get_balance(user_id), MINIGAME_REWARD)
                reveal = channel.messages[0]
                self.assertEqual(
                    reveal["embed"].title,
                    f"🏒 The player was {player_card_label(card)}",
                )
                upload = reveal["file"]
                self.assertEqual(upload.filename, Path(card.image_path).name)
                self.assertTrue(upload.fp.read().startswith(b"\x89PNG\r\n\x1a\n"))
                upload.close()

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

    async def test_incorrect_answer_is_private_and_uses_the_attempt(self) -> None:
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

        response = FakeResponse()
        await view.answer_button.callback(
            SimpleNamespace(user=owner, response=response)
        )
        self.assertEqual(response.messages[0]["content"], "You have already used your one attempt for this game.")
        self.assertEqual(response.modals, [])
        self.assertEqual(self.database.get_balance(1301), 0)

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
        self.assertEqual(MINIGAME_DURATION_SECONDS, 180)
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
        self.assertEqual(self.database.get_xp_profile(1601).total_xp, 0)
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
