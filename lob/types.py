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
    owner: str | None = None   # participant id, for self-trade prevention
    display: int | None = None  # iceberg: visible tranche size (None = lit)
    visible: int = -1  # open qty currently displayed; managed by the engine

    def __post_init__(self) -> None:
        # exact-type checks (`type(x) is int`) reject bool for free, since
        # type(True) is bool, and are faster than isinstance chains — Order
        # construction is on the hot path.
        if not self.order_id:
            raise ValueError("order_id must be a non-empty string")
        if not isinstance(self.side, Side):
            raise TypeError(f"side must be a Side, got {type(self.side).__name__}")
        price = self.price
        if price is not None:
            if type(price) is not int:
                raise TypeError(
                    f"price must be an int (ticks), got {type(price).__name__}")
            if price <= 0:
                raise ValueError(f"price must be positive, got {price}")
        qty = self.quantity
        if type(qty) is not int:
            raise TypeError(
                f"quantity must be an int (ticks), got {type(qty).__name__}")
        if qty <= 0:
            raise ValueError(f"quantity must be positive, got {qty}")
        if self.remaining == -1:
            self.remaining = qty
        elif not (0 <= self.remaining <= qty):
            raise ValueError(
                f"remaining must be within [0, quantity], got {self.remaining}"
            )
        display = self.display
        if display is not None:
            if type(display) is not int:
                raise TypeError("display must be an int")
            if display <= 0:
                raise ValueError("display must be positive")
        if self.visible == -1:
            self.visible = (min(display, self.remaining)
                            if display is not None else self.remaining)

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
