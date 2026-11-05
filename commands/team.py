"""Public lineup and tactical-formation slash commands."""

from __future__ import annotations

import asyncio
import io
import math

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from database import (
    DEFAULT_FORMATION,
    LINEUP_POSITIONS,
    STARTING_LINEUP_POSITIONS,
    TEAM_FORMATIONS,
    CardNotOwned,
    Database,
    DuplicateLineupPlayer,
    InvalidFormation,
    InvalidLineupPosition,
    PlayerNotFound,
    player_card_label,
)
from commands.lineup_image import (
    OUTPUT_FILENAME,
    LineupImageError,
    render_lineup_image,
)

POSITION_LABELS = {
    "LW": "Left Wing",
    "RW": "Right Wing",
    "GK": "Goalie",
    "SUB1": "Substitute 1",
    "SUB2": "Substitute 2",
}

FORMATION_STYLES = {
    "Balanced": "Balanced tactical style with equal emphasis on offense and defense.",
    "Attack": "Offense-first style that emphasizes forward pressure from LW and RW.",
    "Defensive": "Defense-first style that prioritizes protecting the goal.",
}


class TeamCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(
        name="team",
        description="View your starting lineup, substitutes, and formation.",
    )
    async def team(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        lineup = self.database.get_lineup(user_id)
        formation = self.database.get_formation(user_id)
        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s THSL Starting Lineup",
            color=discord.Color.blurple(),
        )

        starters = [
            lineup[position]
            for position in STARTING_LINEUP_POSITIONS
            if position in lineup
        ]
        if starters:
            average = math.floor(
                sum(player.overall for player in starters) / len(starters) + 0.5
            )
            if len(starters) == len(STARTING_LINEUP_POSITIONS):
                overall_text = f"**{average} OVR**"
            else:
                overall_text = (
                    f"**{average} OVR average** · {len(starters)}/"
                    f"{len(STARTING_LINEUP_POSITIONS)} starters filled"
                )
        else:
            overall_text = "Not rated yet · no starting cards assigned"

        embed.add_field(name="Starting lineup OVR", value=overall_text, inline=False)
        embed.add_field(
            name="Formation",
            value=f"**{formation}**\n{FORMATION_STYLES[formation]}",
            inline=False,
        )

        for position in LINEUP_POSITIONS:
            player = lineup.get(position)
            if player is None:
                value = "Empty"
            else:
                value = (
                    f"**{player_card_label(player)}**"
                    f"\nCard position: {player.position}"
                )
            embed.add_field(
                name=f"{position} · {POSITION_LABELS[position]}",
                value=value,
                inline=position in STARTING_LINEUP_POSITIONS,
            )

        if len(lineup) < len(LINEUP_POSITIONS):
            embed.description = (
                "Fill the starting slots and substitutes with cards you own using "
                "`/set position:<LW|RW|GK|SUB1|SUB2> player:<card name>`. "
                "Use `player:CLEAR` to empty a slot."
            )
        await interaction.response.defer(thinking=True)
        try:
            image = await asyncio.to_thread(
                render_lineup_image,
                lineup,
                formation,
                overall_text,
            )
        except LineupImageError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        embed.set_image(url=f"attachment://{OUTPUT_FILENAME}")
        await interaction.followup.send(
            embed=embed,
            file=discord.File(io.BytesIO(image), filename=OUTPUT_FILENAME),
        )

    @app_commands.command(
        name="set",
        description="Assign one of your cards to a starting slot or substitute.",
    )
    @app_commands.describe(
        position="The lineup position to update",
        player="An owned card name, or CLEAR to empty the position",
    )
    @app_commands.choices(
        position=[
            app_commands.Choice(name="LW · Left Wing", value="LW"),
            app_commands.Choice(name="RW · Right Wing", value="RW"),
            app_commands.Choice(name="GK · Goalie", value="GK"),
            app_commands.Choice(name="SUB1 · Substitute 1", value="SUB1"),
            app_commands.Choice(name="SUB2 · Substitute 2", value="SUB2"),
        ]
    )
    async def set_position(
        self,
        interaction: discord.Interaction,
        position: app_commands.Choice[str],
        player: str,
    ) -> None:
        user_id = interaction.user.id
        if player.strip().casefold() == "clear":
            removed = self.database.clear_lineup_position(user_id, position.value)
            message = (
                f"Cleared **{position.value}**."
                if removed
                else f"**{position.value}** was already empty."
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        try:
            assigned = self.database.set_lineup_position(
                user_id,
                position.value,
                player,
            )
        except (
            CardNotOwned,
            DuplicateLineupPlayer,
            InvalidLineupPosition,
            PlayerNotFound,
        ) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"Assigned **{player_card_label(assigned)}** to **{position.value}** "
            f"({POSITION_LABELS[position.value]}).",
            ephemeral=True,
        )

    @app_commands.command(
        name="formation",
        description="Choose your team's tactical formation.",
    )
    @app_commands.choices(
        formation=[
            app_commands.Choice(name=name, value=name)
            for name in TEAM_FORMATIONS
        ]
    )
    async def formation(
        self,
        interaction: discord.Interaction,
        formation: app_commands.Choice[str],
    ) -> None:
        try:
            selected = self.database.set_formation(
                interaction.user.id,
                formation.value,
            )
        except InvalidFormation as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"Saved **{selected}** formation. Your lineup positions remain "
            f"{', '.join(LINEUP_POSITIONS)}.\n{FORMATION_STYLES[selected]}",
            ephemeral=True,
        )


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(TeamCommands(bot.database))
