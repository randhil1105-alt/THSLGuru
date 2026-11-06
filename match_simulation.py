"""Deterministic, testable match simulation rules and pacing."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

MATCH_DURATION_SECONDS = 45
MATCH_MINUTES = 90
MATCH_TICK_SECONDS = 2
GOAL_CHANCE_PER_TICK = 0.14
MAJOR_EVENT_CHANCE_PER_TICK = 0.56
OWN_GOAL_CHANCE = 0.04


@dataclass(frozen=True)
class MatchStarter:
    slot: str
    name: str
    overall: int
    position: str


@dataclass(frozen=True)
class MatchSide:
    user_id: str
    username: str
    formation: str
    starters: tuple[MatchStarter, ...]


@dataclass(frozen=True)
class MatchEvent:
    minute: int
    kind: str
    text: str
    team_id: str
    player_name: str | None = None
    player_team_id: str | None = None
    own_goal: bool = False
    home_score: int = 0
    away_score: int = 0


@dataclass(frozen=True)
class MatchTick:
    elapsed_seconds: float
    minute: int
    home_score: int
    away_score: int
    event: MatchEvent | None = None


@dataclass(frozen=True)
class MatchResult:
    home: MatchSide
    away: MatchSide
    ticks: tuple[MatchTick, ...]
    goals: tuple[MatchEvent, ...]
    home_score: int
    away_score: int
    winner_id: str
    duration_seconds: int = MATCH_DURATION_SECONDS


def match_timeline() -> tuple[tuple[float, int], ...]:
    """Return the live update schedule: 4 simulated minutes per 2 seconds."""
    timeline = [
        (float(elapsed), 1 + (elapsed // MATCH_TICK_SECONDS) * 4)
        for elapsed in range(0, MATCH_DURATION_SECONDS, MATCH_TICK_SECONDS)
    ]
    timeline.append((float(MATCH_DURATION_SECONDS), MATCH_MINUTES))
    return tuple(timeline)


def _scoring_players(side: MatchSide) -> tuple[MatchStarter, ...]:
    skaters = tuple(
        starter for starter in side.starters if starter.slot != "GK"
    )
    return skaters or side.starters


def simulate_match(
    home: MatchSide,
    away: MatchSide,
    *,
    rng: random.Random | None = None,
) -> MatchResult:
    """Generate a complete match while guaranteeing a non-draw final result."""
    if not home.starters or not away.starters:
        raise ValueError("Both teams need valid starting lineups.")

    chooser = rng or random.SystemRandom()
    score = {home.user_id: 0, away.user_id: 0}
    ticks: list[MatchTick] = []
    goals: list[MatchEvent] = []

    for elapsed, minute in match_timeline():
        event: MatchEvent | None = None
        if minute == MATCH_MINUTES:
            if score[home.user_id] == score[away.user_id]:
                scoring_side = chooser.choice((home, away))
                scorer = chooser.choice(_scoring_players(scoring_side))
                score[scoring_side.user_id] += 1
                event = MatchEvent(
                    minute=minute,
                    kind="sudden_death_goal",
                    text=(
                        f"Sudden-death goal! {scorer.name} "
                        f"({scoring_side.username}) scores the winner."
                    ),
                    team_id=scoring_side.user_id,
                    player_name=scorer.name,
                    player_team_id=scoring_side.user_id,
                )
        else:
            roll = chooser.random()
            if roll < GOAL_CHANCE_PER_TICK:
                if chooser.random() < OWN_GOAL_CHANCE:
                    defending_side = chooser.choice((home, away))
                    scoring_side = away if defending_side is home else home
                    scorer = chooser.choice(defending_side.starters)
                    score[scoring_side.user_id] += 1
                    event = MatchEvent(
                        minute=minute,
                        kind="own_goal",
                        text=(
                            f"OWN GOAL! {scorer.name} "
                            f"({defending_side.username}) scores for "
                            f"{scoring_side.username}."
                        ),
                        team_id=scoring_side.user_id,
                        player_name=scorer.name,
                        player_team_id=defending_side.user_id,
                        own_goal=True,
                    )
                else:
                    scoring_side = chooser.choice((home, away))
                    scorer = chooser.choice(_scoring_players(scoring_side))
                    score[scoring_side.user_id] += 1
                    event = MatchEvent(
                        minute=minute,
                        kind="goal",
                        text=(
                            f"GOAL! {scorer.name} ({scoring_side.username}) "
                            f"puts it in the net."
                        ),
                        team_id=scoring_side.user_id,
                        player_name=scorer.name,
                        player_team_id=scoring_side.user_id,
                    )
            elif roll < GOAL_CHANCE_PER_TICK + MAJOR_EVENT_CHANCE_PER_TICK:
                attacking_side = chooser.choice((home, away))
                defending_side = away if attacking_side is home else home
                shooter = chooser.choice(_scoring_players(attacking_side))
                event_kind = chooser.choice(("attack", "save", "miss"))
                if event_kind == "save":
                    goalie = next(
                        (
                            starter
                            for starter in defending_side.starters
                            if starter.slot == "GK"
                        ),
                        None,
                    )
                    if goalie is None:
                        goalie = chooser.choice(defending_side.starters)
                    text = (
                        f"{goalie.name} ({defending_side.username}) makes a "
                        f"save on {shooter.name}'s attack."
                    )
                    player_name = goalie.name
                    player_team_id = defending_side.user_id
                elif event_kind == "miss":
                    text = (
                        f"{shooter.name} ({attacking_side.username}) misses a "
                        "good chance."
                    )
                    player_name = shooter.name
                    player_team_id = attacking_side.user_id
                else:
                    text = (
                        f"{shooter.name} ({attacking_side.username}) drives "
                        "a dangerous attack."
                    )
                    player_name = shooter.name
                    player_team_id = attacking_side.user_id
                event = MatchEvent(
                    minute=minute,
                    kind=event_kind,
                    text=text,
                    team_id=attacking_side.user_id,
                    player_name=player_name,
                    player_team_id=player_team_id,
                )

        if event is not None and event.kind in {
            "goal",
            "own_goal",
            "sudden_death_goal",
        }:
            event = MatchEvent(
                minute=event.minute,
                kind=event.kind,
                text=event.text,
                team_id=event.team_id,
                player_name=event.player_name,
                player_team_id=event.player_team_id,
                own_goal=event.own_goal,
                home_score=score[home.user_id],
                away_score=score[away.user_id],
            )
            goals.append(event)

        ticks.append(
            MatchTick(
                elapsed_seconds=elapsed,
                minute=minute,
                home_score=score[home.user_id],
                away_score=score[away.user_id],
                event=event,
            )
        )

    if not goals or score[home.user_id] == score[away.user_id]:
        raise RuntimeError("The match simulation failed to produce a winner.")

    winner_id = (
        home.user_id
        if score[home.user_id] > score[away.user_id]
        else away.user_id
    )
    return MatchResult(
        home=home,
        away=away,
        ticks=tuple(ticks),
        goals=tuple(goals),
        home_score=score[home.user_id],
        away_score=score[away.user_id],
        winner_id=winner_id,
    )


async def play_match_clock(
    on_tick: Callable[[float, int], Awaitable[None]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Run live updates against one fixed 45-second monotonic deadline."""
    started_at = monotonic()
    for elapsed, minute in match_timeline():
        remaining = started_at + elapsed - monotonic()
        if remaining > 0:
            await sleep(remaining)
        await on_tick(elapsed, minute)
