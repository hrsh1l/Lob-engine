"""Step 7 tests: book queries and the event stream."""

import unittest

from lob.engine import MatchingEngine
from lob.events import Ack, Canceled
from lob.types import Fill, Order, Side, TimeInForce


def buy(oid, price, qty):
    return Order(oid, Side.BUY, price=price, quantity=qty)


def sell(oid, price, qty):
    return Order(oid, Side.SELL, price=price, quantity=qty)


class TestDepth(unittest.TestCase):
    def setUp(self):
        self.eng = MatchingEngine()
        for oid, px, q in [("b1", 100, 10), ("b2", 99, 20), ("b3", 98, 30),
                           ("b4", 100, 5)]:
            self.eng.submit_limit(buy(oid, px, q))
        for oid, px, q in [("s1", 101, 10), ("s2", 102, 20), ("s3", 103, 30)]:
            self.eng.submit_limit(sell(oid, px, q))

    def test_depth_aggregates_levels_best_first(self):
        d = self.eng.depth(2)
        self.assertEqual(d["bids"], [(100, 15), (99, 20)])  # b1+b4 share 100
        self.assertEqual(d["asks"], [(101, 10), (102, 20)])

    def test_depth_n_larger_than_book(self):
        d = self.eng.depth(10)
        self.assertEqual(len(d["bids"]), 3)
        self.assertEqual(len(d["asks"]), 3)

    def test_depth_reflects_fills_and_cancels(self):
        self.eng.submit_limit(buy("taker", 101, 4))   # eats 4 of s1
        self.eng.cancel("b2")
        d = self.eng.depth(3)
        self.assertEqual(d["asks"][0], (101, 6))
        self.assertEqual(d["bids"], [(100, 15), (98, 30)])

    def test_best_bid_ask_helpers(self):
        self.assertEqual(self.eng.best_bid(), 100)
        self.assertEqual(self.eng.best_ask(), 101)


class TestEventStream(unittest.TestCase):
    def test_event_sequence_for_a_partial_cross(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 4))
        eng.submit_limit(buy("b1", 100, 10))
        kinds = [type(e).__name__ for e in eng.events]
        # s1 rests (Ack), then b1 fills 4 (Fill) and rests 6 (Ack)
        self.assertEqual(kinds, ["Ack", "Fill", "Ack"])
        ack_b1 = eng.events[2]
        self.assertEqual((ack_b1.order_id, ack_b1.remaining), ("b1", 6))

    def test_replaying_events_reconstructs_depth(self):
        """The event stream is a complete record: rebuild the book from it."""
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 101, 10))
        eng.submit_limit(sell("s2", 102, 8))
        eng.submit_limit(buy("b1", 101, 4))
        eng.submit_limit(buy("b2", 99, 7))
        eng.cancel("b2")
        eng.submit_limit(buy("b3", 100, 3), tif=TimeInForce.IOC)  # cancels

        open_qty: dict[str, list] = {}  # order_id -> [side, price, qty]
        for e in eng.events:
            if isinstance(e, Ack):
                open_qty[e.order_id] = [e.side, e.price, e.remaining]
            elif isinstance(e, Fill):
                if e.maker_order_id in open_qty:
                    open_qty[e.maker_order_id][2] -= e.quantity
                    if open_qty[e.maker_order_id][2] == 0:
                        del open_qty[e.maker_order_id]
            elif isinstance(e, Canceled):
                open_qty.pop(e.order_id, None)

        rebuilt_asks: dict[int, int] = {}
        rebuilt_bids: dict[int, int] = {}
        for side, price, qty in open_qty.values():
            tgt = rebuilt_bids if side is Side.BUY else rebuilt_asks
            tgt[price] = tgt.get(price, 0) + qty

        d = eng.depth(10)
        self.assertEqual(sorted(rebuilt_bids.items()), sorted(d["bids"]))
        self.assertEqual(sorted(rebuilt_asks.items()), sorted(d["asks"]))

    def test_callback_receives_events_in_order(self):
        seen = []
        eng = MatchingEngine(on_event=seen.append)
        eng.submit_limit(sell("s1", 100, 5))
        eng.submit_limit(buy("b1", 100, 5))
        self.assertEqual(seen, eng.events)
        self.assertEqual([type(e).__name__ for e in seen], ["Ack", "Fill"])

    def test_drain_events(self):
        eng = MatchingEngine()
        eng.submit_limit(buy("b1", 100, 5))
        first = eng.drain_events()
        self.assertEqual(len(first), 1)
        self.assertEqual(eng.events, [])
        eng.cancel("b1")
        second = eng.drain_events()
        self.assertEqual([type(e).__name__ for e in second], ["Canceled"])

    def test_open_order_lookup(self):
        eng = MatchingEngine()
        eng.submit_limit(buy("b1", 100, 5))
        self.assertEqual(eng.open_order("b1").remaining, 5)
        self.assertIsNone(eng.open_order("nope"))


if __name__ == "__main__":
    unittest.main()
