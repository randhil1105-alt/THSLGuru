"""Shared XP command-success markers and notification formatting."""

from __future__ import annotations

import logging

import discord

from database import XPGrant
from xp_system import NORMAL_COMMAND_XP, level_up_pack_rewards

COMMAND_XP_MARKER = "thsl_successful_command_xp"
LOGGER = logging.getLogger("thsl_guru.xp")


def mark_successful_command(interaction: object) -> None:
    extras = getattr(interaction, "extras", None)
    if isinstance(extras, dict):
        extras[COMMAND_XP_MARKER] = True


def xp_notification_text(
    grant: XPGrant,
    *,
    include_level_up: bool = True,
) -> str:
    if grant.reason == "minigame_guess_who" or grant.reason == "minigame_draft":
        message = "🏆 +200 XP | Minigame completed!"
    elif grant.reason == "match_victory":
        message = "⚽ +200 XP | Match victory!"
    elif grant.reason == "match_defeat":
        message = "⭐ +50 XP | Match played!"
    else:
        message = f"⭐ +{grant.amount} XP | Command used!"
    if grant.leveled_up and include_level_up:
        gold_packs, totw_packs = level_up_pack_rewards(
            grant.level_before,
            grant.level_after,
        )
        rewards = [_pack_reward_line(gold_packs, "Gold")]
        if totw_packs:
            rewards.append(_pack_reward_line(totw_packs, "TOTW"))
        message += (
            f"\n🎉 LEVEL UP! You are now Level {grant.level_after}."
            f"\n🎁 Rewards:\n" + "\n".join(f"- {reward}" for reward in rewards)
        )
    return message


def _pack_reward_line(quantity: int, pack_name: str) -> str:
    plural = "" if quantity == 1 else "s"
    return f"{quantity}x {pack_name} Pack{plural}"


def xp_level_up_embed(
    grant: XPGrant,
    *,
    user: object | None = None,
    user_id: str | int | None = None,
    username: str | None = None,
) -> discord.Embed:
    """Build the announcement and exact free-pack rewards for a level gain."""
    player_id = str(
        getattr(user, "id", None)
        or user_id
        or grant.user_id
    )
    player_name = str(
        getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or username
        or ""
    ).strip()
    mention = getattr(user, "mention", None) or f"<@{player_id}>"
    player_line = f"Player: {mention}"
    if player_name:
        player_line += f" ({discord.utils.escape_markdown(player_name)})"
    gold_packs, totw_packs = level_up_pack_rewards(
        grant.level_before,
        grant.level_after,
    )
    rewards = [_pack_reward_line(gold_packs, "Gold")]
    if totw_packs:
        rewards.append(_pack_reward_line(totw_packs, "TOTW"))

    embed = discord.Embed(
        title="🎉 LEVEL UP!",
        description=f"{player_line}\nNew Level: **Level {grant.level_after}**",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="🎁 Rewards",
        value="\n".join(f"- {reward}" for reward in rewards),
        inline=False,
    )
    return embed


async def award_marked_command_xp(
    interaction: object,
    database: object,
) -> XPGrant | None:
    extras = getattr(interaction, "extras", None)
    if not isinstance(extras, dict) or not extras.pop(COMMAND_XP_MARKER, False):
        return None
    user = getattr(interaction, "user", None)
    if user is None or getattr(user, "bot", False):
        return None
    interaction_id = getattr(interaction, "id", None)
    if interaction_id is None:
        return None
    try:
        grant = database.award_xp(
            user.id,
            f"command:{interaction_id}",
            NORMAL_COMMAND_XP,
            "command",
        )
    except Exception:
        LOGGER.exception(
            "Could not award command XP for interaction %s.",
            interaction_id,
        )
        return None
    if not grant.is_new:
        return grant
    try:
        if grant.leveled_up:
            await interaction.followup.send(
                content=xp_notification_text(grant, include_level_up=False),
                embed=xp_level_up_embed(grant, user=user),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                xp_notification_text(grant),
                ephemeral=True,
            )
    except Exception:
        LOGGER.warning(
            "Command XP was saved but its notification could not be sent for "
            "interaction %s.",
            interaction_id,
            exc_info=True,
        )
    return grant
