"""Order book for one market, built from Kalshi's WebSocket snapshot + deltas.

Kalshi lists bids for both sides: YES bids and NO bids. A NO bid at x is a YES offer at 100 - x, so
    YES best bid = max(YES bids)          YES best ask = 100 - max(NO bids)
    NO  best bid = max(NO bids)           NO  best ask = 100 - max(YES bids)

`view(side, own)` reads the book for one side with our own resting order taken out, so the bot never
mistakes its own bid for the market's (the bug that made the old bot raise against itself).
"""
from __future__ import annotations

from dataclasses import dataclass, field


def _key(price_c: float) -> int:
    return int(round(price_c * 10))               # tenths of a cent as integer keys (0.1c ticks)


@dataclass
class View:
    bid: float | None          # best bid for this side (cents), excluding our order
    bid_size: float
    ask: float | None          # best offer for this side (cents)
    ask_size: float

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


@dataclass
class Book:
    ticker: str
    yes: dict[int, float] = field(default_factory=dict)   # price key -> contracts
    no: dict[int, float] = field(default_factory=dict)
    ready: bool = False
    updated_ms: float = 0.0

    def snapshot(self, msg: dict, recv_ms: float) -> None:
        self.yes, self.no = {}, {}
        for name, levels in (("yes", msg.get("yes_dollars_fp") or []), ("no", msg.get("no_dollars_fp") or [])):
            d = self.yes if name == "yes" else self.no
            for px, qty in levels:
                q = float(qty)
                if q > 0:
                    d[_key(float(px) * 100)] = q
        self.ready, self.updated_ms = True, recv_ms

    def delta(self, msg: dict, recv_ms: float) -> None:
        d = self.yes if msg.get("side") == "yes" else self.no
        k = _key(float(msg["price_dollars"]) * 100)
        q = d.get(k, 0.0) + float(msg.get("delta_fp", 0))
        if q > 1e-9:
            d[k] = q
        else:
            d.pop(k, None)
        self.updated_ms = recv_ms

    @staticmethod
    def _best(d: dict[int, float], own_key: int | None = None, own_qty: float = 0.0) -> tuple[float | None, float]:
        for k in sorted(d, reverse=True):
            q = d[k] - (own_qty if k == own_key else 0.0)
            if q > 1e-9:
                return k / 10, q
        return None, 0.0

    def view(self, side: str, own_price: float | None = None, own_qty: float = 0.0) -> View:
        """Best bid/ask for buying `side` ('yes' or 'no'), with our own resting bid on that side removed."""
        own_key = _key(own_price) if own_price is not None else None
        mine, other = (self.yes, self.no) if side == "yes" else (self.no, self.yes)
        bid, bsz = self._best(mine, own_key, own_qty)
        ob, osz = self._best(other)
        return View(bid, bsz, (100 - ob) if ob is not None else None, osz)

    def favourite(self) -> str | None:
        v = self.view("yes")
        if v.mid is None:
            return None
        return "yes" if v.mid >= 50 else "no"
