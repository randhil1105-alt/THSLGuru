"""Persistent single-player knockout tournament command."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from database import (
    TOURNAMENT_COMPLETION_BONUS,
    TOURNAMENT_ROUND_REWARD,
    TOURNAMENT_STAGE_LABELS,
    Database,
    TournamentError,
    TournamentInvalidLineup,
    TournamentNotActive,
)
from tournament_simulation import (
    TournamentAIUnavailable,
    generate_ai_lineup,
    serialize_tournament_result,
    tournament_match_side,
    tournament_scoring_probability,
    tournament_team_rating,
)
from match_simulation import simulate_match

LOGGER = logging.getLogger("thsl_guru.tournament")

SLOT_LABELS = {
    "WING1": "Wing 1",
    "WING2": "Wing 2",
    "CM1": "Center 1",
    "CM2": "Center 2",
    "GK": "Goalie",
}
VIEW_TIMEOUT_SECONDS = 300


def _lineup_lines(lineup: Sequence[dict[str, Any]]) -> str:
    if not lineup:
        return "No valid lineup saved."
    return "\n".join(
        f"**{SLOT_LABELS.get(str(starter['slot']), str(starter['slot']))}** — "
        f"{starter['name']} ({starter['overall']} OVR · {starter['position']})"
        for starter in lineup
    )


def _tournament_embed(
    tournament: dict[str, Any],
    *,
    upcoming_lineup: Sequence[dict[str, Any]] = (),
    note: str | None = None,
) -> discord.Embed:
    rounds = list(tournament.get("rounds", []))
    status = str(tournament["status"])
    current_round = int(tournament["current_round"])
    latest = rounds[-1] if rounds else None

    if latest is not None:
        stage = str(latest["stage"])
        title = f"THSL Tournament · {stage}"
        result = latest["result"]
        score = f"{int(result['home_score'])}–{int(result['away_score'])}"
        description = (
            f"**Victory** · {score}"
            if latest["won"]
            else f"**Eliminated** · {score}"
        )
        player_lineup = latest["player_lineup"]
        opponent_lineup = latest["opponent_lineup"]
        player_rating = int(latest["player_rating"])
        opponent_rating = int(latest["opponent_rating"])
    else:
        stage = TOURNAMENT_STAGE_LABELS[current_round]
        title = f"THSL Tournament · {stage}"
        description = "Your tournament is ready. Play the next round to see the AI team."
        player_lineup = list(upcoming_lineup)
        opponent_lineup = []
        player_rating = tournament_team_rating(player_lineup)
        opponent_rating = 0

    if status == "ACTIVE":
        next_stage = TOURNAMENT_STAGE_LABELS[current_round]
        description += f"\n**Next round:** {next_stage}"
    elif status == "COMPLETED":
        description += "\n**Tournament complete.**"
    else:
        description += "\n**Tournament ended.**"
    if note:
        description += f"\n\n{note}"

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.gold() if status == "COMPLETED" else discord.Color.blurple(),
    )
    if player_lineup:
        embed.add_field(
            name=f"Your team · {player_rating} OVR",
            value=_lineup_lines(player_lineup),
            inline=True,
        )
    else:
        embed.add_field(
            name="Your team",
            value="Use `/set` to fill your current formation before playing.",
            inline=True,
        )

    if opponent_lineup:
        embed.add_field(
            name=f"Opponent · {opponent_rating} OVR",
            value=_lineup_lines(opponent_lineup),
            inline=True,
        )
    else:
        embed.add_field(
            name="Opponent",
            value="A position-balanced AI team is generated for each round.",
            inline=True,
        )

    progress_lines: list[str] = []
    rounds_by_number = {int(item["round_number"]): item for item in rounds}
    for number, stage_name in TOURNAMENT_STAGE_LABELS.items():
        record = rounds_by_number.get(number)
        if record:
            mark = "✅" if record["won"] else "❌"
            progress_lines.append(f"{mark} {stage_name}")
        elif status == "ACTIVE" and number == current_round:
            progress_lines.append(f"▶ {stage_name}")
        else:
            progress_lines.append(f"🔒 {stage_name}")
    embed.add_field(
        name="Tournament progress",
        value=" → ".join(progress_lines),
        inline=False,
    )
    if rounds:
        result_lines = []
        for item in rounds:
            saved_result = item["result"]
            saved_score = (
                f"{int(saved_result['home_score'])}–{int(saved_result['away_score'])}"
            )
            marker = "✅" if item["won"] else "❌"
            earned = int(item["round_reward"]) + int(item["completion_bonus"])
            result_lines.append(
                f"{marker} **{item['stage']}** · {saved_score} · "
                f"{earned:,} coins"
            )
        embed.add_field(
            name="Round results",
            value="\n".join(result_lines),
            inline=False,
        )

    total_earned = sum(
        int(item["round_reward"]) + int(item["completion_bonus"])
        for item in rounds
    )
    last_payout = (
        int(latest["round_reward"]) + int(latest["completion_bonus"])
        if latest
        else 0
    )
    payout_text = (
        f"Last round: **{last_payout:,} coins**\n"
        f"Earned this tournament: **{total_earned:,} coins**"
        if latest
        else f"Win a round to earn **{TOURNAMENT_ROUND_REWARD:,} coins**. "
        f"Complete the tournament for an additional "
        f"**{TOURNAMENT_COMPLETION_BONUS:,} coins**."
    )
    embed.add_field(name="Coins earned", value=payout_text, inline=False)
    return embed


class TournamentView(discord.ui.View):
    def __init__(
        self,
        cog: "TournamentCommands",
        user_id: int | str,
        tournament: dict[str, Any],
        *,
        can_play: bool = True,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.cog = cog
        self.user_id = str(user_id)
        self.tournament_id = str(tournament["tournament_id"])
        self.action = "play" if tournament["status"] == "ACTIVE" else "start"
        self.round_number = int(tournament["current_round"])
        self.expired = False
        self.message: discord.Message | None = None
        self._action_lock = asyncio.Lock()

        if self.action == "play" and can_play:
            label = f"Play {TOURNAMENT_STAGE_LABELS[self.round_number]}"
            style = discord.ButtonStyle.primary
        elif self.action == "start":
            label = "Start a New Tournament"
            style = discord.ButtonStyle.success
        else:
            return

        button = discord.ui.Button(label=label, style=style)
        button.callback = self._button_callback
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.expired:
            await interaction.response.send_message(
                "These tournament buttons expired. Run `/tournament` to reopen your progress.",
                ephemeral=True,
            )
            return False
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "Only the player who opened this tournament can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    async def _button_callback(self, interaction: discord.Interaction) -> None:
        async with self._action_lock:
            if self.expired:
                await interaction.response.send_message(
                    "These tournament buttons expired. Run `/tournament` to reopen your progress.",
                    ephemeral=True,
                )
                return
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                LOGGER.info("A tournament button interaction expired before processing.")
                return
            if self.action == "start":
                await self.cog._start_from_button(interaction)
            else:
                await self.cog._play_from_button(
                    interaction,
                    self.tournament_id,
                    self.round_number,
                )

    async def on_timeout(self) -> None:
        self.expired = True
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.debug("Could not disable an expired tournament view.")

    def bind_message(self, message: discord.Message | None) -> None:
        self.message = message


class TournamentCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(
        name="tournament",
        description="Play or view your three-round knockout tournament.",
    )
    async def tournament(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            LOGGER.info("A tournament command interaction expired before processing.")
            return
        user_id = interaction.user.id
        tournament = self.database.get_tournament_for_user(user_id)
        if tournament is None:
            try:
                tournament = self.database.start_tournament(user_id)
                tournament = await self._play_current_round(
                    user_id,
                    str(interaction.user.display_name),
                    tournament,
                    expected_round=1,
                )
            except (TournamentError, TournamentAIUnavailable) as exc:
                await self._send_action_error(interaction, str(exc))
                return
            except Exception:
                LOGGER.exception("Could not begin a tournament for user %s.", user_id)
                await self._send_action_error(
                    interaction,
                    "The tournament could not start. No round reward was issued. "
                    "Run `/tournament` again to resume."
                )
                return

        upcoming_lineup: Sequence[dict[str, Any]] = ()
        can_play = True
        if tournament["status"] == "ACTIVE":
            try:
                context = self.database.get_tournament_match_context(
                    user_id,
                    str(tournament["tournament_id"]),
                )
                upcoming_lineup = context["player_lineup"]
            except TournamentInvalidLineup:
                can_play = False
        await self._send_tournament(
            interaction,
            tournament,
            upcoming_lineup=upcoming_lineup,
            can_play=can_play,
        )

    async def _play_current_round(
        self,
        user_id: int | str,
        username: str,
        tournament: dict[str, Any],
        *,
        expected_round: int,
    ) -> dict[str, Any]:
        if tournament["status"] != "ACTIVE":
            return tournament
        current_round = int(tournament["current_round"])
        if current_round != expected_round:
            return tournament
        if any(
            int(item["round_number"]) == current_round
            for item in tournament.get("rounds", [])
        ):
            return tournament

        context = self.database.get_tournament_match_context(
            user_id,
            str(tournament["tournament_id"]),
        )
        tournament = context["tournament"]
        current_round = int(tournament["current_round"])
        if current_round != expected_round:
            return tournament
        player_lineup = context["player_lineup"]
        opponent_lineup = generate_ai_lineup(
            context["catalog"],
            str(context["formation"]),
            current_round,
        )
        player_rating = tournament_team_rating(player_lineup)
        opponent_rating = tournament_team_rating(opponent_lineup)
        home = tournament_match_side(
            user_id="player",
            username=username,
            formation=str(context["formation"]),
            lineup=player_lineup,
        )
        away = tournament_match_side(
            user_id="ai",
            username="Tournament AI",
            formation=str(context["formation"]),
            lineup=opponent_lineup,
        )
        result = simulate_match(
            home,
            away,
            home_scoring_probability=tournament_scoring_probability(
                current_round,
                player_rating,
                opponent_rating,
            ),
        )
        return self.database.complete_tournament_round(
            user_id,
            str(tournament["tournament_id"]),
            current_round,
            player_lineup=player_lineup,
            opponent_lineup=opponent_lineup,
            result=serialize_tournament_result(result),
        )

    async def _start_from_button(self, interaction: discord.Interaction) -> None:
        try:
            tournament = self.database.start_tournament(interaction.user.id)
            tournament = await self._play_current_round(
                interaction.user.id,
                str(interaction.user.display_name),
                tournament,
                expected_round=int(tournament["current_round"]),
            )
            await self._edit_tournament(interaction, tournament)
        except (TournamentError, TournamentAIUnavailable) as exc:
            await self._send_action_error(interaction, str(exc))
        except Exception:
            LOGGER.exception(
                "Could not start a tournament from a button for user %s.",
                interaction.user.id,
            )
            await self._send_action_error(
                interaction,
                "The tournament could not start. No duplicate reward was issued. "
                "Run `/tournament` to reopen your progress."
            )

    async def _play_from_button(
        self,
        interaction: discord.Interaction,
        tournament_id: str,
        expected_round: int,
    ) -> None:
        try:
            tournament = self.database.get_tournament_for_user(interaction.user.id)
            if tournament is None or tournament["tournament_id"] != tournament_id:
                raise TournamentNotActive("Run `/tournament` to open your current tournament.")
            tournament = await self._play_current_round(
                interaction.user.id,
                str(interaction.user.display_name),
                tournament,
                expected_round=expected_round,
            )
            await self._edit_tournament(interaction, tournament)
        except (TournamentError, TournamentAIUnavailable) as exc:
            await self._send_action_error(interaction, str(exc))
        except Exception:
            LOGGER.exception(
                "Could not play a tournament round for user %s.",
                interaction.user.id,
            )
            await self._send_action_error(
                interaction,
                "The round could not be shown. Any saved result remains available "
                "with `/tournament`."
            )

    @staticmethod
    async def _send_action_error(
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.InteractionResponded, discord.HTTPException):
            LOGGER.info("A tournament response expired before it could be updated.")

    async def _send_tournament(
        self,
        interaction: discord.Interaction,
        tournament: dict[str, Any],
        *,
        upcoming_lineup: Sequence[dict[str, Any]] = (),
        can_play: bool = True,
    ) -> None:
        view = TournamentView(
            self,
            interaction.user.id,
            tournament,
            can_play=can_play,
        )
        embed = _tournament_embed(
            tournament,
            upcoming_lineup=upcoming_lineup,
            note=(
                "Finish your owned starting lineup with `/set` before continuing."
                if not can_play
                else None
            ),
        )
        try:
            await interaction.edit_original_response(embed=embed, view=view)
            view.bind_message(await interaction.original_response())
        except AttributeError:
            await interaction.response.send_message(
                embed=embed,
                view=view,
                ephemeral=True,
            )
        except discord.HTTPException:
            LOGGER.info(
                "Could not show tournament %s; its result remains saved.",
                tournament["tournament_id"],
            )
            await self._send_action_error(
                interaction,
                "Your tournament progress is saved. Run `/tournament` to reopen it.",
            )

    async def _edit_tournament(
        self,
        interaction: discord.Interaction,
        tournament: dict[str, Any],
    ) -> None:
        upcoming_lineup: Sequence[dict[str, Any]] = ()
        can_play = True
        if tournament["status"] == "ACTIVE":
            try:
                context = self.database.get_tournament_match_context(
                    interaction.user.id,
                    str(tournament["tournament_id"]),
                )
                upcoming_lineup = context["player_lineup"]
            except TournamentInvalidLineup:
                can_play = False
        view = TournamentView(
            self,
            interaction.user.id,
            tournament,
            can_play=can_play,
        )
        view.bind_message(getattr(interaction, "message", None))
        embed = _tournament_embed(
                tournament,
                upcoming_lineup=upcoming_lineup,
                note=(
                    "Finish your owned starting lineup with `/set` before continuing."
                    if not can_play
                    else None
                ),
            )
        try:
            await interaction.edit_original_response(embed=embed, view=view)
        except AttributeError:
            await interaction.response.edit_message(embed=embed, view=view)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(TournamentCommands(bot.database))
