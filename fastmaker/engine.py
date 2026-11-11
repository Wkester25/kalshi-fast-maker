"""The engine: finds each 15-minute window, keeps its order book from the WebSocket, runs the rule on
every book change, places/cancels orders (paper or live), and settles results.

Paper and live run the same decisions; only the executor differs:
  Paper  fills only when a real Kalshi trade prints at or through our price after we'd have been in
         the queue (queue ahead = displayed size when we joined; 0 when we improved the bid).
  Live   real orders; fills come from the WebSocket 'fill' channel (deduplicated by trade id).
"""
from __future__ import annotations

import asyncio
import copy
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config
from .book import Book
from .kalshi import Feed, KalshiError, Rest, dollars_c, iso_ts
from .store import Recorder, Store, et_day
from .strategy import WHOLE_CENT, Action, Order, State, decide, parse_ranges

log = logging.getLogger("engine")
PAPER_LATENCY_MS = 60
SETTLE_AFTER_S = 10            # ask for the result this long after the close


def taker_fee_c(price_c: float, n: int = 1) -> float:
    p = price_c / 100
    return math.ceil(round(7 * p * (1 - p) * n, 6))


@dataclass
class Slot:
    coin: str
    series: str
    group: str
    ticker: str = ""
    open_ts: float = 0.0
    close_ts: float = 0.0
    strike: float | None = None
    book: Book | None = None
    st: State | None = None
    mode: str = "paper"               # mode the window's orders were placed in
    busy: bool = False
    queue: float = 0.0                # paper: contracts ahead of us at our price
    ranges: list = field(default_factory=lambda: list(WHOLE_CENT))   # the market's valid price grid
    place_errors: int = 0             # rejected placements in a row (this window)
    trades: list = field(default_factory=list)


