"""Public match invitations, live simulation, and one-time winner rewards."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from database import (
    MATCH_INVITATION_SECONDS,
    MATCH_REWARD,
    Database,
    InvalidAmount,
    InvalidMatchParticipant,
    MatchActionUnauthorized,
    MatchAlreadyActive,
    MatchNotFound,
    MatchNotPending,
)
from match_simulation import (
    MatchEvent,
    MatchResult,
    MatchSide,
    MatchStarter,
    MatchTick,
    play_match_clock,
    simulate_match,
)

LOGGER = logging.getLogger("thsl_guru.match")

SLOT_LABELS = {
    "WING1": "LW",
    "WING2": "RW",
    "CM1": "CM",
    "CM2": "CM",
    "GK": "GK",
}
EMBED_FIELD_LIMIT = 950


def _participant_name(user: discord.abc.User) -> str:
    return str(getattr(user, "display_name", None) or user.name)[:100]


def _side_from_match(match: dict[str, Any], user_id: str) -> MatchSide:
    lineups = match.get("lineups") or {}
    snapshot = lineups.get(user_id) or {}
    starters = tuple(
        MatchStarter(
            slot=str(starter["slot"]),
            name=str(starter["name"]),
            overall=int(starter["overall"]),
            position=str(starter["position"]),
        )
        for starter in snapshot.get("starters", [])
    )
    username = (
        match["challenger_name"]
        if user_id == str(match["challenger_id"])
        else match["opponent_name"]
    )
    return MatchSide(
        user_id=user_id,
        username=str(username),
        formation=str(snapshot.get("formation", "Balanced")),
        starters=starters,
    )


def _lineup_lines(side: MatchSide) -> list[str]:
    if not side.starters:
        return ["No valid starters saved."]
    return [
        f"**{SLOT_LABELS.get(player.slot, player.slot)}** — "
        f"{player.name} ({player.overall} OVR · {player.position})"
        for player in side.starters
    ]


def _add_chunked_fields(
    embed: discord.Embed,
    *,
    name: str,
    lines: list[str],
    inline: bool = False,
) -> None:
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > EMBED_FIELD_LIMIT and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    for index, chunk in enumerate(chunks, start=1):
        field_name = name if len(chunks) == 1 else f"{name} · {index}"
        embed.add_field(name=field_name, value=chunk, inline=inline)


def _invite_embed(match: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title="Match invitation",
        description=(
            f"<@{match['opponent_id']}>, "
            f"**{match['challenger_name']}** challenged you to a hockey match."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Respond within",
        value=f"{MATCH_INVITATION_SECONDS} seconds",
        inline=False,
    )
    return embed


def _event_line(event: MatchEvent | dict[str, Any]) -> str:
    minute = int(event.minute if isinstance(event, MatchEvent) else event["minute"])
    text = event.text if isinstance(event, MatchEvent) else str(event["text"])
    is_goal = (
        event.kind in {"goal", "own_goal", "sudden_death_goal"}
        if isinstance(event, MatchEvent)
        else str(event["kind"]) in {"goal", "own_goal", "sudden_death_goal"}
    )
    if not is_goal:
        return f"**{minute}'** {text}"
    home_score = (
        event.home_score if isinstance(event, MatchEvent) else int(event["home_score"])
    )
    away_score = (
        event.away_score if isinstance(event, MatchEvent) else int(event["away_score"])
    )
    return f"**{minute}'** {text} — score **{home_score}–{away_score}**"


def _live_embed(
    match: dict[str, Any],
    home: MatchSide,
    away: MatchSide,
    tick: MatchTick,
    events: list[MatchEvent],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Match in progress · {tick.minute}'",
        description=(
            f"<@{home.user_id}> **{home.username}** vs "
            f"<@{away.user_id}> **{away.username}**\n"
            f"Formation: {home.formation} vs {away.formation}"
        ),
        color=discord.Color.dark_blue(),
    )
    embed.add_field(
        name=f"{home.username} · Starting lineup",
        value="\n".join(_lineup_lines(home)),
        inline=True,
    )
    embed.add_field(
        name=f"{away.username} · Starting lineup",
        value="\n".join(_lineup_lines(away)),
        inline=True,
    )
    embed.add_field(
        name="Score",
        value=(
            f"**{home.username} {tick.home_score} – {tick.away_score} "
            f"{away.username}**"
        ),
        inline=False,
    )
    if events:
        _add_chunked_fields(
            embed,
            name="Match events",
            lines=[_event_line(event) for event in events],
        )
    else:
        embed.add_field(name="Match events", value="The opening faceoff.", inline=False)
    embed.set_footer(text="The 90-minute simulation runs for 45 seconds.")
    return embed


def _cancelled_embed(match: dict[str, Any], reason: str) -> discord.Embed:
    embed = discord.Embed(
        title="Match cancelled",
        description=reason,
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="Participants",
        value=(
            f"<@{match['challenger_id']}> **{match['challenger_name']}** vs "
            f"<@{match['opponent_id']}> **{match['opponent_name']}**"
        ),
        inline=False,
    )
    embed.set_footer(text="No match reward was awarded.")
    return embed


def _completed_embed(match: dict[str, Any]) -> discord.Embed:
    result = match.get("result") or {}
    home_id = str(match["challenger_id"])
    away_id = str(match["opponent_id"])
    home = _side_from_match(match, home_id)
    away = _side_from_match(match, away_id)
    winner_id = str(result.get("winner_id") or match.get("winner_id") or "")
    winner_name = (
        home.username if winner_id == home_id else away.username
    )
    home_score = int(result.get("home_score", 0))
    away_score = int(result.get("away_score", 0))
    duration = int(result.get("duration_seconds", 45))

    embed = discord.Embed(
        title=f"Final result · {home.username} vs {away.username}",
        description=(
            f"<@{home_id}> **{home.username}** vs "
            f"<@{away_id}> **{away.username}**"
        ),
        color=discord.Color.green(),
    )
    embed.add_field(
        name=f"{home.username} · {home.formation}",
        value="\n".join(_lineup_lines(home)),
        inline=True,
    )
    embed.add_field(
        name=f"{away.username} · {away.formation}",
        value="\n".join(_lineup_lines(away)),
        inline=True,
    )
    embed.add_field(
        name="Final score",
        value=f"**{home.username} {home_score} – {away_score} {away.username}**",
        inline=False,
    )
    goals = result.get("goals", [])
    if goals:
        _add_chunked_fields(
            embed,
            name="Goal scorers",
            lines=[_event_line(goal) for goal in goals],
        )
    else:
        embed.add_field(name="Goal scorers", value="No goals recorded.", inline=False)
    embed.add_field(
        name="Winner",
        value=f"<@{winner_id}> **{winner_name}**",
        inline=True,
    )
    embed.add_field(
        name="Match duration",
        value=f"{duration} seconds",
        inline=True,
    )
    embed.add_field(
        name="Reward",
        value=f"**{MATCH_REWARD:,} coins** awarded to {winner_name}.",
        inline=False,
    )
    return embed


class MatchInvitationView(discord.ui.View):
    """Persistent buttons tied to one challenged player and one invitation."""

    def __init__(
        self,
        command: "MatchCommands",
        match_id: str,
        opponent_id: str,
    ) -> None:
        super().__init__(timeout=None)
        self.command = command
        self.match_id = str(match_id)
        self.opponent_id = str(opponent_id)
        self._action_lock = asyncio.Lock()

        accept_button = discord.ui.Button(
            label="Accept Match",
            style=discord.ButtonStyle.success,
            custom_id=f"thsl:match:{self.match_id}:accept",
        )
        accept_button.callback = self.accept
        self.add_item(accept_button)

        decline_button = discord.ui.Button(
            label="Decline Match",
            style=discord.ButtonStyle.danger,
            custom_id=f"thsl:match:{self.match_id}:decline",
        )
        decline_button.callback = self.decline
        self.add_item(decline_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.opponent_id:
            await interaction.response.send_message(
                "Only the challenged player can respond to this invitation.",
                ephemeral=True,
            )
            return False
        return True

    async def accept(self, interaction: discord.Interaction) -> None:
        async with self._action_lock:
            try:
                match = self.command.database.accept_match_invitation(
                    self.match_id,
                    interaction.user.id,
                )
            except (MatchNotFound, MatchNotPending, MatchActionUnauthorized) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

            if match["status"] == "EXPIRED":
                embed = _cancelled_embed(
                    match,
                    "The invitation expired before it was accepted.",
                )
                await interaction.response.edit_message(embed=embed, view=None)
                self.command.cancel_invitation_expiry(self.match_id)
                self.stop()
                return
            if match["status"] == "CANCELLED":
                embed = _cancelled_embed(
                    match,
                    match.get("cancel_reason") or "A valid starting lineup is required.",
                )
                await interaction.response.edit_message(embed=embed, view=None)
                self.command.cancel_invitation_expiry(self.match_id)
                self.stop()
                return

            home = _side_from_match(match, str(match["challenger_id"]))
            away = _side_from_match(match, str(match["opponent_id"]))
            initial_tick = MatchTick(0, 1, 0, 0)
            await interaction.response.edit_message(
                embed=_live_embed(match, home, away, initial_tick, []),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.command.cancel_invitation_expiry(self.match_id)
            self.stop()
            message = getattr(interaction, "message", None)
            if message is not None:
                self.command.start_match_task(match, message)

    async def decline(self, interaction: discord.Interaction) -> None:
        async with self._action_lock:
            try:
                match = self.command.database.decline_match_invitation(
                    self.match_id,
                    interaction.user.id,
                )
            except (MatchNotFound, MatchNotPending, MatchActionUnauthorized) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

            self.command.cancel_invitation_expiry(self.match_id)
            self.stop()
            if match["status"] == "EXPIRED":
                embed = _cancelled_embed(
                    match,
                    "The invitation expired before it could be declined.",
                )
            else:
                embed = discord.Embed(
                    title="Match declined",
                    description=(
                        f"**{match['opponent_name']}** declined "
                        f"**{match['challenger_name']}**'s challenge."
                    ),
                    color=discord.Color.orange(),
                )
            await interaction.response.edit_message(embed=embed, view=None)


class MatchCommands(discord_commands.Cog):
    def __init__(
        self,
        database: Database,
        bot: discord_commands.Bot,
    ) -> None:
        self.database = database
        self.bot = bot
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}
        self._match_tasks: dict[str, asyncio.Task[None]] = {}
        self._startup_recovery_done = False

    async def cog_load(self) -> None:
        for match in self.database.get_pending_match_invitations():
            message_id = match.get("invite_message_id")
            if not message_id:
                self.database.cancel_match(
                    str(match["match_id"]),
                    "The invitation message was interrupted before it was saved.",
                )
                continue
            view = MatchInvitationView(
                self,
                str(match["match_id"]),
                str(match["opponent_id"]),
            )
            self.bot.add_view(view, message_id=int(message_id))
            self._schedule_invitation_expiry(match)

    async def cog_unload(self) -> None:
        for task in (*self._expiry_tasks.values(), *self._match_tasks.values()):
            task.cancel()
        self._expiry_tasks.clear()
        self._match_tasks.clear()

    @discord_commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._startup_recovery_done:
            return
        self._startup_recovery_done = True

        for match in self.database.cancel_interrupted_matches():
            await self._edit_match_message(
                match,
                _cancelled_embed(
                    match,
                    match.get("cancel_reason")
                    or "The bot restarted before this match completed.",
                ),
            )

        for match in self.database.get_pending_match_invitations():
            if float(match["expires_at"]) <= time.time():
                expired = self.database.expire_match_invitation(
                    str(match["match_id"]),
                )
                if expired is not None:
                    await self._edit_match_message(
                        expired,
                        _cancelled_embed(
                            expired,
                            "The invitation expired before it was answered.",
                        ),
                    )

        for match in self.database.get_unannounced_match_results():
            await self._publish_completed_result(match)

    @app_commands.command(
        name="match",
        description="Challenge another player to a 45-second hockey match.",
    )
    @app_commands.describe(opponent="The player you want to challenge")
    async def match(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
    ) -> None:
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "Matches can only be started in a server channel.",
                ephemeral=True,
            )
            return
        if opponent.bot:
            await interaction.response.send_message(
                "You can only challenge another player, not a bot.",
                ephemeral=True,
            )
            return
        if opponent.id == interaction.user.id:
            await interaction.response.send_message(
                "You cannot challenge yourself.",
                ephemeral=True,
            )
            return

        match_id = uuid.uuid4().hex
        try:
            match = self.database.create_match_invitation(
                match_id,
                interaction.user.id,
                opponent.id,
                _participant_name(interaction.user),
                _participant_name(opponent),
                interaction.guild.id,
                interaction.channel.id,
            )
        except InvalidMatchParticipant as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except MatchAlreadyActive:
            await interaction.response.send_message(
                "One of you already has a pending challenge or active match.",
                ephemeral=True,
            )
            return

        view = MatchInvitationView(self, match_id, str(opponent.id))
        try:
            await interaction.response.send_message(
                embed=_invite_embed(match),
                view=view,
                allowed_mentions=discord.AllowedMentions(
                    users=[interaction.user, opponent],
                    roles=False,
                    everyone=False,
                    replied_user=False,
                ),
            )
            message = await interaction.original_response()
            if not self.database.set_match_invitation_message(match_id, message.id):
                latest = self.database.get_match(match_id) or match
                if latest["status"] == "EXPIRED":
                    description = "The invitation expired before it could be answered."
                elif latest["status"] == "DECLINED":
                    description = f"**{latest['opponent_name']}** declined the match."
                else:
                    description = "This invitation is no longer available."
                await message.edit(
                    embed=_cancelled_embed(
                        latest,
                        description,
                    ),
                    view=None,
                )
                return
            latest = self.database.get_match(match_id) or match
            if latest["status"] == "PENDING":
                self.bot.add_view(view, message_id=message.id)
                self._schedule_invitation_expiry(latest)
        except Exception:
            self.database.cancel_match(
                match_id,
                "The invitation could not be delivered.",
            )
            raise

    def cancel_invitation_expiry(self, match_id: str) -> None:
        task = self._expiry_tasks.pop(str(match_id), None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _schedule_invitation_expiry(self, match: dict[str, Any]) -> None:
        match_id = str(match["match_id"])
        current = self._expiry_tasks.get(match_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._expire_invitation(match))
        self._expiry_tasks[match_id] = task

    async def _expire_invitation(self, match: dict[str, Any]) -> None:
        match_id = str(match["match_id"])
        try:
            await asyncio.sleep(max(0, float(match["expires_at"]) - time.time()))
            await self.bot.wait_until_ready()
            expired = self.database.expire_match_invitation(match_id)
            if expired is not None:
                await self._edit_match_message(
                    expired,
                    _cancelled_embed(
                        expired,
                        "No response arrived within 60 seconds.",
                    ),
                )
                self._stop_persistent_view(expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Failed to expire match invitation %s.", match_id)
        finally:
            self._expiry_tasks.pop(match_id, None)

    def _stop_persistent_view(self, match: dict[str, Any]) -> None:
        view = MatchInvitationView(
            self,
            str(match["match_id"]),
            str(match["opponent_id"]),
        )
        view.stop()

    def start_match_task(
        self,
        match: dict[str, Any],
        message: discord.Message,
    ) -> None:
        match_id = str(match["match_id"])
        task = asyncio.create_task(self._run_match(match, message))
        self._match_tasks[match_id] = task
        task.add_done_callback(
            lambda finished, identifier=match_id: self._match_tasks.pop(
                identifier,
                None,
            )
        )

    async def _run_match(
        self,
        match: dict[str, Any],
        message: discord.Message,
    ) -> None:
        match_id = str(match["match_id"])
        home = _side_from_match(match, str(match["challenger_id"]))
        away = _side_from_match(match, str(match["opponent_id"]))
        try:
            result = simulate_match(home, away)
            events: list[MatchEvent] = []
            tick_by_time = {
                tick.elapsed_seconds: tick for tick in result.ticks
            }

            async def update(elapsed: float, _minute: int) -> None:
                tick = tick_by_time[elapsed]
                if tick.event is not None:
                    events.append(tick.event)
                try:
                    await message.edit(
                        embed=_live_embed(match, home, away, tick, events),
                        view=None,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    LOGGER.warning(
                        "Could not update live match message %s at minute %d.",
                        match_id,
                        tick.minute,
                    )

            await play_match_clock(update)
            if not await self._participants_are_present(match):
                cancelled = self.database.cancel_match(
                    match_id,
                    "A participant left the server or could not be verified.",
                )
                if cancelled is not None:
                    await self._edit_match_message(
                        cancelled,
                        _cancelled_embed(
                            cancelled,
                            "A participant left the server or could not be "
                            "verified. The match was cancelled without a reward.",
                        ),
                        message=message,
                    )
                return

            result_data = self._result_data(result)
            try:
                awarded = self.database.complete_match(
                    match_id,
                    result.winner_id,
                    result_data,
                )
            except InvalidAmount:
                cancelled = self.database.cancel_match(
                    match_id,
                    "The winner's balance could not accept the reward.",
                )
                if cancelled is not None:
                    await self._edit_match_message(
                        cancelled,
                        _cancelled_embed(
                            cancelled,
                            "The winner's balance could not accept the reward. "
                            "The match was cancelled without awarding coins.",
                        ),
                        message=message,
                    )
                return

            if not awarded:
                current = self.database.get_match(match_id)
                if current and current["status"] == "COMPLETED":
                    await self._publish_completed_result(current, message=message)
                elif current:
                    await self._edit_match_message(
                        current,
                        _cancelled_embed(
                            current,
                            current.get("cancel_reason")
                            or "The match was cancelled before completion.",
                        ),
                        message=message,
                    )
                return

            completed = self.database.get_match(match_id)
            if completed is not None:
                await self._publish_completed_result(completed, message=message)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Match %s stopped before completion.", match_id)
            cancelled = self.database.cancel_match(
                match_id,
                "The match stopped unexpectedly before completion.",
            )
            if cancelled is not None:
                await self._edit_match_message(
                    cancelled,
                    _cancelled_embed(
                        cancelled,
                        "The match stopped unexpectedly and was cancelled "
                        "without a reward.",
                    ),
                    message=message,
                )

    @staticmethod
    def _result_data(result: MatchResult) -> dict[str, Any]:
        return {
            "home_score": result.home_score,
            "away_score": result.away_score,
            "winner_id": result.winner_id,
            "duration_seconds": result.duration_seconds,
            "goals": [
                {
                    "minute": goal.minute,
                    "kind": goal.kind,
                    "text": goal.text,
                    "team_id": goal.team_id,
                    "player_name": goal.player_name,
                    "player_team_id": goal.player_team_id,
                    "own_goal": goal.own_goal,
                    "home_score": goal.home_score,
                    "away_score": goal.away_score,
                }
                for goal in result.goals
            ],
        }

    async def _participants_are_present(self, match: dict[str, Any]) -> bool:
        try:
            guild_id = int(match["guild_id"])
            guild = self.bot.get_guild(guild_id) or await self.bot.fetch_guild(guild_id)
            await guild.fetch_member(int(match["challenger_id"]))
            await guild.fetch_member(int(match["opponent_id"]))
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException:
            LOGGER.warning(
                "Could not verify server membership for match %s.",
                match["match_id"],
            )
            return False

    async def _resolve_message(
        self,
        match: dict[str, Any],
    ) -> tuple[discord.abc.Messageable | None, discord.Message | None]:
        channel_id = match.get("channel_id")
        message_id = match.get("invite_message_id")
        if not channel_id or not message_id:
            return None, None
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            channel = await self.bot.fetch_channel(int(channel_id))
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return channel, None
        return channel, await fetch_message(int(message_id))

    async def _edit_match_message(
        self,
        match: dict[str, Any],
        embed: discord.Embed,
        *,
        message: discord.Message | None = None,
    ) -> bool:
        try:
            if message is None:
                _channel, message = await self._resolve_message(match)
            if message is None:
                return False
            await message.edit(
                embed=embed,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except discord.HTTPException:
            LOGGER.warning(
                "Could not update match message for %s.",
                match.get("match_id"),
                exc_info=True,
            )
            return False

    async def _publish_completed_result(
        self,
        match: dict[str, Any],
        *,
        message: discord.Message | None = None,
    ) -> None:
        if match.get("result") is None:
            return
        sent = await self._edit_match_message(
            match,
            _completed_embed(match),
            message=message,
        )
        if not sent:
            try:
                channel, _message = await self._resolve_message(match)
                if channel is not None:
                    await channel.send(
                        embed=_completed_embed(match),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    sent = True
            except discord.HTTPException:
                LOGGER.warning(
                    "Could not publish completed match result %s.",
                    match.get("match_id"),
                    exc_info=True,
                )
        if sent:
            self.database.mark_match_result_announced(str(match["match_id"]))


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(MatchCommands(bot.database, bot))
