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

CLAIM_COOLDOWN_SECONDS = 24 * 60 * 60
GOLD_PACK_COST = 5_000_000
TOTW_PACK_COST = 20_000_000
SQLITE_INT_MAX = (1 << 63) - 1
SUPPORTED_STATS = ("SHO", "DRI", "SAV", "PAS", "DEF", "ACT", "DIV", "CLE", "SWE")
FORMATION_STARTERS = {
    "Balanced": ("WING1", "CM1", "GK"),
    "Offensive": ("WING1", "WING2", "GK"),
    "Defensive": ("CM1", "CM2", "GK"),
}
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


@dataclass(frozen=True)
class FormationChange:
    formation: str
    moved: tuple[tuple[str, str, Player], ...] = ()
    unassigned: tuple[tuple[str, Player], ...] = ()


@dataclass(frozen=True)
class InventoryEntry:
    player: Player
    quantity: int


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
                    image_path TEXT
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

                CREATE INDEX IF NOT EXISTS idx_players_rarity_name
                    ON players(rarity, name);
                CREATE INDEX IF NOT EXISTS idx_users_balance
                    ON users(balance DESC);
                """
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
                        (name, overall, position, country, team, rarity, value, stats_json, image_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        """Atomically award a weighted GOLD card and enforce the daily cooldown."""
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
            if position in SUBSTITUTE_POSITIONS or (
                position in active_positions
                and card_position_role(player.position)
                == POSITION_ROLE_BY_SLOT.get(position)
            ):
                result[position] = player
        return result

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
