from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from database import (
    Database,
    InsufficientCards,
    TradeActionUnauthorized,
    TradeAlreadyActive,
    TradeNotActive,
)
from main import load_player_seed


class TradeDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "trades.sqlite3"
        self.database = Database(self.database_path)
        self.database.initialize(load_player_seed())
        self.first = "trade-player-one"
        self.second = "trade-player-two"
        self.first_card = self.database.get_player("Dan")
        self.second_card = self.database.get_player("Hala")
        assert self.first_card is not None
        assert self.second_card is not None
        self.database.set_balance(self.first, 5_000)
        self.database.set_balance(self.second, 2_000)
        self.database.add_inventory(self.first, "Dan", 2)
        self.database.add_inventory(self.second, "Hala", 1)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def start_trade(self, trade_id: str = "test-trade") -> dict[str, object]:
        self.database.create_trade_request(
            trade_id,
            self.first,
            self.second,
            "Player One",
            "Player Two",
            "guild",
            "channel",
            now=100,
        )
        return self.database.accept_trade_request(
            trade_id,
            self.second,
            now=101,
        )

    def test_successful_trade_transfers_cards_and_coins_atomically(self) -> None:
        trade = self.start_trade()
        self.assertEqual(trade["status"], "ACTIVE")
        self.database.add_trade_card(
            "test-trade",
            self.first,
            self.first_card.id,
            now=102,
        )
        self.database.add_trade_card(
            "test-trade",
            self.second,
            self.second_card.id,
            now=102,
        )
        self.database.set_trade_coins(
            "test-trade",
            self.first,
            2_500,
            now=102,
        )
        self.database.set_trade_coins(
            "test-trade",
            self.second,
            500,
            now=102,
        )

        self.database.confirm_trade("test-trade", self.first, now=103)
        result = self.database.confirm_trade("test-trade", self.second, now=104)

        self.assertTrue(result["completed"])
        self.assertEqual(result["trade"]["status"], "COMPLETED")
        self.assertEqual(self.database.get_balance(self.first), 3_000)
        self.assertEqual(self.database.get_balance(self.second), 4_000)
        first_inventory = {
            entry.player.name: entry.quantity
            for entry in self.database.get_inventory(self.first)
        }
        second_inventory = {
            entry.player.name: entry.quantity
            for entry in self.database.get_inventory(self.second)
        }
        self.assertEqual(first_inventory["Dan"], 1)
        self.assertEqual(first_inventory["Hala"], 1)
        self.assertEqual(second_inventory["Dan"], 1)
        self.assertNotIn("Hala", second_inventory)
        self.assertEqual(
            result["trade"]["completed_details"]["initiator_coins"],
            2_500,
        )

    def test_declined_request_releases_both_players(self) -> None:
        self.database.create_trade_request(
            "declined",
            self.first,
            self.second,
            "Player One",
            "Player Two",
            "guild",
            "channel",
            now=100,
        )
        declined = self.database.decline_trade_request(
            "declined",
            self.second,
            now=101,
        )
        self.assertEqual(declined["status"], "DECLINED")
        next_trade = self.database.create_trade_request(
            "after-decline",
            self.first,
            self.second,
            "Player One",
            "Player Two",
            "guild",
            "channel",
            now=102,
        )
        self.assertEqual(next_trade["status"], "REQUESTED")

    def test_active_trade_can_be_cancelled_by_either_participant(self) -> None:
        self.start_trade("cancelled")
        cancelled = self.database.cancel_trade(
            "cancelled",
            self.second,
            now=102,
        )
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(self.database.get_balance(self.first), 5_000)
        self.assertEqual(self.database.get_balance(self.second), 2_000)
        self.assertEqual(
            {entry.player.name: entry.quantity for entry in self.database.get_inventory(self.first)},
            {"Dan": 2},
        )

    def test_coin_shortfall_prevents_card_and_coin_transfer(self) -> None:
        self.start_trade("too-expensive")
        self.database.add_trade_card(
            "too-expensive",
            self.first,
            self.first_card.id,
            now=102,
        )
        self.database.set_trade_coins(
            "too-expensive",
            self.first,
            5_001,
            now=102,
        )
        self.database.confirm_trade("too-expensive", self.first, now=103)
        result = self.database.confirm_trade(
            "too-expensive",
            self.second,
            now=104,
        )
        self.assertFalse(result["completed"])
        self.assertEqual(result["error"], "insufficient_coins")
        self.assertEqual(self.database.get_balance(self.first), 5_000)
        self.assertEqual(self.database.get_balance(self.second), 2_000)
        self.assertEqual(
            {entry.player.name: entry.quantity for entry in self.database.get_inventory(self.first)},
            {"Dan": 2},
        )

    def test_missing_offered_card_prevents_partial_coin_transfer(self) -> None:
        self.start_trade("card-was-sold")
        self.database.add_trade_card(
            "card-was-sold",
            self.first,
            self.first_card.id,
            now=102,
        )
        self.database.set_trade_coins(
            "card-was-sold",
            self.first,
            1_000,
            now=102,
        )
        with self.database._transaction() as connection:
            connection.execute(
                "DELETE FROM inventory WHERE user_id = ? AND player_id = ?",
                (self.first, self.first_card.id),
            )

        self.database.confirm_trade("card-was-sold", self.first, now=103)
        result = self.database.confirm_trade("card-was-sold", self.second, now=104)
        self.assertFalse(result["completed"])
        self.assertEqual(result["error"], "missing_cards")
        self.assertEqual(self.database.get_balance(self.first), 5_000)
        self.assertEqual(self.database.get_balance(self.second), 2_000)

    def test_players_cannot_offer_another_players_card(self) -> None:
        self.start_trade("unauthorized-card")
        with self.assertRaises(InsufficientCards):
            self.database.add_trade_card(
                "unauthorized-card",
                self.first,
                self.second_card.id,
                now=102,
            )
        with self.assertRaises(TradeActionUnauthorized):
            self.database.set_trade_coins(
                "unauthorized-card",
                "not-a-participant",
                1,
                now=102,
            )

    def test_offer_change_resets_both_confirmations(self) -> None:
        self.start_trade("reset-confirmations")
        self.database.set_trade_coins(
            "reset-confirmations",
            self.first,
            100,
            now=102,
        )
        first_confirmation = self.database.confirm_trade(
            "reset-confirmations",
            self.first,
            now=103,
        )
        self.assertTrue(first_confirmation["trade"]["initiator_confirmed"])
        changed = self.database.set_trade_coins(
            "reset-confirmations",
            self.second,
            20,
            now=104,
        )
        self.assertFalse(changed["initiator_confirmed"])
        self.assertFalse(changed["invitee_confirmed"])

    def test_competing_trade_requests_for_same_players_are_serialized(self) -> None:
        barrier = Barrier(2)

        def create(trade_id: str) -> str:
            barrier.wait()
            try:
                self.database.create_trade_request(
                    trade_id,
                    self.first,
                    self.second,
                    "Player One",
                    "Player Two",
                    "guild",
                    "channel",
                    now=100,
                )
                return "created"
            except TradeAlreadyActive:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, ("simultaneous-a", "simultaneous-b")))
        self.assertCountEqual(results, ["created", "conflict"])

    def test_completed_trade_cannot_transfer_twice(self) -> None:
        self.start_trade("duplicate-confirm")
        self.database.add_trade_card(
            "duplicate-confirm",
            self.first,
            self.first_card.id,
            now=102,
        )
        self.database.confirm_trade("duplicate-confirm", self.first, now=103)
        self.database.confirm_trade("duplicate-confirm", self.second, now=104)
        balances = (
            self.database.get_balance(self.first),
            self.database.get_balance(self.second),
        )
        with self.assertRaises(TradeNotActive):
            self.database.confirm_trade("duplicate-confirm", self.first, now=105)
        self.assertEqual(
            (
                self.database.get_balance(self.first),
                self.database.get_balance(self.second),
            ),
            balances,
        )
        self.assertEqual(
            {entry.player.name: entry.quantity for entry in self.database.get_inventory(self.second)},
            {"Dan": 1, "Hala": 1},
        )

    def test_active_trade_and_offer_survive_database_reopen(self) -> None:
        self.start_trade("persistent")
        self.database.add_trade_card(
            "persistent",
            self.first,
            self.first_card.id,
            now=102,
        )
        reloaded = Database(self.database_path)
        reloaded.initialize(load_player_seed())
        trade = reloaded.get_trade("persistent")
        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade["status"], "ACTIVE")
        self.assertEqual(len(trade["cards"]), 1)
        self.assertEqual(trade["cards"][0]["player"].name, "Dan")


if __name__ == "__main__":
    unittest.main()
