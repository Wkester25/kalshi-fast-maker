"""The trading rule, as plain decisions with no network code (so it can be tested).

Early favourite maker (what the research supported):
  * between start_s and end_s seconds into the window, if the market's favourite has its best bid in
    [lo_c, hi_c), rest a buy one tick above that bid (never at or above the offer)
  * move up only when someone else outbids us; never chase our own order
  * hold to settlement; sell everything if our side's bid stays below the window's stop_c for its stop_ms
    (the stop of the window that bought the position, even after that window has ended; 0 = hold)

Speed additions (only useful with a fast connection):
  * pull: if our side's mid falls by pull_drop_c within pull_ms, cancel at once and wait pull_cooldown_ms
  * stop and cancels react to every book change, not once a second
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

TICK = 0.1                                   # finest grid; real markets publish their own (see step_at)
WHOLE_CENT = [(0.0, 100.0, 1.0)]             # valid on every Kalshi market


def parse_ranges(m: dict | None) -> list[tuple[float, float, float]]:
    """Kalshi's per-market price grid ('price_ranges', in dollars, in YES terms) as (start_c, end_c, step_c).
    Falls back to whole cents, which every market accepts."""
    out = []
    for r in (m or {}).get("price_ranges") or []:
        try:
            a, b, st = (float(r[k]) * 100 for k in ("start", "end", "step"))
        except (KeyError, TypeError, ValueError):
            continue
        if st > 0 and b > a:
            out.append((a, b, st))
    return sorted(out) or list(WHOLE_CENT)


def step_at(ranges, side: str, price_c: float) -> float:
    """Tick size where `side` trades at price_c (the grid is published in YES prices)."""
    y = price_c if side == "yes" else 100 - price_c
    for a, b, st in ranges or WHOLE_CENT:
        if a - 1e-9 <= y < b - 1e-9:
            return st
    return (ranges or WHOLE_CENT)[-1][2] if y >= 50 else (ranges or WHOLE_CENT)[0][2]


def snap(price_c: float, step: float, up: bool = False) -> float:
    """Put a price on the grid (down by default)."""
    n = price_c / step
    n = math.ceil(n - 1e-6) if up else math.floor(n + 1e-6)
    return round(n * step, 4)


@dataclass
class Order:
    side: str
    price: float
    qty: int
    placed_ms: float
    client_id: str = ""
    order_id: str = ""
    filled: int = 0


@dataclass
class State:
    """Per market, per window."""
    ticker: str
    side: str | None = None            # locked once we bid or hold
    order: Order | None = None
    held: int = 0
    cost_c: float = 0.0                # total paid for what we hold (cents)
    sold: int = 0
    proceeds_c: float = 0.0
    low_since_ms: float | None = None  # when our side's bid first went below the stop
    exiting: bool = False
    cooldown_until_ms: float = 0.0
    last_sell_ms: float = 0.0          # live: when the last stop sell went out (fills arrive on the feed)
    stop: tuple | None = None          # (stop_c, stop_ms) of the window that placed our latest bid
    mids: deque = field(default_factory=lambda: deque(maxlen=400))
    note: str = "waiting"


@dataclass
class Action:
    kind: str            # "place" | "cancel" | "sell"
    side: str = ""
    price: float = 0.0
    qty: int = 0
    reason: str = ""


def bid_price(bid: float, ask: float, hi: float, step: float = TICK) -> float:
    """One tick above the best bid, at least one tick below the offer, below the band's top,
    always on the market's price grid."""
    base = snap(bid, step)
    p = min(round(base + step, 4), snap(ask - step, step))
    if p <= bid + 1e-9 or p >= hi - 1e-9:
        p = base                                # no room to improve: join the bid (on the grid)
    return p


def mmss(s: float) -> str:
    return f"{int(s) // 60}:{int(s) % 60:02d}"


def active_window(rule: dict, secs: float) -> dict | None:
    for w in rule.get("windows") or []:
        if w["start_s"] <= secs <= w["end_s"]:
            return w
    return None


def next_window(rule: dict, secs: float) -> dict | None:
    later = [w for w in rule.get("windows") or [] if w["start_s"] > secs]
    return min(later, key=lambda w: w["start_s"]) if later else None


def stop_for(rule: dict, st: State, secs: float) -> tuple[float, int]:
    """The stop protecting what we hold: the window that bought it; after a restart (unknown), the window
    we're in, else the last one that has started, else the first."""
    if st.stop is not None:
        return st.stop
    wins = rule.get("windows") or []
    w = active_window(rule, secs) or next((x for x in reversed(wins) if x["start_s"] <= secs), None) or \
        (wins[0] if wins else {"stop_c": 50.0, "stop_ms": 1000})
    return float(w["stop_c"]), int(w["stop_ms"])


