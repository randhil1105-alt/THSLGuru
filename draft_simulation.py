"""Position-aware player draft selection and rating-based match simulation."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from database import Player, card_position_role

MINIMUM_WIN_PROBABILITY = 0.10
MAXIMUM_WIN_PROBABILITY = 0.90


class InsufficientDraftCatalog(ValueError):
    """The catalog cannot supply all required cards for a draft or AI team."""


@dataclass(frozen=True)
class DraftMatchResult:
    player_score: int
    ai_score: int
    player_win_probability: float
    player_won: bool


def calculate_squad_average(players: Sequence[Player]) -> int:
    """Calculate the four-card average, rounded to the nearest OVR point."""
    if not players:
        raise ValueError("A squad must contain at least one player.")
    return math.floor(sum(player.overall for player in players) / len(players) + 0.5)


def _players_by_role(players: Sequence[Player]) -> dict[str, list[Player]]:
    result: dict[str, list[Player]] = {"GK": [], "CM": [], "WING": []}
    for player in players:
        role = card_position_role(player.position)
        if role in result:
            result[role].append(player)
    return result


def generate_draft_groups(
    players: Sequence[Player],
    *,
    rng: Any | None = None,
) -> tuple[tuple[Player, ...], ...]:
    """Sample 3 GK, 3 CM, and two distinct groups of 3 Wingers."""
    chooser = rng or random.SystemRandom()
    by_role = _players_by_role(players)
    required = {"GK": 3, "CM": 3, "WING": 6}
    missing = [
        f"{role}: need {count}, found {len(by_role[role])}"
        for role, count in required.items()
        if len(by_role[role]) < count
    ]
    if missing:
        raise InsufficientDraftCatalog(
            "The player catalog does not have enough cards for Draft "
            f"({'; '.join(missing)})."
        )

    goalkeepers = tuple(chooser.sample(by_role["GK"], 3))
    centers = tuple(chooser.sample(by_role["CM"], 3))
    wingers = chooser.sample(by_role["WING"], 6)
    return (
        goalkeepers,
        centers,
        tuple(wingers[:3]),
        tuple(wingers[3:]),
    )


def generate_ai_squad(
    players: Sequence[Player],
    *,
    excluded_player_ids: set[int] | frozenset[int] = frozenset(),
    rng: Any | None = None,
) -> tuple[Player, ...]:
    """Randomly build an AI lineup with one GK, one CM, and two Wingers."""
    chooser = rng or random.SystemRandom()
    by_role = _players_by_role(players)
    excluded = set(excluded_player_ids)
    required = {"GK": 1, "CM": 1, "WING": 2}
    selected: list[Player] = []
    for role, count in required.items():
        eligible = [
            player for player in by_role[role] if player.id not in excluded
        ]
        if len(eligible) < count:
            raise InsufficientDraftCatalog(
                "The catalog does not have enough unused cards to build the AI "
                f"squad ({role}: need {count}, found {len(eligible)})."
            )
        selected.extend(chooser.sample(eligible, count))
    return tuple(selected)


def draft_win_probability(player_average: int, ai_average: int) -> float:
    """Give higher-rated squads better odds while preserving upset chances."""
    rating_difference = int(player_average) - int(ai_average)
    probability = 1.0 / (1.0 + math.exp(-rating_difference / 10.0))
    return min(
        MAXIMUM_WIN_PROBABILITY,
        max(MINIMUM_WIN_PROBABILITY, probability),
    )


def simulate_draft_match(
    player_squad: Sequence[Player],
    ai_squad: Sequence[Player],
    *,
    rng: Any | None = None,
) -> DraftMatchResult:
    """Simulate a randomized score with odds determined by squad averages."""
    if len(player_squad) != 4 or len(ai_squad) != 4:
        raise ValueError("Both Draft teams must contain exactly four players.")
    chooser = rng or random.SystemRandom()
    probability = draft_win_probability(
        calculate_squad_average(player_squad),
        calculate_squad_average(ai_squad),
    )
    player_won = chooser.random() < probability

    if player_won:
        player_score = chooser.randint(1, 4)
        ai_score = chooser.randint(0, player_score - 1)
    else:
        ai_score = chooser.randint(1, 4)
        player_score = chooser.randint(0, ai_score - 1)

    return DraftMatchResult(
        player_score=player_score,
        ai_score=ai_score,
        player_win_probability=probability,
        player_won=player_won,
    )
