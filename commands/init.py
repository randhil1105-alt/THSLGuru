"""Load every Discord command extension once."""

from __future__ import annotations

from typing import Any

COMMAND_MODULES = (
    "commands.player",
    "commands.economy",
    "commands.auction",
    "commands.inventory",
    "commands.packs",
    "commands.team",
    "commands.admin",
    "commands.information",
    "commands.minigame",
    "commands.match",
)

if len(COMMAND_MODULES) != len(set(COMMAND_MODULES)):
    raise RuntimeError("Command extension list contains duplicates.")


async def load_command_modules(bot: Any) -> None:
    """Load each extension if it has not already been loaded on this bot."""
    for module in COMMAND_MODULES:
        if module not in bot.extensions:
            await bot.load_extension(module)
