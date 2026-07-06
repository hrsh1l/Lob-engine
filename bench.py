"""Step 9: throughput benchmark.

Run:  python bench.py

Three workloads, each measured over N operations:
  1. add     — non-crossing GTC limits (pure book insertion)
  2. cancel  — add N resting orders, then cancel all N
  3. mixed   — realistic flow: 50% add, 30% cancel, 20% aggressive limits
"""

import random
import time

from lob.engine import MatchingEngine
from lob.types import Order, Side

N = 100_000


def bench(name, n, fn):
    t0 = time.perf_counter()
    fn()
    dt = time.perf_counter() - t0
    print(f"{name:8s} {n:>9,} ops in {dt:6.3f}s  ->  {n / dt:>10,.0f} ops/s"
          f"  ({dt / n * 1e6:.2f} us/op)")


def workload_add():
    eng = MatchingEngine()
    rng = random.Random(7)
    orders = [
        Order(f"o{i}",
              Side.BUY if rng.random() < 0.5 else Side.SELL,
              price=rng.randint(50, 99) if rng.random() < 0.5 else rng.randint(101, 150),
              quantity=rng.randint(1, 100))
        for i in range(N)
    ]
    # bids 50-99, asks 101-150: nothing ever crosses
    orders = [o if (o.side is Side.BUY) == (o.price < 100) else
              Order(o.order_id, o.side.opposite, o.price, o.quantity)
              for o in orders]

    def run():
        for o in orders:
            eng.submit_limit(o)
    return run


def workload_cancel():
    eng = MatchingEngine()
    rng = random.Random(8)
    ids = []
    for i in range(N):
        px = rng.randint(50, 99)
        eng.submit_limit(Order(f"c{i}", Side.BUY, px, rng.randint(1, 100)))
        ids.append(f"c{i}")
    rng.shuffle(ids)

    def run():
        for oid in ids:
            eng.cancel(oid)
    return run


def workload_mixed():
    rng = random.Random(9)
    eng = MatchingEngine()
    script = []  # pre-generate so we time only engine work
    live = []
    for i in range(N):
        r = rng.random()
        if r < 0.5 or not live:
            side = Side.BUY if rng.random() < 0.5 else Side.SELL
            px = rng.randint(95, 99) if side is Side.BUY else rng.randint(101, 105)
            script.append(("add", f"m{i}", side, px, rng.randint(1, 50)))
            live.append(f"m{i}")
        elif r < 0.8:
            script.append(("cancel", live.pop(rng.randrange(len(live)))))
        else:
            side = Side.BUY if rng.random() < 0.5 else Side.SELL
            px = 105 if side is Side.BUY else 95  # crosses several levels
            script.append(("add", f"m{i}", side, px, rng.randint(1, 50)))

    def run():
        for op in script:
            if op[0] == "add":
                _, oid, side, px, qty = op
                eng.submit_limit(Order(oid, side, px, qty))
            else:
                eng.cancel(op[1])
            eng.events.clear()  # a real consumer would drain; don't grow forever
    return run


if __name__ == "__main__":
    print(f"lob_engine benchmark, N={N:,} per workload\n")
    bench("add", N, workload_add())
    bench("cancel", N, workload_cancel())
    bench("mixed", N, workload_mixed())
