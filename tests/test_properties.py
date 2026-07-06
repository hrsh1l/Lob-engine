"""Step 8: property & edge-case tests.

The centrepiece is differential testing: a deliberately naive reference
matcher (flat dict of resting orders, linear scans, no shared code with
lob/) processes the same randomized order flow as the real engine. After
every operation we compare fills, the set of open orders, and full book
depth, and we check the real book's internal invariants. Any divergence
pinpoints the operation that caused it.
"""

import random
import unittest

from lob.engine import MatchingEngine
from lob.types import Fill, Order, Side, TimeInForce


# --------------------------------------------------------------------- #
# reference implementation (independent, deliberately naive)
# --------------------------------------------------------------------- #

class RefEngine:
    """Price-time priority matcher built on a flat dict + linear scans."""

    def __init__(self):
        self.resting = {}  # oid -> {side, price, qty, seq}
        self._seq = 0

    def _crosses(self, side, taker_price, maker_price):
        if taker_price is None:
            return True
        if side is Side.BUY:
            return maker_price <= taker_price
        return maker_price >= taker_price

    def _best_maker(self, taker_side, taker_price):
        makers = [
            (oid, o) for oid, o in self.resting.items()
            if o["side"] is taker_side.opposite
            and self._crosses(taker_side, taker_price, o["price"])
        ]
        if not makers:
            return None
        if taker_side is Side.BUY:   # cheapest sell first, then oldest
            return min(makers, key=lambda kv: (kv[1]["price"], kv[1]["seq"]))
        return min(makers, key=lambda kv: (-kv[1]["price"], kv[1]["seq"]))

    def _available(self, taker_side, taker_price):
        return sum(
            o["qty"] for o in self.resting.values()
            if o["side"] is taker_side.opposite
            and self._crosses(taker_side, taker_price, o["price"])
        )

    def limit(self, oid, side, price, qty, tif="GTC"):
        if tif == "FOK" and self._available(side, price) < qty:
            return []
        fills = []
        while qty > 0:
            best = self._best_maker(side, price)
            if best is None:
                break
            moid, maker = best
            take = min(qty, maker["qty"])
            fills.append((moid, maker["price"], take))
            maker["qty"] -= take
            qty -= take
            if maker["qty"] == 0:
                del self.resting[moid]
        if qty > 0 and tif == "GTC" and price is not None:
            self._seq += 1
            self.resting[oid] = {"side": side, "price": price,
                                 "qty": qty, "seq": self._seq}
        return fills

    def market(self, oid, side, qty):
        return self.limit(oid, side, None, qty, tif="IOC")

    def cancel(self, oid):
        return self.resting.pop(oid, None) is not None

    def modify(self, oid, new_price=None, new_qty=None):
        o = self.resting.get(oid)
        if o is None:
            return False
        target = o["qty"] if new_qty is None else new_qty
        price_change = new_price is not None and new_price != o["price"]
        if not price_change and target < o["qty"]:
            o["qty"] = target
            return True
        if not price_change and target == o["qty"]:
            return True
        del self.resting[oid]
        price = new_price if new_price is not None else o["price"]
        self.limit(oid, o["side"], price, target)
        return True

    def depth(self):
        bids, asks = {}, {}
        for o in self.resting.values():
            tgt = bids if o["side"] is Side.BUY else asks
            tgt[o["price"]] = tgt.get(o["price"], 0) + o["qty"]
        return bids, asks


# --------------------------------------------------------------------- #
# invariant checks on the real engine
# --------------------------------------------------------------------- #

def assert_invariants(tc, eng):
    bb, ba = eng.best_bid(), eng.best_ask()
    if bb is not None and ba is not None:
        tc.assertLess(bb, ba, "book is crossed or locked")
    for side in (eng.book.bids, eng.book.asks):
        prev = None
        for level in side.levels_best_first():
            live = sum(o.remaining for o in level.orders if o.active)
            tc.assertEqual(level.total_qty, live,
                           f"level {level.price} qty accounting drifted")
            tc.assertGreater(level.total_qty, 0, "empty level not removed")
            if prev is not None:  # strictly worsening prices
                if side is eng.book.bids:
                    tc.assertLess(level.price, prev)
                else:
                    tc.assertGreater(level.price, prev)
            prev = level.price
    # id index <-> book consistency
    for oid, order in eng._orders.items():
        tc.assertTrue(order.active and not order.is_filled)
        level = eng.book.side(order.side).level_at(order.price)
        tc.assertIsNotNone(level, f"indexed order {oid} has no level")
        tc.assertIn(order, level.orders)


