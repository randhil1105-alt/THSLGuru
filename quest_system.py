"""Daily quest definitions and UTC day helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class QuestDefinition:
    key: str
    category: str
    description: str
    metric: str
    target: int
    reward: int


QUESTS_PER_DAY = 3
DAILY_QUEST_POOL = (
    QuestDefinition("match_win_1", "Match", "Win 1 match", "match_win", 1, 5_000_000),
    QuestDefinition("match_win_3", "Match", "Win 3 matches", "match_win", 3, 12_000_000),
    QuestDefinition("match_goals_5", "Match", "Score 5 goals", "match_goals", 5, 4_000_000),
    QuestDefinition("minigame_play_2", "Minigame", "Play 2 minigames", "minigame_play", 2, 3_000_000),
    QuestDefinition("minigame_win_1", "Minigame", "Win 1 minigame", "minigame_win", 1, 5_000_000),
    QuestDefinition("minigame_win_3", "Minigame", "Win 3 minigames", "minigame_win", 3, 10_000_000),
    QuestDefinition("packs_open_2", "Cards", "Open 2 packs", "pack_open", 2, 3_000_000),
    QuestDefinition("packs_open_5", "Cards", "Open 5 packs", "pack_open", 5, 7_000_000),
    QuestDefinition("gold_card_1", "Cards", "Obtain a Gold card", "gold_card", 1, 4_000_000),
    QuestDefinition("trade_1", "Trading", "Complete 1 trade", "trade_complete", 1, 5_000_000),
    QuestDefinition("trade_2", "Trading", "Complete 2 trades", "trade_complete", 2, 10_000_000),
)


def utc_day(timestamp: int | float) -> str:
    """Return the UTC calendar date for a Unix timestamp."""
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
