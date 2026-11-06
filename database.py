"""Persistent SQLite storage and atomic economy operations."""

from __future__ import annotations

import json
import random
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

CLAIM_COOLDOWN_SECONDS = 30 * 60
GOLD_PACK_COST = 5_000_000
TOTW_PACK_COST = 20_000_000
MAX_PACK_QUANTITY = 10
MATCH_REWARD = 5_000_000
MATCH_INVITATION_SECONDS = 60
DRAFT_COOLDOWN_SECONDS = 10 * 60
DRAFT_WIN_REWARD = 200_000
AUCTION_DURATIONS_SECONDS = (30 * 60, 60 * 60, 6 * 60 * 60, 24 * 60 * 60)
SQLITE_INT_MAX = (1 << 63) - 1
SUPPORTED_STATS = ("SHO", "DRI", "SAV", "PAS", "DEF", "ACT", "DIV", "CLE", "SWE")
FORMATION_STARTERS = {
    "Balanced": ("WING1", "CM1", "GK"),
    "Offensive": ("WING1", "WING2", "GK"),
    "Defensive": ("CM1", "CM2", "GK"),
}
AUCTION_JOIN_SQL = """
    SELECT a.*,
           p.id AS card_id,
           p.name AS card_name,
           p.overall AS card_overall,
           p.position AS card_position,
           p.country AS card_country,
           p.team AS card_team,
           p.rarity AS card_rarity,
           p.value AS card_value,
           p.stats_json AS card_stats_json,
           p.image_path AS card_image_path,
           p.allowed_lineup_positions_json AS card_allowed_positions_json
    FROM auctions AS a
    JOIN players AS p ON p.id = a.player_id
"""

STARTING_LINEUP_POSITIONS = ("WING1", "WING2", "CM1", "CM2", "GK")
SUBSTITUTE_POSITIONS = ("SUB1", "SUB2")
LINEUP_POSITIONS = STARTING_LINEUP_POSITIONS + SUBSTITUTE_POSITIONS
TEAM_FORMATIONS = tuple(FORMATION_STARTERS)
DEFAULT_FORMATION = "Balanced"

POSITION_ROLE_BY_SLOT = {
    "WING1": "WING",
    "WING2": "WING",
    "CM1": "CM",
    "CM2": "CM",
    "GK": "GK",
}

LEGACY_SLOT_MAP = {
    "LW": "WING1",
    "RW": "WING2",
    "C": "CM1",
    "GK": "GK",
}


class EconomyError(Exception):
    """Base class for expected economy and player errors."""


class PlayerNotFound(EconomyError):
    pass


class PlayerAlreadyExists(EconomyError):
    pass


class InsufficientFunds(EconomyError):
    pass


class InsufficientCards(EconomyError):
    pass


class CardNotOwned(EconomyError):
    pass


class DuplicateLineupPlayer(EconomyError):
    pass


class InvalidLineupPosition(EconomyError):
    pass


class InvalidFormation(EconomyError):
    pass


class CooldownActive(EconomyError):
    def __init__(self, remaining_seconds: int) -> None:
        self.remaining_seconds = max(0, int(remaining_seconds))
        super().__init__(self.remaining_seconds)


class NoEligibleCards(EconomyError):
    pass


class InvalidAmount(EconomyError):
    pass


class AuctionError(EconomyError):
    """Base class for auction operation errors."""


class AuctionNotFound(AuctionError):
    pass


class AuctionNotActive(AuctionError):
    pass


class AuctionNotSeller(AuctionError):
    pass


class AuctionHasBids(AuctionError):
    pass


class AuctionSelfBid(AuctionError):
    pass


class AuctionBidTooLow(AuctionError):
    def __init__(self, minimum: int) -> None:
        self.minimum = int(minimum)
        super().__init__(self.minimum)


class AuctionDuplicateBid(AuctionError):
    pass


class AuctionExpired(AuctionError):
    pass


class DraftCooldownActive(CooldownActive):
    """Raised when a player tries to start a Draft before their cooldown ends."""


class DraftAlreadyActive(EconomyError):
    pass


class DraftSessionNotFound(EconomyError):
    pass


class DraftSessionUnauthorized(EconomyError):
    pass


class MatchError(EconomyError):
    """Base class for expected match and invitation errors."""


class MatchAlreadyActive(MatchError):
    pass


class MatchNotFound(MatchError):
    pass


class MatchNotPending(MatchError):
    pass


class MatchActionUnauthorized(MatchError):
    pass

class InvalidMatchParticipant(MatchError):
    pass


class InvalidMatchResult(MatchError):
    pass


class InvalidPlayer(EconomyError):
    pass


@dataclass(frozen=True)
class Player:
    id: int
    name: str
    overall: int
    position: str
    country: str
    team: str
    rarity: str
    value: int
    stats: dict[str, int]
    image_path: str | None
    allowed_lineup_positions: tuple[str, ...] | None = None


@dataclass(frozen=True)
class FormationChange:
    formation: str
    moved: tuple[tuple[str, str, Player], ...] = ()
    unassigned: tuple[tuple[str, Player], ...] = ()


@dataclass(frozen=True)
class StartingLineup:
    formation: str
    starters: tuple[dict[str, Any], ...]
    missing_slots: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.missing_slots


@dataclass(frozen=True)
class InventoryEntry:
    player: Player
    quantity: int


@dataclass(frozen=True)
class Auction:
    auction_id: str
    seller_id: str
    seller_name: str
    player: Player
    starting_price: int
    duration_seconds: int
    end_time: int
    status: str
    highest_bid: int | None
    highest_bidder_id: str | None
    highest_bidder_name: str | None
    channel_id: str | None
    message_id: str | None
    created_at: int
    completed_at: int | None
    seller_notification_attempted: bool
    bidder_notification_attempted: bool

    @property
    def current_price(self) -> int:
        return self.highest_bid or self.starting_price


def card_position_role(position: str) -> str | None:
    """Normalize catalog position labels to the strict lineup role groups."""
    normalized = " ".join(
        str(position).strip().upper().replace("_", " ").replace("-", " ").split()
    )
    if normalized in {"GK", "G", "GOALIE", "GOALTENDER", "GOALKEEPER"}:
        return "GK"
    if normalized in {
        "CM",
        "C",
        "CENTER",
        "CENTRE",
        "CENTER MID",
        "CENTRE MID",
        "CENTRAL MID",
        "CENTRAL MIDFIELDER",
        "MIDFIELDER",
        "MID",
    }:
        return "CM"
    if normalized in {
        "W",
        "WI",
        "WING",
        "WINGER",
        "LW",
        "RW",
        "LEFT WING",
        "RIGHT WING",
        "LEFT WINGER",
        "RIGHT WINGER",
    }:
        return "WING"
    return None


def format_card_type(rarity: str) -> str:
    """Present the stored type in a readable, consistent form."""
    raw_type = str(rarity).strip()
    normalized = raw_type.upper()
    if normalized == "GOLD":
        return "Gold"
    if normalized == "TOTW":
        return "TOTW"
    return raw_type.title()


def format_card_label(name: str, overall: int, rarity: str) -> str:
    """Format a card as rating, catalog name, and its stored card type."""
    display_name = str(name).strip()
    card_type = format_card_type(rarity)
    disambiguator = f" ({card_type} {overall})"
    if display_name.casefold().endswith(disambiguator.casefold()):
        display_name = display_name[: -len(disambiguator)]
    return f"{overall} {display_name} ({card_type})"


def player_card_label(player: Player) -> str:
    return format_card_label(player.name, player.overall, player.rarity)


def card_rating_band(overall: int) -> tuple[str, float]:
    """Return the displayed tier and weight for a card's overall rating."""
    rating = int(overall)
    if rating <= 84:
        return "Common", 4.0
    if rating <= 89:
        return "Uncommon", 2.0
    if rating <= 94:
        return "Rare", 1.0
    return "Very rare", 0.25


def card_rating_weight(overall: int) -> float:
    """Return the per-card weight for its overall-rating band."""
    return card_rating_band(overall)[1]


def calculate_pack_odds(players: Sequence[Player]) -> dict[int, float]:
    """Calculate normalized per-card odds for one rarity-filtered pack pool."""
    weights = [card_rating_weight(player.overall) for player in players]
    total_weight = sum(weights)
    if total_weight <= 0:
        return {}
    return {
        player.id: weight / total_weight * 100
        for player, weight in zip(players, weights)
    }


