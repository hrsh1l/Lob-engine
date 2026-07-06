"""The matching engine: crosses incoming orders against the book.

Matching rules (standard price-time priority):

  - An incoming BUY crosses while best_ask <= its limit price.
    An incoming SELL crosses while best_bid >= its limit price.
    A MARKET order (price=None) crosses at any available price.
  - Within the crossing range, fills happen best price first, and FIFO
    within each price level.
  - Every fill executes at the RESTING (maker) order's price. This is how
    real exchanges work: the maker set their price first, and the taker,
    by crossing, accepts it. An aggressively-priced taker gets *price
    improvement* rather than paying its own limit.

Time-in-force:

  - GTC (default): remainder rests on the book.
  - IOC: remainder is cancelled instead of resting.
  - FOK: executes only if the full quantity can trade immediately;
    otherwise nothing happens at all (checked before any fill).
  - Market orders are inherently IOC: unfilled remainder is cancelled.

Cancellation is *lazy*: cancel() flips the order's `active` flag and fixes
the level's quantity accounting in O(1); the dead order object is skimmed
off the front of its FIFO queue whenever matching next reaches it. A level
whose live quantity hits zero is removed immediately.

Every state change is emitted on `self.events` (and to the optional
`on_event` callback) in the order it happened: Ack, Fill, Canceled,
Modified, Rejected.

The engine owns one OrderBook (one instrument). Multi-instrument later is
a router holding {symbol -> MatchingEngine}.
"""

from __future__ import annotations

from typing import Callable, Optional

from lob.book import OrderBook, PriceLevel
from lob.events import (Ack, Canceled, Event, Modified, Rejected, StopPlaced,
                        Triggered)
from lob.types import Fill, Order, Side, TimeInForce


