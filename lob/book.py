"""Order book structure: two sides of sorted price levels.

Data-structure choice
---------------------
Each side is a dict {price -> PriceLevel} plus a sorted list of prices
maintained with bisect. A PriceLevel holds a deque of orders in arrival
order (FIFO = time priority within the level).

Costs, with L = number of *distinct price levels* on a side (typically
tens-to-hundreds, far smaller than order count N):

  - best bid / best ask:            O(1)   (end of the sorted price list)
  - add order to existing level:    O(1)   (dict hit + deque append)
  - add order creating a new level: O(L)   (bisect.insort shifts the list)
  - pop best level when emptied:    O(1)   (list pop from the end)

The main alternative is a heap keyed by price, which makes new-level
insertion O(log L) — but heaps can't cheaply confirm "is this price already
a level?", can't iterate levels in order for depth snapshots, and force
lazy deletion of emptied levels. Since L is small and most adds hit an
existing level, sorted-list-plus-dict is both simpler and faster in
practice. (A production C++ engine would use an intrusive red-black tree
or a dense array indexed by tick — Step 9 discussion.)

Ask prices are stored *negated* so that both sides keep their best price
at the END of the ascending list, where list.pop() is O(1): the best bid
is the highest price (naturally last), and the best ask is the lowest
price (last once negated).
"""

from __future__ import annotations

from bisect import insort
from collections import deque
from typing import Iterator, Optional

from lob.types import Order, Side


class PriceLevel:
    """All resting orders at one price, in FIFO (time-priority) order.

    Tracks total open quantity incrementally so depth queries don't have
    to walk the deque.
    """

    __slots__ = ("price", "orders", "total_qty")

    def __init__(self, price: int) -> None:
        self.price = price
        self.orders: deque[Order] = deque()
        self.total_qty = 0

    def append(self, order: Order) -> None:
        # depth counts only the *displayed* quantity: an iceberg's hidden
        # reserve is invisible to the market, exactly like a real venue
        self.orders.append(order)
        self.total_qty += order.visible

    def reduce(self, qty: int) -> None:
        """Account for `qty` units removed from this level (fill/cancel)."""
        self.total_qty -= qty
        assert self.total_qty >= 0, "level quantity went negative"

    @property
    def is_empty(self) -> bool:
        return not self.orders

    def __len__(self) -> int:
        return len(self.orders)


class BookSide:
    """One side of the book: price levels sorted best-first.

    Prices are stored in `_sorted_keys` ascending, but asks are keyed by
    -price, so the *best* price of either side is always the last element.
    """

    __slots__ = ("side", "_levels", "_sorted_keys")

    def __init__(self, side: Side) -> None:
        self.side = side
        self._levels: dict[int, PriceLevel] = {}   # key -> level
        self._sorted_keys: list[int] = []          # ascending; best is last

    def _key(self, price: int) -> int:
        return -price if self.side is Side.SELL else price

    def add(self, order: Order) -> None:
        """Rest an order on this side (no matching — that's the engine's job)."""
        if order.side is not self.side:
            raise ValueError(f"{order.side} order added to {self.side} side")
        if order.price is None:
            raise ValueError("market orders cannot rest on the book")
        key = self._key(order.price)
        level = self._levels.get(key)
        if level is None:
            level = PriceLevel(order.price)
            self._levels[key] = level
            insort(self._sorted_keys, key)  # O(L), only on new levels
        level.append(order)

    def level_at(self, price: int) -> Optional[PriceLevel]:
        return self._levels.get(self._key(price))

    def best_level(self) -> Optional[PriceLevel]:
        # Emptied levels are removed eagerly, so the last key is always live.
        if not self._sorted_keys:
            return None
        return self._levels[self._sorted_keys[-1]]

    def best_price(self) -> Optional[int]:
        level = self.best_level()
        return level.price if level else None

    def remove_level(self, price: int) -> None:
        """Drop an emptied price level. O(1) if it was the best level."""
        key = self._key(price)
        level = self._levels.pop(key)
        assert level.is_empty, "removing a non-empty level"
        if self._sorted_keys and self._sorted_keys[-1] == key:
            self._sorted_keys.pop()
        else:
            self._sorted_keys.remove(key)  # rare: cancel emptied a mid-book level

    def levels_best_first(self) -> Iterator[PriceLevel]:
        """Iterate levels from best to worst (for depth snapshots)."""
        for key in reversed(self._sorted_keys):
            yield self._levels[key]

    @property
    def is_empty(self) -> bool:
        return not self._sorted_keys

    def __len__(self) -> int:
        """Number of price levels (not orders)."""
        return len(self._sorted_keys)


class OrderBook:
    """A single instrument's book. Multi-instrument later = dict of these."""

    __slots__ = ("bids", "asks")

    def __init__(self) -> None:
        self.bids = BookSide(Side.BUY)
        self.asks = BookSide(Side.SELL)

    def side(self, side: Side) -> BookSide:
        return self.bids if side is Side.BUY else self.asks

    def best_bid(self) -> Optional[int]:
        return self.bids.best_price()

    def best_ask(self) -> Optional[int]:
        return self.asks.best_price()
