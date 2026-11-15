"""AI lineup generation and tournament-only match difficulty."""

from __future__ import annotations

import random
import math
from dataclasses import asdict
from typing import Sequence

from database import (
    FORMATION_STARTERS,
    POSITION_ROLE_BY_SLOT,
    TOURNAMENT_AI_RATING_RANGES,
    TOURNAMENT_STAGE_LABELS,
    Player,
    card_position_role,
)
from match_simulation import MatchResult, MatchSide, MatchStarter


class TournamentAIUnavailable(ValueError):
    """The catalog cannot fill an AI lineup for a tournament stage."""


def average_team_rating(lineup: Sequence[dict[str, object]]) -> int:
    if not lineup:
        return 0
    return math.floor(
        sum(int(starter["overall"]) for starter in lineup) / len(lineup) + 0.5
    )


def generate_ai_lineup(
    catalog: Sequence[Player],
    formation: str,
    round_number: int,
    *,
    rng: random.Random | None = None,
) -> list[dict[str, object]]:
    """Build a distinct, position-valid team from the requested OVR band."""
    if formation not in FORMATION_STARTERS:
        raise TournamentAIUnavailable("The player's formation is not supported.")
    if round_number not in TOURNAMENT_AI_RATING_RANGES:
        raise TournamentAIUnavailable("The tournament stage is not supported.")

    minimum, maximum = TOURNAMENT_AI_RATING_RANGES[round_number]
    slots = FORMATION_STARTERS[formation]
    candidates_by_slot: dict[str, list[Player]] = {}
    for slot in slots:
        role = POSITION_ROLE_BY_SLOT[slot]
        candidates_by_slot[slot] = [
            player
            for player in catalog
            if minimum <= player.overall <= maximum
            and card_position_role(player.position) == role
            and (
                player.allowed_lineup_positions is None
                or slot in player.allowed_lineup_positions
            )
        ]
        if not candidates_by_slot[slot]:
            raise TournamentAIUnavailable(
                f"No {minimum}–{maximum} OVR cards can fill the {slot} slot."
            )

    chooser = rng or random.SystemRandom()
    assignment_order = sorted(slots, key=lambda slot: len(candidates_by_slot[slot]))
    assigned: dict[str, Player] = {}
    used_ids: set[int] = set()

    def assign(index: int) -> bool:
        if index == len(assignment_order):
            return True
        slot = assignment_order[index]
        available = [
            player
            for player in candidates_by_slot[slot]
            if player.id not in used_ids
        ]
        chooser.shuffle(available)
        for player in available:
            assigned[slot] = player
            used_ids.add(player.id)
            if assign(index + 1):
                return True
            used_ids.remove(player.id)
            del assigned[slot]
        return False

    if not assign(0):
        raise TournamentAIUnavailable(
            f"The catalog does not have enough distinct {minimum}–{maximum} OVR "
            "cards for this formation."
        )

    return [
        {
            "player_id": assigned[slot].id,
            "slot": slot,
            "name": assigned[slot].name,
            "overall": assigned[slot].overall,
            "position": assigned[slot].position,
        }
        for slot in slots
    ]


def tournament_team_rating(lineup: Sequence[dict[str, object]]) -> int:
    """Expose average OVR with the same rounding used by tournament records."""
    return average_team_rating(lineup)


def tournament_scoring_probability(
    round_number: int,
    player_rating: int,
    opponent_rating: int,
) -> float:
    """Return a player-side scoring probability that falls each round."""
    if round_number not in TOURNAMENT_STAGE_LABELS:
        raise ValueError("Tournament stage must be between 1 and 3.")
    base_probability = {1: 0.62, 2: 0.50, 3: 0.38}[round_number]
    rating_adjustment = (int(player_rating) - int(opponent_rating)) * 0.006
    return min(0.85, max(0.15, base_probability + rating_adjustment))


def tournament_match_side(
    *,
    user_id: str,
    username: str,
    formation: str,
    lineup: Sequence[dict[str, object]],
) -> MatchSide:
    return MatchSide(
        user_id=user_id,
        username=username,
        formation=formation,
        starters=tuple(
            MatchStarter(
                slot=str(starter["slot"]),
                name=str(starter["name"]),
                overall=int(starter["overall"]),
                position=str(starter["position"]),
            )
            for starter in lineup
        ),
    )


def serialize_tournament_result(result: MatchResult) -> dict[str, object]:
    return {
        "home_score": result.home_score,
        "away_score": result.away_score,
        "winner_id": result.winner_id,
        "duration_seconds": result.duration_seconds,
        "goals": [asdict(goal) for goal in result.goals],
    }