class MatchingEngine:
    __slots__ = ("book", "events", "tick_size", "lot_size",
                 "last_trade_price", "_orders", "_stops", "_on_event",
                 "_next_trade_id")

    def __init__(
        self,
        on_event: Optional[Callable[[Event], None]] = None,
        tick_size: int = 1,
        lot_size: int = 1,
    ) -> None:
        if tick_size < 1 or lot_size < 1:
            raise ValueError("tick_size and lot_size must be >= 1")
        self.book = OrderBook()
        self.events: list[Event] = []
        self.tick_size = tick_size
        self.lot_size = lot_size
        self.last_trade_price: Optional[int] = None
        self._orders: dict[str, Order] = {}  # open (resting) orders by id
        self._stops: dict[str, tuple[Order, int]] = {}  # id -> (order, stop px)
        self._on_event = on_event
        self._next_trade_id = 0

    # ------------------------------------------------------------------ #
    # order entry
    # ------------------------------------------------------------------ #

    def submit_limit(
        self, order: Order, tif: TimeInForce = TimeInForce.GTC
    ) -> list[Fill]:
        """Match `order` against the book; handle the remainder per `tif`.

        Returns the fills generated, in execution order. Empty if nothing
        crossed (or the order was rejected / FOK-killed — see events).
        """
        if order.is_market:
            raise ValueError("market orders go through submit_market()")
        if not self._admit(order):
            return []

        if order.post_only and self._would_cross(order):
            self._emit(Rejected(order.order_id, "post_only_would_cross"))
            return []
        if tif is TimeInForce.FOK and not self._fillable(order):
            self._emit(Canceled(order.order_id, order.remaining, "fok_kill"))
            return []

        fills = self._match(order, limit_price=order.price)

        if order.is_filled:
            pass  # nothing rests, nothing to track
        elif tif is TimeInForce.GTC:
            self.book.side(order.side).add(order)
            self._orders[order.order_id] = order
            self._emit(Ack(order.order_id, order.side, order.price, order.remaining))
        else:  # IOC (or FOK that matched fully above — can't reach here unfilled)
            self._emit(Canceled(order.order_id, order.remaining, "ioc_expired"))
        if fills:
            self._check_stops()
        return fills

    def submit_market(self, order: Order) -> list[Fill]:
        """Match immediately at any price; cancel whatever can't fill."""
        if not order.is_market:
            raise ValueError("submit_market() requires an order with price=None")
        if not self._admit(order):
            return []
        fills = self._match(order, limit_price=None)
        if not order.is_filled:
            self._emit(Canceled(order.order_id, order.remaining, "market_unfilled"))
        if fills:
            self._check_stops()
        return fills

    def submit_stop(self, order: Order, stop_price: int) -> bool:
        """Park a stop order until the market trades through `stop_price`.

        A BUY stop triggers when the last trade prints at or above the
        stop; a SELL stop at or below (the standard convention — stops are
        placed on the losing side of the market). Once triggered the order
        goes live: as a market order if `order.price is None` (stop-market)
        or a GTC limit at `order.price` (stop-limit).
        """
        if isinstance(stop_price, bool) or not isinstance(stop_price, int):
            raise TypeError("stop_price must be an int (ticks)")
        if stop_price <= 0:
            raise ValueError("stop_price must be positive")
        if stop_price % self.tick_size:
            self._emit(Rejected(order.order_id, "stop_price_off_tick"))
            return False
        if not self._admit(order):
            return False
        self._stops[order.order_id] = (order, stop_price)
        self._emit(StopPlaced(order.order_id, order.side, stop_price,
                              order.remaining))
        # trigger immediately if the market already traded through the stop
        self._check_stops()
        return True

    # ------------------------------------------------------------------ #
    # cancel / modify
    # ------------------------------------------------------------------ #

    def cancel(self, order_id: str, reason: str = "user") -> bool:
        """Cancel an open order or parked stop by id. O(1) plus cleanup.

        Returns False if the id is unknown (never rested, already filled,
        or already cancelled).
        """
        stop = self._stops.pop(order_id, None)
        if stop is not None:
            self._emit(Canceled(order_id, stop[0].remaining, reason))
            return True
        order = self._orders.pop(order_id, None)
        if order is None:
            return False
        order.active = False
        side = self.book.side(order.side)
        level = side.level_at(order.price)
        level.reduce(order.remaining)
        if level.total_qty == 0:
            # every order left in the deque is dead; drop the whole level
            level.orders.clear()
            side.remove_level(level.price)
        self._emit(Canceled(order_id, order.remaining, reason))
        return True

    def modify(
        self,
        order_id: str,
        new_price: Optional[int] = None,
        new_qty: Optional[int] = None,
    ) -> bool:
        """Modify an open order.

        A pure quantity *decrease* is done in place and keeps queue
        priority. A price change or quantity increase loses priority: the
        order is cancelled and re-entered as new — and a price change may
        immediately cross and trade.

        `new_qty` is the desired open quantity. Returns False for an
        unknown id.
        """
        order = self._orders.get(order_id)
        if order is None:
            return False
        target_qty = order.remaining if new_qty is None else new_qty
        if target_qty <= 0:
            raise ValueError("new_qty must be positive; use cancel() to remove")
        price_change = new_price is not None and new_price != order.price

        if not price_change and target_qty < order.remaining:
            level = self.book.side(order.side).level_at(order.price)
            level.reduce(order.remaining - target_qty)
            order.remaining = target_qty
            self._emit(Modified(order_id, order.price, target_qty))
            return True
        if not price_change and target_qty == order.remaining:
            return True  # no-op

        # Priority-losing modify: cancel, then re-enter as a brand-new order.
        price = new_price if new_price is not None else order.price
        self.cancel(order_id, reason="modify")
        replacement = Order(order_id, order.side, price=price, quantity=target_qty)
        self.submit_limit(replacement)  # may match if the new price crosses
        return True

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #

    def best_bid(self) -> Optional[int]:
        return self.book.best_bid()

    def best_ask(self) -> Optional[int]:
        return self.book.best_ask()

    def depth(self, n: int = 5) -> dict[str, list[tuple[int, int]]]:
        """Top-n levels per side as (price, total open qty), best first."""
        return {
            "bids": self._depth_side(self.book.bids, n),
            "asks": self._depth_side(self.book.asks, n),
        }

    @staticmethod
    def _depth_side(side, n: int) -> list[tuple[int, int]]:
        out = []
        for level in side.levels_best_first():
            out.append((level.price, level.total_qty))
            if len(out) == n:
                break
        return out

    def open_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def open_orders(self) -> list[Order]:
        """All currently resting orders (no particular order)."""
        return list(self._orders.values())

    def pending_stops(self) -> list[tuple[Order, int]]:
        """Parked stop orders as (order, stop_price) pairs."""
        return list(self._stops.values())

    def drain_events(self) -> list[Event]:
        """Return all events since the last drain, oldest first."""
        out = self.events
        self.events = []
        return out

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _emit(self, event: Event) -> None:
        self.events.append(event)
        if self._on_event is not None:
            self._on_event(event)

    def _admit(self, order: Order) -> bool:
        """Gate every incoming order: unique id, on-tick price, round lot."""
        if order.order_id in self._orders or order.order_id in self._stops:
            self._emit(Rejected(order.order_id, "duplicate_order_id"))
            return False
        if order.price is not None and order.price % self.tick_size:
            self._emit(Rejected(order.order_id, "price_off_tick"))
            return False
        if order.quantity % self.lot_size:
            self._emit(Rejected(order.order_id, "odd_lot"))
            return False
        return True

    def _would_cross(self, order: Order) -> bool:
        """Would this limit order trade immediately against the book?"""
        best = self.book.side(order.side.opposite).best_price()
        return best is not None and self._crosses(order.side, order.price, best)

    def _check_stops(self) -> None:
        """Fire any stops whose trigger the last trade satisfied.

        Triggered orders go live immediately and their own executions can
        trigger further stops (a stop cascade), so loop until quiescent.
        """
        while self._stops and self.last_trade_price is not None:
            last = self.last_trade_price
            fired = [
                oid for oid, (o, stop_px) in self._stops.items()
                if (o.side is Side.BUY and last >= stop_px)
                or (o.side is Side.SELL and last <= stop_px)
            ]
            if not fired:
                return
            for oid in fired:
                order, stop_px = self._stops.pop(oid)
                self._emit(Triggered(oid, stop_px))
                if order.is_market:
                    self._match(order, limit_price=None)
                    if not order.is_filled:
                        self._emit(Canceled(oid, order.remaining,
                                            "market_unfilled"))
                else:
                    self._match(order, limit_price=order.price)
                    if not order.is_filled:
                        self.book.side(order.side).add(order)
                        self._orders[oid] = order
                        self._emit(Ack(oid, order.side, order.price,
                                       order.remaining))

    def _fillable(self, order: Order) -> bool:
        """FOK pre-check: can `order` trade its full size immediately?"""
        opposite = self.book.side(order.side.opposite)
        needed = order.remaining
        for level in opposite.levels_best_first():
            if not self._crosses(order.side, order.price, level.price):
                break
            needed -= level.total_qty  # live qty only; cancels already deducted
            if needed <= 0:
                return True
        return False

    def _match(self, taker: Order, limit_price: Optional[int]) -> list[Fill]:
        """Cross `taker` against the opposite side while prices overlap.

        `limit_price=None` means match at any price (market order).
        Mutates both taker and makers; removes filled makers and emptied
        levels from the book; emits a Fill event per execution.
        """
        opposite = self.book.side(taker.side.opposite)
        fills: list[Fill] = []

        while taker.remaining > 0:
            level = opposite.best_level()
            if level is None:
                break  # no liquidity left on the other side
            if limit_price is not None and not self._crosses(
                taker.side, limit_price, level.price
            ):
                break  # best available price no longer acceptable

            # Fill against this level FIFO until it empties or taker is done.
            while taker.remaining > 0 and level.orders:
                maker = level.orders[0]
                if not maker.active:
                    level.orders.popleft()  # skim a lazily-cancelled order
                    continue
                qty = min(taker.remaining, maker.remaining)
                maker.fill(qty)
                taker.fill(qty)
                level.reduce(qty)
                self._next_trade_id += 1
                fill = Fill(
                    maker_order_id=maker.order_id,
                    taker_order_id=taker.order_id,
                    price=maker.price,  # always the resting price
                    quantity=qty,
                    taker_side=taker.side,
                    trade_id=self._next_trade_id,
                )
                self.last_trade_price = maker.price
                fills.append(fill)
                self._emit(fill)
                if maker.is_filled:
                    level.orders.popleft()
                    del self._orders[maker.order_id]

            # Sweep on live quantity, not deque emptiness: the taker may
            # stop mid-level leaving only lazily-cancelled orders behind,
            # and such a zombie level must not linger as "best".
            if level.total_qty == 0:
                level.orders.clear()
                opposite.remove_level(level.price)

        return fills

    @staticmethod
    def _crosses(taker_side: Side, taker_price: int, maker_price: int) -> bool:
        """Does a taker at `taker_price` accept the maker's price?"""
        if taker_side is Side.BUY:
            return maker_price <= taker_price
        return maker_price >= taker_price
