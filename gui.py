"""Interactive GUI for the matching engine. Run:  python gui.py

Layout:
  left   — order entry (limit / market / stop / stop-limit, TIF, post-only)
           and your open orders + parked stops (select to cancel/modify)
  middle — session stats, last-trade price chart, and the live book ladder
  right  — time & sales tape (colored by aggressor) and the raw event log
  bottom — a realistic order-flow simulator

Simulator realism: Poisson arrivals, heavy-tailed (lognormal) sizes,
exponentially distributed placement depth from the touch, a mean-reverting
fair value with occasional momentum bursts, and aggressor direction biased
toward the fair value — the standard stylized facts of order flow.
"""

from __future__ import annotations

import math
import random
import time
import tkinter as tk
from collections import deque
from tkinter import ttk

from lob.engine import MatchingEngine
from lob.events import Ack, Canceled, Modified, Rejected, StopPlaced, Triggered
from lob.types import Fill, Order, Side, TimeInForce

LADDER_ROWS = 12
ROW_H = 20
LADDER_W = 380
CHART_H = 110
MID_PRICE = 100

BG = "#101418"
FG = "#c8d0d8"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("LOB Matching Engine")

        self.eng = MatchingEngine()
        self.rng = random.Random()
        self._next_id = 1
        self._sim_ids: list[str] = []
        self.sim_running = False

        # market state / session stats
        self.fair = float(MID_PRICE)
        self.burst_dir = 0
        self.burst_left = 0
        self.last_trade: tuple[int, int] | None = None   # (price, qty)
        self.prev_trade_price: int | None = None
        self.volume = 0
        self.notional = 0
        self.trade_count = 0
        self.price_history: deque[int] = deque(maxlen=400)
        self.vwap_history: deque[float] = deque(maxlen=400)
        self.spread_history: deque[int] = deque(maxlen=400)
        self.size_history: deque[tuple[int, Side]] = deque(maxlen=120)

        self._build_widgets()
        self.refresh()

    # ------------------------------------------------------------- UI setup

    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=8)
        root.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        root.columnconfigure(2, weight=1)
        root.rowconfigure(0, weight=1)

        # ---- left: order entry + open orders
        left = ttk.Frame(root)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 8))

        entry = ttk.LabelFrame(left, text="New order", padding=8)
        entry.pack(fill="x")

        self.var_side = tk.StringVar(value="BUY")
        self.var_type = tk.StringVar(value="LIMIT")
        self.var_tif = tk.StringVar(value="GTC")
        self.var_price = tk.IntVar(value=MID_PRICE)
        self.var_stop = tk.IntVar(value=MID_PRICE)
        self.var_qty = tk.IntVar(value=10)
        self.var_post_only = tk.BooleanVar(value=False)

        r = 0
        ttk.Label(entry, text="Side").grid(row=r, column=0, sticky="w")
        ttk.Combobox(entry, textvariable=self.var_side, state="readonly",
                     values=("BUY", "SELL"), width=10).grid(row=r, column=1); r += 1
        ttk.Label(entry, text="Type").grid(row=r, column=0, sticky="w")
        type_box = ttk.Combobox(entry, textvariable=self.var_type,
                                state="readonly", width=10,
                                values=("LIMIT", "MARKET", "STOP", "STOP LIMIT"))
        type_box.grid(row=r, column=1); r += 1
        type_box.bind("<<ComboboxSelected>>", lambda e: self._sync_entry_state())
        ttk.Label(entry, text="TIF").grid(row=r, column=0, sticky="w")
        self.tif_box = ttk.Combobox(entry, textvariable=self.var_tif,
                                    state="readonly",
                                    values=("GTC", "IOC", "FOK"), width=10)
        self.tif_box.grid(row=r, column=1); r += 1
        ttk.Label(entry, text="Price").grid(row=r, column=0, sticky="w")
        self.price_spin = ttk.Spinbox(entry, from_=1, to=10**9,
                                      textvariable=self.var_price, width=11)
        self.price_spin.grid(row=r, column=1); r += 1
        ttk.Label(entry, text="Stop px").grid(row=r, column=0, sticky="w")
        self.stop_spin = ttk.Spinbox(entry, from_=1, to=10**9,
                                     textvariable=self.var_stop, width=11)
        self.stop_spin.grid(row=r, column=1); r += 1
        ttk.Label(entry, text="Qty").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(entry, from_=1, to=10**9, textvariable=self.var_qty,
                    width=11).grid(row=r, column=1); r += 1
        self.post_chk = ttk.Checkbutton(entry, text="post-only",
                                        variable=self.var_post_only)
        self.post_chk.grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Button(entry, text="Submit", command=self.submit).grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        for i in range(r + 1):
            entry.rowconfigure(i, pad=4)
        self._sync_entry_state()

        oo = ttk.LabelFrame(left, text="My open orders & stops", padding=8)
        oo.pack(fill="both", expand=True, pady=(8, 0))
        self.orders_tv = ttk.Treeview(
            oo, columns=("type", "side", "price", "rem"), show="headings",
            height=10, selectmode="browse")
        for col, w, txt in (("type", 45, "Type"), ("side", 42, "Side"),
                            ("price", 62, "Price"), ("rem", 48, "Open")):
            self.orders_tv.heading(col, text=txt)
            self.orders_tv.column(col, width=w, anchor="center")
        self.orders_tv.pack(fill="both", expand=True)

        mod = ttk.Frame(oo)
        mod.pack(fill="x", pady=(6, 0))
        self.var_new_price = tk.StringVar()
        self.var_new_qty = tk.StringVar()
        ttk.Label(mod, text="new px").grid(row=0, column=0)
        ttk.Entry(mod, textvariable=self.var_new_price, width=6).grid(row=0, column=1)
        ttk.Label(mod, text="new qty").grid(row=0, column=2)
        ttk.Entry(mod, textvariable=self.var_new_qty, width=6).grid(row=0, column=3)
        ttk.Button(mod, text="Modify", command=self.modify_selected).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(mod, text="Cancel", command=self.cancel_selected).grid(
            row=1, column=2, columnspan=2, sticky="ew", pady=(4, 0))

        # ---- middle: stats + selectable chart + ladder
        mid = ttk.Frame(root)
        mid.grid(row=0, column=1, sticky="ns", padx=(0, 8))
        self.stats = ttk.Label(mid, text="", font=("Consolas", 10))
        self.stats.pack(anchor="w")

        chart_bar = ttk.Frame(mid)
        chart_bar.pack(fill="x", pady=(2, 0))
        ttk.Label(chart_bar, text="Chart:").pack(side="left")
        self.var_chart = tk.StringVar(value="Price")
        chart_box = ttk.Combobox(
            chart_bar, textvariable=self.var_chart, state="readonly", width=12,
            values=("Price", "Depth", "Volume", "Spread", "None"))
        chart_box.pack(side="left", padx=4)
        chart_box.bind("<<ComboboxSelected>>", lambda e: self._on_chart_change())

        self.chart = tk.Canvas(mid, width=LADDER_W, height=CHART_H, bg=BG,
                               highlightthickness=0)
        self.chart.pack(pady=(2, 6))
        ladder_h = (LADDER_ROWS * 2 + 1) * ROW_H + 8
        self.canvas = tk.Canvas(mid, width=LADDER_W, height=ladder_h, bg=BG,
                                highlightthickness=0)
        self.canvas.pack()

        # ---- right: tape + events
        right = ttk.Frame(root)
        right.grid(row=0, column=2, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        tape_f = ttk.LabelFrame(right, text="Time & sales", padding=4)
        tape_f.grid(row=0, column=0, sticky="nsew")
        tape_f.columnconfigure(0, weight=1); tape_f.rowconfigure(0, weight=1)
        self.tape = tk.Text(tape_f, width=44, height=13, state="disabled",
                            bg=BG, fg=FG, font=("Consolas", 9))
        self.tape.grid(row=0, column=0, sticky="nsew")
        ts = ttk.Scrollbar(tape_f, command=self.tape.yview)
        ts.grid(row=0, column=1, sticky="ns")
        self.tape.configure(yscrollcommand=ts.set)
        self.tape.tag_configure("buy", foreground="#57c765")
        self.tape.tag_configure("sell", foreground="#e06055")

        ev_f = ttk.LabelFrame(right, text="Events", padding=4)
        ev_f.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        ev_f.columnconfigure(0, weight=1); ev_f.rowconfigure(0, weight=1)
        self.log = tk.Text(ev_f, width=44, height=13, state="disabled",
                           bg=BG, fg=FG, font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(ev_f, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)
        for tag, color in (("fill", "#ffd75f"), ("ack", "#7fd77f"),
                           ("cancel", "#d0806a"), ("reject", "#ff5f5f"),
                           ("stop", "#6fb8e0")):
            self.log.tag_configure(tag, foreground=color)

        # ---- bottom: simulator
        sim = ttk.LabelFrame(root, text="Market simulator", padding=8)
        sim.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.sim_btn = ttk.Button(sim, text="Start", width=10,
                                  command=self.toggle_sim)
        self.sim_btn.pack(side="left")
        ttk.Label(sim, text="   intensity (orders/sec)").pack(side="left")
        self.var_speed = tk.IntVar(value=12)
        ttk.Scale(sim, from_=1, to=80, variable=self.var_speed,
                  length=220).pack(side="left", padx=6)
        self.status = ttk.Label(sim, text="")
        self.status.pack(side="right")

    def _sync_entry_state(self) -> None:
        t = self.var_type.get()
        has_limit = t in ("LIMIT", "STOP LIMIT")
        self.price_spin.configure(state="normal" if has_limit else "disabled")
        self.stop_spin.configure(
            state="normal" if t in ("STOP", "STOP LIMIT") else "disabled")
        self.tif_box.configure(state="readonly" if t == "LIMIT" else "disabled")
        self.post_chk.configure(state="normal" if t == "LIMIT" else "disabled")

    # ------------------------------------------------------------- actions

    def _new_id(self, prefix: str = "u") -> str:
        oid = f"{prefix}{self._next_id}"
        self._next_id += 1
        return oid

    def submit(self) -> None:
        try:
            side = Side[self.var_side.get()]
            qty = int(self.var_qty.get())
            t = self.var_type.get()
            oid = self._new_id()
            if t == "MARKET":
                self.eng.submit_market(Order(oid, side, None, qty))
            elif t == "LIMIT":
                self.eng.submit_limit(
                    Order(oid, side, int(self.var_price.get()), qty,
                          post_only=self.var_post_only.get()),
                    tif=TimeInForce[self.var_tif.get()])
            elif t == "STOP":
                self.eng.submit_stop(Order(oid, side, None, qty),
                                     stop_price=int(self.var_stop.get()))
            else:  # STOP LIMIT
                self.eng.submit_stop(
                    Order(oid, side, int(self.var_price.get()), qty),
                    stop_price=int(self.var_stop.get()))
        except (ValueError, TypeError, tk.TclError) as exc:
            self._log_line(f"!! {exc}", "reject")
        self.refresh()

    def _selected_order_id(self) -> str | None:
        sel = self.orders_tv.selection()
        return sel[0] if sel else None  # iid == order_id

    def cancel_selected(self) -> None:
        oid = self._selected_order_id()
        if oid:
            self.eng.cancel(oid)
            self.refresh()

    def modify_selected(self) -> None:
        oid = self._selected_order_id()
        if not oid:
            return
        try:
            np = self.var_new_price.get().strip()
            nq = self.var_new_qty.get().strip()
            ok = self.eng.modify(oid,
                                 new_price=int(np) if np else None,
                                 new_qty=int(nq) if nq else None)
            if not ok:
                self._log_line(f"!! cannot modify {oid} (stops: cancel+renew)",
                               "reject")
        except ValueError as exc:
            self._log_line(f"!! {exc}", "reject")
        self.refresh()

    # ------------------------------------------------------------ simulator

    def toggle_sim(self) -> None:
        self.sim_running = not self.sim_running
        self.sim_btn.configure(text="Stop" if self.sim_running else "Start")
        if self.sim_running:
            if not self.eng.open_orders():
                self._seed_book()
            self._sim_tick()

    def _seed_book(self) -> None:
        """Populate both sides around fair value so trading can begin."""
        f = round(self.fair)
        for i in range(1, 11):
            for _ in range(self.rng.randint(1, 3)):
                oid = self._new_id("sim")
                sz = self._sim_size()
                self.eng.submit_limit(Order(oid, Side.BUY, max(f - i, 1), sz))
                self._sim_ids.append(oid)
                oid = self._new_id("sim")
                self.eng.submit_limit(Order(oid, Side.SELL, f + i,
                                            self._sim_size()))
                self._sim_ids.append(oid)

    def _sim_size(self) -> int:
        """Heavy-tailed order size: mostly small, occasionally huge."""
        return min(max(int(math.exp(self.rng.gauss(1.7, 0.9))), 1), 400)

    def _sim_buy_prob(self) -> float:
        """Aggressors lean toward fair value: fair above mid → more buying."""
        bb, ba = self.eng.best_bid(), self.eng.best_ask()
        mid = (bb + ba) / 2 if bb is not None and ba is not None else self.fair
        return 1 / (1 + math.exp(-(self.fair - mid) * 1.5))

    def _sim_tick(self) -> None:
        if not self.sim_running:
            return
        rng = self.rng

        # fair value: mean-reverting random walk + occasional momentum burst
        if self.burst_left:
            self.burst_left -= 1
            self.fair += self.burst_dir * 0.25
        elif rng.random() < 0.008:
            self.burst_dir = rng.choice((-1, 1))
            self.burst_left = rng.randint(20, 60)
        self.fair += rng.gauss(0, 0.10) + 0.015 * (MID_PRICE - self.fair)
        self.fair = max(self.fair, 5.0)

        self._sim_ids = [o for o in self._sim_ids
                         if self.eng.open_order(o) is not None
                         or any(s[0].order_id == o
                                for s in self.eng.pending_stops())]

        r = rng.random()
        aggr_buy = rng.random() < self._sim_buy_prob()
        side = Side.BUY if aggr_buy else Side.SELL
        qty = self._sim_size()
        bb, ba = self.eng.best_bid(), self.eng.best_ask()

        if r < 0.62 or (bb is None and ba is None):
            # passive limit: exponential depth from the touch (never crosses)
            passive_side = rng.choice((Side.BUY, Side.SELL))
            off = min(int(rng.expovariate(0.45)), 15)
            if passive_side is Side.BUY:
                touch = (ba - 1) if ba is not None else round(self.fair) - 1
                px = max(touch - off, 1)
            else:
                touch = (bb + 1) if bb is not None else round(self.fair) + 1
                px = max(touch + off, 1)
            oid = self._new_id("sim")
            self.eng.submit_limit(Order(oid, passive_side, px, qty))
            self._sim_ids.append(oid)
        elif r < 0.74:
            # marketable limit: crosses the touch, sweeps a level or two
            ref = ba if side is Side.BUY else bb
            if ref is not None:
                px = ref + rng.randint(0, 2) if side is Side.BUY \
                    else max(ref - rng.randint(0, 2), 1)
                self.eng.submit_limit(Order(self._new_id("sim"), side, px, qty),
                                      tif=TimeInForce.IOC)
        elif r < 0.83:
            self.eng.submit_market(
                Order(self._new_id("sim"), side, None, max(qty // 2, 1)))
        elif r < 0.86 and self.eng.last_trade_price is not None:
            # occasional resting stop, a few ticks away on the losing side
            last = self.eng.last_trade_price
            s_side = rng.choice((Side.BUY, Side.SELL))
            stop_px = last + rng.randint(2, 6) if s_side is Side.BUY \
                else max(last - rng.randint(2, 6), 1)
            oid = self._new_id("sim")
            self.eng.submit_stop(Order(oid, s_side, None, qty), stop_price=stop_px)
            self._sim_ids.append(oid)
        elif self._sim_ids:
            self.eng.cancel(rng.choice(self._sim_ids))

        self.refresh()
        # Poisson arrivals: exponential inter-arrival at the chosen intensity
        rate = max(self.var_speed.get(), 1)
        delay = max(int(rng.expovariate(rate) * 1000), 8)
        self.after(delay, self._sim_tick)

    # -------------------------------------------------------------- refresh

    def refresh(self) -> None:
        self._drain_events()
        bb, ba = self.eng.best_bid(), self.eng.best_ask()
        if bb is not None and ba is not None:
            self.spread_history.append(ba - bb)
        self._redraw_ladder()
        self._redraw_chart()
        self._redraw_open_orders()
        self._redraw_stats()

    def _on_chart_change(self) -> None:
        if self.var_chart.get() == "None":
            self.chart.pack_forget()
        elif not self.chart.winfo_manager():
            self.chart.pack(pady=(2, 6), before=self.canvas)
        self._redraw_chart()

    def _redraw_stats(self) -> None:
        bb, ba = self.eng.best_bid(), self.eng.best_ask()
        vwap = self.notional / self.volume if self.volume else 0
        arrow = ""
        if self.last_trade and self.prev_trade_price is not None:
            if self.last_trade[0] > self.prev_trade_price:
                arrow = " ^"
            elif self.last_trade[0] < self.prev_trade_price:
                arrow = " v"
        last = f"{self.last_trade[0]}{arrow}" if self.last_trade else "-"
        spread = ba - bb if bb is not None and ba is not None else "-"
        self.stats.configure(
            text=f"last {last}   spread {spread}   vol {self.volume:,}   "
                 f"vwap {vwap:,.2f}   trades {self.trade_count:,}")
        self.status.configure(
            text=f"bid {bb if bb is not None else '-'} / "
                 f"ask {ba if ba is not None else '-'}   "
                 f"open {len(self.eng.open_orders())}  "
                 f"stops {len(self.eng.pending_stops())}")

    def _drain_events(self) -> None:
        for e in self.eng.drain_events():
            if isinstance(e, Fill):
                if self.last_trade:
                    self.prev_trade_price = self.last_trade[0]
                self.last_trade = (e.price, e.quantity)
                self.volume += e.quantity
                self.notional += e.quantity * e.price
                self.trade_count += 1
                self.price_history.append(e.price)
                self.vwap_history.append(self.notional / self.volume)
                self.size_history.append((e.quantity, e.taker_side))
                tag = "buy" if e.taker_side is Side.BUY else "sell"
                self._tape_line(
                    f"{time.strftime('%H:%M:%S')}  {e.quantity:>5} @ {e.price}",
                    tag)
                self._log_line(
                    f"TRADE #{e.trade_id} {e.quantity} @ {e.price} "
                    f"({e.taker_side.value.lower()} aggr)", "fill")
            elif isinstance(e, Ack):
                self._log_line(
                    f"ack   {e.order_id}: {e.side.value} {e.remaining} @ {e.price}",
                    "ack")
            elif isinstance(e, Canceled):
                self._log_line(
                    f"xcl   {e.order_id}: {e.quantity} ({e.reason})", "cancel")
            elif isinstance(e, Modified):
                self._log_line(
                    f"mod   {e.order_id}: now {e.remaining} @ {e.price}", "ack")
            elif isinstance(e, StopPlaced):
                self._log_line(
                    f"stop  {e.order_id}: {e.side.value} {e.quantity} "
                    f"trigger {e.stop_price}", "stop")
            elif isinstance(e, Triggered):
                self._log_line(
                    f"TRIG  {e.order_id} @ stop {e.stop_price}", "stop")
            elif isinstance(e, Rejected):
                self._log_line(f"REJ   {e.order_id}: {e.reason}", "reject")

    def _bounded_insert(self, widget: tk.Text, text: str, tag: str) -> None:
        widget.configure(state="normal")
        widget.insert("end", text + "\n", tag)
        if int(widget.index("end-1c").split(".")[0]) > 500:
            widget.delete("1.0", "100.0")
        widget.see("end")
        widget.configure(state="disabled")

    def _log_line(self, text: str, tag: str) -> None:
        self._bounded_insert(self.log, text, tag)

    def _tape_line(self, text: str, tag: str) -> None:
        self._bounded_insert(self.tape, text, tag)

    def _redraw_chart(self) -> None:
        mode = self.var_chart.get()
        if mode == "None":
            return
        c = self.chart
        c.delete("all")
        {"Price": self._chart_price,
         "Depth": self._chart_depth,
         "Volume": self._chart_volume,
         "Spread": self._chart_spread}[mode]()

    def _chart_placeholder(self, text: str) -> None:
        self.chart.create_text(LADDER_W / 2, CHART_H / 2, text=text,
                               fill="#405060", font=("Consolas", 9))

    def _series_line(self, pts, color, width=1, dash=None) -> None:
        """Plot a series scaled to the union range of price/vwap axes."""
        lo, hi = self._axis_range
        span = max(hi - lo, 1e-9)
        n = len(pts)
        coords = []
        for i, p in enumerate(pts):
            x = 4 + i * (LADDER_W - 46) / max(n - 1, 1)
            y = 6 + (hi - p) * (CHART_H - 12) / span
            coords.extend((x, y))
        self.chart.create_line(*coords, fill=color, width=width, dash=dash)

    def _chart_price(self) -> None:
        c = self.chart
        pts = list(self.price_history)
        if len(pts) < 2:
            self._chart_placeholder("no trades yet")
            return
        vwap = list(self.vwap_history)
        all_vals = pts + vwap
        self._axis_range = (min(all_vals), max(all_vals))
        lo, hi = self._axis_range
        self._series_line(vwap, "#d0a040", dash=(2, 2))
        self._series_line(pts, "#5fa8d3")
        c.create_text(LADDER_W - 6, 10, text=f"{hi:g}", anchor="e",
                      fill="#607080", font=("Consolas", 8))
        c.create_text(LADDER_W - 6, CHART_H - 10, text=f"{lo:g}", anchor="e",
                      fill="#607080", font=("Consolas", 8))
        last = pts[-1]
        span = max(hi - lo, 1e-9)
        y = 6 + (hi - last) * (CHART_H - 12) / span
        up = len(pts) < 2 or pts[-1] >= pts[-2]
        c.create_text(LADDER_W - 6, y, text=str(last), anchor="e",
                      fill="#57c765" if up else "#e06055",
                      font=("Consolas", 9, "bold"))
        c.create_text(8, 10, text="price / vwap", anchor="w",
                      fill="#405060", font=("Consolas", 8))

    def _chart_depth(self) -> None:
        """Classic depth chart: cumulative resting qty vs price."""
        c = self.chart
        d = self.eng.depth(30)
        bids, asks = d["bids"], d["asks"]
        if not bids or not asks:
            self._chart_placeholder("need both sides of the book")
            return
        lo_px = bids[-1][0]
        hi_px = asks[-1][0]
        px_span = max(hi_px - lo_px, 1)

        def x_of(px):
            return 4 + (px - lo_px) * (LADDER_W - 8) / px_span

        cum = 0
        bid_pts = []
        for px, q in bids:  # best -> worst (rightmost -> left)
            bid_pts.append((px, cum))
            cum += q
            bid_pts.append((px, cum))
        max_bid = cum
        cum = 0
        ask_pts = []
        for px, q in asks:
            ask_pts.append((px, cum))
            cum += q
            ask_pts.append((px, cum))
        max_cum = max(max_bid, cum, 1)

        def y_of(v):
            return CHART_H - 4 - v * (CHART_H - 16) / max_cum

        for pts, color in ((bid_pts, "#57c765"), (ask_pts, "#e06055")):
            coords = []
            for px, v in pts:
                coords.extend((x_of(px), y_of(v)))
            c.create_line(*coords, fill=color, width=2)
        mid_x = x_of((bids[0][0] + asks[0][0]) / 2)
        c.create_line(mid_x, 4, mid_x, CHART_H - 4, fill="#30404c",
                      dash=(2, 3))
        c.create_text(8, 10, text=f"depth  {lo_px} … {hi_px}", anchor="w",
                      fill="#405060", font=("Consolas", 8))

    def _chart_volume(self) -> None:
        """Recent trade sizes as bars, colored by aggressor side."""
        c = self.chart
        sizes = list(self.size_history)
        if not sizes:
            self._chart_placeholder("no trades yet")
            return
        max_q = max(q for q, _ in sizes)
        n = len(sizes)
        bar_w = max((LADDER_W - 12) / max(n, 1), 1.5)
        for i, (q, taker_side) in enumerate(sizes):
            x = 6 + i * bar_w
            h = max(q * (CHART_H - 18) / max_q, 1)
            color = "#57c765" if taker_side is Side.BUY else "#e06055"
            c.create_rectangle(x, CHART_H - 4 - h, x + bar_w * 0.8,
                               CHART_H - 4, fill=color, width=0)
        c.create_text(8, 10, text=f"trade sizes (max {max_q})", anchor="w",
                      fill="#405060", font=("Consolas", 8))

    def _chart_spread(self) -> None:
        c = self.chart
        pts = list(self.spread_history)
        if len(pts) < 2:
            self._chart_placeholder("no two-sided market yet")
            return
        self._axis_range = (min(pts), max(pts))
        self._series_line(pts, "#b58fd0")
        c.create_text(LADDER_W - 6, 10, text=str(max(pts)), anchor="e",
                      fill="#607080", font=("Consolas", 8))
        c.create_text(LADDER_W - 6, CHART_H - 10, text=str(min(pts)),
                      anchor="e", fill="#607080", font=("Consolas", 8))
        c.create_text(8, 10, text=f"spread (now {pts[-1]})", anchor="w",
                      fill="#405060", font=("Consolas", 8))

    def _redraw_ladder(self) -> None:
        c = self.canvas
        c.delete("all")
        d = self.eng.depth(LADDER_ROWS)
        asks = list(reversed(d["asks"]))
        bids = d["bids"]
        max_qty = max([q for _, q in asks + bids] or [1])
        bar_max = LADDER_W // 2 - 60

        y = 4 + (LADDER_ROWS - len(asks)) * ROW_H
        for px, q in asks:
            w = max(int(q / max_qty * bar_max), 2)
            c.create_rectangle(LADDER_W // 2 + 40, y + 3,
                               LADDER_W // 2 + 40 + w, y + ROW_H - 3,
                               fill="#a03030", width=0)
            c.create_text(LADDER_W // 2, y + ROW_H / 2, text=str(px),
                          fill="#e0a0a0", font=("Consolas", 10, "bold"))
            c.create_text(LADDER_W // 2 + 46 + w, y + ROW_H / 2, text=str(q),
                          fill=FG, anchor="w", font=("Consolas", 9))
            y += ROW_H

        y = 4 + LADDER_ROWS * ROW_H
        bb, ba = self.eng.best_bid(), self.eng.best_ask()
        spread = f"spread {ba - bb}" if bb is not None and ba is not None else "—"
        c.create_line(8, y + ROW_H / 2, LADDER_W - 8, y + ROW_H / 2,
                      fill="#30404c", dash=(3, 3))
        c.create_text(LADDER_W // 2, y + ROW_H / 2, text=spread,
                      fill="#8090a0", font=("Consolas", 9))
        y += ROW_H

        for px, q in bids:
            w = max(int(q / max_qty * bar_max), 2)
            c.create_rectangle(LADDER_W // 2 - 40 - w, y + 3,
                               LADDER_W // 2 - 40, y + ROW_H - 3,
                               fill="#2f7d3f", width=0)
            c.create_text(LADDER_W // 2, y + ROW_H / 2, text=str(px),
                          fill="#9fd7a8", font=("Consolas", 10, "bold"))
            c.create_text(LADDER_W // 2 - 46 - w, y + ROW_H / 2, text=str(q),
                          fill=FG, anchor="e", font=("Consolas", 9))
            y += ROW_H

    def _redraw_open_orders(self) -> None:
        tv = self.orders_tv
        selected = set(tv.selection())
        tv.delete(*tv.get_children())
        for o in sorted(self.eng.open_orders(), key=lambda o: o.seq):
            tv.insert("", "end", iid=o.order_id,
                      values=("LMT", o.side.value, o.price, o.remaining))
        for o, stop_px in sorted(self.eng.pending_stops(),
                                 key=lambda t: t[0].seq):
            kind = "STP" if o.is_market else "STPL"
            px = f"{stop_px}" if o.is_market else f"{stop_px}→{o.price}"
            tv.insert("", "end", iid=o.order_id,
                      values=(kind, o.side.value, px, o.remaining))
        for iid in selected:
            if tv.exists(iid):
                tv.selection_add(iid)


if __name__ == "__main__":
    App().mainloop()
