"""Core domain types for the matching engine.

Money is represented as integer price *ticks* (e.g. for a stock quoted in
cents, price 10050 means $100.50). Floats are never used for prices or
quantities: float rounding would eventually make two economically-equal
prices compare unequal, corrupting the book.

Time priority is represented by a monotonically increasing sequence number
assigned by the engine, not by wall-clock time: wall clocks can repeat or go
backwards (NTP adjustments), which would break FIFO ordering.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from itertools import count


class Side(enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class TimeInForce(enum.Enum):
    GTC = "GTC"  # good-til-cancelled: rest any remainder (the default)
    IOC = "IOC"  # immediate-or-cancel: match what you can, cancel the rest
    FOK = "FOK"  # fill-or-kill: fill entirely and immediately, or not at all


# Engine-wide sequence for time priority. A single global counter keeps
# ordering total even across multiple books (multi-instrument later).
_next_seq = count(1)


def next_seq() -> int:
    """Return the next time-priority sequence number."""
    return next(_next_seq)


@dataclass(slots=True)
class Order:
    """A resting or incoming order.

    `quantity` is the original size and never changes; `remaining` is the
    open size, decremented as fills occur. Keeping both lets us report
    partial-fill state without reconstructing it from trade history.

    `price=None` marks a MARKET order: it matches at any available price
    and never rests on the book, so it needs no limit.
    """

    order_id: str
    side: Side
    price: int | None   # integer ticks > 0, or None for a market order
    quantity: int       # original size; must be > 0
    seq: int = field(default_factory=next_seq)
    remaining: int = -1  # sentinel; set to quantity in __post_init__
    active: bool = True  # False once cancelled (lazy deletion from the book)
    post_only: bool = False  # maker-only: reject instead of crossing

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValueError("order_id must be a non-empty string")
        if not isinstance(self.side, Side):
            raise TypeError(f"side must be a Side, got {type(self.side).__name__}")
        # bool is a subclass of int, so exclude it explicitly.
        for name, value in (("price", self.price), ("quantity", self.quantity)):
            if name == "price" and value is None:
                continue  # market order
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int (ticks), got {type(value).__name__}")
        if self.price is not None and self.price <= 0:
            raise ValueError(f"price must be positive, got {self.price}")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.remaining == -1:
            self.remaining = self.quantity
        elif not (0 <= self.remaining <= self.quantity):
            raise ValueError(
                f"remaining must be within [0, quantity], got {self.remaining}"
            )

    @property
    def is_market(self) -> bool:
        return self.price is None

    @property
    def is_filled(self) -> bool:
        return self.remaining == 0

    def fill(self, qty: int) -> None:
        """Reduce the open size by `qty` (a fill of that many units)."""
        if qty <= 0:
            raise ValueError(f"fill qty must be positive, got {qty}")
        if qty > self.remaining:
            raise ValueError(f"fill qty {qty} exceeds remaining {self.remaining}")
        self.remaining -= qty


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution: an incoming (taker) order matched a resting (maker)
    order for `quantity` units at `price` — always the *maker's* price.
    Frozen: a trade that happened never changes.

    `taker_side` is the aggressor side (what a real feed publishes as the
    trade's tick direction); `trade_id` is the engine-assigned monotonic
    execution id.
    """

    maker_order_id: str
    taker_order_id: str
    price: int
    quantity: int
    taker_side: Side | None = None
    trade_id: int = 0

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError(f"fill price must be positive, got {self.price}")
        if self.quantity <= 0:
            raise ValueError(f"fill quantity must be positive, got {self.quantity}")
