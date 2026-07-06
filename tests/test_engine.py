"""Step 3 tests: limit order matching."""

import unittest

from lob.engine import MatchingEngine
from lob.types import Order, Side


def buy(oid, price, qty):
    return Order(oid, Side.BUY, price=price, quantity=qty)


def sell(oid, price, qty):
    return Order(oid, Side.SELL, price=price, quantity=qty)


class TestNoCross(unittest.TestCase):
    def test_non_crossing_orders_rest(self):
        eng = MatchingEngine()
        self.assertEqual(eng.submit_limit(buy("b1", 100, 10)), [])
        self.assertEqual(eng.submit_limit(sell("s1", 101, 10)), [])
        self.assertEqual(eng.book.best_bid(), 100)
        self.assertEqual(eng.book.best_ask(), 101)

    def test_equal_price_crosses_but_adjacent_does_not(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 101, 10))
        # bid at 100 < ask at 101: no trade
        self.assertEqual(eng.submit_limit(buy("b1", 100, 5)), [])
        # bid at 101 == ask at 101: trades
        fills = eng.submit_limit(buy("b2", 101, 5))
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].price, 101)


class TestSimpleCross(unittest.TestCase):
    def test_exact_full_fill_both_sides(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 10))
        fills = eng.submit_limit(buy("b1", 100, 10))
        self.assertEqual(len(fills), 1)
        f = fills[0]
        self.assertEqual((f.maker_order_id, f.taker_order_id), ("s1", "b1"))
        self.assertEqual((f.price, f.quantity), (100, 10))
        # book is empty on both sides
        self.assertIsNone(eng.book.best_bid())
        self.assertIsNone(eng.book.best_ask())

    def test_fill_at_resting_price_price_improvement(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 10))
        # taker willing to pay up to 105 still fills at the maker's 100
        fills = eng.submit_limit(buy("b1", 105, 10))
        self.assertEqual(fills[0].price, 100)

    def test_partial_fill_remainder_rests(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 4))
        fills = eng.submit_limit(buy("b1", 100, 10))
        self.assertEqual(sum(f.quantity for f in fills), 4)
        # remainder of the buy (6) now rests as the best bid
        self.assertEqual(eng.book.best_bid(), 100)
        self.assertEqual(eng.book.bids.best_level().total_qty, 6)
        self.assertIsNone(eng.book.best_ask())

    def test_taker_fills_leaving_maker_partial(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 10))
        fills = eng.submit_limit(buy("b1", 100, 4))
        self.assertEqual(sum(f.quantity for f in fills), 4)
        # maker keeps its place with 6 remaining
        self.assertEqual(eng.book.asks.best_level().total_qty, 6)
        self.assertEqual(eng.book.asks.best_level().orders[0].order_id, "s1")


class TestPriority(unittest.TestCase):
    def test_price_priority_across_levels(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("cheap", 100, 5))
        eng.submit_limit(sell("mid", 101, 5))
        eng.submit_limit(sell("dear", 102, 5))
        fills = eng.submit_limit(buy("b1", 102, 12))
        self.assertEqual(
            [(f.maker_order_id, f.price, f.quantity) for f in fills],
            [("cheap", 100, 5), ("mid", 101, 5), ("dear", 102, 2)],
        )
        # 'dear' keeps 3 on the book
        self.assertEqual(eng.book.asks.best_level().total_qty, 3)

    def test_time_priority_within_level(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("first", 100, 5))
        eng.submit_limit(sell("second", 100, 5))
        fills = eng.submit_limit(buy("b1", 100, 7))
        self.assertEqual(
            [(f.maker_order_id, f.quantity) for f in fills],
            [("first", 5), ("second", 2)],
        )

    def test_sell_taker_walks_bids_downward(self):
        eng = MatchingEngine()
        eng.submit_limit(buy("high", 102, 5))
        eng.submit_limit(buy("low", 100, 5))
        fills = eng.submit_limit(sell("s1", 100, 8))
        self.assertEqual(
            [(f.maker_order_id, f.price, f.quantity) for f in fills],
            [("high", 102, 5), ("low", 100, 3)],
        )


class TestBookIntegrity(unittest.TestCase):
    def test_emptied_level_is_removed_and_next_promoted(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 5))
        eng.submit_limit(sell("s2", 101, 5))
        eng.submit_limit(buy("b1", 100, 5))  # wipes the 100 level exactly
        self.assertEqual(eng.book.best_ask(), 101)

    def test_aggressive_remainder_rests_at_its_own_limit(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 5))
        # buys 5 @ 100, remainder 5 rests at its limit of 103
        eng.submit_limit(buy("b1", 103, 10))
        self.assertEqual(eng.book.best_bid(), 103)
        self.assertEqual(eng.book.bids.best_level().total_qty, 5)

    def test_conservation_of_quantity(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 7))
        eng.submit_limit(sell("s2", 101, 3))
        taker = buy("b1", 101, 15)
        fills = eng.submit_limit(taker)
        filled = sum(f.quantity for f in fills)
        self.assertEqual(filled, 10)
        self.assertEqual(taker.remaining, 5)
        self.assertEqual(filled + taker.remaining, taker.quantity)


if __name__ == "__main__":
    unittest.main()