def engine_depth_dicts(eng):
    d = eng.depth(10**9)
    return dict(d["bids"]), dict(d["asks"])


# --------------------------------------------------------------------- #
# the differential simulation
# --------------------------------------------------------------------- #

def run_sim(tc, seed, ops=500):
    rng = random.Random(seed)
    eng = MatchingEngine()
    ref = RefEngine()
    open_ids = []  # mirrors both engines' open sets
    fill_log = []

    for i in range(ops):
        roll = rng.random()
        oid = f"o{seed}_{i}"
        side = rng.choice((Side.BUY, Side.SELL))
        price = rng.randint(95, 105)
        qty = rng.randint(1, 20)

        if roll < 0.55 or not open_ids:  # GTC limit
            got = eng.submit_limit(Order(oid, side, price, qty))
            want = ref.limit(oid, side, price, qty)
        elif roll < 0.65:  # IOC limit
            got = eng.submit_limit(Order(oid, side, price, qty),
                                   tif=TimeInForce.IOC)
            want = ref.limit(oid, side, price, qty, tif="IOC")
        elif roll < 0.70:  # FOK limit
            got = eng.submit_limit(Order(oid, side, price, qty),
                                   tif=TimeInForce.FOK)
            want = ref.limit(oid, side, price, qty, tif="FOK")
        elif roll < 0.75:  # market
            got = eng.submit_market(Order(oid, side, None, qty))
            want = ref.market(oid, side, qty)
        elif roll < 0.90:  # cancel a random open order
            victim = rng.choice(open_ids)
            tc.assertEqual(eng.cancel(victim), ref.cancel(victim), f"op {i}")
            got, want = [], []
        else:  # modify a random open order
            victim = rng.choice(open_ids)
            if rng.random() < 0.5:
                new_qty = rng.randint(1, 20)
                tc.assertEqual(eng.modify(victim, new_qty=new_qty),
                               ref.modify(victim, new_qty=new_qty), f"op {i}")
            else:
                new_price = rng.randint(95, 105)
                tc.assertEqual(eng.modify(victim, new_price=new_price),
                               ref.modify(victim, new_price=new_price), f"op {i}")
            # modify fills surface via events; ref returns them internally —
            # depth comparison below catches any divergence
            got = want = None

        if got is not None:
            got_t = [(f.maker_order_id, f.price, f.quantity) for f in got]
            tc.assertEqual(got_t, want, f"fills diverged at op {i} ({oid})")
            fill_log.extend(got_t)

        open_ids = sorted(ref.resting.keys())
        tc.assertEqual(sorted(eng._orders.keys()), open_ids,
                       f"open-order sets diverged at op {i}")
        tc.assertEqual(engine_depth_dicts(eng), ref.depth(),
                       f"depth diverged at op {i}")
        assert_invariants(tc, eng)

    return fill_log


class TestDifferential(unittest.TestCase):
    def test_random_flow_matches_reference_seed_1(self):
        run_sim(self, seed=1)

    def test_random_flow_matches_reference_seed_2(self):
        run_sim(self, seed=2)

    def test_random_flow_matches_reference_seed_3(self):
        run_sim(self, seed=3)

    def test_determinism_same_seed_same_fills(self):
        a = run_sim(self, seed=42, ops=300)
        b = run_sim(self, seed=42, ops=300)
        self.assertEqual(a, b)


class TestEdgeCases(unittest.TestCase):
    def test_zero_and_negative_quantities_rejected_at_construction(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                Order("x", Side.BUY, price=100, quantity=bad)

    def test_book_never_locks_on_equal_prices(self):
        eng = MatchingEngine()
        eng.submit_limit(Order("s", Side.SELL, 100, 5))
        eng.submit_limit(Order("b", Side.BUY, 100, 5))
        # equal prices must trade, never coexist
        self.assertIsNone(eng.best_bid())
        self.assertIsNone(eng.best_ask())

    def test_giant_sweep_leaves_consistent_book(self):
        eng = MatchingEngine()
        for i in range(50):
            eng.submit_limit(Order(f"s{i}", Side.SELL, 100 + i % 10, 10))
        eng.submit_market(Order("sweep", Side.BUY, None, 10_000))
        self.assertIsNone(eng.best_ask())
        self.assertEqual(eng._orders, {})
        assert_invariants(self, eng)


if __name__ == "__main__":
    unittest.main()
