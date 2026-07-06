"""Engine output events.

Every state change the engine makes is reported as exactly one event, in
the order it happened. A caller that replays the event stream can
reconstruct the book — this is the seed of a market-data feed.

`Fill` (defined in lob.types) is also an event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from lob.types import Fill, Side


@dataclass(frozen=True, slots=True)
class Ack:
    """Order accepted and now resting on the book."""

    order_id: str
    side: Side
    price: int
    remaining: int


@dataclass(frozen=True, slots=True)
class Canceled:
    """Open quantity removed from the market (never traded)."""

    order_id: str
    quantity: int  # the open qty that was cancelled
    reason: str    # "user", "ioc_expired", "fok_kill", "market_unfilled", "modify"


@dataclass(frozen=True, slots=True)
class Modified:
    """In-place quantity reduction; queue priority retained."""

    order_id: str
    price: int
    remaining: int


@dataclass(frozen=True, slots=True)
class Rejected:
    """Order refused; no state change occurred."""

    order_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class StopPlaced:
    """Stop order accepted; parked until the trigger trades."""

    order_id: str
    side: Side
    stop_price: int
    quantity: int


@dataclass(frozen=True, slots=True)
class Triggered:
    """A parked stop's trigger price traded; the order is now live."""

    order_id: str
    stop_price: int


Event = Union[Ack, Fill, Canceled, Modified, Rejected, StopPlaced, Triggered]
