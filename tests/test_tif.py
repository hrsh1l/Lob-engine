"""Step 6 tests: time-in-force (GTC default, IOC, FOK)."""

import unittest

from lob.engine import MatchingEngine
from lob.events import Canceled
from lob.types import Order, Side, TimeInForce


def buy(oid, price, qty):
    return Order(oid, Side.BUY, price=price, quantity=qty)


def sell(oid, price, qty):
    return Order(oid, Side.SELL, price=price, quantity=qty)


class TestIOC(unittest.TestCase):
    def test_ioc_partial_fill_cancels_remainder(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 4))
        fills = eng.submit_limit(buy("b1", 100, 10), tif=TimeInForce.IOC)
        self.assertEqual(sum(f.quantity for f in fills), 4)
        self.assertIsNone(eng.book.best_bid())  # remainder did NOT rest
        cancel = [e for e in eng.events if isinstance(e, Canceled)][-1]
        self.assertEqual((cancel.order_id, cancel.quantity, cancel.reason),
                         ("b1", 6, "ioc_expired"))

    def test_ioc_no_liquidity_cancels_all(self):
        eng = MatchingEngine()
        fills = eng.submit_limit(buy("b1", 100, 10), tif=TimeInForce.IOC)
        self.assertEqual(fills, [])
        self.assertIsNone(eng.book.best_bid())

    def test_ioc_full_fill_no_cancel_event(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 10))
        fills = eng.submit_limit(buy("b1", 100, 10), tif=TimeInForce.IOC)
        self.assertEqual(sum(f.quantity for f in fills), 10)
        self.assertFalse([e for e in eng.events if isinstance(e, Canceled)])

    def test_ioc_respects_limit_price(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("cheap", 100, 5))
        eng.submit_limit(sell("dear", 105, 5))
        fills = eng.submit_limit(buy("b1", 102, 10), tif=TimeInForce.IOC)
        # takes the 100s, won't touch the 105s, cancels the rest
        self.assertEqual(sum(f.quantity for f in fills), 5)
        self.assertEqual(eng.book.best_ask(), 105)
        self.assertIsNone(eng.book.best_bid())


class TestFOK(unittest.TestCase):
    def test_fok_fills_fully_when_possible(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 6))
        eng.submit_limit(sell("s2", 101, 6))
        fills = eng.submit_limit(buy("b1", 101, 10), tif=TimeInForce.FOK)
        self.assertEqual(sum(f.quantity for f in fills), 10)

    def test_fok_insufficient_liquidity_no_fills_at_all(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 6))
        fills = eng.submit_limit(buy("b1", 100, 10), tif=TimeInForce.FOK)
        self.assertEqual(fills, [])
        # the resting sell is untouched — not even partially filled
        self.assertEqual(eng.book.asks.best_level().total_qty, 6)
        cancel = [e for e in eng.events if isinstance(e, Canceled)][-1]
        self.assertEqual((cancel.order_id, cancel.reason), ("b1", "fok_kill"))

    def test_fok_only_counts_liquidity_within_limit(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 6))
        eng.submit_limit(sell("s2", 105, 6))  # outside the 101 limit
        fills = eng.submit_limit(buy("b1", 101, 10), tif=TimeInForce.FOK)
        self.assertEqual(fills, [])  # 6 within limit < 10 needed → kill

    def test_fok_ignores_cancelled_quantity(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 6))
        eng.submit_limit(sell("s2", 100, 6))
        eng.cancel("s2")
        # only 6 live at 100, not 12
        fills = eng.submit_limit(buy("b1", 100, 10), tif=TimeInForce.FOK)
        self.assertEqual(fills, [])

    def test_fok_exact_size_works(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 10))
        fills = eng.submit_limit(buy("b1", 100, 10), tif=TimeInForce.FOK)
        self.assertEqual(sum(f.quantity for f in fills), 10)


class TestGTCDefault(unittest.TestCase):
    def test_default_tif_is_gtc_and_rests(self):
        eng = MatchingEngine()
        eng.submit_limit(buy("b1", 100, 10))  # no tif argument
        self.assertEqual(eng.book.best_bid(), 100)


if __name__ == "__main__":
    unittest.main()
