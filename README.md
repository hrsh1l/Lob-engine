# lob_engine

A limit order book matching engine in pure Python (stdlib only).
Price-time priority, single instrument, designed so multi-instrument is a
`dict[symbol, MatchingEngine]` away.

Order types: limit (GTC/IOC/FOK, optional post-only), market, stop-market,
stop-limit (with cascade triggering off the last-trade price). The engine
enforces configurable tick and lot sizes, and every fill carries a
monotonic trade id and the aggressor side, like a real execution feed.

`python gui.py` opens an interactive front-end: order entry, live book
ladder, last-trade chart, time & sales tape colored by aggressor, session
VWAP/volume stats, and a realistic order-flow simulator (Poisson arrivals,
heavy-tailed sizes, mean-reverting fair value with momentum bursts).

## Usage

```python
from lob import MatchingEngine, Order, Side, TimeInForce

eng = MatchingEngine()

eng.submit_limit(Order("s1", Side.SELL, price=10050, quantity=100))   # rests
fills = eng.submit_limit(Order("b1", Side.BUY, price=10060, quantity=40))
# fills -> [Fill(maker='s1', taker='b1', price=10050, qty=40)]  maker's price!

eng.submit_market(Order("m1", Side.BUY, price=None, quantity=10))
eng.submit_limit(Order("b2", Side.BUY, 10040, 50), tif=TimeInForce.IOC)
eng.cancel("s1")
eng.modify("b2", new_qty=30)          # decrease: keeps queue priority
eng.modify("b2", new_price=10055)     # reprice: loses priority, may trade

eng.best_bid(), eng.best_ask()
eng.depth(5)          # {"bids": [(price, qty), ...], "asks": [...]}
eng.drain_events()    # Ack / Fill / Canceled / Modified / Rejected, in order
```

## Layout

| File | Contents |
|---|---|
| `lob/types.py` | `Side`, `TimeInForce`, `Order`, `Fill`; integer-tick validation |
| `lob/book.py` | `PriceLevel` (FIFO deque + running qty), `BookSide` (dict + sorted keys), `OrderBook` |
| `lob/events.py` | `Ack`, `Canceled`, `Modified`, `Rejected` (+ `Fill`) |
| `lob/engine.py` | `MatchingEngine`: matching loop, TIF, cancel/modify, queries, events |
| `tests/` | 89 tests incl. differential testing vs. an independent naive matcher |
| `bench.py` | throughput benchmark |

## Testing

```
python -m unittest discover -s tests
```

The strongest check is `tests/test_properties.py`: a deliberately naive
reference matcher (flat dict, linear scans, zero shared code) processes the
same randomized order flow — adds, IOC/FOK, markets, cancels, modifies —
and fills, open-order sets, and full depth are compared after **every**
operation, alongside book invariants (never crossed/locked, per-level
quantity accounting, id-index ↔ book consistency).

## Design notes

- **Integer ticks.** Prices/quantities are validated `int`s; floats raise.
- **Maker's price.** Fills always execute at the resting order's price.
- **Time priority** is an engine-assigned monotonic sequence, not wall time.
- **Lazy cancellation.** `cancel()` flips a flag and fixes quantity
  accounting in O(1); dead orders are skimmed off queue fronts during
  matching. A level whose live quantity hits zero is removed eagerly so
  best-price queries never see zombies.
- **Modify semantics.** Quantity decrease is in-place (priority kept);
  price change or size increase is cancel + re-enter (priority lost,
  re-matched — a repricing that crosses will trade immediately).
- **Events.** Every state change emits exactly one event; replaying the
  stream reconstructs the book (tested).

## Benchmark (Python 3.12, this machine)

```
add        100,000 ops in  0.680s  ->  146,961 ops/s  (6.80 us/op)
cancel     100,000 ops in  0.555s  ->  180,188 ops/s  (5.55 us/op)
mixed      100,000 ops in  0.609s  ->  164,202 ops/s  (6.09 us/op)
```

## What a production engine does differently

~150k ops/s is fine for research/backtesting; real venues do millions/sec
at sub-microsecond latency. The gap is closed by:

- **No interpreter.** C++/Rust; the hot path is branch-predictable,
  allocation-free machine code.
- **Arena / pool allocation.** Orders live in a preallocated slab, referenced
  by index; no GC, no `malloc` in the hot path, cache-friendly layout.
- **Intrusive doubly-linked lists** per level: cancel unlinks in true O(1)
  with zero allocation, instead of lazy flags or deque scans.
- **Dense price arrays.** Instruments trade in a narrow band, so levels are
  a flat array indexed by tick offset — best-price moves are pointer bumps,
  not tree walks.
- **Sequence-numbered input, single-threaded core.** One thread owns the
  book (no locks); determinism comes from a totally ordered input log, which
  also gives replayability and hot-standby failover for free.
- **Binary protocols** (ITCH/OUCH-style) and kernel-bypass networking; the
  event stream here is the toy version of a market-data feed.