class Database:
    """A small connection-per-operation SQLite repository.

    Every economy mutation starts with ``BEGIN IMMEDIATE``. SQLite therefore
    serializes competing claims, sales, grants, and pack purchases before their
    balance checks are made.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.path),
            timeout=15,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self, seed_players: Sequence[Mapping[str, Any]] = ()) -> None:
        """Create the schema and insert missing seed cards without overwriting edits."""
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    overall INTEGER NOT NULL CHECK (overall BETWEEN 0 AND 99),
                    position TEXT NOT NULL,
                    country TEXT NOT NULL,
                    team TEXT NOT NULL,
                    rarity TEXT NOT NULL,
                    value INTEGER NOT NULL CHECK (value >= 0),
                    stats_json TEXT NOT NULL DEFAULT '{}',
                    image_path TEXT,
                    allowed_lineup_positions_json TEXT
                );

                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
                    last_claim_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS inventory (
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    PRIMARY KEY (user_id, player_id)
                );

                CREATE TABLE IF NOT EXISTS pack_purchases (
                    interaction_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    player_id INTEGER NOT NULL REFERENCES players(id),
                    rarity TEXT NOT NULL CHECK (rarity IN ('GOLD', 'TOTW')),
                    cost INTEGER NOT NULL CHECK (cost > 0),
                    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lineup_slots (
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    position TEXT NOT NULL CHECK (position IN ('LW', 'RW', 'GK')),
                    player_id INTEGER NOT NULL REFERENCES players(id),
                    PRIMARY KEY (user_id, position),
                    UNIQUE (user_id, player_id)
                );

                CREATE TABLE IF NOT EXISTS center_slots (
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    position TEXT NOT NULL CHECK (position = 'C'),
                    player_id INTEGER NOT NULL REFERENCES players(id),
                    PRIMARY KEY (user_id, position),
                    UNIQUE (user_id, player_id)
                );

                CREATE TABLE IF NOT EXISTS substitute_slots (
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    position TEXT NOT NULL CHECK (position IN ('SUB1', 'SUB2')),
                    player_id INTEGER NOT NULL REFERENCES players(id),
                    PRIMARY KEY (user_id, position),
                    UNIQUE (user_id, player_id)
                );

                CREATE TABLE IF NOT EXISTS team_settings (
                    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                    formation TEXT NOT NULL
                        CHECK (formation IN ('Balanced', 'Attack', 'Defensive'))
                );

                CREATE TABLE IF NOT EXISTS team_starters (
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    position TEXT NOT NULL CHECK (
                        position IN ('WING1', 'WING2', 'CM1', 'CM2', 'GK')
                    ),
                    player_id INTEGER NOT NULL REFERENCES players(id),
                    PRIMARY KEY (user_id, position),
                    UNIQUE (user_id, player_id)
                );

                CREATE TABLE IF NOT EXISTS team_settings_v2 (
                    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                    formation TEXT NOT NULL
                        CHECK (formation IN ('Balanced', 'Offensive', 'Defensive'))
                );

                CREATE TABLE IF NOT EXISTS team_data_migrations (
                    version TEXT PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS team_migration_notices (
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    notice TEXT NOT NULL,
                    PRIMARY KEY (user_id, notice)
                );

                CREATE TABLE IF NOT EXISTS matches (
                    match_id TEXT PRIMARY KEY,
                    challenger_id TEXT NOT NULL,
                    opponent_id TEXT NOT NULL,
                    challenger_name TEXT NOT NULL,
                    opponent_name TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    invite_message_id TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'PENDING', 'ACTIVE', 'COMPLETED', 'DECLINED',
                            'EXPIRED', 'CANCELLED'
                        )
                    ),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    lineups_json TEXT,
                    result_json TEXT,
                    winner_id TEXT,
                    reward_amount INTEGER NOT NULL DEFAULT 0 CHECK (reward_amount >= 0),
                    announced_at INTEGER,
                    cancel_reason TEXT
                );

                CREATE TABLE IF NOT EXISTS active_match_users (
                    user_id TEXT PRIMARY KEY,
                    match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS draft_cooldowns (
                    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                    last_started_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS draft_sessions (
                    game_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK (
                        status IN ('ACTIVE', 'COMPLETED', 'CANCELLED')
                    ),
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    won INTEGER CHECK (won IN (0, 1)),
                    reward_amount INTEGER NOT NULL DEFAULT 0 CHECK (reward_amount >= 0),
                    result_json TEXT
                );

                CREATE TABLE IF NOT EXISTS auctions (
                    auction_id TEXT PRIMARY KEY,
                    seller_id TEXT NOT NULL REFERENCES users(user_id),
                    seller_name TEXT NOT NULL,
                    player_id INTEGER NOT NULL REFERENCES players(id),
                    starting_price INTEGER NOT NULL CHECK (starting_price > 0),
                    duration_seconds INTEGER NOT NULL CHECK (
                        duration_seconds IN (1800, 3600, 21600, 86400)
                    ),
                    end_time INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('ACTIVE', 'COMPLETED', 'UNSOLD', 'CANCELLED')
                    ),
                    highest_bid INTEGER CHECK (
                        highest_bid IS NULL OR highest_bid > 0
                    ),
                    highest_bidder_id TEXT REFERENCES users(user_id),
                    highest_bidder_name TEXT,
                    channel_id TEXT,
                    message_id TEXT,
                    created_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    seller_notification_attempted INTEGER NOT NULL DEFAULT 0 CHECK (
                        seller_notification_attempted IN (0, 1)
                    ),
                    bidder_notification_attempted INTEGER NOT NULL DEFAULT 0 CHECK (
                        bidder_notification_attempted IN (0, 1)
                    ),
                    CHECK (
                        (highest_bid IS NULL AND highest_bidder_id IS NULL
                         AND highest_bidder_name IS NULL)
                        OR
                        (highest_bid IS NOT NULL AND highest_bidder_id IS NOT NULL
                         AND highest_bidder_name IS NOT NULL)
                    )
                );

                CREATE TABLE IF NOT EXISTS auction_bids (
                    bid_id TEXT PRIMARY KEY,
                    auction_id TEXT NOT NULL REFERENCES auctions(auction_id),
                    interaction_id TEXT NOT NULL UNIQUE,
                    bidder_id TEXT NOT NULL REFERENCES users(user_id),
                    bidder_name TEXT NOT NULL,
                    amount INTEGER NOT NULL CHECK (amount > 0),
                    status TEXT NOT NULL CHECK (
                        status IN ('HELD', 'OUTBID', 'SUPERSEDED', 'WON')
                    ),
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_players_rarity_name
                    ON players(rarity, name);
                CREATE INDEX IF NOT EXISTS idx_users_balance
                    ON users(balance DESC);
                CREATE INDEX IF NOT EXISTS idx_matches_status_expiry
                    ON matches(status, expires_at);
                CREATE INDEX IF NOT EXISTS idx_draft_sessions_user_status
                    ON draft_sessions(user_id, status);
                CREATE INDEX IF NOT EXISTS idx_auctions_status_end
                    ON auctions(status, end_time);
                CREATE INDEX IF NOT EXISTS idx_auctions_seller_created
                    ON auctions(seller_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_auction_bids_auction_status
                    ON auction_bids(auction_id, status);
                """
            )
            player_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(players)")
            }
            if "allowed_lineup_positions_json" not in player_columns:
                connection.execute(
                    "ALTER TABLE players ADD COLUMN allowed_lineup_positions_json TEXT"
                )
        finally:
            connection.close()

        for player in seed_players:
            try:
                self.add_player(player)
            except PlayerAlreadyExists:
                # Existing administrator edits are the source of truth after first seed.
                continue
        self._migrate_legacy_team_data()

    def _migrate_legacy_team_data(self) -> None:
        """Copy compatible legacy lineup data without changing legacy records."""
        migration_version = "formation-lineup-v2"
        with self._transaction() as connection:
            migrated = connection.execute(
                "SELECT 1 FROM team_data_migrations WHERE version = ?",
                (migration_version,),
            ).fetchone()
            if migrated:
                return

            user_rows = connection.execute(
                """
                SELECT user_id FROM team_settings
                UNION SELECT user_id FROM lineup_slots
                UNION SELECT user_id FROM center_slots
                UNION SELECT user_id FROM substitute_slots
                """
            ).fetchall()
            for user_row in user_rows:
                user_id = str(user_row["user_id"])
                old_settings = connection.execute(
                    "SELECT formation FROM team_settings WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                old_formation = (
                    str(old_settings["formation"]) if old_settings else DEFAULT_FORMATION
                )
                formation = {
                    "Attack": "Offensive",
                    "Balanced": "Balanced",
                    "Defensive": "Defensive",
                }.get(old_formation, DEFAULT_FORMATION)
                active_positions = FORMATION_STARTERS[formation]
                connection.execute(
                    """
                    INSERT OR IGNORE INTO team_settings_v2(user_id, formation)
                    VALUES (?, ?)
                    """,
                    (user_id, formation),
                )

                legacy_rows = connection.execute(
                    """
                    SELECT legacy.position AS legacy_position, players.*
                    FROM (
                        SELECT user_id, position, player_id
                        FROM lineup_slots WHERE user_id = ?
                        UNION ALL
                        SELECT user_id, position, player_id
                        FROM center_slots WHERE user_id = ?
                    ) AS legacy
                    JOIN players ON players.id = legacy.player_id
                    ORDER BY CASE legacy.position
                        WHEN 'LW' THEN 0
                        WHEN 'C' THEN 1
                        WHEN 'RW' THEN 2
                        WHEN 'GK' THEN 3
                        ELSE 4
                    END
                    """,
                    (user_id, user_id),
                ).fetchall()

                assignments: dict[str, sqlite3.Row] = {}
                overflow: list[tuple[str, sqlite3.Row, str]] = []
                notices: list[str] = []
                for row in legacy_rows:
                    legacy_position = str(row["legacy_position"])
                    mapped_position = LEGACY_SLOT_MAP.get(legacy_position)
                    role = card_position_role(str(row["position"]))
                    player_label = format_card_label(
                        str(row["name"]),
                        int(row["overall"]),
                        str(row["rarity"]),
                    )
                    owned = connection.execute(
                        """
                        SELECT 1 FROM inventory
                        WHERE user_id = ? AND player_id = ? AND quantity > 0
                        """,
                        (user_id, int(row["id"])),
                    ).fetchone()
                    if not owned:
                        notices.append(
                            f"{legacy_position}: {player_label} is no longer owned "
                            "and was left unassigned."
                        )
                        continue
                    if mapped_position is None or role != POSITION_ROLE_BY_SLOT.get(
                        mapped_position
                    ):
                        notices.append(
                            f"{legacy_position}: {player_label} has an incompatible "
                            f"card position ({row['position']}) and was left unassigned."
                        )
                    elif mapped_position in active_positions and mapped_position not in assignments:
                        assignments[mapped_position] = row
                    else:
                        overflow.append((legacy_position, row, role))

                for legacy_position, row, role in overflow:
                    available_position = next(
                        (
                            position
                            for position in active_positions
                            if position not in assignments
                            and POSITION_ROLE_BY_SLOT[position] == role
                        ),
                        None,
                    )
                    player_label = format_card_label(
                        str(row["name"]),
                        int(row["overall"]),
                        str(row["rarity"]),
                    )
                    if available_position is None:
                        notices.append(
                            f"{legacy_position}: {player_label} was not assigned because "
                            f"{formation} has no additional {role} starter slot."
                        )
                    else:
                        assignments[available_position] = row
                        notices.append(
                            f"{legacy_position}: {player_label} was moved to "
                            f"{available_position} to fit {formation}."
                        )

                for position, row in assignments.items():
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO team_starters(user_id, position, player_id)
                        VALUES (?, ?, ?)
                        """,
                        (user_id, position, int(row["id"])),
                    )
                for notice in notices:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO team_migration_notices(user_id, notice)
                        VALUES (?, ?)
                        """,
                        (user_id, notice),
                    )

            connection.execute(
                "INSERT INTO team_data_migrations(version) VALUES (?)",
                (migration_version,),
            )

    @staticmethod
    def _validate_user_id(user_id: str | int) -> str:
        value = str(user_id).strip()
        if not value or len(value) > 64:
            raise ValueError("A valid user ID is required.")
        return value

    @staticmethod
    def _validate_amount(amount: int, *, allow_zero: bool = False) -> int:
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise InvalidAmount("Amount must be a whole number.")
        minimum = 0 if allow_zero else 1
        if amount < minimum or amount > SQLITE_INT_MAX:
            raise InvalidAmount("Amount is outside the supported range.")
        return amount

    @staticmethod
    def _validate_image_path(image_path: str | None) -> str | None:
        if image_path is None or not str(image_path).strip():
            return None
        candidate = PurePosixPath(str(image_path).strip())
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or not candidate.parts
            or candidate.parts[0] != "cards"
        ):
            raise InvalidPlayer("Card images must use a relative path inside cards/.")
        return candidate.as_posix()

    @staticmethod
    def _validate_player_values(player: Mapping[str, Any]) -> dict[str, Any]:
        try:
            name = str(player["name"]).strip()
            overall = int(player["overall"])
            position = str(player["position"]).strip()
            country = str(player["country"]).strip()
            team = str(player["team"]).strip()
            rarity = str(player["rarity"]).strip().upper()
            value = int(player["value"])
            stats = player.get("stats", {})
            if not isinstance(stats, Mapping):
                raise TypeError("stats must be an object")
            normalized_stats: dict[str, int] = {}
            for key, stat_value in stats.items():
                stat_name = str(key).strip().upper()
                if stat_name not in SUPPORTED_STATS:
                    raise ValueError(f"Unsupported player stat: {stat_name}")
                stat_number = int(stat_value)
                if not 0 <= stat_number <= 99:
                    raise ValueError(f"{stat_name} must be between 0 and 99.")
                normalized_stats[stat_name] = stat_number
            image_path = Database._validate_image_path(player.get("image_path"))
            allowed_positions = player.get("allowed_lineup_positions")
            if allowed_positions is None:
                normalized_allowed_positions = None
            else:
                if not isinstance(allowed_positions, (list, tuple)):
                    raise TypeError("allowed_lineup_positions must be a list")
                normalized_allowed_positions = tuple(
                    dict.fromkeys(str(slot).strip().upper() for slot in allowed_positions)
                )
                if (
                    not normalized_allowed_positions
                    or any(slot not in LINEUP_POSITIONS for slot in normalized_allowed_positions)
                ):
                    raise ValueError("allowed_lineup_positions contains an invalid slot")
                if any(
                    slot in POSITION_ROLE_BY_SLOT
                    and card_position_role(position)
                    != POSITION_ROLE_BY_SLOT[slot]
                    for slot in normalized_allowed_positions
                ):
                    raise ValueError(
                        "allowed_lineup_positions must match the card's position"
                    )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidPlayer(f"Invalid player record: {exc}") from exc

        if not name or len(name) > 80:
            raise InvalidPlayer("Player names must contain 1 to 80 characters.")
        if not 0 <= overall <= 99:
            raise InvalidPlayer("Overall rating must be between 0 and 99.")
        if not position or not country or not team or not rarity:
            raise InvalidPlayer("Position, country, team, and rarity are required.")
        if len(position) > 12 or len(country) > 60 or len(team) > 100 or len(rarity) > 24:
            raise InvalidPlayer("One or more player fields are too long.")
        if value < 0 or value > SQLITE_INT_MAX:
            raise InvalidPlayer("Player value must be a non-negative whole number.")

        return {
            "name": name,
            "overall": overall,
            "position": position,
            "country": country,
            "team": team,
            "rarity": rarity,
            "value": value,
            "stats": normalized_stats,
            "image_path": image_path,
            "allowed_lineup_positions": normalized_allowed_positions,
        }

    @staticmethod
    def _player_from_row(row: sqlite3.Row) -> Player:
        return Player(
            id=int(row["id"]),
            name=str(row["name"]),
            overall=int(row["overall"]),
            position=str(row["position"]),
            country=str(row["country"]),
            team=str(row["team"]),
            rarity=str(row["rarity"]),
            value=int(row["value"]),
            stats=json.loads(row["stats_json"]),
            image_path=row["image_path"],
            allowed_lineup_positions=(
                tuple(json.loads(row["allowed_lineup_positions_json"]))
                if row["allowed_lineup_positions_json"] is not None
                else None
            ),
        )

    @staticmethod
    def _ensure_user(connection: sqlite3.Connection, user_id: str) -> None:
        connection.execute(
            "INSERT INTO users(user_id, balance) VALUES (?, 0) "
            "ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )

    def add_player(self, player: Mapping[str, Any]) -> Player:
        normalized = self._validate_player_values(player)
        with self._transaction() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO players
                        (name, overall, position, country, team, rarity, value,
                         stats_json, image_path, allowed_lineup_positions_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized["name"],
                        normalized["overall"],
                        normalized["position"],
                        normalized["country"],
                        normalized["team"],
                        normalized["rarity"],
                        normalized["value"],
                        json.dumps(normalized["stats"], separators=(",", ":")),
                        normalized["image_path"],
                        (
                            json.dumps(normalized["allowed_lineup_positions"])
                            if normalized["allowed_lineup_positions"] is not None
                            else None
                        ),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "players.name" in str(exc).lower() or "unique" in str(exc).lower():
                    raise PlayerAlreadyExists(normalized["name"]) from exc
                raise
            row = connection.execute(
                "SELECT * FROM players WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._player_from_row(row)

    def get_player(self, name: str) -> Player | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM players WHERE name = ? COLLATE NOCASE",
                (str(name).strip(),),
            ).fetchone()
        return self._player_from_row(row) if row else None

    def get_players(self, *, rarity: str | None = None) -> list[Player]:
        with self._read_connection() as connection:
            if rarity:
                rows = connection.execute(
                    "SELECT * FROM players WHERE rarity = ? ORDER BY name COLLATE NOCASE",
                    (rarity.strip().upper(),),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM players ORDER BY name COLLATE NOCASE"
                ).fetchall()
        return [self._player_from_row(row) for row in rows]

    def get_balance(self, user_id: str | int) -> int:
        normalized_id = self._validate_user_id(user_id)
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT balance FROM users WHERE user_id = ?", (normalized_id,)
            ).fetchone()
        return int(row["balance"]) if row else 0

    def set_balance(self, user_id: str | int, amount: int) -> int:
        normalized_id = self._validate_user_id(user_id)
        amount = self._validate_amount(amount, allow_zero=True)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO users(user_id, balance) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance
                """,
                (normalized_id, amount),
            )
        return amount

    def add_coins(self, user_id: str | int, amount: int) -> int:
        normalized_id = self._validate_user_id(user_id)
        amount = self._validate_amount(amount, allow_zero=True)
        with self._transaction() as connection:
            self._ensure_user(connection, normalized_id)
            row = connection.execute(
                "SELECT balance FROM users WHERE user_id = ?", (normalized_id,)
            ).fetchone()
            current = int(row["balance"])
            if current + amount > SQLITE_INT_MAX:
                raise InvalidAmount("That balance would exceed the supported maximum.")
            connection.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (amount, normalized_id),
            )
            return current + amount

    def start_draft_session(
        self,
        user_id: str | int,
        game_id: str,
        *,
        now: int | None = None,
    ) -> None:
        """Reserve a Draft and persist its per-user cooldown atomically."""
        normalized_id = self._validate_user_id(user_id)
        normalized_game_id = str(game_id).strip()
        if not normalized_game_id or len(normalized_game_id) > 100:
            raise InvalidAmount("A valid Draft session ID is required.")
        current_time = int(time.time() if now is None else now)

        with self._transaction() as connection:
            self._ensure_user(connection, normalized_id)
            active = connection.execute(
                """
                SELECT game_id, started_at FROM draft_sessions
                WHERE user_id = ? AND status = 'ACTIVE'
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (normalized_id,),
            ).fetchone()
            if active is not None:
                if current_time < int(active["started_at"]) + DRAFT_COOLDOWN_SECONDS:
                    raise DraftAlreadyActive("You already have a Draft in progress.")
                connection.execute(
                    """
                    UPDATE draft_sessions
                    SET status = 'CANCELLED', finished_at = ?
                    WHERE game_id = ? AND status = 'ACTIVE'
                    """,
                    (current_time, str(active["game_id"])),
                )

            cooldown = connection.execute(
                "SELECT last_started_at FROM draft_cooldowns WHERE user_id = ?",
                (normalized_id,),
            ).fetchone()
            if cooldown is not None:
                remaining = (
                    int(cooldown["last_started_at"])
                    + DRAFT_COOLDOWN_SECONDS
                    - current_time
                )
                if remaining > 0:
                    raise DraftCooldownActive(remaining)

            connection.execute(
                """
                INSERT INTO draft_sessions(game_id, user_id, status, started_at)
                VALUES (?, ?, 'ACTIVE', ?)
                """,
                (normalized_game_id, normalized_id, current_time),
            )
            connection.execute(
                """
                INSERT INTO draft_cooldowns(user_id, last_started_at)
                VALUES (?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET last_started_at = excluded.last_started_at
                """,
                (normalized_id, current_time),
            )

    def finish_draft_session(
        self,
        game_id: str,
        user_id: str | int,
        *,
        won: bool,
        result: Mapping[str, Any],
        now: int | None = None,
    ) -> tuple[bool, int]:
        """Persist a Draft result and award a win once in one transaction."""
        normalized_id = self._validate_user_id(user_id)
        if not isinstance(won, bool) or not isinstance(result, Mapping):
            raise InvalidAmount("A valid Draft result is required.")
        try:
            serialized_result = json.dumps(result, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise InvalidAmount("The Draft result could not be saved.") from exc
        normalized_game_id = str(game_id)
        current_time = int(time.time() if now is None else now)

        with self._transaction() as connection:
            session = connection.execute(
                "SELECT user_id, status FROM draft_sessions WHERE game_id = ?",
                (normalized_game_id,),
            ).fetchone()
            if session is None:
                raise DraftSessionNotFound("This Draft session no longer exists.")
            if str(session["user_id"]) != normalized_id:
                raise DraftSessionUnauthorized(
                    "Only the player who started this Draft can finish it."
                )

            self._ensure_user(connection, normalized_id)
            balance_row = connection.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (normalized_id,),
            ).fetchone()
            current_balance = int(balance_row["balance"])
            if str(session["status"]) != "ACTIVE":
                return False, current_balance

            reward = DRAFT_WIN_REWARD if won else 0
            if current_balance + reward > SQLITE_INT_MAX:
                raise InvalidAmount("Your balance cannot accept the Draft reward.")
            if reward:
                connection.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (reward, normalized_id),
                )
            connection.execute(
                """
                UPDATE draft_sessions
                SET status = 'COMPLETED', finished_at = ?, won = ?,
                    reward_amount = ?, result_json = ?
                WHERE game_id = ? AND user_id = ? AND status = 'ACTIVE'
                """,
                (
                    current_time,
                    int(won),
                    reward,
                    serialized_result,
                    normalized_game_id,
                    normalized_id,
                ),
            )
            return True, current_balance + reward

    def cancel_draft_session(
        self,
        game_id: str,
        user_id: str | int,
        *,
        now: int | None = None,
    ) -> bool:
        """Cancel an unfinished Draft without changing the player's balance."""
        normalized_id = self._validate_user_id(user_id)
        current_time = int(time.time() if now is None else now)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE draft_sessions
                SET status = 'CANCELLED', finished_at = ?
                WHERE game_id = ? AND user_id = ? AND status = 'ACTIVE'
                """,
                (current_time, str(game_id), normalized_id),
            )
            return cursor.rowcount == 1

    def get_leaderboard(self, limit: int = 10) -> list[tuple[str, int]]:
        if not 1 <= int(limit) <= 100:
            raise InvalidAmount("Leaderboard limit must be between 1 and 100.")
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT user_id, balance FROM users
                WHERE balance > 0
                ORDER BY balance DESC, user_id ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [(str(row["user_id"]), int(row["balance"])) for row in rows]

    def claim_gold_card(
        self,
        user_id: str | int,
        now: int | None = None,
        rng: Any | None = None,
    ) -> tuple[Player, int]:
        """Atomically award a weighted GOLD card and enforce a 30-minute cooldown."""
        normalized_id = self._validate_user_id(user_id)
        current_time = int(time.time() if now is None else now)
        chooser = rng if rng is not None else random.SystemRandom()
        with self._transaction() as connection:
            self._ensure_user(connection, normalized_id)
            candidates = connection.execute(
                "SELECT * FROM players WHERE rarity = 'GOLD' ORDER BY id"
            ).fetchall()
            if not candidates:
                raise NoEligibleCards("GOLD")

            row = connection.execute(
                "SELECT balance, last_claim_at FROM users WHERE user_id = ?",
                (normalized_id,),
            ).fetchone()
            last_claim = row["last_claim_at"]
            if last_claim is not None:
                next_claim_at = int(last_claim) + CLAIM_COOLDOWN_SECONDS
                if current_time < next_claim_at:
                    raise CooldownActive(next_claim_at - current_time)

            weights = [
                card_rating_weight(int(candidate["overall"]))
                for candidate in candidates
            ]
            selected = chooser.choices(candidates, weights=weights, k=1)[0]
            player_id = int(selected["id"])
            owned = connection.execute(
                "SELECT quantity FROM inventory WHERE user_id = ? AND player_id = ?",
                (normalized_id, player_id),
            ).fetchone()
            if owned and int(owned["quantity"]) >= SQLITE_INT_MAX:
                raise InvalidAmount("That inventory quantity is too large.")

            connection.execute(
                "UPDATE users SET last_claim_at = ? WHERE user_id = ?",
                (current_time, normalized_id),
            )
            connection.execute(
                """
                INSERT INTO inventory(user_id, player_id, quantity)
                VALUES (?, ?, 1)
                ON CONFLICT(user_id, player_id)
                DO UPDATE SET quantity = inventory.quantity + 1
                """,
                (normalized_id, player_id),
            )
            return (
                self._player_from_row(selected),
                current_time + CLAIM_COOLDOWN_SECONDS,
            )

    # Atomic auction settlement and bid operations.
    def _finish_auction_locked(
        self,
        connection: sqlite3.Connection,
        auction: sqlite3.Row,
        completed_at: int,
    ) -> Auction:
        auction_id = str(auction["auction_id"])
        seller_id = str(auction["seller_id"])
        player_id = int(auction["player_id"])
        held_bid = connection.execute(
            """
            SELECT * FROM auction_bids
            WHERE auction_id = ? AND status = 'HELD'
            """,
            (auction_id,),
        ).fetchone()

        if held_bid is None:
            self._add_inventory_copy(connection, seller_id, player_id)
            connection.execute(
                """
                UPDATE auctions
                SET status = 'UNSOLD', completed_at = ?,
                    seller_notified = 0, bidder_notified = 1
                WHERE auction_id = ? AND status = 'ACTIVE'
                """,
                (completed_at, auction_id),
            )
        else:
            amount = int(held_bid["amount"])
            bidder_id = str(held_bid["bidder_id"])
            if (
                auction["highest_bid"] is None
                or int(auction["highest_bid"]) != amount
                or str(auction["highest_bidder_id"]) != bidder_id
            ):
                raise RuntimeError(f"Auction {auction_id} has inconsistent held bid.")
            seller = connection.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (seller_id,),
            ).fetchone()
            if seller is None:
                raise RuntimeError(f"Auction {auction_id} seller account is missing.")
            seller_balance = int(seller["balance"])
            if seller_balance + amount > SQLITE_INT_MAX:
                raise InvalidAmount("The auction proceeds exceed the supported balance.")

            self._add_inventory_copy(connection, bidder_id, player_id)
            connection.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (amount, seller_id),
            )
            connection.execute(
                "UPDATE auction_bids SET status = 'WON' WHERE bid_id = ?",
                (str(held_bid["bid_id"]),),
            )
            connection.execute(
                """
                UPDATE auctions
                SET status = 'COMPLETED', completed_at = ?,
                    seller_notified = 0, bidder_notified = 0
                WHERE auction_id = ? AND status = 'ACTIVE'
                """,
                (completed_at, auction_id),
            )

        row = self._auction_row(connection, auction_id)
        if row is None:
            raise AuctionNotFound(auction_id)
        return self._auction_from_row(row)

    def place_auction_bid(
        self,
        auction_id: str,
        bidder_id: str | int,
        bidder_name: str,
        amount: int,
        interaction_id: str | int,
        *,
        now: int | None = None,
    ) -> Auction:
        normalized_auction_id = str(auction_id).strip()
        normalized_bidder_id = self._validate_user_id(bidder_id)
        normalized_bidder_name = str(bidder_name).strip()[:80] or normalized_bidder_id
        normalized_interaction_id = str(interaction_id).strip()
        amount = self._validate_amount(amount)
        current_time = int(time.time() if now is None else now)
        if (
            not normalized_auction_id
            or not normalized_interaction_id
            or len(normalized_interaction_id) > 128
        ):
            raise InvalidAmount("That bid request is invalid.")

        expired = False
        result: Auction | None = None
        with self._transaction() as connection:
            duplicate = connection.execute(
                "SELECT 1 FROM auction_bids WHERE interaction_id = ?",
                (normalized_interaction_id,),
            ).fetchone()
            if duplicate:
                raise AuctionDuplicateBid("That bid has already been processed.")

            auction = connection.execute(
                "SELECT * FROM auctions WHERE auction_id = ?",
                (normalized_auction_id,),
            ).fetchone()
            if auction is None:
                raise AuctionNotFound(normalized_auction_id)
            if str(auction["status"]) != "ACTIVE":
                raise AuctionNotActive("This auction is no longer active.")
            if current_time >= int(auction["end_time"]):
                self._finish_auction_locked(connection, auction, current_time)
                expired = True
            else:
                if str(auction["seller_id"]) == normalized_bidder_id:
                    raise AuctionSelfBid("You cannot bid on your own auction.")

                current_high = (
                    int(auction["highest_bid"])
                    if auction["highest_bid"] is not None
                    else None
                )
                minimum = (
                    current_high + 1
                    if current_high is not None
                    else int(auction["starting_price"])
                )
                if amount < minimum:
                    raise AuctionBidTooLow(minimum)

                held_bid = connection.execute(
                    """
                    SELECT * FROM auction_bids
                    WHERE auction_id = ? AND status = 'HELD'
                    """,
                    (normalized_auction_id,),
                ).fetchone()
                same_high_bidder = (
                    held_bid is not None
                    and str(held_bid["bidder_id"]) == normalized_bidder_id
                )
                if held_bid is not None and current_high is None:
                    raise RuntimeError(
                        f"Auction {normalized_auction_id} has a held bid without a price."
                    )
                debit_amount = (
                    amount - current_high
                    if same_high_bidder and current_high is not None
                    else amount
                )
                self._ensure_user(connection, normalized_bidder_id)
                bidder = connection.execute(
                    "SELECT balance FROM users WHERE user_id = ?",
                    (normalized_bidder_id,),
                ).fetchone()
                available = int(bidder["balance"])
                if available < debit_amount:
                    raise InsufficientFunds(
                        f"Need {debit_amount:,} available coins; "
                        f"you have {available:,}."
                    )
                updated = connection.execute(
                    """
                    UPDATE users SET balance = balance - ?
                    WHERE user_id = ? AND balance >= ?
                    """,
                    (debit_amount, normalized_bidder_id, debit_amount),
                )
                if updated.rowcount != 1:
                    raise InsufficientFunds(
                        "Your available balance changed before the bid was placed."
                    )

                if held_bid is not None:
                    previous_bidder_id = str(held_bid["bidder_id"])
                    previous_amount = int(held_bid["amount"])
                    if same_high_bidder:
                        connection.execute(
                            "UPDATE auction_bids SET status = 'SUPERSEDED' "
                            "WHERE bid_id = ? AND status = 'HELD'",
                            (str(held_bid["bid_id"]),),
                        )
                    else:
                        previous_user = connection.execute(
                            "SELECT balance FROM users WHERE user_id = ?",
                            (previous_bidder_id,),
                        ).fetchone()
                        if previous_user is None:
                            raise RuntimeError(
                                f"Auction {normalized_auction_id} bidder account is missing."
                            )
                        previous_balance = int(previous_user["balance"])
                        if previous_balance + previous_amount > SQLITE_INT_MAX:
                            raise InvalidAmount(
                                "The outbid funds cannot be returned safely right now."
                            )
                        connection.execute(
                            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                            (previous_amount, previous_bidder_id),
                        )
                        connection.execute(
                            "UPDATE auction_bids SET status = 'OUTBID' "
                            "WHERE bid_id = ? AND status = 'HELD'",
                            (str(held_bid["bid_id"]),),
                        )

                connection.execute(
                    """
                    INSERT INTO auction_bids
                        (bid_id, auction_id, interaction_id, bidder_id,
                         bidder_name, amount, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'HELD', ?)
                    """,
                    (
                        normalized_interaction_id,
                        normalized_auction_id,
                        normalized_interaction_id,
                        normalized_bidder_id,
                        normalized_bidder_name,
                        amount,
                        current_time,
                    ),
                )
                connection.execute(
                    """
                    UPDATE auctions
                    SET highest_bid = ?, highest_bidder_id = ?,
                        highest_bidder_name = ?
                    WHERE auction_id = ? AND status = 'ACTIVE'
                    """,
                    (
                        amount,
                        normalized_bidder_id,
                        normalized_bidder_name,
                        normalized_auction_id,
                    ),
                )
                row = self._auction_row(connection, normalized_auction_id)
                if row is None:
                    raise AuctionNotFound(normalized_auction_id)
                result = self._auction_from_row(row)

        if expired:
            raise AuctionExpired("This auction has expired and has been settled.")
        if result is None:
            raise RuntimeError("The bid was not recorded.")
        return result

    def finalize_expired_auctions(self, *, now: int | None = None) -> list[Auction]:
        current_time = int(time.time() if now is None else now)
        completed: list[Auction] = []
        with self._transaction() as connection:
            expired = connection.execute(
                """
                SELECT * FROM auctions
                WHERE status = 'ACTIVE' AND end_time <= ?
                ORDER BY end_time ASC
                """,
                (current_time,),
            ).fetchall()
            for auction in expired:
                completed.append(
                    self._finish_auction_locked(connection, auction, current_time)
                )
        return completed

    def list_auctions_pending_notification(self) -> list[Auction]:
        with self._read_connection() as connection:
            rows = connection.execute(
                AUCTION_JOIN_SQL
                + """
                WHERE (a.status = 'UNSOLD' AND a.seller_notified = 0)
                   OR (a.status = 'COMPLETED'
                       AND (a.seller_notified = 0 OR a.bidder_notified = 0))
                ORDER BY a.completed_at ASC
                """
            ).fetchall()
        return [self._auction_from_row(row) for row in rows]

    def mark_auction_notification_attempted(
        self,
        auction_id: str,
        recipient: str,
    ) -> None:
        column = {
            "seller": "seller_notified",
            "bidder": "bidder_notified",
        }.get(recipient)
        if column is None:
            raise InvalidAmount("Unknown auction notification recipient.")
        with self._transaction() as connection:
            connection.execute(
                f"UPDATE auctions SET {column} = 1 "
                "WHERE auction_id = ? AND status IN ('COMPLETED', 'UNSOLD')",
                (str(auction_id).strip(),),
            )

    def add_inventory(
        self, user_id: str | int, player_name: str, quantity: int = 1
    ) -> int:
        normalized_id = self._validate_user_id(user_id)
        quantity = self._validate_amount(quantity)
        with self._transaction() as connection:
            player = connection.execute(
                "SELECT id FROM players WHERE name = ? COLLATE NOCASE",
                (str(player_name).strip(),),
            ).fetchone()
            if not player:
                raise PlayerNotFound(player_name)
            self._ensure_user(connection, normalized_id)
            current = connection.execute(
                "SELECT quantity FROM inventory WHERE user_id = ? AND player_id = ?",
                (normalized_id, int(player["id"])),
            ).fetchone()
            current_quantity = int(current["quantity"]) if current else 0
            if current_quantity + quantity > SQLITE_INT_MAX:
                raise InvalidAmount("That inventory quantity is too large.")
            connection.execute(
                """
                INSERT INTO inventory(user_id, player_id, quantity)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, player_id)
                DO UPDATE SET quantity = inventory.quantity + excluded.quantity
                """,
                (normalized_id, int(player["id"]), quantity),
            )
            return current_quantity + quantity

    def get_inventory(self, user_id: str | int) -> list[InventoryEntry]:
        normalized_id = self._validate_user_id(user_id)
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT p.*, i.quantity
                FROM inventory AS i
                JOIN players AS p ON p.id = i.player_id
                WHERE i.user_id = ?
                ORDER BY p.rarity, p.name COLLATE NOCASE
                """,
                (normalized_id,),
            ).fetchall()
        return [
            InventoryEntry(self._player_from_row(row), int(row["quantity"]))
            for row in rows
        ]

    def get_collection(self, user_id: str | int) -> list[InventoryEntry]:
        normalized_id = self._validate_user_id(user_id)
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT p.*, COALESCE(i.quantity, 0) AS quantity
                FROM players AS p
                LEFT JOIN inventory AS i
                    ON i.player_id = p.id AND i.user_id = ?
                ORDER BY p.rarity, p.name COLLATE NOCASE
                """,
                (normalized_id,),
            ).fetchall()
        return [
            InventoryEntry(self._player_from_row(row), int(row["quantity"]))
            for row in rows
        ]

    @staticmethod
    def _starting_lineup_from_connection(
        connection: sqlite3.Connection,
        user_id: str,
    ) -> StartingLineup:
        formation_row = connection.execute(
            "SELECT formation FROM team_settings_v2 WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        formation = (
            str(formation_row["formation"]) if formation_row else DEFAULT_FORMATION
        )
        active_positions = FORMATION_STARTERS[formation]
        placeholders = ", ".join("?" for _ in active_positions)
        rows = connection.execute(
            f"""
            SELECT team_starters.position AS lineup_position, players.*
            FROM team_starters
            JOIN players ON players.id = team_starters.player_id
            JOIN inventory
                ON inventory.user_id = team_starters.user_id
                AND inventory.player_id = team_starters.player_id
            WHERE team_starters.user_id = ?
                AND team_starters.position IN ({placeholders})
                AND inventory.quantity > 0
            """,
            (user_id, *active_positions),
        ).fetchall()

        valid_by_position: dict[str, Player] = {}
        for row in rows:
            position = str(row["lineup_position"])
            player = Database._player_from_row(row)
            allowed_positions = player.allowed_lineup_positions
            if (
                position in active_positions
                and card_position_role(player.position)
                == POSITION_ROLE_BY_SLOT.get(position)
                and (allowed_positions is None or position in allowed_positions)
            ):
                valid_by_position[position] = player

        starters = tuple(
            {
                "slot": position,
                "name": valid_by_position[position].name,
                "overall": valid_by_position[position].overall,
                "position": valid_by_position[position].position,
            }
            for position in active_positions
            if position in valid_by_position
        )
        missing = tuple(
            position for position in active_positions if position not in valid_by_position
        )
        return StartingLineup(
            formation=formation,
            starters=starters,
            missing_slots=missing,
        )

    def get_starting_lineup(self, user_id: str | int) -> StartingLineup:
        """Return a validated snapshot of only the current formation's starters."""
        normalized_id = self._validate_user_id(user_id)
        with self._read_connection() as connection:
            return self._starting_lineup_from_connection(connection, normalized_id)

    @staticmethod
    def _validate_lineup_position(position: str) -> str:
        normalized = str(position).strip().upper()
        normalized = {
            "LW": "WING1",
            "RW": "WING2",
            "C": "CM1",
        }.get(normalized, normalized)
        if normalized not in LINEUP_POSITIONS:
            raise InvalidLineupPosition(
                f"Position must be one of: {', '.join(LINEUP_POSITIONS)}."
            )
        return normalized

    def get_lineup(self, user_id: str | int) -> dict[str, Player]:
        """Return only the active three starters and the two substitute slots."""
        normalized_id = self._validate_user_id(user_id)
        active_positions = FORMATION_STARTERS[self.get_formation(normalized_id)]
        placeholders = ", ".join("?" for _ in active_positions)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT team_starters.position AS lineup_position, players.*
                FROM team_starters
                JOIN players ON players.id = team_starters.player_id
                JOIN inventory
                    ON inventory.user_id = team_starters.user_id
                    AND inventory.player_id = team_starters.player_id
                WHERE team_starters.user_id = ?
                    AND team_starters.position IN ({placeholders})
                    AND inventory.quantity > 0
                UNION ALL
                SELECT substitute_slots.position AS lineup_position, players.*
                FROM substitute_slots
                JOIN players ON players.id = substitute_slots.player_id
                JOIN inventory
                    ON inventory.user_id = substitute_slots.user_id
                    AND inventory.player_id = substitute_slots.player_id
                WHERE substitute_slots.user_id = ?
                    AND inventory.quantity > 0
                """,
                (normalized_id, *active_positions, normalized_id),
            ).fetchall()
        result: dict[str, Player] = {}
        for row in rows:
            position = str(row["lineup_position"])
            player = self._player_from_row(row)
            if position in SUBSTITUTE_POSITIONS and (
                player.allowed_lineup_positions is None
                or position in player.allowed_lineup_positions
            ):
                result[position] = player
            elif (
                position in active_positions
                and card_position_role(player.position)
                == POSITION_ROLE_BY_SLOT.get(position)
                and (
                    player.allowed_lineup_positions is None
                    or position in player.allowed_lineup_positions
                )
            ):
                result[position] = player
        return result

    @staticmethod
    def _match_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        record["lineups"] = (
            json.loads(record.pop("lineups_json"))
            if record.get("lineups_json") is not None
            else None
        )
        record["result"] = (
            json.loads(record.pop("result_json"))
            if record.get("result_json") is not None
            else None
        )
        return record

    @staticmethod
    def _release_match_reservations(
        connection: sqlite3.Connection,
        match_id: str,
    ) -> None:
        connection.execute(
            "DELETE FROM active_match_users WHERE match_id = ?",
            (match_id,),
        )

    def create_match_invitation(
        self,
        match_id: str,
        challenger_id: str | int,
        opponent_id: str | int,
        challenger_name: str,
        opponent_name: str,
        guild_id: str | int,
        channel_id: str | int,
        *,
        now: int | float | None = None,
        expires_at: int | float | None = None,
    ) -> dict[str, Any]:
        """Reserve both participants and persist a single-use match invitation."""
        challenger = self._validate_user_id(challenger_id)
        opponent = self._validate_user_id(opponent_id)
        if challenger == opponent:
            raise InvalidMatchParticipant("You cannot challenge yourself.")

        normalized_match_id = str(match_id).strip()
        if not normalized_match_id or len(normalized_match_id) > 100:
            raise ValueError("A valid match ID is required.")
        current_time = float(time.time() if now is None else now)
        expiration = (
            current_time + MATCH_INVITATION_SECONDS
            if expires_at is None
            else float(expires_at)
        )
        if expiration <= current_time:
            raise ValueError("The invitation expiration must be in the future.")

        with self._transaction() as connection:
            for participant_id in (challenger, opponent):
                expired_rows = connection.execute(
                    """
                    SELECT matches.match_id
                    FROM matches
                    JOIN active_match_users
                        ON active_match_users.match_id = matches.match_id
                    WHERE active_match_users.user_id = ?
                        AND matches.status = 'PENDING'
                        AND matches.expires_at <= ?
                    """,
                    (participant_id, current_time),
                ).fetchall()
                for expired_row in expired_rows:
                    expired_id = str(expired_row["match_id"])
                    connection.execute(
                        """
                        UPDATE matches
                        SET status = 'EXPIRED', finished_at = ?
                        WHERE match_id = ? AND status = 'PENDING'
                        """,
                        (current_time, expired_id),
                    )
                    self._release_match_reservations(connection, expired_id)

            active = connection.execute(
                """
                SELECT user_id FROM active_match_users
                WHERE user_id IN (?, ?)
                """,
                (challenger, opponent),
            ).fetchall()
            if active:
                active_ids = ", ".join(str(row["user_id"]) for row in active)
                raise MatchAlreadyActive(
                    f"At least one participant already has a pending invitation "
                    f"or active match ({active_ids})."
                )

            connection.execute(
                """
                INSERT INTO matches(
                    match_id, challenger_id, opponent_id,
                    challenger_name, opponent_name, guild_id, channel_id,
                    status, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    normalized_match_id,
                    challenger,
                    opponent,
                    str(challenger_name).strip()[:100] or challenger,
                    str(opponent_name).strip()[:100] or opponent,
                    self._validate_user_id(guild_id),
                    self._validate_user_id(channel_id),
                    current_time,
                    expiration,
                ),
            )
            connection.executemany(
                "INSERT INTO active_match_users(user_id, match_id) VALUES (?, ?)",
                ((challenger, normalized_match_id), (opponent, normalized_match_id)),
            )
            row = connection.execute(
                "SELECT * FROM matches WHERE match_id = ?",
                (normalized_match_id,),
            ).fetchone()
        result = self._match_from_row(row)
        assert result is not None
        return result

    def set_match_invitation_message(
        self,
        match_id: str,
        message_id: str | int,
        *,
        now: int | float | None = None,
    ) -> bool:
        sent_at = float(time.time() if now is None else now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT status FROM matches WHERE match_id = ?
                """,
                (str(match_id),),
            ).fetchone()
            if row is None or str(row["status"]) not in {"PENDING", "ACTIVE"}:
                return False
            if str(row["status"]) == "ACTIVE":
                connection.execute(
                    "UPDATE matches SET invite_message_id = ? WHERE match_id = ?",
                    (self._validate_user_id(message_id), str(match_id)),
                )
            else:
                connection.execute(
                    """
                    UPDATE matches
                    SET invite_message_id = ?, created_at = ?, expires_at = ?
                    WHERE match_id = ? AND status = 'PENDING'
                    """,
                    (
                        self._validate_user_id(message_id),
                        sent_at,
                        sent_at + MATCH_INVITATION_SECONDS,
                        str(match_id),
                    ),
                )
            return True

    def get_match(self, match_id: str) -> dict[str, Any] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM matches WHERE match_id = ?",
                (str(match_id),),
            ).fetchone()
        return self._match_from_row(row)

    def get_pending_match_invitations(self) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM matches
                WHERE status = 'PENDING'
                ORDER BY created_at, match_id
                """
            ).fetchall()
        return [self._match_from_row(row) for row in rows if row is not None]

    def get_active_match_for_user(
        self,
        user_id: str | int,
    ) -> dict[str, Any] | None:
        normalized_id = self._validate_user_id(user_id)
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT matches.*
                FROM active_match_users
                JOIN matches ON matches.match_id = active_match_users.match_id
                WHERE active_match_users.user_id = ?
                """,
                (normalized_id,),
            ).fetchone()
        return self._match_from_row(row)

    def accept_match_invitation(
        self,
        match_id: str,
        accepter_id: str | int,
        *,
        now: int | float | None = None,
    ) -> dict[str, Any]:
        """Atomically accept an invitation and snapshot both valid lineups."""
        normalized_id = self._validate_user_id(accepter_id)
        current_time = float(time.time() if now is None else now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM matches WHERE match_id = ?",
                (str(match_id),),
            ).fetchone()
            if row is None:
                raise MatchNotFound("This match invitation no longer exists.")
            if str(row["opponent_id"]) != normalized_id:
                raise MatchActionUnauthorized(
                    "Only the challenged player can accept this match."
                )
            if str(row["status"]) != "PENDING":
                raise MatchNotPending("This invitation is no longer pending.")

            if float(row["expires_at"]) <= current_time:
                connection.execute(
                    """
                    UPDATE matches SET status = 'EXPIRED', finished_at = ?
                    WHERE match_id = ? AND status = 'PENDING'
                    """,
                    (current_time, str(match_id)),
                )
                self._release_match_reservations(connection, str(match_id))
                expired = connection.execute(
                    "SELECT * FROM matches WHERE match_id = ?",
                    (str(match_id),),
                ).fetchone()
                result = self._match_from_row(expired)
                assert result is not None
                return result

            lineups_by_user: dict[str, dict[str, Any]] = {}
            lineup_errors: list[str] = []
            participants = (
                (str(row["challenger_id"]), str(row["challenger_name"])),
                (str(row["opponent_id"]), str(row["opponent_name"])),
            )
            for participant_id, participant_name in participants:
                lineup = self._starting_lineup_from_connection(
                    connection,
                    participant_id,
                )
                lineups_by_user[participant_id] = {
                    "formation": lineup.formation,
                    "starters": list(lineup.starters),
                    "missing_slots": list(lineup.missing_slots),
                }
                if not lineup.valid:
                    lineup_errors.append(
                        f"{participant_name} is missing valid starter slot(s): "
                        f"{', '.join(lineup.missing_slots)}"
                    )

            lineups_json = json.dumps(lineups_by_user, separators=(",", ":"))
            if lineup_errors:
                reason = "Invalid starting lineup. " + "; ".join(lineup_errors) + "."
                connection.execute(
                    """
                    UPDATE matches
                    SET status = 'CANCELLED', finished_at = ?,
                        lineups_json = ?, cancel_reason = ?
                    WHERE match_id = ? AND status = 'PENDING'
                    """,
                    (current_time, lineups_json, reason, str(match_id)),
                )
                self._release_match_reservations(connection, str(match_id))
            else:
                connection.execute(
                    """
                    UPDATE matches
                    SET status = 'ACTIVE', started_at = ?, lineups_json = ?
                    WHERE match_id = ? AND status = 'PENDING'
                    """,
                    (current_time, lineups_json, str(match_id)),
                )

            updated = connection.execute(
                "SELECT * FROM matches WHERE match_id = ?",
                (str(match_id),),
            ).fetchone()
        result = self._match_from_row(updated)
        assert result is not None
        return result

    def decline_match_invitation(
        self,
        match_id: str,
        decliner_id: str | int,
        *,
        now: int | float | None = None,
    ) -> dict[str, Any]:
        normalized_id = self._validate_user_id(decliner_id)
        current_time = float(time.time() if now is None else now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM matches WHERE match_id = ?",
                (str(match_id),),
            ).fetchone()
            if row is None:
                raise MatchNotFound("This match invitation no longer exists.")
            if str(row["opponent_id"]) != normalized_id:
                raise MatchActionUnauthorized(
                    "Only the challenged player can decline this match."
                )
            if str(row["status"]) != "PENDING":
                raise MatchNotPending("This invitation is no longer pending.")

            if float(row["expires_at"]) <= current_time:
                status = "EXPIRED"
            else:
                status = "DECLINED"
            connection.execute(
                """
                UPDATE matches SET status = ?, finished_at = ?
                WHERE match_id = ? AND status = 'PENDING'
                """,
                (status, current_time, str(match_id)),
            )
            self._release_match_reservations(connection, str(match_id))
            updated = connection.execute(
                "SELECT * FROM matches WHERE match_id = ?",
                (str(match_id),),
            ).fetchone()
        result = self._match_from_row(updated)
        assert result is not None
        return result

    def expire_match_invitation(
        self,
        match_id: str,
        *,
        now: int | float | None = None,
    ) -> dict[str, Any] | None:
        current_time = float(time.time() if now is None else now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM matches
                WHERE match_id = ? AND status = 'PENDING' AND expires_at <= ?
                """,
                (str(match_id), current_time),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE matches SET status = 'EXPIRED', finished_at = ?
                WHERE match_id = ? AND status = 'PENDING'
                """,
                (current_time, str(match_id)),
            )
            self._release_match_reservations(connection, str(match_id))
            updated = connection.execute(
                "SELECT * FROM matches WHERE match_id = ?",
                (str(match_id),),
            ).fetchone()
        return self._match_from_row(updated)

    def cancel_match(
        self,
        match_id: str,
        reason: str,
        *,
        now: int | float | None = None,
    ) -> dict[str, Any] | None:
        current_time = float(time.time() if now is None else now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM matches
                WHERE match_id = ? AND status IN ('PENDING', 'ACTIVE')
                """,
                (str(match_id),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE matches SET status = 'CANCELLED', finished_at = ?,
                    cancel_reason = ?
                WHERE match_id = ? AND status IN ('PENDING', 'ACTIVE')
                """,
                (current_time, str(reason)[:1000], str(match_id)),
            )
            self._release_match_reservations(connection, str(match_id))
            updated = connection.execute(
                "SELECT * FROM matches WHERE match_id = ?",
                (str(match_id),),
            ).fetchone()
        return self._match_from_row(updated)

    def cancel_match_for_user(
        self,
        user_id: str | int,
        reason: str,
        *,
        guild_id: str | int | None = None,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        normalized_id = self._validate_user_id(user_id)
        current_time = int(time.time() if now is None else now)
        with self._transaction() as connection:
            if guild_id is None:
                row = connection.execute(
                    """
                    SELECT matches.* FROM active_match_users
                    JOIN matches ON matches.match_id = active_match_users.match_id
                    WHERE active_match_users.user_id = ?
                    """,
                    (normalized_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT matches.* FROM active_match_users
                    JOIN matches ON matches.match_id = active_match_users.match_id
                    WHERE active_match_users.user_id = ? AND matches.guild_id = ?
                    """,
                    (normalized_id, self._validate_user_id(guild_id)),
                ).fetchone()
            if row is None:
                return None
            match_id = str(row["match_id"])
            connection.execute(
                """
                UPDATE matches SET status = 'CANCELLED', finished_at = ?,
                    cancel_reason = ?
                WHERE match_id = ? AND status IN ('PENDING', 'ACTIVE')
                """,
                (current_time, str(reason)[:1000], match_id),
            )
            self._release_match_reservations(connection, match_id)
            updated = connection.execute(
                "SELECT * FROM matches WHERE match_id = ?",
                (match_id,),
            ).fetchone()
        return self._match_from_row(updated)

    def cancel_interrupted_matches(
        self,
        reason: str = "The bot restarted before this match completed.",
        *,
        now: int | None = None,
    ) -> list[dict[str, Any]]:
        """Cancel in-progress games after a process restart; never award coins."""
        current_time = int(time.time() if now is None else now)
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM matches
                WHERE status = 'ACTIVE'
                    OR (status = 'PENDING' AND invite_message_id IS NULL)
                ORDER BY created_at, match_id
                """
            ).fetchall()
            match_ids = [str(row["match_id"]) for row in rows]
            for match_id in match_ids:
                connection.execute(
                    """
                    UPDATE matches SET status = 'CANCELLED', finished_at = ?,
                        cancel_reason = ?
                    WHERE match_id = ? AND status IN ('PENDING', 'ACTIVE')
                    """,
                    (current_time, str(reason)[:1000], match_id),
                )
                self._release_match_reservations(connection, match_id)
            updated_rows = [
                connection.execute(
                    "SELECT * FROM matches WHERE match_id = ?",
                    (match_id,),
                ).fetchone()
                for match_id in match_ids
            ]
        return [
            self._match_from_row(row)
            for row in updated_rows
            if row is not None
        ]

    def complete_match(
        self,
        match_id: str,
        winner_id: str | int,
        result: Mapping[str, Any],
        *,
        now: int | None = None,
    ) -> bool:
        """Award the fixed prize and persist the result exactly once."""
        normalized_winner = self._validate_user_id(winner_id)
        current_time = int(time.time() if now is None else now)
        serialized_result = json.dumps(result, separators=(",", ":"))
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM matches WHERE match_id = ?",
                (str(match_id),),
            ).fetchone()
            if row is None:
                raise MatchNotFound("This match no longer exists.")
            if str(row["status"]) == "COMPLETED":
                return False
            if str(row["status"]) != "ACTIVE":
                return False
            if normalized_winner not in {
                str(row["challenger_id"]),
                str(row["opponent_id"]),
            }:
                raise InvalidMatchParticipant(
                    "The winner must be one of the match participants."
                )
            try:
                home_score = int(result["home_score"])
                away_score = int(result["away_score"])
                result_winner = str(result["winner_id"])
                goals = result["goals"]
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidMatchResult(
                    "A completed match needs a score, goals, and winner."
                ) from exc
            if (
                home_score < 0
                or away_score < 0
                or home_score == away_score
                or not isinstance(goals, list)
                or not goals
            ):
                raise InvalidMatchResult(
                    "A completed match must include at least one goal and a winner."
                )
            expected_winner = (
                str(row["challenger_id"])
                if home_score > away_score
                else str(row["opponent_id"])
            )
            if normalized_winner != expected_winner or result_winner != expected_winner:
                raise InvalidMatchResult(
                    "The recorded winner must match the final score."
                )

            self._ensure_user(connection, normalized_winner)
            balance_row = connection.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (normalized_winner,),
            ).fetchone()
            balance = int(balance_row["balance"])
            if balance + MATCH_REWARD > SQLITE_INT_MAX:
                raise InvalidAmount(
                    "The winner's balance cannot accept the match reward."
                )
            connection.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (MATCH_REWARD, normalized_winner),
            )
            connection.execute(
                """
                UPDATE matches
                SET status = 'COMPLETED', winner_id = ?, reward_amount = ?,
                    finished_at = ?, result_json = ?, announced_at = NULL
                WHERE match_id = ? AND status = 'ACTIVE'
                """,
                (
                    normalized_winner,
                    MATCH_REWARD,
                    current_time,
                    serialized_result,
                    str(match_id),
                ),
            )
            self._release_match_reservations(connection, str(match_id))
            return True

    def get_unannounced_match_results(self) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM matches
                WHERE status = 'COMPLETED' AND announced_at IS NULL
                ORDER BY finished_at, match_id
                """
            ).fetchall()
        return [self._match_from_row(row) for row in rows if row is not None]

    def mark_match_result_announced(
        self,
        match_id: str,
        *,
        now: int | None = None,
    ) -> bool:
        current_time = int(time.time() if now is None else now)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE matches SET announced_at = ?
                WHERE match_id = ? AND status = 'COMPLETED'
                    AND announced_at IS NULL
                """,
                (current_time, str(match_id)),
            )
            return cursor.rowcount == 1

    def get_lineup_migration_notices(self, user_id: str | int) -> list[str]:
        normalized_id = self._validate_user_id(user_id)
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT notice FROM team_migration_notices
                WHERE user_id = ? ORDER BY notice
                """,
                (normalized_id,),
            ).fetchall()
        return [str(row["notice"]) for row in rows]

    def set_lineup_position(
        self,
        user_id: str | int,
        position: str,
        player_name: str | int,
    ) -> Player:
        normalized_id = self._validate_user_id(user_id)
        normalized_position = self._validate_lineup_position(position)
        lookup = str(player_name).strip()
        if not lookup:
            raise PlayerNotFound(player_name)

        with self._transaction() as connection:
            self._ensure_user(connection, normalized_id)
            formation_row = connection.execute(
                "SELECT formation FROM team_settings_v2 WHERE user_id = ?",
                (normalized_id,),
            ).fetchone()
            formation = (
                str(formation_row["formation"])
                if formation_row
                else DEFAULT_FORMATION
            )
            if (
                normalized_position in STARTING_LINEUP_POSITIONS
                and normalized_position
                not in FORMATION_STARTERS[formation]
            ):
                raise InvalidLineupPosition(
                    f"{normalized_position} is not a starter slot in the "
                    f"{formation} formation. Active positions: "
                    f"{', '.join(FORMATION_STARTERS[formation])}."
                )

            if lookup.isdecimal():
                player = connection.execute(
                    "SELECT * FROM players WHERE id = ?",
                    (int(lookup),),
                ).fetchone()
            else:
                player = connection.execute(
                    "SELECT * FROM players WHERE name = ? COLLATE NOCASE",
                    (lookup,),
                ).fetchone()
            if not player:
                raise PlayerNotFound(lookup)

            player_id = int(player["id"])
            if player["allowed_lineup_positions_json"] is not None:
                allowed_positions = tuple(
                    json.loads(player["allowed_lineup_positions_json"])
                )
                if normalized_position not in allowed_positions:
                    label = format_card_label(
                        player["name"],
                        player["overall"],
                        player["rarity"],
                    )
                    raise InvalidLineupPosition(
                        f"**{label}** can only be assigned to "
                        f"{', '.join(allowed_positions)}."
                    )
            if normalized_position in STARTING_LINEUP_POSITIONS:
                expected_role = POSITION_ROLE_BY_SLOT[normalized_position]
                actual_role = card_position_role(str(player["position"]))
                if actual_role != expected_role:
                    role_label = {
                        "WING": "Wing",
                        "CM": "CM",
                        "GK": "GK",
                    }[expected_role]
                    raise InvalidLineupPosition(
                        f"Only {role_label} cards can occupy {normalized_position}. "
                        f"**{format_card_label(player['name'], player['overall'], player['rarity'])}** "
                        f"has position **{player['position']}**."
                    )

            owned = connection.execute(
                """
                SELECT quantity FROM inventory
                WHERE user_id = ? AND player_id = ?
                """,
                (normalized_id, player_id),
            ).fetchone()
            if not owned or int(owned["quantity"]) < 1:
                raise CardNotOwned(
                    f"You must own **{format_card_label(player['name'], player['overall'], player['rarity'])}** before adding it to your team."
                )

            existing_slot = connection.execute(
                """
                SELECT position FROM team_starters
                WHERE user_id = ? AND player_id = ?
                UNION ALL
                SELECT position FROM substitute_slots
                WHERE user_id = ? AND player_id = ?
                LIMIT 1
                """,
                (normalized_id, player_id, normalized_id, player_id),
            ).fetchone()
            if existing_slot and str(existing_slot["position"]) != normalized_position:
                raise DuplicateLineupPlayer(
                    f"**{format_card_label(player['name'], player['overall'], player['rarity'])}** "
                    f"is already assigned to {existing_slot['position']}. "
                    "Clear that slot first."
                )

            target_table = (
                "substitute_slots"
                if normalized_position in SUBSTITUTE_POSITIONS
                else "team_starters"
            )
            connection.execute(
                f"""
                INSERT INTO {target_table}(user_id, position, player_id)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, position)
                DO UPDATE SET player_id = excluded.player_id
                """,
                (normalized_id, normalized_position, player_id),
            )
            connection.execute(
                "DELETE FROM team_migration_notices WHERE user_id = ?",
                (normalized_id,),
            )
            return self._player_from_row(player)

    def clear_lineup_position(
        self,
        user_id: str | int,
        position: str,
    ) -> bool:
        normalized_id = self._validate_user_id(user_id)
        normalized_position = self._validate_lineup_position(position)
        target_table = (
            "substitute_slots"
            if normalized_position in SUBSTITUTE_POSITIONS
            else "team_starters"
        )
        with self._transaction() as connection:
            result = connection.execute(
                f"DELETE FROM {target_table} WHERE user_id = ? AND position = ?",
                (normalized_id, normalized_position),
            )
            if result.rowcount:
                connection.execute(
                    "DELETE FROM team_migration_notices WHERE user_id = ?",
                    (normalized_id,),
                )
            return result.rowcount > 0

    def get_formation(self, user_id: str | int) -> str:
        normalized_id = self._validate_user_id(user_id)
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT formation FROM team_settings_v2 WHERE user_id = ?",
                (normalized_id,),
            ).fetchone()
        return str(row["formation"]) if row else DEFAULT_FORMATION

    def set_formation_with_report(
        self,
        user_id: str | int,
        formation: str,
    ) -> FormationChange:
        normalized_id = self._validate_user_id(user_id)
        choices = {value.casefold(): value for value in TEAM_FORMATIONS}
        normalized_formation = choices.get(str(formation).strip().casefold())
        if normalized_formation is None:
            raise InvalidFormation(
                f"Formation must be one of: {', '.join(TEAM_FORMATIONS)}."
            )

        new_positions = FORMATION_STARTERS[normalized_formation]
        with self._transaction() as connection:
            self._ensure_user(connection, normalized_id)
            rows = connection.execute(
                """
                SELECT team_starters.position AS assigned_position, players.*
                FROM team_starters
                JOIN players ON players.id = team_starters.player_id
                WHERE team_starters.user_id = ?
                """,
                (normalized_id,),
            ).fetchall()
            assigned = {
                str(row["assigned_position"]): self._player_from_row(row)
                for row in rows
            }
            retained: dict[str, Player] = {}
            used_player_ids: set[int] = set()

            for position in new_positions:
                player = assigned.get(position)
                if (
                    player is not None
                    and card_position_role(player.position)
                    == POSITION_ROLE_BY_SLOT[position]
                ):
                    retained[position] = player
                    used_player_ids.add(player.id)

            moved: list[tuple[str, str, Player]] = []
            unassigned: list[tuple[str, Player]] = []
            for old_position in STARTING_LINEUP_POSITIONS:
                player = assigned.get(old_position)
                if player is None or player.id in used_player_ids:
                    continue
                role = card_position_role(player.position)
                destination = next(
                    (
                        candidate
                        for candidate in new_positions
                        if candidate not in retained
                        and POSITION_ROLE_BY_SLOT[candidate] == role
                    ),
                    None,
                )
                if destination is None:
                    unassigned.append((old_position, player))
                else:
                    retained[destination] = player
                    used_player_ids.add(player.id)
                    moved.append((old_position, destination, player))

            connection.execute(
                "DELETE FROM team_starters WHERE user_id = ?",
                (normalized_id,),
            )
            for position, player in retained.items():
                connection.execute(
                    """
                    INSERT INTO team_starters(user_id, position, player_id)
                    VALUES (?, ?, ?)
                    """,
                    (normalized_id, position, player.id),
                )
            connection.execute(
                """
                INSERT INTO team_settings_v2(user_id, formation)
                VALUES (?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET formation = excluded.formation
                """,
                (normalized_id, normalized_formation),
            )
        return FormationChange(
            formation=normalized_formation,
            moved=tuple(moved),
            unassigned=tuple(unassigned),
        )

    def set_formation(self, user_id: str | int, formation: str) -> str:
        """Compatibility wrapper returning the selected formation name."""
        return self.set_formation_with_report(user_id, formation).formation

    def open_pack(
        self,
        user_id: str | int,
        rarity: str,
        cost: int,
        rng: Any | None = None,
        purchase_id: str | int | None = None,
    ) -> tuple[Player, int]:
        """Charge and award exactly one card of the requested rarity, atomically.

        When supplied, purchase_id makes a Discord component purchase idempotent
        across retries by returning the original award without charging again.
        """
        normalized_id = self._validate_user_id(user_id)
        expected_rarity = rarity.strip().upper()
        normalized_purchase_id: str | None = None
        if purchase_id is not None:
            normalized_purchase_id = str(purchase_id).strip()
            if not normalized_purchase_id or len(normalized_purchase_id) > 128:
                raise InvalidAmount("A valid pack purchase ID is required.")
        if expected_rarity not in {"GOLD", "TOTW"}:
            raise InvalidPlayer("Packs can only award GOLD or TOTW cards.")
        cost = self._validate_amount(cost)
        expected_cost = {
            "GOLD": GOLD_PACK_COST,
            "TOTW": TOTW_PACK_COST,
        }[expected_rarity]
        if cost != expected_cost:
            raise InvalidAmount(
                f"{expected_rarity} packs must cost exactly {expected_cost:,} coins."
            )
        chooser = rng if rng is not None else random.SystemRandom()

        with self._transaction() as connection:
            if normalized_purchase_id is not None:
                previous = connection.execute(
                    """
                    SELECT purchases.user_id, purchases.rarity, purchases.cost,
                           purchases.balance_after, players.*
                    FROM pack_purchases AS purchases
                    JOIN players ON players.id = purchases.player_id
                    WHERE purchases.interaction_id = ?
                    """,
                    (normalized_purchase_id,),
                ).fetchone()
                if previous:
                    if (
                        str(previous["user_id"]) != normalized_id
                        or str(previous["rarity"]) != expected_rarity
                        or int(previous["cost"]) != cost
                    ):
                        raise InvalidAmount(
                            "That pack purchase ID has already been used."
                        )
                    return (
                        self._player_from_row(previous),
                        int(previous["balance_after"]),
                    )

            candidates = connection.execute(
                "SELECT * FROM players WHERE rarity = ? ORDER BY id",
                (expected_rarity,),
            ).fetchall()
            if not candidates:
                raise NoEligibleCards(expected_rarity)

            self._ensure_user(connection, normalized_id)
            user = connection.execute(
                "SELECT balance FROM users WHERE user_id = ?", (normalized_id,)
            ).fetchone()
            balance = int(user["balance"])
            if balance < cost:
                raise InsufficientFunds(
                    f"Need {cost:,} coins; balance is {balance:,}."
                )

            weights = [
                card_rating_weight(int(candidate["overall"]))
                for candidate in candidates
            ]
            selected = chooser.choices(candidates, weights=weights, k=1)[0]
            owned = connection.execute(
                "SELECT quantity FROM inventory WHERE user_id = ? AND player_id = ?",
                (normalized_id, int(selected["id"])),
            ).fetchone()
            if owned and int(owned["quantity"]) >= SQLITE_INT_MAX:
                raise InvalidAmount("That inventory quantity is too large.")
            update = connection.execute(
                "UPDATE users SET balance = balance - ? "
                "WHERE user_id = ? AND balance >= ?",
                (cost, normalized_id, cost),
            )
            if update.rowcount != 1:
                raise InsufficientFunds("The balance changed before the pack purchase.")
            connection.execute(
                """
                INSERT INTO inventory(user_id, player_id, quantity)
                VALUES (?, ?, 1)
                ON CONFLICT(user_id, player_id)
                DO UPDATE SET quantity = inventory.quantity + 1
                """,
                (normalized_id, int(selected["id"])),
            )
            remaining_balance = balance - cost
            if normalized_purchase_id is not None:
                connection.execute(
                    """
                    INSERT INTO pack_purchases
                        (interaction_id, user_id, player_id, rarity, cost,
                         balance_after, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_purchase_id,
                        normalized_id,
                        int(selected["id"]),
                        expected_rarity,
                        cost,
                        remaining_balance,
                        int(time.time()),
                    ),
                )
            return self._player_from_row(selected), remaining_balance

    def open_packs(
        self,
        user_id: str | int,
        rarity: str,
        cost: int,
        quantity: int,
        rng: Any | None = None,
        purchase_id: str | int | None = None,
    ) -> tuple[list[Player], int]:
        """Charge once and award a batch of same-rarity packs atomically."""
        normalized_id = self._validate_user_id(user_id)
        quantity = self._validate_amount(quantity)
        if quantity > MAX_PACK_QUANTITY:
            raise InvalidAmount(
                f"You can buy at most {MAX_PACK_QUANTITY} packs at once."
            )

        expected_rarity = rarity.strip().upper()
        if expected_rarity not in {"GOLD", "TOTW"}:
            raise InvalidPlayer("Packs can only award GOLD or TOTW cards.")
        cost = self._validate_amount(cost)
        expected_cost = {
            "GOLD": GOLD_PACK_COST,
            "TOTW": TOTW_PACK_COST,
        }[expected_rarity]
        if cost != expected_cost:
            raise InvalidAmount(
                f"{expected_rarity} packs must cost exactly {expected_cost:,} coins."
            )
        total_cost = cost * quantity
        if total_cost > SQLITE_INT_MAX:
            raise InvalidAmount("The total pack cost exceeds the supported maximum.")

        normalized_purchase_id: str | None = None
        batch_purchase_ids: list[str] = []
        if purchase_id is not None:
            normalized_purchase_id = str(purchase_id).strip()
            if not normalized_purchase_id:
                raise InvalidAmount("A valid pack purchase ID is required.")
            batch_purchase_ids = [
                f"{normalized_purchase_id}:batch:{index:02d}"
                for index in range(1, quantity + 1)
            ]
            if max(map(len, batch_purchase_ids)) > 128:
                raise InvalidAmount("A valid pack purchase ID is required.")

        chooser = rng if rng is not None else random.SystemRandom()
        with self._transaction() as connection:
            if normalized_purchase_id is not None:
                previous_single = connection.execute(
                    "SELECT 1 FROM pack_purchases WHERE interaction_id = ?",
                    (normalized_purchase_id,),
                ).fetchone()
                if previous_single:
                    raise InvalidAmount("That pack purchase ID has already been used.")

                batch_prefix = f"{normalized_purchase_id}:batch:"
                previous_rows = connection.execute(
                    """
                    SELECT purchases.interaction_id, purchases.user_id,
                           purchases.rarity, purchases.cost,
                           purchases.balance_after, players.*
                    FROM pack_purchases AS purchases
                    JOIN players ON players.id = purchases.player_id
                    WHERE substr(purchases.interaction_id, 1, ?) = ?
                    """,
                    (len(batch_prefix), batch_prefix),
                ).fetchall()
                if previous_rows:
                    previous_by_id = {
                        str(row["interaction_id"]): row for row in previous_rows
                    }
                    if set(previous_by_id) != set(batch_purchase_ids):
                        raise InvalidAmount(
                            "That pack purchase ID has already been used."
                        )
                    ordered_previous = [
                        previous_by_id[key] for key in batch_purchase_ids
                    ]
                    if any(
                        str(row["user_id"]) != normalized_id
                        or str(row["rarity"]) != expected_rarity
                        or int(row["cost"]) != cost
                        for row in ordered_previous
                    ):
                        raise InvalidAmount(
                            "That pack purchase ID has already been used."
                        )
                    return (
                        [self._player_from_row(row) for row in ordered_previous],
                        int(ordered_previous[-1]["balance_after"]),
                    )

            candidates = connection.execute(
                "SELECT * FROM players WHERE rarity = ? ORDER BY id",
                (expected_rarity,),
            ).fetchall()
            if not candidates:
                raise NoEligibleCards(expected_rarity)

            self._ensure_user(connection, normalized_id)
            user = connection.execute(
                "SELECT balance FROM users WHERE user_id = ?", (normalized_id,)
            ).fetchone()
            balance = int(user["balance"])
            if balance < total_cost:
                raise InsufficientFunds(
                    f"Need {total_cost:,} coins; balance is {balance:,}."
                )

            weights = [
                card_rating_weight(int(candidate["overall"]))
                for candidate in candidates
            ]
            selected_cards = [
                chooser.choices(candidates, weights=weights, k=1)[0]
                for _ in range(quantity)
            ]

            inventory_additions: dict[int, int] = {}
            for selected in selected_cards:
                player_id = int(selected["id"])
                inventory_additions[player_id] = (
                    inventory_additions.get(player_id, 0) + 1
                )
            for player_id, addition in inventory_additions.items():
                owned = connection.execute(
                    "SELECT quantity FROM inventory "
                    "WHERE user_id = ? AND player_id = ?",
                    (normalized_id, player_id),
                ).fetchone()
                if owned and int(owned["quantity"]) + addition > SQLITE_INT_MAX:
                    raise InvalidAmount("That inventory quantity is too large.")

            update = connection.execute(
                "UPDATE users SET balance = balance - ? "
                "WHERE user_id = ? AND balance >= ?",
                (total_cost, normalized_id, total_cost),
            )
            if update.rowcount != 1:
                raise InsufficientFunds(
                    "The balance changed before the pack purchase."
                )

            for player_id, addition in inventory_additions.items():
                connection.execute(
                    """
                    INSERT INTO inventory(user_id, player_id, quantity)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, player_id)
                    DO UPDATE SET quantity = inventory.quantity + excluded.quantity
                    """,
                    (normalized_id, player_id, addition),
                )

            if normalized_purchase_id is not None:
                remaining_balance = balance
                created_at = int(time.time())
                for purchase_key, selected in zip(
                    batch_purchase_ids, selected_cards
                ):
                    remaining_balance -= cost
                    connection.execute(
                        """
                        INSERT INTO pack_purchases
                            (interaction_id, user_id, player_id, rarity, cost,
                             balance_after, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            purchase_key,
                            normalized_id,
                            int(selected["id"]),
                            expected_rarity,
                            cost,
                            remaining_balance,
                            created_at,
                        ),
                    )

            return (
                [self._player_from_row(selected) for selected in selected_cards],
                balance - total_cost,
            )

    @staticmethod
    def _auction_from_row(row: sqlite3.Row) -> Auction:
        allowed_json = row["card_allowed_positions_json"]
        player = Player(
            id=int(row["card_id"]),
            name=str(row["card_name"]),
            overall=int(row["card_overall"]),
            position=str(row["card_position"]),
            country=str(row["card_country"]),
            team=str(row["card_team"]),
            rarity=str(row["card_rarity"]),
            value=int(row["card_value"]),
            stats=json.loads(row["card_stats_json"]),
            image_path=row["card_image_path"],
            allowed_lineup_positions=(
                tuple(json.loads(allowed_json)) if allowed_json is not None else None
            ),
        )
        return Auction(
            auction_id=str(row["auction_id"]),
            seller_id=str(row["seller_id"]),
            seller_name=str(row["seller_name"]),
            player=player,
            starting_price=int(row["starting_price"]),
            duration_seconds=int(row["duration_seconds"]),
            end_time=int(row["end_time"]),
            status=str(row["status"]),
            highest_bid=(
                int(row["highest_bid"]) if row["highest_bid"] is not None else None
            ),
            highest_bidder_id=(
                str(row["highest_bidder_id"])
                if row["highest_bidder_id"] is not None
                else None
            ),
            highest_bidder_name=(
                str(row["highest_bidder_name"])
                if row["highest_bidder_name"] is not None
                else None
            ),
            channel_id=str(row["channel_id"]) if row["channel_id"] else None,
            message_id=str(row["message_id"]) if row["message_id"] else None,
            created_at=int(row["created_at"]),
            completed_at=(
                int(row["completed_at"]) if row["completed_at"] is not None else None
            ),
            seller_notification_attempted=bool(
                row["seller_notification_attempted"]
            ),
            bidder_notification_attempted=bool(
                row["bidder_notification_attempted"]
            ),
        )

    @staticmethod
    def _add_inventory_copy(
        connection: sqlite3.Connection,
        user_id: str,
        player_id: int,
    ) -> None:
        owned = connection.execute(
            "SELECT quantity FROM inventory WHERE user_id = ? AND player_id = ?",
            (user_id, player_id),
        ).fetchone()
        if owned and int(owned["quantity"]) >= SQLITE_INT_MAX:
            raise InvalidAmount("That inventory quantity is too large.")
        connection.execute(
            """
            INSERT INTO inventory(user_id, player_id, quantity)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, player_id)
            DO UPDATE SET quantity = inventory.quantity + 1
            """,
            (user_id, player_id),
        )

    @staticmethod
    def _remove_inventory_copy(
        connection: sqlite3.Connection,
        user_id: str,
        player_id: int,
        quantity: int,
    ) -> None:
        if quantity <= 1:
            connection.execute(
                "DELETE FROM inventory WHERE user_id = ? AND player_id = ?",
                (user_id, player_id),
            )
        else:
            connection.execute(
                "UPDATE inventory SET quantity = quantity - 1 "
                "WHERE user_id = ? AND player_id = ?",
                (user_id, player_id),
            )

    @staticmethod
    def _auction_row(
        connection: sqlite3.Connection,
        auction_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            AUCTION_JOIN_SQL + " WHERE a.auction_id = ?",
            (auction_id,),
        ).fetchone()

    def create_auction(
        self,
        seller_id: str | int,
        seller_name: str,
        player_id: int,
        starting_price: int,
        duration_seconds: int,
        auction_id: str,
        channel_id: str | int,
        *,
        now: int | None = None,
    ) -> Auction:
        normalized_seller_id = self._validate_user_id(seller_id)
        normalized_auction_id = str(auction_id).strip()
        normalized_channel_id = str(channel_id).strip()
        normalized_seller_name = str(seller_name).strip()[:80] or normalized_seller_id
        starting_price = self._validate_amount(starting_price)
        duration_seconds = int(duration_seconds)
        current_time = int(time.time() if now is None else now)
        try:
            player_id = int(player_id)
        except (TypeError, ValueError) as exc:
            raise InvalidAmount("Choose a valid card from your inventory.") from exc
        if (
            not normalized_auction_id
            or len(normalized_auction_id) > 100
            or not normalized_channel_id
            or duration_seconds not in AUCTION_DURATIONS_SECONDS
            or current_time + duration_seconds > SQLITE_INT_MAX
        ):
            raise InvalidAmount("Auction details are invalid.")

        with self._transaction() as connection:
            self._ensure_user(connection, normalized_seller_id)
            player = connection.execute(
                "SELECT id FROM players WHERE id = ?",
                (player_id,),
            ).fetchone()
            if player is None:
                raise PlayerNotFound(str(player_id))
            owned = connection.execute(
                "SELECT quantity FROM inventory WHERE user_id = ? AND player_id = ?",
                (normalized_seller_id, player_id),
            ).fetchone()
            quantity = int(owned["quantity"]) if owned else 0
            if quantity < 1:
                raise CardNotOwned("You do not own an available copy of that card.")

            self._remove_inventory_copy(
                connection,
                normalized_seller_id,
                player_id,
                quantity,
            )
            connection.execute(
                """
                INSERT INTO auctions
                    (auction_id, seller_id, seller_name, player_id,
                     starting_price, duration_seconds, end_time, status,
                     channel_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (
                    normalized_auction_id,
                    normalized_seller_id,
                    normalized_seller_name,
                    player_id,
                    starting_price,
                    duration_seconds,
                    current_time + duration_seconds,
                    normalized_channel_id,
                    current_time,
                ),
            )
            row = self._auction_row(connection, normalized_auction_id)
            if row is None:
                raise RuntimeError("Created auction could not be loaded.")
            return self._auction_from_row(row)

    def get_auction(self, auction_id: str) -> Auction | None:
        with self._read_connection() as connection:
            row = self._auction_row(connection, str(auction_id).strip())
        return self._auction_from_row(row) if row is not None else None

    def list_active_auctions(
        self,
        *,
        exclude_seller_id: str | int | None = None,
    ) -> list[Auction]:
        query = AUCTION_JOIN_SQL + " WHERE a.status = 'ACTIVE'"
        parameters: tuple[object, ...] = ()
        if exclude_seller_id is not None:
            query += " AND a.seller_id != ?"
            parameters = (self._validate_user_id(exclude_seller_id),)
        query += " ORDER BY a.end_time ASC, a.created_at ASC"
        with self._read_connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._auction_from_row(row) for row in rows]

    def list_user_auctions(
        self,
        user_id: str | int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Auction]:
        normalized_id = self._validate_user_id(user_id)
        limit = max(1, min(100, int(limit)))
        offset = max(0, int(offset))
        with self._read_connection() as connection:
            rows = connection.execute(
                AUCTION_JOIN_SQL
                + """
                WHERE a.seller_id = ?
                ORDER BY a.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (normalized_id, limit, offset),
            ).fetchall()
        return [self._auction_from_row(row) for row in rows]

    def list_unpublished_auctions(self) -> list[Auction]:
        with self._read_connection() as connection:
            rows = connection.execute(
                AUCTION_JOIN_SQL
                + """
                WHERE a.status = 'ACTIVE'
                  AND a.channel_id IS NOT NULL
                  AND a.message_id IS NULL
                ORDER BY a.created_at ASC
                """
            ).fetchall()
        return [self._auction_from_row(row) for row in rows]

    def set_auction_message(
        self,
        auction_id: str,
        channel_id: str | int,
        message_id: str | int,
    ) -> Auction:
        normalized_id = str(auction_id).strip()
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE auctions
                SET channel_id = ?, message_id = ?
                WHERE auction_id = ?
                """,
                (str(channel_id), str(message_id), normalized_id),
            )
            if updated.rowcount != 1:
                raise AuctionNotFound(normalized_id)
            row = self._auction_row(connection, normalized_id)
            if row is None:
                raise AuctionNotFound(normalized_id)
            return self._auction_from_row(row)

    def cancel_auction(
        self,
        auction_id: str,
        seller_id: str | int,
        *,
        now: int | None = None,
    ) -> Auction:
        normalized_id = str(auction_id).strip()
        normalized_seller_id = self._validate_user_id(seller_id)
        current_time = int(time.time() if now is None else now)
        with self._transaction() as connection:
            auction = connection.execute(
                "SELECT * FROM auctions WHERE auction_id = ?",
                (normalized_id,),
            ).fetchone()
            if auction is None:
                raise AuctionNotFound(normalized_id)
            if str(auction["seller_id"]) != normalized_seller_id:
                raise AuctionNotSeller("Only the seller can cancel this auction.")
            if str(auction["status"]) != "ACTIVE":
                raise AuctionNotActive("This auction is no longer active.")
            if auction["highest_bid"] is not None:
                raise AuctionHasBids(
                    "Auctions cannot be cancelled after the first bid."
                )

            self._add_inventory_copy(
                connection,
                normalized_seller_id,
                int(auction["player_id"]),
            )
            connection.execute(
                """
                UPDATE auctions
                SET status = 'CANCELLED', completed_at = ?,
                    seller_notified = 1, bidder_notified = 1
                WHERE auction_id = ? AND status = 'ACTIVE'
                """,
                (current_time, normalized_id),
            )
            row = self._auction_row(connection, normalized_id)
            if row is None:
                raise AuctionNotFound(normalized_id)
            return self._auction_from_row(row)
    def sell_card(
        self, user_id: str | int, player_name: str, quantity: int = 1
    ) -> tuple[int, int]:
        """Sell owned copies at the listed value; return (coins earned, new balance)."""
        normalized_id = self._validate_user_id(user_id)
        quantity = self._validate_amount(quantity)
        with self._transaction() as connection:
            player = connection.execute(
                "SELECT * FROM players WHERE name = ? COLLATE NOCASE",
                (str(player_name).strip(),),
            ).fetchone()
            if not player:
                raise PlayerNotFound(player_name)
            owned = connection.execute(
                "SELECT quantity FROM inventory WHERE user_id = ? AND player_id = ?",
                (normalized_id, int(player["id"])),
            ).fetchone()
            owned_quantity = int(owned["quantity"]) if owned else 0
            if owned_quantity < quantity:
                raise InsufficientCards(
                    f"You own {owned_quantity} copy/copies of "
                    f"{format_card_label(player['name'], player['overall'], player['rarity'])}."
                )

            proceeds = int(player["value"]) * quantity
            if proceeds > SQLITE_INT_MAX:
                raise InvalidAmount("The sale proceeds exceed the supported maximum.")
            self._ensure_user(connection, normalized_id)
            user = connection.execute(
                "SELECT balance FROM users WHERE user_id = ?", (normalized_id,)
            ).fetchone()
            balance = int(user["balance"])
            if balance + proceeds > SQLITE_INT_MAX:
                raise InvalidAmount("The sale would exceed the supported balance.")

            if owned_quantity == quantity:
                connection.execute(
                    "DELETE FROM inventory WHERE user_id = ? AND player_id = ?",
                    (normalized_id, int(player["id"])),
                )
            else:
                connection.execute(
                    "UPDATE inventory SET quantity = quantity - ? "
                    "WHERE user_id = ? AND player_id = ?",
                    (quantity, normalized_id, int(player["id"])),
                )
            connection.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (proceeds, normalized_id),
            )
            return proceeds, balance + proceeds
