"""The trading rule, as plain decisions with no network code (so it can be tested).

Early favourite maker (what the research supported):
  * between start_s and end_s seconds into the window, if the market's favourite has its best bid in
    [lo_c, hi_c), rest a buy one tick above that bid (never at or above the offer)
  * move up only when someone else outbids us; never chase our own order
  * hold to settlement; sell everything if our side's bid stays below stop_c for stop_ms

Speed additions (only useful with a fast connection):
  * pull: if our side's mid falls by pull_drop_c within pull_ms, cancel at once and wait pull_cooldown_ms
  * stop and cancels react to every book change, not once a second
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

TICK = 0.1


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
    mids: deque = field(default_factory=lambda: deque(maxlen=400))
    note: str = "waiting"


@dataclass
class Action:
    kind: str            # "place" | "cancel" | "sell"
    side: str = ""
    price: float = 0.0
    qty: int = 0
    reason: str = ""


def bid_price(bid: float, ask: float, hi: float) -> float:
    """One tick above the best bid, at least one tick below the offer, below the band's top."""
    p = round(min(round(bid / TICK) * TICK + TICK, round(ask / TICK) * TICK - TICK), 1)
    if p <= bid - 1e-9 or p >= hi - 1e-9:
        p = round(bid, 1)                       # spread is one tick, or the band's top: join the bid
    return p


def decide(rule: dict, st: State, secs: float, now_ms: float, view, fav: str | None, want_qty: int) -> list[Action]:
    """view: book.View for st.side (or for fav when no side yet), with our own order removed."""
    acts: list[Action] = []
    side = st.side or fav
    if side is None or view is None:
        st.note = "no book yet"
        return acts

    # ---- holding: the market stop
    if st.held > 0:
        if st.exiting:
            acts.append(Action("sell", side, max((view.bid or 1) - 5, 1), st.held, "stop: still selling"))
            return acts
        b = view.bid
        if b is not None and b < rule["stop_c"]:
            if st.low_since_ms is None:
                st.low_since_ms = now_ms
            if now_ms - st.low_since_ms >= rule["stop_ms"]:
                st.exiting = True
                acts.append(Action("sell", side, max(b - 5, 1), st.held,
                                   f"stop: bid {b:g}c under {rule['stop_c']:g}c for {rule['stop_ms']} ms"))
                return acts
            st.note = f"holding {st.held} {side.upper()}, bid {b:g}c below stop"
        else:
            st.low_since_ms = None
            st.note = f"holding {st.held} {side.upper()}"
        if st.held >= want_qty:
            return acts

    # ---- time window
    if not (rule["start_s"] <= secs <= rule["end_s"]):
        if st.order:
            acts.append(Action("cancel", reason="outside bidding time"))
        if st.held == 0:
            st.note = "waiting for bidding time" if secs < rule["start_s"] else "done for this window"
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
            acts.append(Action("cancel", reason="cooling down after a pull"))
        st.note = "cooling down after a pull"
        return acts

    # ---- band
    b, a = view.bid, view.ask
    if b is None or a is None or not (rule["lo_c"] <= b < rule["hi_c"]) or a > 99.95:
        if st.order:
            acts.append(Action("cancel", reason=f"left the {rule['lo_c']:g}-{rule['hi_c']:g}c band"))
        st.note = f"favourite {side.upper()} {b if b is not None else '-'}/{a if a is not None else '-'}, waiting for band"
        return acts

    price = bid_price(b, a, rule["hi_c"])
    qty = want_qty - st.held
    if st.order:
        if b > st.order.price + 1e-9 and price > st.order.price + 1e-9:     # someone else outbid us
            acts.append(Action("cancel", reason=f"outbid at {b:g}"))
            acts.append(Action("place", side, price, qty, f"move up to {price:g}"))
        else:
            st.note = f"bid {st.order.side.upper()} {st.order.price:g}c (market {b:g}/{a:g})"
        return acts
    if qty > 0:
        acts.append(Action("place", side, price, qty, f"favourite {side.upper()} {b:g}/{a:g}"))
    return acts
