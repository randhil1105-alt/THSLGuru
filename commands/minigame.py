"""Public, server-scoped hockey player guessing game."""

from __future__ import annotations

import asyncio
import io
import logging
import random
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import (
    format_coins,
    player_card_label,
    player_embed,
    resolve_card_image,
)
from config import DEFAULT_MINIGAME_REWARD
from database import Database, Player

LOGGER = logging.getLogger("thsl_guru.minigame")

MINIGAME_DURATION_SECONDS = 60


@dataclass
class ActiveGame:
    guild_id: int
    channel_id: int
    channel: discord.abc.Messageable
    player: Player
    timeout_task: asyncio.Task[None] | None = None


class MinigameCommands(discord_commands.Cog):
    def __init__(
        self,
        database: Database,
        *,
        reward: int = DEFAULT_MINIGAME_REWARD,
        duration_seconds: float = MINIGAME_DURATION_SECONDS,
    ) -> None:
        if reward <= 0:
            raise ValueError("The minigame reward must be a positive integer.")
        if duration_seconds <= 0:
            raise ValueError("The minigame duration must be positive.")
        self.database = database
        self.reward = int(reward)
        self.duration_seconds = float(duration_seconds)
        self.active_games: dict[int, ActiveGame] = {}
        self._lock = asyncio.Lock()

    @app_commands.command(
        name="minigame",
        description="Guess the hockey player from three public clues.",
    )
    async def minigame(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or channel is None:
            await interaction.response.send_message(
                "The minigame can only be played in a server channel.",
                ephemeral=False,
            )
            return

        async with self._lock:
            existing = self.active_games.get(guild.id)
            if existing is not None:
                await interaction.response.send_message(
                    f"A game is already active in <#{existing.channel_id}>.",
                    ephemeral=False,
                )
                return

            players = self.database.get_players()
            if not players:
                await interaction.response.send_message(
                    "There are no player cards in the catalog, so a game cannot start.",
                    ephemeral=False,
                )
                return

            game = ActiveGame(
                guild_id=guild.id,
                channel_id=channel.id,
                channel=channel,
                player=random.choice(players),
            )
            embed = discord.Embed(
                title="🏒 GUESS WHO?",
                description=(
                    "Who is this player?\n"
                    "Everyone can guess in this channel. First correct guess wins. "
                    f"You have {int(self.duration_seconds)} seconds."
                ),
                color=discord.Color.blurple(),
            )
            embed.add_field(name="🌍 Country / Nationality", value=game.player.country)
            embed.add_field(
                name="⭐ Overall rating (OVR)",
                value=f"{game.player.overall} OVR",
            )
            embed.add_field(name="🏒 Position", value=game.player.position)

            self.active_games[guild.id] = game
            try:
                await interaction.response.send_message(
                    embed=embed,
                    ephemeral=False,
                )
            except Exception:
                self.active_games.pop(guild.id, None)
                raise

            game.timeout_task = asyncio.create_task(self._expire_game(game))

    @discord_commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        if (
            guild is None
            or author is None
            or getattr(author, "bot", False)
            or not getattr(message, "content", "").strip()
        ):
            return

        async with self._lock:
            game = self.active_games.get(guild.id)
            if (
                game is None
                or getattr(message.channel, "id", None) != game.channel_id
            ):
                return

            guess = message.content.strip()
            if guess.casefold() != game.player.name.strip().casefold():
                result = "incorrect"
                balance = None
            else:
                try:
                    balance = self.database.add_coins(author.id, self.reward)
                except Exception:
                    LOGGER.exception("Could not save a minigame reward.")
                    result = "reward_error"
                else:
                    result = "winner"
                    self.active_games.pop(game.guild_id, None)
                    if game.timeout_task is not None:
                        game.timeout_task.cancel()

        if result == "incorrect":
            await game.channel.send(
                f"{author.mention}, that guess was incorrect.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if result == "reward_error":
            await game.channel.send(
                "That was the correct answer, but the coin reward could not be "
                "saved. The game is still active; please try again.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await self._reveal(
            game,
            winner=author,
            balance=balance,
        )

    async def _expire_game(self, game: ActiveGame) -> None:
        try:
            await asyncio.sleep(self.duration_seconds)
        except asyncio.CancelledError:
            return

        async with self._lock:
            if self.active_games.get(game.guild_id) is not game:
                return
            self.active_games.pop(game.guild_id, None)

        await self._reveal(game)

    async def _reveal(
        self,
        game: ActiveGame,
        *,
        winner: Any | None = None,
        balance: int | None = None,
    ) -> None:
        embed = player_embed(game.player)
        embed.title = f"🏒 The player was {player_card_label(game.player)}"
        if winner is None:
            embed.description = "Time is up. Nobody guessed correctly."
        else:
            username = getattr(
                winner,
                "name",
                getattr(winner, "display_name", "player"),
            )
            mention = getattr(winner, "mention", f"<@{winner.id}>")
            embed.description = (
                f"{mention} (`{username}`) guessed correctly and wins "
                f"**{format_coins(self.reward)}**. "
                f"New balance: **{format_coins(int(balance or 0))}**."
            )

        image_path = resolve_card_image(game.player)
        upload: discord.File | None = None
        if image_path is not None:
            upload = discord.File(
                io.BytesIO(image_path.read_bytes()),
                filename=image_path.name,
            )
            embed.set_image(url=f"attachment://{image_path.name}")
        else:
            embed.add_field(
                name="Card image",
                value="No original card image is available for this player.",
                inline=False,
            )

        await game.channel.send(
            embed=embed,
            file=upload,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def cog_unload(self) -> None:
        for game in self.active_games.values():
            if game.timeout_task is not None:
                game.timeout_task.cancel()
        self.active_games.clear()


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(
        MinigameCommands(
            bot.database,
            reward=getattr(bot, "minigame_reward", DEFAULT_MINIGAME_REWARD),
        )
    )
