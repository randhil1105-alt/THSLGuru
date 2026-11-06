"""Private, owner-controlled player draft selection and match flow."""

from __future__ import annotations

import asyncio
import io
import logging
import random
from collections.abc import Callable, Sequence
from typing import Any

import discord

from commands.common import format_coins, player_card_label, resolve_card_image
from database import (
    DRAFT_COOLDOWN_SECONDS,
    DRAFT_WIN_REWARD,
    Database,
    Player,
    format_card_type,
)
from draft_simulation import (
    DraftMatchResult,
    calculate_squad_average,
    generate_ai_squad,
    simulate_draft_match,
)

LOGGER = logging.getLogger("thsl_guru.minigame.draft")

DRAFT_GROUP_LABELS = (
    "Goalkeeper (GK)",
    "Center Midfielder (CM)",
    "Winger (WI) · Group 1",
    "Winger (WI) · Group 2",
)


class DraftSessionView(discord.ui.View):
    """One private, four-pick Draft with a single atomic reward settlement."""

    def __init__(
        self,
        database: Database,
        *,
        owner_id: int,
        username: str,
        game_id: str,
        groups: Sequence[Sequence[Player]],
        catalog: Sequence[Player],
        on_finished: Callable[[DraftSessionView], None] | None = None,
        rng: Any | None = None,
    ) -> None:
        super().__init__(timeout=DRAFT_COOLDOWN_SECONDS)
        self.database = database
        self.owner_id = int(owner_id)
        self.username = str(username)[:100]
        self.game_id = str(game_id)
        self.groups = tuple(tuple(group) for group in groups)
        self.catalog = tuple(catalog)
        self.on_finished = on_finished
        self.rng = rng or random.SystemRandom()
        self.selected: list[Player] = []
        self.selected_ids: set[int] = set()
        self.stage_index = 0
        self.finished = False
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._add_pick_buttons()

    def _add_pick_buttons(self) -> None:
        self.clear_items()
        if self.stage_index >= len(self.groups):
            return
        for player in self.groups[self.stage_index]:
            button = discord.ui.Button(
                label=f"{player.name} · {player.overall} OVR"[:80],
                style=discord.ButtonStyle.primary,
                custom_id=(
                    f"thsl:draft:{self.game_id}:"
                    f"{self.stage_index}:{player.id}"
                ),
            )

            async def callback(
                interaction: discord.Interaction,
                selected_player: Player = player,
            ) -> None:
                await self.choose(interaction, selected_player)

            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.owner_id:
            await interaction.response.send_message(
                "Only the player who started this Draft can make selections.",
                ephemeral=True,
            )
            return False
        return True

    def _option_embeds_and_files(
        self,
    ) -> tuple[list[discord.Embed], list[discord.File]]:
        group = self.groups[self.stage_index]
        heading = discord.Embed(
            title=f"Draft · Choose {DRAFT_GROUP_LABELS[self.stage_index]}",
            description=(
                f"Pick one of these three cards. "
                f"Selection {self.stage_index + 1} of {len(self.groups)}."
            ),
            color=discord.Color.blurple(),
        )
        embeds = [heading]
        files: list[discord.File] = []
        for player in group:
            embed = discord.Embed(
                title=player_card_label(player),
                color=discord.Color.gold()
                if player.rarity.upper() == "GOLD"
                else discord.Color.dark_purple(),
            )
            embed.add_field(name="Overall rating", value=f"{player.overall} OVR")
            embed.add_field(name="Position", value=player.position)
            embed.add_field(name="Card type", value=format_card_type(player.rarity))
            image_path = resolve_card_image(player)
            if image_path is not None:
                try:
                    image_bytes = image_path.read_bytes()
                except OSError:
                    image_path = None
                else:
                    filename = (
                        f"draft-{self.game_id}-{self.stage_index}-{player.id}"
                        f"{image_path.suffix.lower()}"
                    )
                    files.append(
                        discord.File(
                            io.BytesIO(image_bytes),
                            filename=filename,
                        )
                    )
                    embed.set_image(url=f"attachment://{filename}")
            if image_path is None:
                embed.add_field(
                    name="Card artwork",
                    value="Artwork is unavailable for this card.",
                    inline=False,
                )
            embeds.append(embed)
        return embeds, files

    def _result_embed(
        self,
        *,
        user_squad: Sequence[Player],
        ai_squad: Sequence[Player],
        user_average: int,
        ai_average: int,
        ai_team_name: str,
        match: DraftMatchResult,
        new_balance: int,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Draft match result",
            color=discord.Color.green() if match.player_won else discord.Color.red(),
        )
        embed.add_field(
            name=f"Your Draft Squad · {user_average} OVR average",
            value="\n".join(
                f"• {player.name} — {player.overall} OVR "
                f"({format_card_type(player.rarity)}) · {player.position}"
                for player in user_squad
            ),
            inline=False,
        )
        embed.add_field(
            name=f"{ai_team_name} · AI · {ai_average} OVR average",
            value="\n".join(
                f"• {player.name} — {player.overall} OVR "
                f"({format_card_type(player.rarity)}) · {player.position}"
                for player in ai_squad
            ),
            inline=False,
        )
        embed.add_field(
            name="Final score",
            value=(
                f"**{self.username} {match.player_score} — "
                f"{match.ai_score} {ai_team_name}**"
            ),
            inline=False,
        )
        if match.player_won:
            embed.description = "You won the Draft match!"
            embed.add_field(
                name="Reward",
                value=(
                    f"💰 **{format_coins(DRAFT_WIN_REWARD)}**\n"
                    f"💼 New balance: **{format_coins(new_balance)}**"
                ),
                inline=False,
            )
        else:
            embed.description = (
                "You've been knocked out! Better luck next time.\n\n"
                "Come back in 10 minutes to try again."
            )
            embed.add_field(
                name="Reward",
                value="No coins were awarded.",
                inline=False,
            )
        return embed

    def _result_artwork(
        self,
        players: Sequence[Player],
    ) -> tuple[list[discord.Embed], list[discord.File]]:
        embeds: list[discord.Embed] = []
        files: list[discord.File] = []
        for player in players:
            image_path = resolve_card_image(player)
            if image_path is None:
                continue
            filename = f"draft-result-{self.game_id}-{player.id}{image_path.suffix.lower()}"
            try:
                image_bytes = image_path.read_bytes()
            except OSError:
                continue
            card_embed = discord.Embed(
                title=player_card_label(player),
                description=f"{player.overall} OVR · {format_card_type(player.rarity)}",
            )
            card_embed.set_image(url=f"attachment://{filename}")
            files.append(
                discord.File(
                    io.BytesIO(image_bytes),
                    filename=filename,
                )
            )
            embeds.append(card_embed)
        return embeds, files

    async def choose(
        self,
        interaction: discord.Interaction,
        player: Player,
    ) -> None:
        if int(interaction.user.id) != self.owner_id:
            await interaction.response.send_message(
                "Only the player who started this Draft can make selections.",
                ephemeral=True,
            )
            return

        async with self._lock:
            if self.finished:
                await interaction.response.send_message(
                    "This Draft has already ended.",
                    ephemeral=True,
                )
                return
            if self.stage_index >= len(self.groups):
                await interaction.response.send_message(
                    "All Draft selections are complete.",
                    ephemeral=True,
                )
                return
            if player.id in self.selected_ids:
                await interaction.response.send_message(
                    "You cannot select the same card twice.",
                    ephemeral=True,
                )
                return
            if player.id not in {option.id for option in self.groups[self.stage_index]}:
                await interaction.response.send_message(
                    "That card is not available for this Draft position.",
                    ephemeral=True,
                )
                return

            self.selected.append(player)
            self.selected_ids.add(player.id)
            self.stage_index += 1
            if self.stage_index < len(self.groups):
                self._add_pick_buttons()
                embeds, files = self._option_embeds_and_files()
                await interaction.response.edit_message(
                    content=None,
                    embeds=embeds,
                    attachments=files,
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            await self._finish_draft(interaction)

    async def _finish_draft(self, interaction: discord.Interaction) -> None:
        self.finished = True
        user_squad = tuple(self.selected)
        try:
            ai_squad = generate_ai_squad(
                self.catalog,
                excluded_player_ids=self.selected_ids,
                rng=self.rng,
            )
            user_average = calculate_squad_average(user_squad)
            ai_average = calculate_squad_average(ai_squad)
            country_names = [player.country for player in ai_squad if player.country]
            ai_team_name = self.rng.choice(country_names) if country_names else "AI Team"
            match = simulate_draft_match(user_squad, ai_squad, rng=self.rng)
            result_data: dict[str, Any] = {
                "user_squad": [
                    {
                        "id": player.id,
                        "name": player.name,
                        "overall": player.overall,
                        "position": player.position,
                        "rarity": player.rarity,
                    }
                    for player in user_squad
                ],
                "ai_squad": [
                    {
                        "id": player.id,
                        "name": player.name,
                        "overall": player.overall,
                        "position": player.position,
                        "rarity": player.rarity,
                    }
                    for player in ai_squad
                ],
                "user_average": user_average,
                "ai_average": ai_average,
                "ai_team_name": ai_team_name,
                "player_score": match.player_score,
                "ai_score": match.ai_score,
                "player_win_probability": match.player_win_probability,
                "player_won": match.player_won,
            }
            completed, balance = self.database.finish_draft_session(
                self.game_id,
                self.owner_id,
                won=match.player_won,
                result=result_data,
            )
            if not completed:
                raise RuntimeError("This Draft was already closed.")
        except Exception:
            LOGGER.exception("Draft session %s could not be completed.", self.game_id)
            self.database.cancel_draft_session(self.game_id, self.owner_id)
            self.finished = True
            self.stop()
            if self.on_finished is not None:
                self.on_finished(self)
            await interaction.response.edit_message(
                content="The Draft could not be completed. No reward was awarded.",
                embeds=[],
                attachments=[],
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        result_embed = self._result_embed(
            user_squad=user_squad,
            ai_squad=ai_squad,
            user_average=user_average,
            ai_average=ai_average,
            ai_team_name=ai_team_name,
            match=match,
            new_balance=balance,
        )
        artwork_embeds, artwork_files = self._result_artwork(user_squad)
        self.stop()
        if self.on_finished is not None:
            self.on_finished(self)
        await interaction.response.edit_message(
            content=None,
            embeds=[result_embed, *artwork_embeds],
            attachments=artwork_files,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.database.cancel_draft_session(self.game_id, self.owner_id)
        self.stop()
        if self.on_finished is not None:
            self.on_finished(self)
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Your Draft expired. No reward was awarded.",
                    embeds=[],
                    attachments=[],
                    view=None,
                )
            except discord.HTTPException:
                LOGGER.debug("Could not update an expired Draft prompt.")

