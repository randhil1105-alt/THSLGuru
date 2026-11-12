"""XP reward constants and deterministic level progression."""

from __future__ import annotations

from dataclasses import dataclass
from math import isqrt

NORMAL_COMMAND_XP = 50
MINIGAME_COMPLETION_XP = 200
MATCH_VICTORY_XP = 200
MATCH_DEFEAT_XP = 50
LEVEL_UP_COIN_REWARD = 3_000_000


@dataclass(frozen=True)
class XPProgress:
    total_xp: int
    level: int
    progress_xp: int
    xp_to_next_level: int
    xp_remaining: int
    progress_percent: int


@dataclass(frozen=True)
class XPGrant:
    event_id: str
    user_id: str
    amount: int
    reason: str
    total_xp: int
    level_before: int
    level_after: int
    is_new: bool

    @property
    def leveled_up(self) -> bool:
        return self.level_after > self.level_before


def xp_required_for_level(level: int) -> int:
    """Return cumulative XP at the start of a level."""
    if level < 1:
        raise ValueError("Levels start at 1.")
    previous_level = level - 1
    return 100 * previous_level * (previous_level + 4)


def level_up_pack_rewards(level_before: int, level_after: int) -> tuple[int, int]:
    """Return (Gold, TOTW) packs earned across a level transition."""
    previous = int(level_before)
    current = int(level_after)
    if previous < 1 or current < 1:
        raise ValueError("Levels start at 1.")
    if current <= previous:
        return 0, 0
    return current - previous, current // 10 - previous // 10


def calculate_xp_progress(total_xp: int) -> XPProgress:
    """Calculate level and progress using the progressive threshold sequence."""
    xp = max(0, int(total_xp))
    level = isqrt(xp // 100 + 4) - 1
    current_threshold = xp_required_for_level(level)
    next_threshold = xp_required_for_level(level + 1)
    progress_xp = xp - current_threshold
    xp_to_next_level = next_threshold - current_threshold
    return XPProgress(
        total_xp=xp,
        level=level,
        progress_xp=progress_xp,
        xp_to_next_level=xp_to_next_level,
        xp_remaining=next_threshold - xp,
        progress_percent=(progress_xp * 100) // xp_to_next_level,
    )
