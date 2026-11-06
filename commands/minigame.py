"""Public, individual hockey-player guessing games."""

from __future__ import annotations

import asyncio
import io
import logging
import random
import re
from collections.abc import Callable

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from commands.common import (
    format_coins,
    player_card_label,
    player_embed,
    resolve_card_image,
)
from database import Database, Player

LOGGER = logging.getLogger("thsl_guru.minigame")

MINIGAME_REWARD = 500_000
MINIGAME_DURATION_SECONDS = 180


def _normalize_answer(value: str) -> str:
    """Compare names case-insensitively and collapse extra whitespace."""
    return re.sub(r"\s+", " ", value).strip().casefold()


class IndividualMinigameView(discord.ui.View):
    """One player's independent, owner-only answer session."""

    def __init__(
        self,
        database: Database,
        *,
        owner_id: int,
        player: Player,
        channel: discord.abc.Messageable,
        duration_seconds: float = MINIGAME_DURATION_SECONDS,
        game_id: str | None = None,
        on_finished: Callable[[IndividualMinigameView], None] | None = None,
    ) -> None:
        super().__init__(timeout=duration_seconds)
        self.database = database
        self.owner_id = int(owner_id)
        self.player = player
        self.channel = channel
        self.duration_seconds = float(duration_seconds)
        self.game_id = str(game_id) if game_id is not None else f"{self.owner_id}:{id(self)}"
        self.on_finished = on_finished
        self.attempted_user_game_ids: set[tuple[str, int]] = set()
        self.message: discord.Message | None = None
        self.finished = False
        self._deadline_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

        self.answer_button = discord.ui.Button(
            label="Answer",
            style=discord.ButtonStyle.primary,
        )
        self.answer_button.callback = self._answer_clicked
        self.add_item(self.answer_button)

    def start_deadline(self) -> None:
        if self._deadline_task is None:
            self._deadline_task = asyncio.create_task(self._expire_after_deadline())

    def cancel_deadline(self) -> None:
        task = self._deadline_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _expire_after_deadline(self) -> None:
        try:
            await asyncio.sleep(self.duration_seconds)
        except asyncio.CancelledError:
            return
        await self.on_timeout()

    async def _answer_clicked(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the member who started this game can answer it.",
                ephemeral=True,
            )
            return
        if self.has_used_attempt(interaction.user.id):
            await interaction.response.send_message(
                "You have already used your one attempt for this game.",
                ephemeral=True,
            )
            return
        if self.finished:
            await interaction.response.send_message(
                "This game has ended.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(GuessAnswerModal(self))

    def has_used_attempt(self, user_id: int) -> bool:
        return (self.game_id, int(user_id)) in self.attempted_user_game_ids

    def _finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.answer_button.disabled = True
        self.stop()
        self.cancel_deadline()
        if self.on_finished is not None:
            self.on_finished(self)

    async def submit_answer(
        self,
        interaction: discord.Interaction,
        answer: str,
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the member who started this game can answer it.",
                ephemeral=True,
            )
            return

        attempt_key = (self.game_id, int(interaction.user.id))
        async with self._lock:
            if self.finished:
                result = "finished"
                balance = None
            elif attempt_key in self.attempted_user_game_ids:
                result = "attempt_used"
                balance = None
            else:
                self.attempted_user_game_ids.add(attempt_key)
                self.answer_button.disabled = True
                if _normalize_answer(answer) != _normalize_answer(self.player.name):
                    result = "incorrect"
                    balance = None
                else:
                    try:
                        balance = self.database.add_coins(
                            self.owner_id,
                            MINIGAME_REWARD,
                        )
                    except Exception:
                        LOGGER.exception("Could not save a minigame reward.")
                        result = "reward_error"
                        balance = None
                    else:
                        self._finish()
                        result = "correct"

        if result == "finished":
            await interaction.response.send_message(
                "This game has already ended.",
                ephemeral=True,
            )
            return
        if result == "attempt_used":
            await interaction.response.send_message(
                "You have already used your one attempt for this game.",
                ephemeral=True,
            )
            return
        if result == "incorrect":
            await interaction.response.send_message(
                "That answer is incorrect. Your one attempt has been used.",
                ephemeral=True,
            )
            await self._disable_prompt_button()
            return
        if result == "reward_error":
            await interaction.response.send_message(
                "That answer is correct, but the reward could not be saved. "
                "Your one attempt has been used.",
                ephemeral=True,
            )
            await self._disable_prompt_button()
            return

        await interaction.response.send_message(
            f"Correct! You won **{format_coins(MINIGAME_REWARD)}**.",
            ephemeral=True,
        )
        await self._disable_prompt_button()
        await self._send_reveal(
            winner=interaction.user,
            balance=balance,
        )

    async def on_timeout(self) -> None:
        async with self._lock:
            if self.finished:
                return
            self._finish()

        await self._disable_prompt_button()
        await self._send_reveal()

    async def _disable_prompt_button(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            LOGGER.debug("Could not update the finished minigame prompt.")

    async def _send_reveal(
        self,
        *,
        winner: discord.User | discord.Member | None = None,
        balance: int | None = None,
    ) -> None:
        embed = player_embed(self.player)
        embed.title = f"🏒 The player was {player_card_label(self.player)}"
        if winner is None:
            embed.description = "Time is up. Your game has ended without a reward."
        else:
            mention = winner.mention
            embed.description = (
                f"{mention} (`{winner.name}`) guessed correctly and wins "
                f"**{format_coins(MINIGAME_REWARD)}**. "
                f"New balance: **{format_coins(int(balance or 0))}**."
            )

        image_path = resolve_card_image(self.player)
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

        await self.channel.send(
            embed=embed,
            file=upload,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class GuessAnswerModal(discord.ui.Modal, title="Answer"):
    def __init__(self, game: IndividualMinigameView) -> None:
        super().__init__(timeout=120)
        self.game = game
        self.player_name = discord.ui.TextInput(
            label="Player name",
            placeholder="Enter the player's name",
            max_length=100,
        )
        self.add_item(self.player_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.game.submit_answer(
            interaction,
            self.player_name.value,
        )


class MinigameCommands(discord_commands.Cog):
    def __init__(
        self,
        database: Database,
        *,
        duration_seconds: float = MINIGAME_DURATION_SECONDS,
    ) -> None:
        if duration_seconds <= 0:
            raise ValueError("The minigame duration must be positive.")
        self.database = database
        self.duration_seconds = float(duration_seconds)
        self._active_games: dict[tuple[int, str], IndividualMinigameView] = {}
        self._active_games_by_owner: dict[int, IndividualMinigameView] = {}

    def _register_game(self, game: IndividualMinigameView) -> None:
        game.on_finished = self._remove_game
        self._active_games[(game.owner_id, game.game_id)] = game
        self._active_games_by_owner[game.owner_id] = game

    def _remove_game(self, game: IndividualMinigameView) -> None:
        self._active_games.pop((game.owner_id, game.game_id), None)
        if self._active_games_by_owner.get(game.owner_id) is game:
            self._active_games_by_owner.pop(game.owner_id, None)

    @app_commands.command(
        name="minigame",
        description="Guess a hockey player from three clues.",
    )
    @app_commands.describe(
        answer="Enter your one guess for a game you started.",
    )
    async def minigame(
        self,
        interaction: discord.Interaction,
        answer: str | None = None,
    ) -> None:
        if answer is not None:
            game = self._active_games_by_owner.get(int(interaction.user.id))
            if game is None or game.finished:
                await interaction.response.send_message(
                    "You have no active minigame to answer. Run `/minigame` first.",
                    ephemeral=True,
                )
                return
            await game.submit_answer(interaction, answer)
            return

        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "The minigame can only be played in a server channel.",
                ephemeral=False,
            )
            return

        owner_id = int(interaction.user.id)
        active_game = self._active_games_by_owner.get(owner_id)
        if active_game is not None and not active_game.finished:
            await interaction.response.send_message(
                "You already have an active minigame. Use `/minigame answer` "
                "to submit your one guess.",
                ephemeral=True,
            )
            return
        if active_game is not None:
            self._remove_game(active_game)

        players = self.database.get_players()
        if not players:
            await interaction.response.send_message(
                "There are no player cards in the catalog, so a game cannot start.",
                ephemeral=False,
            )
            return

        player = random.choice(players)
        view = IndividualMinigameView(
            self.database,
            owner_id=interaction.user.id,
            player=player,
            channel=interaction.channel,
            duration_seconds=self.duration_seconds,
            game_id=(
                str(interaction.id)
                if getattr(interaction, "id", None) is not None
                else None
            ),
        )
        self._register_game(view)
        embed = discord.Embed(
            title="🏒 GUESS WHO?",
            description=(
                "Guess the player using the clues below. "
                "Only you can answer this game. "
                "You get one attempt. "
                f"You have {int(self.duration_seconds)} seconds. "
                "Submit it with `/minigame answer` or the Answer button."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="🌍 Nationality",
            value=player.country,
        )
        embed.add_field(
            name="⭐ Rating",
            value=f"{player.overall} OVR",
        )
        embed.add_field(
            name="🏒 Position",
            value=player.position,
        )

        try:
            await interaction.response.send_message(
                embed=embed,
                view=view,
                ephemeral=False,
            )
        except Exception:
            self._remove_game(view)
            view.stop()
            raise
        try:
            view.message = await interaction.original_response()
        finally:
            view.start_deadline()


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(MinigameCommands(bot.database))
