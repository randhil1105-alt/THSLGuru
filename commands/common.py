"""Shared formatting and card-image helpers for command extensions."""

from __future__ import annotations

from pathlib import Path

import discord

from database import (
    InventoryEntry,
    Player,
    format_card_type,
    player_card_label,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CARDS_DIR = (PROJECT_ROOT / "cards").resolve()

RARITY_COLORS = {
    "GOLD": discord.Color.gold(),
    "TOTW": discord.Color.dark_purple(),
    "S2 TOTS": discord.Color.blue(),
    "S1 TOTS": discord.Color.blue(),
    "S1 TOTY": discord.Color.blue(),
}


def format_coins(amount: int) -> str:
    return f"{amount:,} coins"


def player_embed(player: Player) -> discord.Embed:
    embed = discord.Embed(
        title=player_card_label(player),
        color=RARITY_COLORS.get(player.rarity, discord.Color.blurple()),
    )
    embed.add_field(name="Position", value=player.position, inline=True)
    embed.add_field(name="Country", value=player.country, inline=True)
    embed.add_field(name="Team", value=player.team, inline=True)
    embed.add_field(
        name="Card Type",
        value=format_card_type(player.rarity),
        inline=True,
    )
    embed.add_field(name="Value", value=format_coins(player.value), inline=True)
    if player.stats:
        stats = " · ".join(
            f"**{name}** {value}" for name, value in player.stats.items()
        )
    else:
        stats = "No individual stats recorded."
    embed.add_field(name="Stats", value=stats, inline=False)
    return embed


def resolve_card_image(player: Player) -> Path | None:
    if not player.image_path:
        return None
    image_path = (PROJECT_ROOT / player.image_path).resolve()
    if not image_path.is_relative_to(CARDS_DIR):
        return None
    if image_path.is_file():
        return image_path
    return None


async def send_player_card(
    interaction: discord.Interaction,
    player: Player,
    *,
    ephemeral: bool = False,
    content: str | None = None,
) -> None:
    embed = player_embed(player)
    image_path = resolve_card_image(player)
    upload: discord.File | None = None
    if image_path:
        upload = discord.File(image_path, filename=image_path.name)
        embed.set_image(url=f"attachment://{image_path.name}")
    elif player.image_path:
        embed.add_field(
            name="Card image",
            value=f"Image not found. Upload it to `{player.image_path}`.",
            inline=False,
        )
    else:
        embed.add_field(
            name="Card image",
            value="No image is assigned to this card yet.",
            inline=False,
        )
    await interaction.response.send_message(
        content=content,
        embed=embed,
        file=upload,
        ephemeral=ephemeral,
    )


def inventory_embed(
    title: str,
    entries: list[InventoryEntry],
    *,
    collection: bool = False,
) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    if not entries:
        embed.description = "No player cards are in the catalog yet."
        return embed

    lines: list[str] = []
    for entry in entries:
        if collection:
            ownership = f"Owned ×{entry.quantity}" if entry.quantity else "Not owned"
            lines.append(
                f"**{player_card_label(entry.player)}** · {ownership}"
            )
        else:
            lines.append(
                f"**{player_card_label(entry.player)}** ×{entry.quantity}"
            )

    description = "\n".join(lines)
    if len(description) > 3900:
        description = description[:3890].rsplit("\n", 1)[0] + "\n…"
    embed.description = description
    embed.set_footer(text=f"{len(entries)} card type(s)")
    return embed