def decide(rule: dict, st: State, secs: float, now_ms: float, view, fav: str | None, want_qty: int | None = None,
           ranges=None) -> list[Action]:
    """view: book.View for st.side (or for fav when no side yet), with our own order removed.
    want_qty: None = the active window's contracts; the engine passes what we hold when we may not buy.
    ranges: the market's price grid (parse_ranges); None = 0.1c everywhere (tests)."""
    acts: list[Action] = []
    win = active_window(rule, secs)
    if want_qty is None:
        want_qty = int(win["contracts"]) if win else 0
    side = st.side or fav
    if side is None or view is None:
        st.note = "no book yet"
        return acts

    # ---- holding: the market stop
    if st.held > 0:
        if st.exiting:
            acts.append(Action("sell", side, max(math.floor((view.bid or 1) - 5), 1), st.held, "stop: still selling"))
            return acts
        # no bids at all on our side is the worst case, not "no information": treat it as 0 for the stop
        b = view.bid if view.bid is not None else 0.0
        stop_c, stop_ms = stop_for(rule, st, secs)
        if stop_c <= 0:                                # stop 0 = hold to settlement, never sell
            st.low_since_ms = None
            st.note = f"holding {st.held} {side.upper()} to settlement (no stop)"
        elif b < stop_c:
            if st.low_since_ms is None:
                st.low_since_ms = now_ms
            if now_ms - st.low_since_ms >= stop_ms:
                st.exiting = True
                acts.append(Action("sell", side, max(math.floor(b - 5), 1), st.held,
                                   f"stop: bid {b:g}c under {stop_c:g}c for {stop_ms} ms"))
                return acts
            st.note = f"holding {st.held} {side.upper()}, bid {b:g}c below stop"
        else:
            st.low_since_ms = None
            st.note = f"holding {st.held} {side.upper()}"
        if st.held >= want_qty:
            return acts

    # ---- trading windows
    if win is None:
        if st.order:
            acts.append(Action("cancel", reason="outside the trading windows"))
        if st.held == 0:
            nxt = next_window(rule, secs)
            st.note = f"next window at {mmss(nxt['start_s'])}" if nxt else "done for this market"
        return acts
    if st.exiting or st.sold:
        return acts

    # ---- speed: pull on a sharp drop of our side's mid
    mid = view.mid
    if mid is not None:
        st.mids.append((now_ms, mid))
        while st.mids and now_ms - st.mids[0][0] > rule["pull_ms"]:
            st.mids.popleft()
        if rule.get("pull") and st.mids:
            peak = max(m for _, m in st.mids)
            if peak - mid >= rule["pull_drop_c"]:
                st.cooldown_until_ms = now_ms + rule["pull_cooldown_ms"]
                st.mids.clear()
                if st.order:
                    acts.append(Action("cancel", reason=f"pull: {side.upper()} fell {peak - mid:.1f}c fast"))
                st.note = "pulled - market moving against us"
                return acts
    if now_ms < st.cooldown_until_ms:
        if st.order:
            acts.append(Action("cancel", reason="paused"))
        st.note = st.note if st.note.startswith("order rejected") else "paused after a fast drop"
        return acts

    # ---- band
    b, a = view.bid, view.ask
    if b is None or a is None or not (win["lo_c"] <= b < win["hi_c"]) or a > 99.95:
        if st.order:
            acts.append(Action("cancel", reason=f"left the {win['lo_c']:g}-{win['hi_c']:g}c band"))
        st.note = f"favourite {side.upper()} {b if b is not None else '-'}/{a if a is not None else '-'}, waiting for band"
        return acts

    step = step_at(ranges, side, b) if ranges else TICK
    price = bid_price(b, a, win["hi_c"], step)
    qty = want_qty - st.held
    if st.order:
        if b > st.order.price + 1e-9 and price > st.order.price + 1e-9:     # someone else outbid us
            acts.append(Action("cancel", reason=f"outbid at {b:g}"))
            acts.append(Action("place", side, price, qty, f"move up to {price:g}"))
            st.stop = (float(win["stop_c"]), int(win["stop_ms"]))
        else:
            st.note = f"bid {st.order.side.upper()} {st.order.price:g}c (market {b:g}/{a:g})"
        return acts
    if qty > 0:
        acts.append(Action("place", side, price, qty, f"favourite {side.upper()} {b:g}/{a:g}"))
        st.stop = (float(win["stop_c"]), int(win["stop_ms"]))     # what we buy here keeps this window's stop
    return acts
