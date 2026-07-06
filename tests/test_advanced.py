"""Tests for market-structure accuracy features: stops, post-only,
tick/lot size enforcement, trade ids and aggressor flags."""

import unittest

from lob.engine import MatchingEngine
from lob.events import Canceled, Rejected, StopPlaced, Triggered
from lob.types import Fill, Order, Side, TimeInForce


def buy(oid, price, qty, **kw):
    return Order(oid, Side.BUY, price=price, quantity=qty, **kw)


def sell(oid, price, qty, **kw):
    return Order(oid, Side.SELL, price=price, quantity=qty, **kw)


class TestTradeMetadata(unittest.TestCase):
    def test_trade_ids_monotonic_and_aggressor_recorded(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 5))
        eng.submit_limit(sell("s2", 101, 5))
        fills = eng.submit_limit(buy("b1", 101, 10))
        self.assertEqual([f.trade_id for f in fills], [1, 2])
        self.assertTrue(all(f.taker_side is Side.BUY for f in fills))
        self.assertEqual(eng.last_trade_price, 101)

    def test_last_trade_price_tracks_tape(self):
        eng = MatchingEngine()
        self.assertIsNone(eng.last_trade_price)
        eng.submit_limit(buy("b1", 100, 5))
        eng.submit_limit(sell("s1", 100, 5))
        self.assertEqual(eng.last_trade_price, 100)


class TestTickAndLotSize(unittest.TestCase):
    def test_off_tick_price_rejected(self):
        eng = MatchingEngine(tick_size=5)
        fills = eng.submit_limit(buy("b1", 102, 10))  # 102 % 5 != 0
        self.assertEqual(fills, [])
        rej = [e for e in eng.events if isinstance(e, Rejected)][0]
        self.assertEqual(rej.reason, "price_off_tick")
        self.assertIsNone(eng.best_bid())

    def test_odd_lot_rejected(self):
        eng = MatchingEngine(lot_size=100)
        eng.submit_limit(buy("b1", 100, 150))
        rej = [e for e in eng.events if isinstance(e, Rejected)][0]
        self.assertEqual(rej.reason, "odd_lot")

    def test_conforming_orders_pass(self):
        eng = MatchingEngine(tick_size=5, lot_size=10)
        eng.submit_limit(buy("b1", 105, 20))
        self.assertEqual(eng.best_bid(), 105)

    def test_bad_engine_config(self):
        with self.assertRaises(ValueError):
            MatchingEngine(tick_size=0)


class TestPostOnly(unittest.TestCase):
    def test_post_only_rests_when_passive(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 101, 5))
        eng.submit_limit(buy("b1", 100, 5, post_only=True))
        self.assertEqual(eng.best_bid(), 100)

    def test_post_only_rejected_when_it_would_cross(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 101, 5))
        fills = eng.submit_limit(buy("b1", 101, 5, post_only=True))
        self.assertEqual(fills, [])
        rej = [e for e in eng.events if isinstance(e, Rejected)][0]
        self.assertEqual(rej.reason, "post_only_would_cross")
        # the resting ask is untouched and nothing rested on the bid
        self.assertEqual(eng.book.asks.best_level().total_qty, 5)
        self.assertIsNone(eng.best_bid())


