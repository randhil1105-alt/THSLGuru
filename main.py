"""Entry point for the THSL Guru Discord hockey-card bot."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.init import load_command_modules
from config import PROJECT_ROOT, Settings, load_settings
from database import Database

LOGGER = logging.getLogger("thsl_guru")


def load_player_seed(path: Path | None = None) -> list[dict[str, object]]:
    seed_path = path or PROJECT_ROOT / "data" / "players.json"
    try:
        records = json.loads(seed_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Player seed file was not found: {seed_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Player seed file is invalid JSON: {seed_path}") from exc
    if not isinstance(records, list):
        raise RuntimeError("data/players.json must contain a list of player records.")
    if not all(isinstance(record, dict) for record in records):
        raise RuntimeError("Every player seed entry must be a JSON object.")
    return records


class GuruBot(discord_commands.Bot):
    def __init__(
        self,
        database: Database,
        development_guild_id: int | None = None,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )
        self.database = database
        self.development_guild_id = development_guild_id

    async def setup_hook(self) -> None:
        await load_command_modules(self)
        try:
            global_commands = await self.tree.sync()
            global_command_names = sorted(command.name for command in global_commands)
            LOGGER.info(
                "Discord accepted %d global slash commands: %s",
                len(global_commands),
                ", ".join(global_command_names) or "(none)",
            )
            minigame_command = next(
                (command for command in global_commands if command.name == "minigame"),
                None,
            )
            if minigame_command is not None:
                mode_option = next(
                    (
                        option
                        for option in (minigame_command.options or [])
                        if option.name == "mode"
                    ),
                    None,
                )
                mode_choices = [
                    choice.name
                    for choice in (mode_option.choices or [])
                ] if mode_option is not None else []
                LOGGER.info(
                    "Synced /minigame mode choices: %s",
                    ", ".join(mode_choices) or "(none)",
                )

            if global_commands:
                auction_command = next(
                    (
                        command
                        for command in global_commands
                        if command.name == "auction"
                    ),
                    None,
                )
                auction_subcommands = (
                    sorted(
                        option.name
                        for option in (auction_command.options or [])
                    )
                    if auction_command is not None
                    else []
                )
                LOGGER.info(
                    "Synced /auction subcommands: %s",
                    ", ".join(auction_subcommands) or "(none)",
                )

                public_command_names = {
                    command.name
                    for command in global_commands
                    if command.default_member_permissions is None
                }
                for command_name in ("auction", "claim", "minigame"):
                    if command_name in public_command_names:
                        LOGGER.info(
                            "Verified /%s has no default member permission restriction.",
                            command_name,
                        )
                    else:
                        LOGGER.error(
                            "Synced /%s is missing or has a default member permission restriction.",
                            command_name,
                        )

            restricted_commands = sorted(
                command.name
                for command in global_commands
                if command.default_member_permissions is not None
            )
            LOGGER.info(
                "Synced %d global slash commands (%d available to everyone; "
                "%d with default member restrictions).",
                len(global_commands),
                len(global_commands) - len(restricted_commands),
                len(restricted_commands),
            )
            if restricted_commands:
                LOGGER.info(
                    "Global commands with default_member_permissions: %s",
                    ", ".join(restricted_commands),
                )

            if self.development_guild_id is not None:
                guild = discord.Object(id=self.development_guild_id)
                self.tree.copy_global_to(guild=guild)
                commands = await self.tree.sync(guild=guild)
                sync_scope = f"to development guild {self.development_guild_id}"
                LOGGER.info(
                    "Synced %d slash commands %s.",
                    len(commands),
                    sync_scope,
                )
        except Exception:
            LOGGER.exception("Slash-command registration failed.")
            raise


def create_bot(
    database: Database,
    development_guild_id: int | None = None,
) -> GuruBot:
    """Construct a bot without connecting, for command-loading tests."""
    bot = GuruBot(database, development_guild_id)

    @bot.tree.error
    async def handle_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        public_response = getattr(
            getattr(interaction, "command", None),
            "name",
            None,
        ) in {"packs", "pack", "inventory", "balance", "minigame", "match"}
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need administrator permission to use that command."
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=not public_response,
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=not public_response,
                )
            return

        LOGGER.error(
            "Slash command failed (%s).",
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "The command failed. Please try again later."
        if interaction.response.is_done():
            await interaction.followup.send(
                message,
                ephemeral=not public_response,
            )
        else:
            await interaction.response.send_message(
                message,
                ephemeral=not public_response,
            )

    return bot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings: Settings = load_settings()
        database = Database(settings.database_path)
        database.initialize(load_player_seed())
        bot = create_bot(database, settings.development_guild_id)
    except (RuntimeError, OSError) as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(2) from exc

    LOGGER.info("Starting THSL Guru Discord bot.")
    try:
        bot.run(settings.token, log_handler=None)
    except discord.LoginFailure as exc:
        LOGGER.error("Discord rejected the configured bot token. Replace it in Secrets.")
        raise SystemExit(2) from exc
    except Exception:
        LOGGER.exception("Bot stopped because of a startup or runtime error.")
        raise


if __name__ == "__main__":
    main()
