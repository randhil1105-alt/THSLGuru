"""Runtime configuration for the THSL Guru Discord bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "guru.sqlite3"
DEFAULT_MINIGAME_REWARD = 500_000


@dataclass(frozen=True)
class Settings:
    token: str
    database_path: Path
    development_guild_id: int | None = None
    minigame_reward: int = DEFAULT_MINIGAME_REWARD


def load_settings() -> Settings:
    """Read configuration from the process environment without exposing secrets."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is missing. Add the rotated bot token through "
            "Replit Secrets or the host's environment-variable settings."
        )

    guild_value = os.environ.get("DISCORD_GUILD_ID", "").strip()
    guild_id: int | None = None
    if guild_value:
        try:
            guild_id = int(guild_value)
        except ValueError as exc:
            raise RuntimeError("DISCORD_GUILD_ID must be a positive integer.") from exc
        if guild_id <= 0:
            raise RuntimeError("DISCORD_GUILD_ID must be a positive integer.")

    database_value = os.environ.get("BOT_DATABASE_PATH", "").strip()
    database_path = (
        Path(database_value).expanduser()
        if database_value
        else DEFAULT_DATABASE_PATH
    )
    reward_value = os.environ.get("MINIGAME_REWARD", "").strip()
    minigame_reward = DEFAULT_MINIGAME_REWARD
    if reward_value:
        try:
            minigame_reward = int(reward_value)
        except ValueError as exc:
            raise RuntimeError("MINIGAME_REWARD must be a positive integer.") from exc
        if minigame_reward <= 0:
            raise RuntimeError("MINIGAME_REWARD must be a positive integer.")

    return Settings(
        token=token,
        database_path=database_path,
        development_guild_id=guild_id,
        minigame_reward=minigame_reward,
    )
