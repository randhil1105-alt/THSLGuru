from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from commands.auction import AuctionCommands, DURATION_CHOICES, _auction_embed
from commands.common import player_card_label
from database import (
    AUCTION_DURATIONS_SECONDS,
    AuctionBidTooLow,
    AuctionDuplicateBid,
    AuctionExpired,
    AuctionHasBids,
    AuctionNotSeller,
    AuctionSelfBid,
    CardNotOwned,
    Database,
    InsufficientFunds,
)
from main import load_player_seed


class AuctionDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "auction.sqlite3"
        self.database = Database(self.path)
        self.database.initialize(load_player_seed())
        self.card = self.database.get_player("Dan")
        assert self.card is not None

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_auction(
        self,
        *,
        seller: str = "seller",
        copies: int = 1,
        price: int = 100,
        duration: int = AUCTION_DURATIONS_SECONDS[0],
        auction_id: str = "auction-1",
        now: int = 1_000,
    ):
        self.database.add_inventory(seller, self.card.name, copies)
        return self.database.create_auction(
            seller,
            f"User {seller}",
            self.card.id,
            price,
            duration,
            auction_id,
            987654321,
            now=now,
        )

    def test_listing_owned_card_removes_only_one_available_copy(self) -> None:
        auction = self.create_auction(copies=2)
        inventory = self.database.get_inventory("seller")
        self.assertEqual([(entry.player.id, entry.quantity) for entry in inventory], [
            (self.card.id, 1)
        ])
        self.assertEqual(auction.status, "ACTIVE")
        self.assertEqual(auction.player, self.card)
        self.assertEqual(auction.seller_name, "User seller")
        self.assertEqual(auction.end_time, 1_000 + AUCTION_DURATIONS_SECONDS[0])

    def test_cannot_list_a_card_not_owned_or_list_a_removed_copy_twice(self) -> None:
        with self.assertRaises(CardNotOwned):
            self.database.create_auction(
                "seller",
                "Seller",
                self.card.id,
                100,
                AUCTION_DURATIONS_SECONDS[0],
                "unowned",
                987654321,
                now=1_000,
            )
        self.create_auction()
        with self.assertRaises(CardNotOwned):
            self.database.create_auction(
                "seller",
                "Seller",
                self.card.id,
                100,
                AUCTION_DURATIONS_SECONDS[0],
                "duplicate-copy",
                987654321,
                now=1_001,
            )
        self.assertEqual(self.database.get_inventory("seller"), [])

    def test_bidding_reserves_coins_and_outbidding_releases_previous_reserve(self) -> None:
        self.create_auction()
        self.database.set_balance("bidder-a", 1_000)
        self.database.set_balance("bidder-b", 1_000)

        first = self.database.place_auction_bid(
            "auction-1", "bidder-a", "Bidder A", 100, "interaction-a", now=1_001
        )
        self.assertEqual(first.highest_bid, 100)
        self.assertEqual(self.database.get_balance("bidder-a"), 900)

        second = self.database.place_auction_bid(
            "auction-1", "bidder-b", "Bidder B", 150, "interaction-b", now=1_002
        )
        self.assertEqual(second.highest_bid, 150)
        self.assertEqual(second.highest_bidder_id, "bidder-b")
        self.assertEqual(self.database.get_balance("bidder-a"), 1_000)
        self.assertEqual(self.database.get_balance("bidder-b"), 850)

    def test_rejects_self_bids_low_bids_and_bids_over_available_balance(self) -> None:
        self.create_auction()
        self.database.set_balance("bidder", 99)
        with self.assertRaises(AuctionSelfBid):
            self.database.place_auction_bid(
                "auction-1", "seller", "Seller", 100, "self-bid", now=1_001
            )
        with self.assertRaises(AuctionBidTooLow) as low:
            self.database.place_auction_bid(
                "auction-1", "bidder", "Bidder", 99, "low-bid", now=1_001
            )
        self.assertEqual(low.exception.minimum, 100)
        with self.assertRaises(InsufficientFunds):
            self.database.place_auction_bid(
                "auction-1", "bidder", "Bidder", 100, "unaffordable", now=1_001
            )
        self.assertEqual(self.database.get_balance("bidder"), 99)
        self.assertIsNone(self.database.get_auction("auction-1").highest_bid)

    def test_same_bidder_can_raise_bid_using_only_the_increment(self) -> None:
        self.create_auction()
        self.database.set_balance("bidder", 1_000)
        self.database.place_auction_bid(
            "auction-1", "bidder", "Bidder", 100, "bid-1", now=1_001
        )
        with self.assertRaises(AuctionDuplicateBid):
            self.database.place_auction_bid(
                "auction-1", "bidder", "Bidder", 110, "bid-1", now=1_002
            )
        raised = self.database.place_auction_bid(
            "auction-1", "bidder", "Bidder", 150, "bid-2", now=1_003
        )
        self.assertEqual(raised.highest_bid, 150)
        self.assertEqual(self.database.get_balance("bidder"), 850)

    def test_expiration_transfers_card_and_payment_once(self) -> None:
        self.create_auction()
        self.database.set_balance("winner", 500)
        self.database.place_auction_bid(
            "auction-1", "winner", "Winner", 125, "winning-bid", now=1_001
        )
        end_time = 1_000 + AUCTION_DURATIONS_SECONDS[0]
        completed = self.database.finalize_expired_auctions(now=end_time)
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "COMPLETED")
        self.assertEqual(self.database.get_balance("winner"), 375)
        self.assertEqual(self.database.get_balance("seller"), 125)
        self.assertEqual(self.database.get_inventory("seller"), [])
        self.assertEqual(
            sum(entry.quantity for entry in self.database.get_inventory("winner")),
            1,
        )
        self.assertEqual(
            [(item.auction_id, item.seller_notification_attempted,
              item.bidder_notification_attempted)
             for item in self.database.list_auctions_pending_notification()],
            [("auction-1", False, False)],
        )
        self.database.mark_auction_notification_attempted("auction-1", "seller")
        self.assertEqual(
            [(item.auction_id, item.bidder_notification_attempted)
             for item in self.database.list_auctions_pending_notification()],
            [("auction-1", False)],
        )
        self.database.mark_auction_notification_attempted("auction-1", "bidder")
        self.assertEqual(self.database.list_auctions_pending_notification(), [])

        self.assertEqual(self.database.finalize_expired_auctions(now=end_time + 1), [])
        self.assertEqual(self.database.get_balance("seller"), 125)
        self.assertEqual(
            sum(entry.quantity for entry in self.database.get_inventory("winner")),
            1,
        )

    def test_unsold_expiration_returns_card_and_records_seller_notification(self) -> None:
        self.create_auction()
        end_time = 1_000 + AUCTION_DURATIONS_SECONDS[0]
        result = self.database.finalize_expired_auctions(now=end_time)
        self.assertEqual(result[0].status, "UNSOLD")
        self.assertEqual(
            sum(entry.quantity for entry in self.database.get_inventory("seller")),
            1,
        )
        self.assertEqual(self.database.get_balance("seller"), 0)
        pending = self.database.list_auctions_pending_notification()
        self.assertEqual([item.auction_id for item in pending], ["auction-1"])
        self.database.mark_auction_notification_attempted("auction-1", "seller")
        self.assertEqual(self.database.list_auctions_pending_notification(), [])

    def test_cancellation_returns_card_and_requires_own_unbid_active_listing(self) -> None:
        self.create_auction()
        with self.assertRaises(AuctionNotSeller):
            self.database.cancel_auction("auction-1", "other", now=1_001)
        cancelled = self.database.cancel_auction("auction-1", "seller", now=1_001)
        self.assertEqual(cancelled.status, "CANCELLED")
        self.assertEqual(
            sum(entry.quantity for entry in self.database.get_inventory("seller")),
            1,
        )
        with self.assertRaises(AuctionNotSeller):
            self.database.cancel_auction("auction-1", "other", now=1_002)

        self.create_auction(auction_id="auction-2")
        self.database.set_balance("bidder", 500)
        self.database.place_auction_bid(
            "auction-2", "bidder", "Bidder", 100, "bid-auction-2", now=1_001
        )
        with self.assertRaises(AuctionHasBids):
            self.database.cancel_auction("auction-2", "seller", now=1_002)

    def test_expired_auction_cannot_be_cancelled_and_is_settled(self) -> None:
        self.create_auction()
        with self.assertRaises(AuctionExpired):
            self.database.cancel_auction(
                "auction-1",
                "seller",
                now=1_000 + AUCTION_DURATIONS_SECONDS[0],
            )
        auction = self.database.get_auction("auction-1")
        self.assertIsNotNone(auction)
        assert auction is not None
        self.assertEqual(auction.status, "UNSOLD")
        self.assertEqual(len(self.database.get_inventory("seller")), 1)

    def test_expired_bid_is_rejected_after_the_auction_is_settled(self) -> None:
        self.create_auction()
        self.database.set_balance("bidder", 500)
        with self.assertRaises(AuctionExpired):
            self.database.place_auction_bid(
                "auction-1",
                "bidder",
                "Bidder",
                100,
                "late-bid",
                now=1_000 + AUCTION_DURATIONS_SECONDS[0],
            )
        auction = self.database.get_auction("auction-1")
        self.assertIsNotNone(auction)
        assert auction is not None
        self.assertEqual(auction.status, "UNSOLD")
        self.assertEqual(self.database.get_balance("bidder"), 500)
        self.assertEqual(len(self.database.get_inventory("seller")), 1)

    def test_active_auction_and_reserved_bid_survive_database_reopen(self) -> None:
        self.create_auction()
        self.database.set_balance("bidder", 500)
        self.database.place_auction_bid(
            "auction-1", "bidder", "Bidder", 125, "persistent-bid", now=1_001
        )

        reopened = Database(self.path)
        reopened.initialize(load_player_seed())
        auction = reopened.get_auction("auction-1")
        self.assertIsNotNone(auction)
        assert auction is not None
        self.assertEqual(auction.status, "ACTIVE")
        self.assertEqual(auction.highest_bid, 125)
        self.assertEqual(reopened.get_balance("bidder"), 375)
        self.assertEqual(reopened.get_inventory("seller"), [])

        completed = reopened.finalize_expired_auctions(
            now=1_000 + AUCTION_DURATIONS_SECONDS[0]
        )
        self.assertEqual(completed[0].status, "COMPLETED")
        self.assertEqual(reopened.get_balance("seller"), 125)
        self.assertEqual(len(reopened.get_inventory("bidder")), 1)

    def test_concurrent_bids_are_serialized_and_only_one_bid_remains_held(self) -> None:
        self.create_auction()
        self.database.set_balance("bidder-a", 1_000)
        self.database.set_balance("bidder-b", 1_000)
        barrier = Barrier(2)

        def bid(user_id: str, amount: int, interaction_id: str) -> str:
            barrier.wait()
            try:
                self.database.place_auction_bid(
                    "auction-1",
                    user_id,
                    user_id,
                    amount,
                    interaction_id,
                    now=1_001,
                )
                return "accepted"
            except AuctionBidTooLow:
                return "too_low"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [
                pool.submit(bid, "bidder-a", 200, "parallel-a"),
                pool.submit(bid, "bidder-b", 300, "parallel-b"),
            ]
            results = [future.result() for future in outcomes]

        self.assertEqual(
            results.count("accepted"),
            1 if results.count("too_low") else 2,
        )
        auction = self.database.get_auction("auction-1")
        self.assertIsNotNone(auction)
        assert auction is not None
        self.assertEqual(auction.highest_bid, 300)
        with self.database._read_connection() as connection:
            held = connection.execute(
                "SELECT COUNT(*) AS total FROM auction_bids "
                "WHERE auction_id = ? AND status = 'HELD'",
                ("auction-1",),
            ).fetchone()
        self.assertEqual(int(held["total"]), 1)
        self.assertGreaterEqual(self.database.get_balance("bidder-a"), 0)
        self.assertGreaterEqual(self.database.get_balance("bidder-b"), 0)


class AuctionCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "auction-command.sqlite3")
        self.database.initialize(load_player_seed())
        self.card = self.database.get_player("Dan")
        assert self.card is not None

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def make_bot(self, channel: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(
            database=self.database,
            get_channel=Mock(return_value=channel),
            fetch_channel=AsyncMock(return_value=channel),
            get_user=Mock(return_value=None),
            fetch_user=AsyncMock(),
            add_view=Mock(),
            is_closed=Mock(return_value=True),
        )

    async def test_s2_tots_cards_can_be_listed_with_their_artwork(self) -> None:
        names = (
            "EBS (S2 TOTS 99)",
            "Jaycob (S2 TOTS 96)",
            "Licha (S2 TOTS 96)",
        )
        for index, name in enumerate(names):
            with self.subTest(card=name):
                card = self.database.get_player(name)
                self.assertIsNotNone(card)
                assert card is not None
                seller_id = 950 + index
                self.database.add_inventory(seller_id, name)
                channel = SimpleNamespace(
                    id=850 + index,
                    send=AsyncMock(return_value=SimpleNamespace(id=750 + index)),
                )
                cog = AuctionCommands(self.make_bot(channel), self.database)
                response = SimpleNamespace(defer=AsyncMock())
                followup = SimpleNamespace(send=AsyncMock())
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=seller_id, name=f"seller{index}"),
                    guild=SimpleNamespace(id=650),
                    channel=channel,
                    response=response,
                    followup=followup,
                )

                await AuctionCommands.sell.callback(
                    cog,
                    interaction,
                    str(card.id),
                    100_000,
                    DURATION_CHOICES[0],
                )

                published = channel.send.await_args.kwargs
                self.assertEqual(published["embed"].title, player_card_label(card))
                self.assertEqual(
                    published["embed"].image.url,
                    f"attachment://{Path(card.image_path).name}",
                )
                self.assertEqual(published["file"].filename, Path(card.image_path).name)
                self.assertEqual(
                    self.database.list_user_auctions(seller_id)[0].player.id,
                    card.id,
                )
                published["file"].close()

    async def test_sell_command_publishes_artwork_and_removes_available_card(self) -> None:
        seller_id = 900
        self.database.add_inventory(seller_id, self.card.name, 2)
        channel = SimpleNamespace(
            id=800,
            send=AsyncMock(return_value=SimpleNamespace(id=700)),
        )
        bot = self.make_bot(channel)
        cog = AuctionCommands(bot, self.database)
        response = SimpleNamespace(defer=AsyncMock())
        followup = SimpleNamespace(send=AsyncMock())
        user = SimpleNamespace(id=seller_id, name="seller_username")
        interaction = SimpleNamespace(
            user=user,
            guild=SimpleNamespace(id=600),
            channel=channel,
            response=response,
            followup=followup,
        )

        await AuctionCommands.sell.callback(
            cog,
            interaction,
            str(self.card.id),
            25_000,
            DURATION_CHOICES[0],
        )

        response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        followup.send.assert_awaited_once()
        self.assertTrue(followup.send.await_args.kwargs["ephemeral"])
        published = channel.send.await_args.kwargs
        self.assertEqual(published["embed"].title, player_card_label(self.card))
        self.assertEqual(published["embed"].image.url, f"attachment://{Path(self.card.image_path).name}")
        self.assertEqual(published["file"].filename, Path(self.card.image_path).name)
        self.assertEqual(
            [(entry.player.id, entry.quantity) for entry in self.database.get_inventory(seller_id)],
            [(self.card.id, 1)],
        )
        auctions = self.database.list_user_auctions(seller_id)
        self.assertEqual(len(auctions), 1)
        self.assertEqual(auctions[0].seller_name, "seller_username")
        self.assertEqual(auctions[0].message_id, "700")

    async def test_auction_embed_contains_card_seller_bid_and_end_details(self) -> None:
        self.database.add_inventory("seller", self.card.name)
        auction = self.database.create_auction(
            "seller",
            "seller_name",
            self.card.id,
            5_000,
            AUCTION_DURATIONS_SECONDS[0],
            "embed-auction",
            800,
            now=1_000,
        )
        embed, upload = _auction_embed(auction)
        try:
            self.assertEqual(embed.title, player_card_label(self.card))
            self.assertIn("GOLD", embed.description)
            self.assertEqual(
                {field.name for field in embed.fields},
                {
                    "Seller",
                    "Starting price",
                    "Current bid",
                    "Highest bidder",
                    "Ends",
                    "Status",
                },
            )
            self.assertEqual(embed.fields[0].value, "seller_name")
            self.assertEqual(embed.fields[2].value, "No bids yet")
            self.assertEqual(embed.fields[3].value, "No bids yet")
            self.assertEqual(embed.image.url, f"attachment://{Path(self.card.image_path).name}")
            self.assertIsNotNone(upload)
        finally:
            if upload is not None:
                upload.close()

    async def test_active_bid_button_is_restored_after_reloading_command_cog(self) -> None:
        self.database.add_inventory("seller", self.card.name)
        auction = self.database.create_auction(
            "seller",
            "seller_name",
            self.card.id,
            5_000,
            AUCTION_DURATIONS_SECONDS[0],
            "restore-auction",
            800,
            now=1_000,
        )
        self.database.set_auction_message(auction.auction_id, 800, 700)
        channel = SimpleNamespace()
        bot = self.make_bot(channel)
        cog = AuctionCommands(bot, self.database)

        await cog.cog_load()
        try:
            bot.add_view.assert_called_once()
            self.assertEqual(bot.add_view.call_args.kwargs["message_id"], 700)
        finally:
            await cog.cog_unload()

    async def test_auction_post_is_edited_after_a_successful_bid(self) -> None:
        self.database.add_inventory("seller", self.card.name)
        auction = self.database.create_auction(
            "seller",
            "Seller",
            self.card.id,
            100,
            AUCTION_DURATIONS_SECONDS[0],
            "update-auction",
            800,
            now=1_000,
        )
        auction = self.database.set_auction_message(auction.auction_id, 800, 700)
        message = SimpleNamespace(edit=AsyncMock())
        channel = SimpleNamespace(
            id=800,
            fetch_message=AsyncMock(return_value=message),
        )
        bot = self.make_bot(channel)
        cog = AuctionCommands(bot, self.database)
        self.database.set_balance("bidder", 500)
        updated = self.database.place_auction_bid(
            auction.auction_id,
            "bidder",
            "Bidder",
            125,
            "post-update-bid",
            now=1_001,
        )

        await cog._update_auction_post(updated)

        message.edit.assert_awaited_once()
        embed = message.edit.await_args.kwargs["embed"]
        self.assertEqual(embed.fields[2].value, "125 coins")
        self.assertEqual(embed.fields[3].value, "Bidder")

    async def test_background_maintenance_settles_expired_auctions(self) -> None:
        self.database.add_inventory("seller", self.card.name)
        self.database.create_auction(
            "seller",
            "Seller",
            self.card.id,
            100,
            AUCTION_DURATIONS_SECONDS[0],
            "maintenance-auction",
            800,
            now=1,
        )
        bot = self.make_bot(SimpleNamespace())
        cog = AuctionCommands(bot, self.database)
        cog._notify_user = AsyncMock()

        await cog._maintenance_pass()

        auction = self.database.get_auction("maintenance-auction")
        self.assertIsNotNone(auction)
        assert auction is not None
        self.assertEqual(auction.status, "UNSOLD")
        self.assertEqual(len(self.database.get_inventory("seller")), 1)
        cog._notify_user.assert_awaited_once_with(auction, recipient="seller")


if __name__ == "__main__":
    unittest.main()

