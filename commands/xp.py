"""Shared XP command-success markers and notification formatting."""

from __future__ import annotations

import logging

from database import XPGrant
from xp_system import NORMAL_COMMAND_XP

COMMAND_XP_MARKER = "thsl_successful_command_xp"
LOGGER = logging.getLogger("thsl_guru.xp")


def mark_successful_command(interaction: object) -> None:
    extras = getattr(interaction, "extras", None)
    if isinstance(extras, dict):
        extras[COMMAND_XP_MARKER] = True


def xp_notification_text(grant: XPGrant) -> str:
    if grant.reason == "minigame_guess_who" or grant.reason == "minigame_draft":
        message = "🏆 +200 XP | Minigame completed!"
    elif grant.reason == "match_victory":
        message = "⚽ +200 XP | Match victory!"
    elif grant.reason == "match_defeat":
        message = "⭐ +50 XP | Match played!"
    else:
        message = f"⭐ +{grant.amount} XP | Command used!"
    if grant.leveled_up:
        message += f"\n🎉 LEVEL UP! You are now Level {grant.level_after}!"
    return message


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
