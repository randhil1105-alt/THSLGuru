"""Public lineup and tactical-formation slash commands."""

from __future__ import annotations

import asyncio
import io
import math

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from database import (
    FORMATION_STARTERS,
    SUBSTITUTE_POSITIONS,
    TEAM_FORMATIONS,
    CardNotOwned,
    Database,
    DuplicateLineupPlayer,
    InvalidFormation,
    InvalidLineupPosition,
    PlayerNotFound,
    Player,
    player_card_label,
)
from commands.common import format_coins
from commands.lineup_image import (
    OUTPUT_FILENAME,
    LineupImageError,
    render_lineup_image,
)
from commands.xp import mark_successful_command

POSITION_LABELS = {
    "WING1": "Wing 1",
    "WING2": "Wing 2",
    "CM1": "CM 1",
    "CM2": "CM 2",
    "GK": "Goalie",
    "SUB1": "Substitute 1",
    "SUB2": "Substitute 2",
}

FORMATION_STYLES = {
    "Balanced": "1 Wing · 1 CM · 1 GK",
    "Offensive": "2 Wings · 1 GK",
    "Defensive": "2 CMs · 1 GK",
}


def lineup_card_label(player: Player) -> str:
    """Show a catalog card's display name with its exact OVR and rarity."""
    return f"{player_card_label(player)} — {player.overall} OVR"


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
        active_positions = FORMATION_STARTERS[formation]
        team_positions = active_positions + SUBSTITUTE_POSITIONS
        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s THSL STARTING LINEUP",
            color=discord.Color.blurple(),
        )

        starters = [
            lineup[position]
            for position in active_positions
            if position in lineup
        ]
        if starters:
            average = math.floor(
                sum(player.overall for player in starters) / len(starters) + 0.5
            )
            if len(starters) == len(active_positions):
                overall_text = f"**{average} OVR**"
            else:
                overall_text = (
                    f"**{average} OVR average** · {len(starters)}/"
                    f"{len(active_positions)} starters filled"
                )
        else:
            overall_text = "Not rated yet · 0/3 starters filled"

        embed.add_field(name="Starting lineup OVR", value=overall_text, inline=False)
        total_value = sum(player.value for player in lineup.values())
        value_text = format_coins(total_value)
        embed.add_field(
            name="Team value",
            value=f"**{value_text}** · 3 starter slots and 2 substitute slots",
            inline=False,
        )
        embed.add_field(
            name="Formation",
            value=(
                f"**{formation}** · {FORMATION_STYLES[formation]}\n"
                "Each formation has exactly three starting slots."
            ),
            inline=False,
        )

        for position in team_positions:
            player = lineup.get(position)
            if player is None:
                value = "Empty"
            else:
                value = (
                    f"**{lineup_card_label(player)}**"
                    f"\nCard position: {player.position}"
                    f"\nValue: {format_coins(player.value)}"
                )
            embed.add_field(
                name=f"{position} · {POSITION_LABELS[position]}",
                value=value,
                inline=position in active_positions,
            )

        description_parts: list[str] = []
        if len(lineup) < len(team_positions):
            description_parts.append(
                "Fill the starting slots and substitutes with cards you own using "
                "`/set position:<active slot> player:<owned card>`. "
                "Use `player:CLEAR` to empty a slot. The team value sums the "
                "listed values of all assigned starters and substitutes."
            )
        notices = self.database.get_lineup_migration_notices(user_id)
        if notices:
            description_parts.append("Saved lineup adjustments:\n" + "\n".join(notices))
        embed.description = "\n\n".join(description_parts) or None
        await interaction.response.defer(thinking=True)
        try:
            image = await asyncio.to_thread(
                render_lineup_image,
                lineup,
                formation,
                overall_text,
                value_text,
            )
        except LineupImageError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        embed.set_image(url=f"attachment://{OUTPUT_FILENAME}")
        await interaction.followup.send(
            embed=embed,
            file=discord.File(io.BytesIO(image), filename=OUTPUT_FILENAME),
        )
        mark_successful_command(interaction)

    @app_commands.command(
        name="set",
        description="Assign one of your cards to a starting slot or substitute.",
    )
    @app_commands.describe(
        position="The lineup position to update",
        player="Choose an owned card version, or CLEAR to empty the position",
    )
    @app_commands.choices(
        position=[
            app_commands.Choice(name="Wing 1", value="WING1"),
            app_commands.Choice(name="Wing 2", value="WING2"),
            app_commands.Choice(name="CM 1", value="CM1"),
            app_commands.Choice(name="CM 2", value="CM2"),
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
            mark_successful_command(interaction)
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
            f"Assigned **{lineup_card_label(assigned)}** to **{position.value}** "
            f"({POSITION_LABELS[position.value]}).",
            ephemeral=True,
        )
        mark_successful_command(interaction)

    @set_position.autocomplete("player")
    async def set_player_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        query = current.strip().casefold()
        selected_position = getattr(
            getattr(interaction, "namespace", None),
            "position",
            None,
        )
        selected_position = getattr(selected_position, "value", selected_position)
        selected_position = (
            str(selected_position).strip().upper()
            if selected_position is not None
            else None
        )
        choices: list[app_commands.Choice[str]] = []
        if not query or "clear".startswith(query) or query in "clear":
            choices.append(
                app_commands.Choice(name="CLEAR · Empty this slot", value="CLEAR")
            )
        for entry in self.database.get_inventory(interaction.user.id):
            player = entry.player
            if (
                selected_position
                and player.allowed_lineup_positions is not None
                and selected_position not in player.allowed_lineup_positions
            ):
                continue
            label = lineup_card_label(player)
            if entry.quantity > 1:
                label += f" ×{entry.quantity}"
            if query and query not in label.casefold():
                continue
            choices.append(
                app_commands.Choice(name=label[:100], value=str(player.id))
            )
            if len(choices) == 25:
                break
        return choices

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
            change = self.database.set_formation_with_report(
                interaction.user.id,
                formation.value,
            )
        except InvalidFormation as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        active_positions = FORMATION_STARTERS[change.formation]
        message = (
            f"Saved **{change.formation}** ({FORMATION_STYLES[change.formation]}). "
            "Active starter slots: "
            f"{' · '.join(POSITION_LABELS[position] for position in active_positions)}."
        )
        if change.moved:
            moved = ", ".join(
                f"{player_card_label(player)} moved from {old} to {new}"
                for old, new, player in change.moved
            )
            message += f"\nCompatible cards kept in matching roles: {moved}."
        if change.unassigned:
            unassigned = ", ".join(
                f"{position} — {player_card_label(player)}"
                for position, player in change.unassigned
            )
            message += (
                f"\nLeft empty because this formation has no compatible starter "
                f"slot: {unassigned}. Those cards remain in your inventory and "
                "were not moved to another role."
            )
        notices = self.database.get_lineup_migration_notices(interaction.user.id)
        if notices:
            message += "\nSaved lineup adjustments: " + " ".join(notices)
        await interaction.response.send_message(message, ephemeral=True)
        mark_successful_command(interaction)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(TeamCommands(bot.database))
