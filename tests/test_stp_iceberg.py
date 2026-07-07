"""Tests for self-trade prevention and iceberg orders."""

import unittest

from lob.engine import MatchingEngine
from lob.events import Canceled
from lob.types import Fill, Order, Side


def buy(oid, price, qty, **kw):
    return Order(oid, Side.BUY, price=price, quantity=qty, **kw)


def sell(oid, price, qty, **kw):
    return Order(oid, Side.SELL, price=price, quantity=qty, **kw)


def fills_of(eng):
    return [e for e in eng.events if isinstance(e, Fill)]


class TestSTP(unittest.TestCase):
    def test_off_by_default_self_trade_happens(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 5, owner="a"))
        fills = eng.submit_limit(buy("b1", 100, 5, owner="a"))
        self.assertEqual(len(fills), 1)  # library default: no STP

    def test_cancel_resting_skips_own_and_matches_next(self):
        eng = MatchingEngine(stp="cancel_resting")
        eng.submit_limit(sell("own", 100, 5, owner="a"))
        eng.submit_limit(sell("other", 100, 5, owner="b"))
        fills = eng.submit_limit(buy("b1", 100, 5, owner="a"))
        # own order cancelled, trade happens against b's order
        self.assertEqual([(f.maker_order_id, f.quantity) for f in fills],
                         [("other", 5)])
        xcl = [e for e in eng.events if isinstance(e, Canceled)][0]
        self.assertEqual((xcl.order_id, xcl.quantity, xcl.reason),
                         ("own", 5, "stp"))
        self.assertIsNone(eng.open_order("own"))

    def test_cancel_incoming_kills_taker_remainder(self):
        eng = MatchingEngine(stp="cancel_incoming")
        eng.submit_limit(sell("other", 100, 3, owner="b"))
        eng.submit_limit(sell("own", 100, 5, owner="a"))
        fills = eng.submit_limit(buy("b1", 100, 10, owner="a"))
        # fills 3 against b, then meets own order and dies
        self.assertEqual(sum(f.quantity for f in fills), 3)
        xcl = [e for e in eng.events if isinstance(e, Canceled)][0]
        self.assertEqual((xcl.order_id, xcl.quantity, xcl.reason),
                         ("b1", 7, "stp"))
        # own resting order untouched, taker did not rest
        self.assertEqual(eng.open_order("own").remaining, 5)
        self.assertIsNone(eng.best_bid())

    def test_anonymous_orders_never_stp(self):
        eng = MatchingEngine(stp="cancel_resting")
        eng.submit_limit(sell("s1", 100, 5))          # owner None
        fills = eng.submit_limit(buy("b1", 100, 5))   # owner None
        self.assertEqual(len(fills), 1)

    def test_bad_policy_rejected(self):
        with self.assertRaises(ValueError):
            MatchingEngine(stp="nope")


class TestIceberg(unittest.TestCase):
    def test_depth_shows_only_display(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("ice", 100, 30, display=10))
        self.assertEqual(eng.depth()["asks"], [(100, 10)])
        self.assertEqual(eng.open_order("ice").remaining, 30)

    def test_tranche_reload_goes_to_back_of_queue(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("ice", 100, 30, display=10))
        eng.submit_limit(sell("lit", 100, 5))
        fills = eng.submit_limit(buy("b1", 100, 12))
        # 10 from the iceberg's first tranche, then the LIT order (the
        # reloaded tranche re-queued BEHIND it), then 0 more needed
        self.assertEqual([(f.maker_order_id, f.quantity) for f in fills],
                         [("ice", 10), ("lit", 2)])
        # level shows lit remainder 3 + fresh tranche 10
        self.assertEqual(eng.depth()["asks"], [(100, 13)])

    def test_iceberg_exhausts_through_multiple_reloads(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("ice", 100, 25, display=10))
        fills = eng.submit_limit(buy("b1", 100, 25))
        self.assertEqual([f.quantity for f in fills], [10, 10, 5])
        self.assertTrue(all(f.maker_order_id == "ice" for f in fills))
        self.assertIsNone(eng.best_ask())
        self.assertIsNone(eng.open_order("ice"))

    def test_cancel_iceberg_cancels_hidden_too(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("ice", 100, 30, display=10))
        eng.submit_limit(buy("b1", 100, 4))  # visible now 6, hidden 20
        self.assertTrue(eng.cancel("ice"))
        xcl = [e for e in eng.events if isinstance(e, Canceled)][-1]
        self.assertEqual(xcl.quantity, 26)
        self.assertIsNone(eng.best_ask())

    def test_modify_decrease_iceberg_keeps_display(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("ice", 100, 30, display=10))
        eng.modify("ice", new_qty=20)
        self.assertEqual(eng.depth()["asks"], [(100, 10)])
        self.assertEqual(eng.open_order("ice").remaining, 20)

    def test_incoming_iceberg_matches_full_size_then_rests_display(self):
        eng = MatchingEngine()
        eng.submit_limit(buy("b1", 100, 12))
        # incoming iceberg sells its full 12 against the bid, remainder
        # 18 rests showing 10
        fills = eng.submit_limit(sell("ice", 100, 30, display=10))
        self.assertEqual(sum(f.quantity for f in fills), 12)
        self.assertEqual(eng.depth()["asks"], [(100, 10)])
        self.assertEqual(eng.open_order("ice").remaining, 18)

    def test_display_validation(self):
        with self.assertRaises(ValueError):
            Order("x", Side.BUY, 100, 10, display=0)
        with self.assertRaises(TypeError):
            Order("x", Side.BUY, 100, 10, display=2.5)


if __name__ == "__main__":
    unittest.main()