@dataclass
class Pending:
    """A finished window that still needs its result."""
    slot: Slot
    since: float


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.rest = Rest(cfg.get("env", "real"))
        self.store = Store()
        self.rec = Recorder(cfg.get("record", True))
        self.feed = Feed(self.rest, self.on_ws, self.on_ws_state)
        self.slots: dict[str, Slot] = {c: Slot(c, s, g) for c, (s, g) in config.MARKETS.items()}
        self.by_ticker: dict[str, Slot] = {}
        self.pending: list[Pending] = []
        self.index: dict[str, dict] = {}
        self.seen_fills: set[str] = set()
        self._foreign_seen: set[str] = set()
        self.order_kind: dict[str, str] = {}      # order_id -> "buy"/"sell" (fallback when no client id)
        self.halted = ""
        self.started = time.time()
        self.feed.indices = list(config.CF_INDEX.values())
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        self.store.event("info", "", f"started ({self.cfg['mode']} mode, {self.cfg['env']} exchange)")
        if self.rest.has_key:
            asyncio.create_task(self._clear_leftovers())
        self._tasks = [asyncio.create_task(self.feed.run()), asyncio.create_task(self._roll_loop()),
                       asyncio.create_task(self._timer_loop()), asyncio.create_task(self._settle_loop())]

    async def stop(self):
        for s in self.slots.values():
            if s.st and s.st.order:
                await self._cancel(s, "shutting down")
        await self.feed.stop()
        for t in self._tasks:
            t.cancel()
        self.rec.close()
        await self.rest.close()

    async def _clear_leftovers(self):
        """Orders from a previous run (crash, kill) are no longer tracked: take them off the exchange."""
        try:
            n, errs = await self.rest.cancel_mine()
            if n or errs:
                self.event("warn" if errs else "info", "", f"cancelled {n} leftover order(s) from a previous run"
                           + (f"; failed: {errs}" if errs else ""))
        except Exception as e:                                         # noqa: BLE001
            self.event("warn", "", f"could not check for leftover orders: {e}")

    def event(self, level: str, coin: str, msg: str):
        log.info("%s %s %s", level, coin, msg)
        self.store.event(level, coin, msg)

    # ------------------------------------------------------------ settings helpers
    @property
    def live(self) -> bool:
        return self.cfg["mode"] == "live" and self.rest.has_key

    def rule(self, slot: Slot) -> dict:
        return self.cfg["rules"][slot.group]

    def news_now(self, slot: Slot) -> str | None:
        ns = self.cfg.get("news_skip") or {}
        if not ns.get("enabled"):
            return None
        o = datetime.fromtimestamp(slot.open_ts, timezone(timedelta(hours=-5)))
        if ns.get("weekdays_only", True) and o.weekday() >= 5:
            return None
        for t in ns.get("times_et", []):
            hh, mm = (int(x) for x in t.split(":"))
            n = o.replace(hour=hh, minute=mm, second=0).timestamp()
            if slot.open_ts - 60 <= n <= slot.close_ts:
                return t
        return None

    # ------------------------------------------------------------ windows
    async def _roll_loop(self):
        while True:
            now = time.time()
            for s in self.slots.values():
                if s.ticker and now < s.close_ts - 0.5:
                    continue
                if s.ticker:                           # window over: hand to settlement, start fresh
                    if s.st and s.st.order:
                        await self._cancel(s, "window closed")
                    if s.st and (s.st.held > 0 or s.st.sold > 0):
                        # a copy: this slot object is reused for the next window right below
                        self.pending.append(Pending(copy.copy(s), now))
                    self.by_ticker.pop(s.ticker, None)
                    await self.feed.drop_markets([s.ticker])
                    s.ticker, s.st, s.book = "", None, None
                try:
                    m = await self.rest.open_market(s.series)
                except KalshiError as e:
                    log.debug("market lookup %s: %s", s.coin, e)
                    continue
                if not m:
                    continue
                s.ticker, s.open_ts, s.close_ts = m["ticker"], iso_ts(m["open_time"]), iso_ts(m["close_time"])
                try:
                    s.strike = float(m.get("floor_strike")) if m.get("floor_strike") not in (None, "") else None
                except (TypeError, ValueError):
                    s.strike = None
                if "price_ranges" not in m:
                    try:
                        m = dict(m, **{k: v for k, v in (await self.rest.market(s.ticker) or {}).items()
                                       if k == "price_ranges"})
                    except KalshiError:
                        pass
                s.ranges, s.place_errors = parse_ranges(m), 0
                s.book, s.st, s.mode = Book(s.ticker), State(s.ticker), self.cfg["mode"]
                self.by_ticker[s.ticker] = s
                self.rec.write("market", [int(now * 1000), s.coin, s.ticker, s.open_ts, s.close_ts, s.strike, ""])
                await self.feed.add_markets([s.ticker])
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------ websocket
    def on_ws_state(self, state: str):
        self.event("info" if state == "connected" else "warn", "", f"feed {state}")
        if state == "connected":
            if self.live:
                asyncio.create_task(self._reconcile_fills())
            return
        # blind until the feed is back: books are stale (fresh snapshots come on resubscribe),
        # so stop deciding on them and take resting bids off the exchange
        for s in self.slots.values():
            if s.book:
                s.book.ready = False
            if s.st and s.st.order:
                asyncio.create_task(self._cancel(s, "feed lost"))

    def on_ws(self, d: dict, recv_ms: float):
        t = d.get("type")
        m = d.get("msg") or {}
        if t in ("orderbook_snapshot", "orderbook_delta"):
            s = self.by_ticker.get(m.get("market_ticker", ""))
            if not s or not s.book:
                return
            before = s.book.view("yes")
            if t == "orderbook_snapshot":
                s.book.snapshot(m, recv_ms)
            else:
                s.book.delta(m, recv_ms)
            after = s.book.view("yes")
            if (before.bid, before.ask, before.bid_size, before.ask_size) != (after.bid, after.ask, after.bid_size, after.ask_size):
                self.rec.write("book", [int(recv_ms), s.ticker, after.bid, after.bid_size, after.ask, after.ask_size])
            self.evaluate(s, recv_ms)
        elif t == "trade":
            tk = m.get("market_ticker", "")
            self.rec.write("trade", [int(recv_ms), m.get("ts_ms"), tk, dollars_c(m.get("yes_price_dollars", 0)),
                                     m.get("count_fp"), m.get("taker_side")])
            s = self.by_ticker.get(tk)
            if s and s.st and s.st.order and s.mode == "paper":
                self._paper_trade(s, m, recv_ms)
        elif t == "fill":
            self._live_fill(m)
        elif t == "cfbenchmarks_value":
            avg = (m.get("last_60s_windowed_average_15min") or m.get("avg_60s_data") or {}).get("value")
            val = None
            try:
                import json as _j
                val = float(_j.loads(m.get("data") or "{}").get("value"))
            except (ValueError, TypeError):
                pass
            self.index[m.get("index_id", "")] = {"value": val, "avg60": float(avg) if avg else None, "ms": recv_ms}
            self.rec.write("index", [int(recv_ms), m.get("index_id"), val, avg])

    # ------------------------------------------------------------ decisions
    async def _timer_loop(self):
        while True:                                   # time-based decisions (window start/end, stop timer)
            now = time.time() * 1000
            for s in list(self.by_ticker.values()):
                self.evaluate(s, now)
            await asyncio.sleep(0.1)

    def evaluate(self, s: Slot, now_ms: float):
        if s.busy or not s.st or not s.book or not s.book.ready:
            return
        st = s.st
        rule = self.rule(s)
        secs = now_ms / 1000 - s.open_ts
        fav = s.book.favourite()
        side = st.side or fav
        if side is None:
            return
        own = st.order
        view = s.book.view(side, own.price if own else None, (own.qty - own.filled) if own else 0)
        can_trade, why = self.can_trade(s)
        want = None if can_trade else max(st.held, 0)            # None: the active window's contracts
        acts = decide(rule, st, secs, now_ms, view, fav, want, s.ranges)
        if not can_trade:
            if why and st.held == 0:
                st.note = why
            acts = [a for a in acts if a.kind != "place"]
            if st.order and not any(a.kind == "cancel" for a in acts) and why not in ("", None):
                acts.insert(0, Action("cancel", reason=why))
        acts = [a for a in acts if not (a.kind == "sell" and now_ms - st.last_sell_ms < 1500)]
        if acts:
            s.busy = True
            asyncio.create_task(self._act(s, acts, view))

    def can_trade(self, s: Slot) -> tuple[bool, str]:
        c = self.cfg
        if self.halted:
            return False, f"halted: {self.halted}"
        if not c["trading"]:
            return False, "trading is off"
        if not c["markets"].get(s.coin):
            return False, "market switched off"
        rule = self.rule(s)
        if not rule.get("enabled"):
            return False, f"{s.group} rule off"
        if c["mode"] == "live" and not self.rest.has_key:
            return False, "live mode needs an API key"
        news = self.news_now(s)
        if news:
            return False, f"skipping window (news at {news})"
        lim = c["limits"]
        today = self.today()
        if today["lost"] >= lim["stop_after_losses"]:
            return False, f"stopped for today after {today['lost']} losses"
        if -today["pnl_c"] + self.open_risk_c() >= lim["daily_loss_usd"] * 100:
            if not (s.st and (s.st.order or s.st.held)):
                return False, "daily loss limit reached"
        if not (s.st and (s.st.order or s.st.held)):
            busy = sum(1 for o in self.by_ticker.values() if o.group == s.group and o is not s and o.st
                       and (o.st.order or o.st.held))
            cap = lim["max_open_crypto"] if s.group == "crypto" else lim["max_open_commodity"]
            if busy >= cap:
                return False, f"waiting: {busy} {s.group} markets already in play (max {cap})"
        return True, ""

    def today(self) -> dict:
        """Today's results for the current mode, cached for a second (checked on every book change)."""
        now = time.time()
        key = (self.cfg["mode"], et_day(now))
        if getattr(self, "_today_key", None) != key or now - self._today_t > 1.0:
            self._today, self._today_key, self._today_t = self.store.summary(*key), key, now
        return self._today

    async def set_mode(self, mode: str):
        """Switching paper/live cancels whatever is resting; held positions settle in the mode they were bought."""
        for s in self.slots.values():
            if s.st and s.st.order:
                await self._cancel(s, f"switching to {mode}")
        self.cfg["mode"] = mode
        config.save(self.cfg)
        self.event("info", "", f"mode set to {mode.upper()}")

    async def set_trading(self, on: bool):
        self.cfg["trading"] = bool(on)
        if on:
            self.halted = ""
        config.save(self.cfg)
        if not on:
            for s in self.slots.values():
                if s.st and s.st.order:
                    await self._cancel(s, "trading switched off")
        self.event("info", "", f"trading switched {'ON' if on else 'OFF'} ({self.cfg['mode']})")

    def open_risk_c(self) -> float:
        tot = 0.0
        for s in self.by_ticker.values():
            if not s.st:
                continue
            if s.st.held:
                tot += s.st.cost_c * (s.st.held / max(s.st.held + s.st.sold, 1))
            if s.st.order:
                tot += (s.st.order.qty - s.st.order.filled) * s.st.order.price
        return tot

    # ------------------------------------------------------------ actions
    async def _act(self, s: Slot, acts: list[Action], view):
        try:
            for a in acts:
                if a.kind == "cancel":
                    await self._cancel(s, a.reason)
                elif a.kind == "place":
                    await self._place(s, a, view)
                elif a.kind == "sell":
                    await self._sell(s, a, view)
        except Exception as e:                                        # noqa: BLE001
            self.event("error", s.coin, f"{type(e).__name__}: {e}")
        finally:
            s.busy = False

    async def _place(self, s: Slot, a: Action, view):
        st = s.st
        lim = self.cfg["limits"]
        qty = min(a.qty, int(lim["max_order_usd"] * 100 // max(a.price, 1)))
        room = lim["daily_loss_usd"] * 100 + self.today()["pnl_c"] - self.open_risk_c()
        qty = min(qty, int(room // max(a.price, 1)))
        if qty < 1:
            st.note = "no room under the daily loss limit / max per order"
            return
        s.mode = self.cfg["mode"]
        cid = "fmb-" + uuid.uuid4().hex[:20]
        joining = view.bid is not None and abs(a.price - view.bid) < 1e-6
        if s.mode == "live":
            ok, msg = await self.rest.ensure_shard_cash(s.ticker, qty * a.price)
            if msg:
                self.event("info" if ok else "warn", s.coin, msg)
            if not ok:
                st.note = msg
                return
            st.order = Order(a.side, a.price, qty, time.time() * 1000, cid)
            try:
                r = await self.rest.place_bid(s.ticker, a.side, qty, a.price, s.close_ts - 2, cid)
            except Exception as e:                                    # noqa: BLE001
                st.order = None                                       # nothing is resting: don't track a phantom
                s.place_errors += 1
                msg = str(e)
                if "price" in msg.lower() and s.ranges != WHOLE_CENT:
                    s.ranges = list(WHOLE_CENT)                       # grid mismatch: fall back to whole cents
                    msg += " - switching this market to whole cents"
                if s.place_errors >= 3:                               # stop hammering the API this window
                    st.cooldown_until_ms = time.time() * 1000 + 60_000
                    msg += " - pausing this market for 60 s"
                st.note = "order rejected: " + str(e)
                self.event("error", s.coin, f"bid {a.side.upper()} {a.price:g}c rejected: {msg}")
                return
            if r.get("rejected"):                                     # normal in a fast market: retry next update
                st.order = None
                st.note = r["rejected"]
                self.event("info", s.coin, f"bid {a.side.upper()} {a.price:g}c not placed: {r['rejected']}")
                return
            s.place_errors = 0
            st.order.order_id = r.get("order_id", "")
            if st.order.order_id:
                self.order_kind[st.order.order_id] = "buy"
        else:
            st.order = Order(a.side, a.price, qty, time.time() * 1000 + PAPER_LATENCY_MS, cid)
            s.queue = view.bid_size if joining else 0.0
        st.side = a.side
        st.note = f"bid {a.side.upper()} {a.price:g}c x{qty}"
        self.event("bid", s.coin, f"{s.mode} bid {a.side.upper()} {a.price:g}c x{qty} ({a.reason})")

    async def _cancel(self, s: Slot, reason: str):
        st = s.st
        if not st or not st.order:
            return
        o = st.order
        if s.mode == "live" and o.order_id:
            for attempt in range(2):
                try:
                    await self.rest.cancel(o.order_id, s.ticker)
                    break
                except KalshiError as e:
                    if e.status == 404:               # already gone (filled or expired); fills arrive on the feed
                        break
                    if attempt == 1:
                        self.event("error", s.coin, f"cancel failed twice ({e}) - cancelling everything and halting")
                        await self.halt(f"could not cancel an order on {s.coin}")
                        return
                    await asyncio.sleep(0.2)
        st.order = None
        self.event("cancel", s.coin, f"{s.mode} cancel {o.side.upper()} {o.price:g}c ({reason})")

    async def _sell(self, s: Slot, a: Action, view):
        st = s.st
        if st.held <= 0:
            st.exiting = False
            return
        if st.order:
            await self._cancel(s, "stop")
        st.last_sell_ms = time.time() * 1000
        if s.mode == "live":
            r = await self.rest.sell_ioc(s.ticker, a.side, st.held, a.price, "fms-" + uuid.uuid4().hex[:20])
            if r.get("order_id"):
                self.order_kind[r["order_id"]] = "sell"
            # fills (and their prices) arrive on the feed; the response is logged only
            self.event("stop", s.coin, f"live stop sell {r.get('fill_count')}/{st.held} {a.side.upper()} "
                                        f"(floor {a.price:g}c) - {a.reason}")
        else:
            px = max((view.bid or a.price) - 0.5, a.price, 1)
            n = st.held
            fee = taker_fee_c(px, n)
            st.sold += n
            st.held = 0
            st.proceeds_c += n * px - fee
            self.event("stop", s.coin, f"paper stop sold {n} {a.side.upper()} at {px:g}c - {a.reason}")
        if st.held == 0:
            st.exiting = False

    async def halt(self, why: str):
        self.halted = why
        self.cfg["trading"] = False
        config.save(self.cfg)
        try:                                                          # only our orders: other bots may share the account
            n, errs = await self.rest.cancel_mine()
            if errs:
                self.event("error", "", f"some cancels failed - check Kalshi: {errs}")
        except Exception as e:                                         # noqa: BLE001
            self.event("error", "", f"cancelling our orders failed - check Kalshi: {e}")
        for s in self.slots.values():
            if s.st:
                s.st.order = None
        self.event("error", "", f"HALTED: {why}. Trading switched off.")

    # ------------------------------------------------------------ fills
    def _paper_trade(self, s: Slot, m: dict, recv_ms: float):
        o = s.st.order
        if recv_ms < o.placed_ms:
            return
        maker_side = "yes" if m.get("taker_side") == "no" else "no"
        if maker_side != o.side:
            return
        px = dollars_c(m["yes_price_dollars"]) if maker_side == "yes" else dollars_c(m["no_price_dollars"])
        cnt = float(m.get("count_fp") or 0)
        rem = o.qty - o.filled
        got = 0
        if px < o.price - 1e-6:
            got = rem
        elif abs(px - o.price) < 1e-6:
            s.queue -= cnt
            if s.queue < 0:
                got = int(min(rem, -s.queue))
                s.queue = 0
        if got > 0:
            self._apply_buy(s, got, o.price, 0.0)
            o.filled += got
            if o.filled >= o.qty:
                s.st.order = None

    def _live_fill(self, m: dict):
        tid = m.get("trade_id", "")
        if not tid or tid in self.seen_fills:
            return
        self.seen_fills.add(tid)
        s = self.by_ticker.get(m.get("market_ticker", "")) or next(
            (p.slot for p in self.pending if p.slot.ticker == m.get("market_ticker")), None)
        if not s or not s.st:
            if str(m.get("client_order_id") or "").startswith(("fmb-", "fms-")):
                self.event("warn", "", f"fill for an untracked market {m.get('market_ticker')}")
            return
        cid = m.get("client_order_id") or ""
        oid = m.get("order_id", "")
        # the fill feed reports every fill on the account: only ours count (another bot may trade it too)
        if cid.startswith("fms-") or (not cid and self.order_kind.get(oid) == "sell"):
            kind = "sell"
        elif cid.startswith("fmb-") or (not cid and self.order_kind.get(oid) == "buy"):
            kind = "buy"
        else:
            if tid not in self._foreign_seen:
                self._foreign_seen.add(tid)
                log.info("ignoring a fill that isn't ours: %s %s", m.get("market_ticker"), cid or oid)
            return
        n = int(float(m.get("count_fp") or 0))
        # we only ever hold one side per window; trust that over the message's own fields
        side = s.st.side or next((x for x in (m.get("purchased_side"), m.get("side")) if x in ("yes", "no")), None)
        if side is None:
            self.event("warn", s.coin, f"fill with unknown side ignored: {m}")
            return
        s.st.side = side
        yes_px = dollars_c(m.get("yes_price_dollars", 0))
        px = yes_px if side == "yes" else 100 - yes_px
        fee = float(m.get("fee_cost") or 0) * 100
        if kind == "buy":
            self._apply_buy(s, n, px, fee)
            o = s.st.order
            if o and (o.order_id == m.get("order_id") or o.client_id == cid):
                o.filled += n
                if o.filled >= o.qty:
                    s.st.order = None
        else:
            s.st.sold += n
            s.st.held = max(s.st.held - n, 0)
            s.st.proceeds_c += n * px - fee

    def _apply_buy(self, s: Slot, n: int, px: float, fee: float):
        st = s.st
        st.held += n
        st.cost_c += n * px + fee
        st.side = st.side or s.book.favourite()
        self.event("fill", s.coin, f"{s.mode} filled {n} {st.side.upper()} at {px:g}c")

    async def _reconcile_fills(self):
        """After a reconnect, pick up fills we may have missed while the feed was down."""
        try:
            since = int(min([s.open_ts for s in self.by_ticker.values()] or [time.time() - 900]))
            r = await self.rest.request("GET", "/portfolio/fills", {"min_ts": since, "limit": 200})
            for f in r.get("fills") or []:
                f.setdefault("market_ticker", f.get("ticker"))
                if f.get("trade_id") and f["trade_id"] not in self.seen_fills:
                    self._live_fill(f)
        except Exception as e:                                         # noqa: BLE001
            self.event("warn", "", f"fill check after reconnect failed: {e}")

    # ------------------------------------------------------------ settlement
    async def _settle_loop(self):
        while True:
            await self.settle_once()
            await asyncio.sleep(3)

    async def settle_once(self):
        now = time.time()
        for p in list(self.pending):
            s = p.slot
            if now < s.close_ts + SETTLE_AFTER_S:
                continue
            try:
                m = await self.rest.market(s.ticker) or {}
            except KalshiError:
                continue
            res = str(m.get("result", "")).lower()
            if res not in ("yes", "no"):
                if now - p.since > 3 * 3600:
                    self.event("warn", s.coin, f"no result for {s.ticker} after 3 h - dropped")
                    self.pending.remove(p)
                continue
            st = s.st
            won = res == st.side
            bought = st.held + st.sold
            payout = st.held * 100 if won else 0
            pnl = payout + st.proceeds_c - st.cost_c
            outcome = "sold" if st.sold and not st.held else ("won" if won else "lost")
            avg = st.cost_c / bought if bought else 0
            self.store.result(s.mode, s.coin, s.ticker, st.side, bought, round(avg, 2), st.sold, outcome, round(pnl, 2))
            self.rec.write("market", [int(now * 1000), s.coin, s.ticker, s.open_ts, s.close_ts, s.strike, res])
            self.event("result", s.coin, f"{s.mode} {st.side.upper()} x{bought} {outcome} {pnl:+.1f}c")
            self.pending.remove(p)

    # ------------------------------------------------------------ status for the web page
    def status(self) -> dict:
        now = time.time()
        mode = self.cfg["mode"]
        rows = []
        coin_pnl = self.store.by_coin(mode, et_day())
        for c, s in self.slots.items():
            row = {"coin": c, "group": s.group, "enabled": self.cfg["markets"].get(c, False), "ticker": s.ticker,
                   "left_s": max(s.close_ts - now, 0) if s.ticker else None, "pnl_today_c": coin_pnl.get(c, 0.0)}
            if s.book and s.book.ready:
                v = s.book.view("yes")
                row.update(yes_bid=v.bid, yes_ask=v.ask, fav=s.book.favourite())
            if s.st:
                row.update(note=s.st.note, held=s.st.held, side=s.st.side,
                           order=({"side": s.st.order.side, "price": s.st.order.price,
                                   "qty": s.st.order.qty - s.st.order.filled} if s.st.order else None))
            idx = self.index.get(config.CF_INDEX.get(c, ""))
            if idx:
                row["index"] = idx["value"]
                row["strike"] = s.strike
            rows.append(row)
        return {
            "now": now, "mode": mode, "env": self.cfg["env"], "trading": self.cfg["trading"],
            "halted": self.halted, "has_key": self.rest.has_key, "key_error": self.rest.key_error,
            "feed": self.feed.state, "msgs": self.feed.msgs, "skew_ms": self.feed.skew_ms,
            "rest_ms": self.rest.rtt_ms, "uptime_s": now - self.started,
            "today": self.store.summary(mode, et_day()), "all": self.store.summary(mode),
            "other_today": self.store.summary("paper" if mode == "live" else "live", et_day()),
            "open_risk_c": self.open_risk_c(), "markets": rows,
            "events": self.store.recent_events(40), "results": self.store.recent_results(25),
            "daily": self.store.daily(mode),
        }
