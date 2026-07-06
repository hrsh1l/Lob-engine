"""Step 4 tests: market orders."""

import unittest

from lob.engine import MatchingEngine
from lob.events import Canceled
from lob.types import Order, Side


def buy(oid, price, qty):
    return Order(oid, Side.BUY, price=price, quantity=qty)


def sell(oid, price, qty):
    return Order(oid, Side.SELL, price=price, quantity=qty)


def mkt(oid, side, qty):
    return Order(oid, side, price=None, quantity=qty)


class TestMarketOrders(unittest.TestCase):
    def test_market_buy_takes_best_asks_first(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 101, 5))
        eng.submit_limit(sell("s2", 100, 5))
        fills = eng.submit_market(mkt("m1", Side.BUY, 8))
        self.assertEqual(
            [(f.maker_order_id, f.price, f.quantity) for f in fills],
            [("s2", 100, 5), ("s1", 101, 3)],
        )

    def test_market_never_rests(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 5))
        eng.submit_market(mkt("m1", Side.BUY, 20))
        # 15 unfilled units are cancelled, not rested as a bid
        self.assertIsNone(eng.book.best_bid())
        cancels = [e for e in eng.events if isinstance(e, Canceled)]
        self.assertEqual(cancels[-1].order_id, "m1")
        self.assertEqual(cancels[-1].quantity, 15)
        self.assertEqual(cancels[-1].reason, "market_unfilled")

    def test_market_into_empty_book_cancels_everything(self):
        eng = MatchingEngine()
        fills = eng.submit_market(mkt("m1", Side.SELL, 10))
        self.assertEqual(fills, [])
        cancels = [e for e in eng.events if isinstance(e, Canceled)]
        self.assertEqual(cancels[0].quantity, 10)

    def test_market_sell_walks_bids(self):
        eng = MatchingEngine()
        eng.submit_limit(buy("b1", 102, 4))
        eng.submit_limit(buy("b2", 100, 4))
        fills = eng.submit_market(mkt("m1", Side.SELL, 6))
        self.assertEqual(
            [(f.maker_order_id, f.price, f.quantity) for f in fills],
            [("b1", 102, 4), ("b2", 100, 2)],
        )

    def test_market_order_validation(self):
        eng = MatchingEngine()
        with self.assertRaises(ValueError):
            eng.submit_market(buy("b1", 100, 5))  # has a price: not a market order
        with self.assertRaises(ValueError):
            eng.submit_limit(mkt("m1", Side.BUY, 5))  # no price: not a limit

    def test_market_order_cannot_rest_on_book_directly(self):
        eng = MatchingEngine()
        with self.assertRaises(ValueError):
            eng.book.bids.add(mkt("m1", Side.BUY, 5))


if __name__ == "__main__":
    unittest.main()
