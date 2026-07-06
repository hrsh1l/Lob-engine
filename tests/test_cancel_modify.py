"""Step 5 tests: cancel and modify."""

import unittest

from lob.engine import MatchingEngine
from lob.events import Canceled, Modified
from lob.types import Order, Side


def buy(oid, price, qty):
    return Order(oid, Side.BUY, price=price, quantity=qty)


def sell(oid, price, qty):
    return Order(oid, Side.SELL, price=price, quantity=qty)


class TestCancel(unittest.TestCase):
    def test_cancel_removes_liquidity(self):
        eng = MatchingEngine()
        eng.submit_limit(buy("b1", 100, 10))
        self.assertTrue(eng.cancel("b1"))
        self.assertIsNone(eng.book.best_bid())
        # and a later sell at 100 finds nothing to hit
        self.assertEqual(eng.submit_limit(sell("s1", 100, 5)), [])

    def test_cancel_unknown_or_repeated_id(self):
        eng = MatchingEngine()
        self.assertFalse(eng.cancel("ghost"))
        eng.submit_limit(buy("b1", 100, 10))
        self.assertTrue(eng.cancel("b1"))
        self.assertFalse(eng.cancel("b1"))  # already cancelled

    def test_cancel_filled_order_fails(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 5))
        eng.submit_limit(buy("b1", 100, 5))  # fully fills s1
        self.assertFalse(eng.cancel("s1"))

    def test_cancel_mid_queue_preserves_neighbors_fifo(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("first", 100, 5))
        eng.submit_limit(sell("victim", 100, 5))
        eng.submit_limit(sell("third", 100, 5))
        eng.cancel("victim")
        self.assertEqual(eng.book.asks.best_level().total_qty, 10)
        fills = eng.submit_limit(buy("b1", 100, 10))
        self.assertEqual(
            [(f.maker_order_id, f.quantity) for f in fills],
            [("first", 5), ("third", 5)],  # victim skipped
        )
        self.assertIsNone(eng.book.best_ask())  # level fully gone

    def test_cancel_last_live_order_drops_level(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 5))
        eng.submit_limit(sell("s2", 101, 5))
        eng.cancel("s1")
        self.assertEqual(eng.book.best_ask(), 101)

    def test_cancel_partially_filled_order_cancels_remainder_only(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("s1", 100, 10))
        eng.submit_limit(buy("b1", 100, 4))  # s1 has 6 left
        self.assertTrue(eng.cancel("s1"))
        cancels = [e for e in eng.events if isinstance(e, Canceled)]
        self.assertEqual(cancels[-1].quantity, 6)

    def test_zombie_level_not_reported_as_best(self):
        # Fill the only live order at a level that still holds a dead one:
        # the level must disappear, not linger at qty 0.
        eng = MatchingEngine()
        eng.submit_limit(sell("live", 100, 5))
        eng.submit_limit(sell("dead", 100, 5))
        eng.cancel("dead")
        eng.submit_limit(buy("b1", 100, 5))  # consumes 'live' exactly
        self.assertIsNone(eng.book.best_ask())
        self.assertEqual(eng.depth()["asks"], [])


class TestModify(unittest.TestCase):
    def test_qty_decrease_keeps_priority(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("keeper", 100, 10))
        eng.submit_limit(sell("rival", 100, 10))
        self.assertTrue(eng.modify("keeper", new_qty=4))
        fills = eng.submit_limit(buy("b1", 100, 4))
        self.assertEqual(fills[0].maker_order_id, "keeper")  # still first
        mods = [e for e in eng.events if isinstance(e, Modified)]
        self.assertEqual((mods[0].order_id, mods[0].remaining), ("keeper", 4))

    def test_qty_increase_loses_priority(self):
        eng = MatchingEngine()
        eng.submit_limit(sell("grower", 100, 5))
        eng.submit_limit(sell("rival", 100, 5))
        eng.modify("grower", new_qty=8)
        fills = eng.submit_limit(buy("b1", 100, 5))
        self.assertEqual(fills[0].maker_order_id, "rival")  # grower went to the back
        self.assertEqual(eng.open_order("grower").remaining, 8)

    def test_price_change_loses_priority_and_may_trade(self):
        eng = MatchingEngine()
        eng.submit_limit(buy("b1", 99, 5))
        eng.submit_limit(sell("s1", 101, 5))
        # repricing the sell down to 99 crosses the bid and trades immediately
        self.assertTrue(eng.modify("s1", new_price=99))
        fills = [e for e in eng.events if type(e).__name__ == "Fill"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].price, 99)  # maker (the resting bid... ) price
        self.assertIsNone(eng.book.best_ask())
        self.assertIsNone(eng.book.best_bid())

    def test_modify_unknown_id(self):
        eng = MatchingEngine()
        self.assertFalse(eng.modify("ghost", new_qty=5))

    def test_modify_to_nonpositive_qty_rejected(self):
        eng = MatchingEngine()
        eng.submit_limit(buy("b1", 100, 5))
        with self.assertRaises(ValueError):
            eng.modify("b1", new_qty=0)

    def test_noop_modify(self):
        eng = MatchingEngine()
        eng.submit_limit(buy("b1", 100, 5))
        self.assertTrue(eng.modify("b1", new_qty=5))
        # still resting, same priority object
        self.assertEqual(eng.open_order("b1").remaining, 5)


class TestDuplicateIds(unittest.TestCase):
    def test_duplicate_open_id_rejected(self):
        eng = MatchingEngine()
        eng.submit_limit(buy("b1", 100, 5))
        fills = eng.submit_limit(buy("b1", 101, 5))
        self.assertEqual(fills, [])
        rejects = [e for e in eng.events if type(e).__name__ == "Rejected"]
        self.assertEqual(rejects[0].reason, "duplicate_order_id")
        # book unchanged: still just the original 5 @ 100
        self.assertEqual(eng.book.best_bid(), 100)
        self.assertEqual(eng.book.bids.best_level().total_qty, 5)

    def test_id_reusable_after_cancel(self):
        eng = MatchingEngine()
        eng.submit_limit(buy("b1", 100, 5))
        eng.cancel("b1")
        self.assertEqual(eng.submit_limit(buy("b1", 100, 5)), [])
        self.assertEqual(eng.book.best_bid(), 100)


if __name__ == "__main__":
    unittest.main()