class TestStops(unittest.TestCase):
    def _seed(self, eng):
        """Book around 100 with a last trade at 100."""
        eng.submit_limit(buy("bid1", 99, 50))
        eng.submit_limit(buy("bid2", 98, 50))
        eng.submit_limit(sell("ask1", 101, 50))
        eng.submit_limit(sell("ask2", 102, 50))
        eng.submit_limit(sell("seed", 100, 1))
        eng.submit_limit(buy("seed2", 100, 1))  # prints 100 on the tape

    def test_stop_parks_until_trigger(self):
        eng = MatchingEngine()
        self._seed(eng)
        # sell stop-market: triggers if the market trades down to 99
        eng.submit_stop(Order("stp1", Side.SELL, None, 10), stop_price=99)
        self.assertEqual(len(eng.pending_stops()), 1)
        self.assertTrue(any(isinstance(e, StopPlaced) for e in eng.events))
        # an uptick trade at 101 must NOT trigger a 99 sell stop
        eng.submit_limit(buy("up", 101, 1))
        self.assertEqual(len(eng.pending_stops()), 1)
        # now trade down through 99
        eng.submit_limit(sell("down", 99, 1))
        self.assertEqual(eng.pending_stops(), [])
        trig = [e for e in eng.events if isinstance(e, Triggered)][0]
        self.assertEqual((trig.order_id, trig.stop_price), ("stp1", 99))
        # the stop-market sold 10 into the bids
        fills = [e for e in eng.events if isinstance(e, Fill)
                 and e.taker_order_id == "stp1"]
        self.assertEqual(sum(f.quantity for f in fills), 10)

    def test_buy_stop_triggers_on_uptick(self):
        eng = MatchingEngine()
        self._seed(eng)
        eng.submit_stop(Order("stp1", Side.BUY, None, 5), stop_price=101)
        eng.submit_limit(buy("up", 101, 1))  # prints 101
        self.assertEqual(eng.pending_stops(), [])

    def test_stop_limit_rests_after_trigger(self):
        eng = MatchingEngine()
        self._seed(eng)
        # sell stop-limit: trigger 99, limit 97 (rests if book runs out above 97)
        eng.submit_stop(Order("stp1", Side.SELL, 97, 200), stop_price=99)
        eng.submit_limit(sell("down", 99, 1))
        # it sold what it could >= 97 and the remainder rests at 97
        self.assertEqual(eng.best_ask(), 97)
        self.assertIsNotNone(eng.open_order("stp1"))

    def test_stop_already_triggered_fires_immediately(self):
        eng = MatchingEngine()
        self._seed(eng)  # last trade = 100
        eng.submit_stop(Order("stp1", Side.BUY, None, 5), stop_price=100)
        self.assertEqual(eng.pending_stops(), [])  # fired on entry

    def test_stop_cascade(self):
        eng = MatchingEngine()
        self._seed(eng)
        # two sell stops: the first one's executions trigger the second
        eng.submit_stop(Order("stpA", Side.SELL, None, 60), stop_price=99)
        eng.submit_stop(Order("stpB", Side.SELL, None, 10), stop_price=98)
        eng.submit_limit(sell("down", 99, 1))  # prints 99 -> A fires
        # A's 60 lots eat the 99s and print 98 -> B fires too
        self.assertEqual(eng.pending_stops(), [])
        b_fills = [e for e in eng.events if isinstance(e, Fill)
                   and e.taker_order_id == "stpB"]
        self.assertTrue(b_fills)

    def test_cancel_pending_stop(self):
        eng = MatchingEngine()
        self._seed(eng)
        eng.submit_stop(Order("stp1", Side.SELL, None, 10), stop_price=95)
        self.assertTrue(eng.cancel("stp1"))
        self.assertEqual(eng.pending_stops(), [])
        self.assertFalse(eng.cancel("stp1"))

    def test_stop_id_collides_with_open_order(self):
        eng = MatchingEngine()
        self._seed(eng)
        ok = eng.submit_stop(Order("bid1", Side.SELL, None, 10), stop_price=95)
        self.assertFalse(ok)
        rej = [e for e in eng.events if isinstance(e, Rejected)][-1]
        self.assertEqual(rej.reason, "duplicate_order_id")

    def test_stop_price_validation(self):
        eng = MatchingEngine()
        with self.assertRaises(ValueError):
            eng.submit_stop(Order("s", Side.SELL, None, 10), stop_price=-1)
        with self.assertRaises(TypeError):
            eng.submit_stop(Order("s", Side.SELL, None, 10), stop_price=99.5)


if __name__ == "__main__":
    unittest.main()
