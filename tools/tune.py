#!/usr/bin/env python3
"""Find the best fast-pull and stop settings for your active trading windows, from recorded data.

    python tools/tune.py                         # on the server, reads data/ and config.json next to it
    python tools/tune.py --data path/to/unzipped  # on a PC, after downloading days from the Data tab
    python tools/tune.py --group crypto --days 3

How it works
  * Every recorded market is replayed through the bot's own decision code (fastmaker.strategy.decide),
    in the order your server received the data (recv_ms), so it can't use anything the bot wouldn't have
    known at that moment.
  * Entries: fills are simulated like paper mode (queue position when we join, a fill needs a real trade
    at or through our price, 60 ms order latency). Fast pull changes which bids get filled, so each pull
    setting is replayed in full.
  * Exits: from each fill to the close, every stop setting is tried on the recorded bids; a stop sells at
    the bid less 0.5c and pays the taker fee; otherwise the position settles on the market's result.
  * Pull is per rule (it's shared by the rule's windows); stop is chosen per window.
  * Overfitting guard: settings are picked on the earliest 70% of markets and judged on the latest 30%.
    A change is only recommended when it also beats your current settings on that held-out 30%.

It runs at low priority so the bot keeps the CPU it needs. Results are per 1 contract.
"""
from __future__ import annotations

import argparse
import copy
import csv
import gzip
import heapq
import json
import math
import os
import statistics
import sys
import time
from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastmaker import config  # noqa: E402
from fastmaker.book import View  # noqa: E402
from fastmaker.strategy import WHOLE_CENT, Order, State, active_window, decide  # noqa: E402

LATENCY_MS = 60                        # same as the bot's paper mode
TAPERED = [(0.0, 10.0, 0.1), (10.0, 90.0, 1.0), (90.0, 100.0, 0.1)]

PULL_GRID = [None] + [(d, ms, cd) for d in (0.5, 1.0, 1.5, 2.0, 3.0) for ms in (500, 1000, 2000, 4000)
                      for cd in (2000, 5000, 15000)]
PULL_GRID_QUICK = [None] + [(d, ms, cd) for d in (1.0, 1.5, 2.5) for ms in (1000, 3000) for cd in (3000, 15000)]
STOP_MS_GRID = (0, 250, 500, 1000, 2000, 3000, 5000, 10000)


def stop_price_grid(lo_c: float) -> list[float]:
    cands = {0.0} | {float(x) for x in range(10, 100, 10)} | {lo_c - d for d in (1, 2, 3, 5, 8)}
    return sorted(c for c in cands if c == 0 or 1 <= c < lo_c)


# ------------------------------------------------------------------ data
NAN = float("nan")


@dataclass
class Market:
    """One market's recording, as compact arrays (a few bytes per row, shared by the worker processes)."""
    ticker: str
    coin: str = ""
    group: str = ""
    open: float = 0.0
    close: float = 0.0
    result: str = ""
    bt: array = field(default_factory=lambda: array("q"))     # book: recv ms
    yb: array = field(default_factory=lambda: array("d"))     # YES bid (nan = none)
    ybs: array = field(default_factory=lambda: array("d"))
    ya: array = field(default_factory=lambda: array("d"))     # YES ask (nan = none)
    yas: array = field(default_factory=lambda: array("d"))
    tt: array = field(default_factory=lambda: array("q"))     # trades: recv ms
    tp: array = field(default_factory=lambda: array("d"))     # YES price
    tc: array = field(default_factory=lambda: array("d"))     # contracts
    tk: array = field(default_factory=lambda: array("b"))     # taker side: 1 = yes, 0 = no
    fractional: bool = False


def _nn(x: float):
    return None if x != x else x


def _f(x):
    return float(x) if x not in ("", None) else None


def find_day_dirs(data: Path) -> list[Path]:
    if (data / "rec").is_dir():
        data = data / "rec"
    if data.name[:2] == "20" and len(data.name) == 10:
        return [data]
    days = sorted(p for p in data.iterdir() if p.is_dir() and p.name[:2] == "20" and len(p.name) == 10)
    if not days:                                # an unzipped download: <folder>/<day>/...
        days = sorted(p for d in data.iterdir() if d.is_dir() for p in [d] if any(d.glob("book*.csv.gz")))
    return days


def _rows(day: Path, stream: str):
    for f in sorted(day.glob(f"{stream}*.csv.gz")):
        try:
            with gzip.open(f, "rt", newline="") as fh:
                rd = csv.reader(fh)
                next(rd, None)
                for r in rd:
                    yield r
        except (EOFError, OSError):            # the hour still being written ends without a gzip trailer
            continue


def _reorder(arrs: list, key: array) -> None:
    """Sort parallel arrays by `key` in place (only needed when files overlap in time)."""
    if all(key[i] <= key[i + 1] for i in range(len(key) - 1)):
        return
    idx = sorted(range(len(key)), key=key.__getitem__)
    for a in arrs:
        b = array(a.typecode, (a[i] for i in idx))
        a[:] = b


def load_markets(days: list[Path]) -> dict[str, Market]:
    """Every market's open/close and (where the bot recorded it) result. Small and fast."""
    mk: dict[str, Market] = {}
    for day in days:
        for r in _rows(day, "market"):
            if len(r) < 7 or r[1] not in config.MARKETS:
                continue
            m = mk.setdefault(r[2], Market(r[2]))
            m.coin, m.open, m.close = r[1], float(r[3]), float(r[4])
            m.group = config.MARKETS[r[1]][1]
            if r[6] in ("yes", "no"):
                m.result = r[6]
    return mk


def infer_result(m: Market) -> str:
    """The bot only asks Kalshi for results of markets it traded. For the rest, the book in the last 20 s
    before the close settles it: favourite bid >= 95c (checked against every recorded result: no misses)."""
    hi = int(m.close * 1000) - 1000
    i = bisect_right(m.bt, hi) - 1
    if i < 0 or m.bt[i] < hi - 19_000:
        return ""
    if m.yb[i] == m.yb[i] and m.yb[i] >= 95:
        return "yes"
    if m.ya[i] == m.ya[i] and m.ya[i] <= 5:
        return "no"
    return ""


def load_rows(days: list[Path], mk: dict[str, Market], from_s: dict[str, float]) -> dict[str, Market]:
    """Book and trades only for the markets asked for, and only from `from_s` seconds into each market until
    its close - what the replay needs. (A day is ~50 million book rows; loading all of it would need GBs.)"""
    lo = {t: int((mk[t].open + max(from_s[t], 0)) * 1000) for t in from_s}
    hi = {t: int(mk[t].close * 1000) - 500 for t in from_s}         # nothing after the bot lets go
    mk = {t: Market(t, mk[t].coin, mk[t].group, mk[t].open, mk[t].close, mk[t].result) for t in from_s}  # fresh
    for day in days:
        for r in _rows(day, "book"):
            t = r[1]
            if t not in lo or len(r) < 6:
                continue
            ms = int(r[0])
            if not lo[t] <= ms < hi[t]:
                continue
            m = mk[t]
            m.bt.append(ms)
            m.yb.append(float(r[2]) if r[2] else NAN)
            m.ybs.append(float(r[3]) if r[3] else 0.0)
            m.ya.append(float(r[4]) if r[4] else NAN)
            m.yas.append(float(r[5]) if r[5] else 0.0)
            if not m.fractional:
                for x in (r[2], r[4]):
                    if x and abs(float(x) - round(float(x))) > 1e-6:
                        m.fractional = True
        for r in _rows(day, "trade"):
            t = r[2]
            if t not in lo or len(r) < 6 or not r[3]:
                continue
            ms = int(r[0])
            if not lo[t] <= ms < hi[t]:
                continue
            m = mk[t]
            m.tt.append(ms)
            m.tp.append(float(r[3]))
            m.tc.append(float(r[4] or 0))
            m.tk.append(1 if r[5] == "yes" else 0)
    out = {}
    for t in from_s:
        m = mk[t]
        if not m.bt:
            continue
        _reorder([m.yb, m.ybs, m.ya, m.yas, m.bt], m.bt)
        _reorder([m.tp, m.tc, m.tk, m.tt], m.tt)
        _dedupe(m)
        if not m.result:
            m.result = infer_result(m)
        if m.result:
            out[t] = m
    return out


def _dedupe(m: Market) -> None:
    """Older daily files and the hourly files can overlap: drop exact repeats (they're adjacent once sorted)."""
    def run(key, arrs):
        keep = [i for i in range(len(key)) if i == 0 or key[i] != key[i - 1]
                or any(a[i] != a[i - 1] and not (a[i] != a[i] and a[i - 1] != a[i - 1]) for a in arrs)]
        if len(keep) != len(key):
            for a in [key] + arrs:
                a[:] = array(a.typecode, (a[i] for i in keep))
    run(m.bt, [m.yb, m.ybs, m.ya, m.yas])
    run(m.tt, [m.tp, m.tc, m.tk])


def load_settings(path: Path) -> dict:
    raw = json.loads(path.read_text()) if path.exists() else {}
    cfg = config._merge(config.DEFAULTS, raw)
    saved = raw.get("rules") or {}
    for g in cfg["rules"]:
        r = dict(cfg["rules"][g])
        if isinstance(saved.get(g), dict) and "windows" not in saved[g]:
            r.pop("windows", None)
        cfg["rules"][g] = config.normalize_rule(r)
    return cfg


def news_blocked(cfg: dict, m: Market) -> bool:
    ns = cfg.get("news_skip") or {}
    if not ns.get("enabled"):
        return False
    o = datetime.fromtimestamp(m.open, timezone(timedelta(hours=-5)))
    if ns.get("weekdays_only", True) and o.weekday() >= 5:
        return False
    for t in ns.get("times_et", []):
        hh, mm = (int(x) for x in t.split(":"))
        n = o.replace(hour=hh, minute=mm, second=0).timestamp()
        if m.open - 60 <= n <= m.close:
            return True
    return False


# ------------------------------------------------------------------ replay
def views(m: Market, i: int):
    yb, ybs, ya, yas = _nn(m.yb[i]), m.ybs[i], _nn(m.ya[i]), m.yas[i]
    yes = View(yb, ybs, ya, yas)
    no = View(None if ya is None else round(100 - ya, 4), yas, None if yb is None else round(100 - yb, 4), ybs)
    return yes, no


def side_bid(m: Market, i: int, side: str) -> float:
    """Our side's best bid at book row i (no bids = 0, the way the bot's stop sees it)."""
    if side == "yes":
        b = m.yb[i]
        return 0.0 if b != b else b
    a = m.ya[i]
    return 0.0 if a != a else 100 - a


def sim_entry(m: Market, rule: dict) -> tuple | None:
    """Replay one market's bidding with this rule (stops off). Returns (fill_ms, price, side, window) or None."""
    wins = rule["windows"]
    t0 = int((m.open + min(w["start_s"] for w in wins)) * 1000)
    t1 = int((m.open + max(w["end_s"] for w in wins) + 1) * 1000)
    ranges = TAPERED if m.fractional else list(WHOLE_CENT)
    st = State(m.ticker)
    queue_ahead = 0.0
    win_of_order = -1
    i = bisect_left(m.bt, t0)
    i_end = bisect_right(m.bt, t1)
    cur = views(m, i - 1) if i > 0 else None             # the book as it stood when bidding opens
    j = bisect_left(m.tt, t0)
    j_end = bisect_right(m.tt, t1)
    ticks = [(w["start_s"] + m.open) * 1000 for w in wins] + [(w["end_s"] + m.open) * 1000 + 1 for w in wins]
    heapq.heapify(ticks)

    def evaluate(now):
        nonlocal queue_ahead, win_of_order
        if cur is None:
            return
        yes, no = cur
        fav = None if yes.mid is None else ("yes" if yes.mid >= 50 else "no")
        side = st.side or fav
        if side is None:
            return
        view = yes if side == "yes" else no
        for a in decide(rule, st, now / 1000 - m.open, now, view, fav, None, ranges):
            if a.kind == "cancel":
                st.order = None
            elif a.kind == "place":
                st.order = Order(a.side, a.price, a.qty, now + LATENCY_MS)
                st.side = a.side
                joining = view.bid is not None and abs(a.price - view.bid) < 1e-6
                queue_ahead = view.bid_size if joining else 0.0
                w = active_window(rule, now / 1000 - m.open)
                win_of_order = wins.index(w) if w in wins else -1
        if st.cooldown_until_ms > now:
            heapq.heappush(ticks, st.cooldown_until_ms + 1)

    while i < i_end or j < j_end:
        book_next = i < i_end and (j >= j_end or m.bt[i] <= m.tt[j])   # book first on equal times
        t = m.bt[i] if book_next else m.tt[j]
        while ticks and ticks[0] < t:
            tk = heapq.heappop(ticks)
            if t0 <= tk <= t1:
                evaluate(tk)
        if book_next:
            if cur is not None:
                # the live bot samples the book every 100 ms, so the price just before this change is in its
                # fast-pull memory; give the replay the same memory
                yes, no = cur
                fav = None if yes.mid is None else ("yes" if yes.mid >= 50 else "no")
                side = st.side or fav
                mid = (yes if side == "yes" else no).mid if side else None
                if mid is not None:
                    st.mids.append((t - 1, mid))
            cur = views(m, i)
            i += 1
            evaluate(t)
        else:
            ypx, cnt, taker = m.tp[j], m.tc[j], "yes" if m.tk[j] else "no"
            j += 1
            o = st.order
            if o is None or t < o.placed_ms:
                continue
            maker = "yes" if taker == "no" else "no"
            if maker != o.side:
                continue
            px = ypx if maker == "yes" else 100 - ypx
            if px < o.price - 1e-6:
                return (t, o.price, o.side, win_of_order)
            if abs(px - o.price) < 1e-6:
                queue_ahead -= cnt
                if queue_ahead < 0:
                    return (t, o.price, o.side, win_of_order)
    return None


def bid_path(m: Market, fill_ms: int, side: str) -> list[tuple[int, float]]:
    i = bisect_right(m.bt, fill_ms)
    return [(m.bt[k], side_bid(m, k, side)) for k in range(i, len(m.bt))]


def taker_fee_c(price_c: float) -> float:
    p = price_c / 100
    return math.ceil(round(7 * p * (1 - p), 6))


def exit_pnl(path, entry_c: float, won: bool, stop_c: float, stop_ms: int, start_bid: float, fill_ms: float,
             close_ms: float) -> float:
    """P&L of one contract from its fill to the close under one stop setting (mirrors the bot's stop)."""
    def sold(b):
        px = max(b - 0.5, math.floor(b - 5), 1.0)
        return px - taker_fee_c(px) - entry_c
    if stop_c > 0:
        low, prev = (fill_ms if start_bid < stop_c else None), start_bid
        for t, b in path:
            if low is not None and t - low >= stop_ms:
                return sold(prev)
            if b < stop_c:
                if low is None:
                    low = t
                if stop_ms == 0:
                    return sold(b)
            else:
                low = None
            prev = b
        if low is not None and close_ms - 500 - low >= stop_ms:
            return sold(prev)
    return (100.0 if won else 0.0) - entry_c


# ------------------------------------------------------------------ search (worker side)
_M: dict[str, Market] = {}


def _init(markets):
    global _M
    _M = markets
    try:
        os.nice(19)
    except (AttributeError, OSError):
        pass


def _run_one(item):
    p, job = item
    return p, _run_pull(job)


def _run_pull(args):
    """All entries for one pull setting, then every stop setting for every resulting fill."""
    rule, tickers, stop_grids = args
    out = []
    for t in tickers:
        m = _M[t]
        f = sim_entry(m, rule)
        if not f:
            continue
        fill_ms, price, side, w = f
        if w < 0:
            continue
        path = bid_path(m, fill_ms, side)
        k = bisect_right(m.bt, fill_ms) - 1
        start_bid = side_bid(m, k, side) if k >= 0 else 0.0
        won = m.result == side
        pnl = array("d", (exit_pnl(path, price, won, sc, sm, start_bid, fill_ms, m.close * 1000)
                          for sc, sm in stop_grids[w]))           # in the order of stop_grids[w]
        out.append({"close": m.close, "win": w, "pnl": pnl})
    return out


# ------------------------------------------------------------------ report
def summarize(vals):
    n = len(vals)
    if not n:
        return {"n": 0, "total": 0.0, "mean": 0.0, "se": 0.0}
    mean = sum(vals) / n
    se = statistics.stdev(vals) / math.sqrt(n) if n > 1 else 0.0
    return {"n": n, "total": sum(vals), "mean": mean, "se": se}


def fmt(s):
    if not s["n"]:
        return "no fills"
    return f"{s['n']:4d} fills  {s['total']:+8.1f}c total  {s['mean']:+6.2f}c/contract (+/-{s['se']:.2f})"


def pull_label(p):
    return "off" if p is None else f"drop {p[0]:g}c within {p[1]} ms, pause {p[2]} ms"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=ROOT / "data", help="data folder, rec folder, or unzipped download")
    ap.add_argument("--config", type=Path, default=ROOT / "config.json")
    ap.add_argument("--group", choices=["crypto", "commodity"], help="only this rule (default: every enabled rule)")
    ap.add_argument("--days", type=int, default=0, help="only the most recent N days (default: all)")
    ap.add_argument("--all-markets", action="store_true", help="include markets switched off in the dashboard")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--json", type=Path, help="also write the full results here")
    ap.add_argument("--quick", action="store_true", help="13 fast-pull settings instead of 61 (much faster)")
    a = ap.parse_args()

    try:
        os.nice(19)
    except (AttributeError, OSError):
        pass
    cfg = load_settings(a.config)
    days = find_day_dirs(a.data) if a.data.exists() else []
    if a.days:
        days = days[-a.days:]
    if not days:
        sys.exit(f"No recordings found under {a.data}")
    print(f"Recordings: {len(days)} day(s), {days[0].name} .. {days[-1].name}", flush=True)
    all_markets = load_markets(days)

    groups = [a.group] if a.group else [g for g in ("commodity", "crypto") if cfg["rules"][g]["enabled"]]
    if not groups:
        sys.exit("Both rules are switched off - pass --group crypto or --group commodity")
    report = {}
    for g in groups:
        rule0 = cfg["rules"][g]
        wins = rule0["windows"]
        want = [t for t, m in all_markets.items() if m.group == g
                and (a.all_markets or cfg["markets"].get(m.coin)) and not news_blocked(cfg, m)]
        t_load = time.time()
        first = min(w["start_s"] for w in wins) - 60               # a minute of book before bidding opens
        markets = load_rows(days, all_markets, {t: first for t in want})
        print(f"\n{g}: loaded {len(markets)} settled markets ({time.time() - t_load:.0f} s; results the bot didn't "
              f"record are read from the closing prices)", flush=True)
        tickers = [t for _, t in sorted((markets[t].close, t) for t in markets)]
        # skip markets whose favourite never reaches any window's band while the window is open
        def reachable(m):
            for w in wins:
                a = bisect_left(m.bt, int((m.open + w["start_s"]) * 1000))
                b = bisect_right(m.bt, int((m.open + w["end_s"]) * 1000))
                for k in range(a, b):
                    yb, ya = _nn(m.yb[k]), _nn(m.ya[k])
                    fav_bid = yb if (yb is not None and ya is not None and yb + ya >= 100) or ya is None else \
                        (None if ya is None else 100 - ya)
                    if fav_bid is not None and w["lo_c"] - 1 <= fav_bid < w["hi_c"]:
                        return True
            return False
        tickers = [t for t in tickers if reachable(markets[t])]
        print(f"\n=== {g.upper()} rule: {len(wins)} window(s), {len(tickers)} markets in range", flush=True)
        if not tickers:
            print("  nothing to replay")
            continue
        split = tickers[int(len(tickers) * 0.7)] if len(tickers) >= 10 else None
        cut = markets[split].close if split else float("inf")
        stop_grids = [[(sc, sm) for sc in stop_price_grid(w["lo_c"]) for sm in STOP_MS_GRID] for w in wins]
        cur_pull = (rule0["pull_drop_c"], rule0["pull_ms"], rule0["pull_cooldown_ms"]) if rule0.get("pull") else None
        grid = list(dict.fromkeys((PULL_GRID_QUICK if a.quick else PULL_GRID) + [cur_pull]))
        cur_stops = [(float(w["stop_c"]), int(w["stop_ms"])) for w in wins]
        for i, w in enumerate(wins):                       # make sure the current stop is in the grid
            if cur_stops[i] not in stop_grids[i]:
                stop_grids[i].append(cur_stops[i])

        jobs = []
        for p in grid:
            r = copy.deepcopy(rule0)
            r["windows"] = [dict(w, stop_c=0, contracts=1) for w in wins]
            r["pull"] = p is not None
            if p:
                r["pull_drop_c"], r["pull_ms"], r["pull_cooldown_ms"] = p
            jobs.append((r, tickers, stop_grids))
        t_run = time.time()
        print(f"  replaying {len(grid)} fast-pull settings x {len(tickers)} markets "
              f"on {a.workers} worker(s) at low priority...", flush=True)
        fills_by_pull = {}
        with Pool(a.workers, initializer=_init, initargs=(markets,)) as pool:
            for k, (p, res) in enumerate(pool.imap_unordered(_run_one, [(p, j) for p, j in zip(grid, jobs)]), 1):
                fills_by_pull[p] = res
                el = time.time() - t_run
                print(f"    {k}/{len(grid)} settings done, about {el / k * (len(grid) - k) / 60:.0f} min to go", flush=True)
        print(f"  done in {time.time() - t_run:.0f} s", flush=True)

        key_idx = [{k: n for n, k in enumerate(g_)} for g_ in stop_grids]

        def score(pull, stops, part):
            vals = []
            for f in fills_by_pull[pull]:
                if (part == "train") == (f["close"] < cut) or part == "all":
                    vals.append(f["pnl"][key_idx[f["win"]][stops[f["win"]]]])
            return summarize(vals)

        def best_stops(pull, part="train"):
            out = []
            for i in range(len(wins)):
                tot = {}
                for f in fills_by_pull[pull]:
                    if f["win"] == i and ((f["close"] < cut) == (part == "train")):
                        for k, v in zip(stop_grids[i], f["pnl"]):
                            tot[k] = tot.get(k, 0.0) + v
                # ties: keep the current setting, else the less trigger-happy one (lower price, longer delay)
                out.append(max(tot, key=lambda k: (round(tot[k], 6), k == cur_stops[i], -k[0], k[1]))
                           if tot else cur_stops[i])
            return out

        cands = []
        for p in grid:
            st = best_stops(p)
            cands.append((score(p, st, "train")["total"], p, st))
        cands.sort(key=lambda x: (-round(x[0], 6), x[1] != cur_pull, x[1] is not None))   # ties: current, then off
        _, best_p, best_s = cands[0]
        base_tr, base_te = score(cur_pull, cur_stops, "train"), score(cur_pull, cur_stops, "test")
        best_tr, best_te = score(best_p, best_s, "train"), score(best_p, best_s, "test")

        print("\n  Tuned on the earliest 70% of markets, judged on the latest 30%"
              + ("" if split else " (too few markets to split - everything is in-sample!)"))
        print(f"  CURRENT   pull {pull_label(cur_pull)}")
        for i, w in enumerate(wins):
            print(f"            window {i + 1} {config_label(w)}: stop {stop_label(cur_stops[i])}")
        print(f"     tuning set : {fmt(base_tr)}")
        print(f"     held out   : {fmt(base_te)}")
        print(f"  BEST      pull {pull_label(best_p)}")
        for i, w in enumerate(wins):
            print(f"            window {i + 1} {config_label(w)}: stop {stop_label(best_s[i])}")
        print(f"     tuning set : {fmt(best_tr)}")
        print(f"     held out   : {fmt(best_te)}")

        print("\n  Top fast-pull settings (each with its best stops):")
        for tr, p, st in cands[:6]:
            te = score(p, st, "test")
            print(f"    {pull_label(p):<38} tuning {tr:+8.1f}c   held out {te['total']:+8.1f}c over {te['n']} fills")

        print("\n  Stop per window (with the best pull), held-out results:")
        for i, w in enumerate(wins):
            rows, seen = [], set()
            for k in stop_grids[i]:
                if k[0] == 0:                                   # every delay is the same "hold"
                    if 0 in seen:
                        continue
                    seen.add(0)
                v = [f["pnl"][key_idx[i][k]] for f in fills_by_pull[best_p] if f["win"] == i and f["close"] >= cut]
                if v:
                    rows.append((sum(v), k, len(v)))
            rows.sort(key=lambda x: -x[0])
            print(f"    window {i + 1} {config_label(w)}: " +
                  ", ".join(f"{stop_label(k)} {t:+.1f}c" for t, k, _ in rows[:4]) + (" ..." if len(rows) > 4 else ""))

        n_te = best_te["n"]
        verdict = []
        if not split or n_te < 30:
            verdict.append("NOT ENOUGH DATA to trust a change: keep collecting (aim for 30+ held-out fills).")
        elif best_te["total"] <= base_te["total"] + 1e-9:
            verdict.append("KEEP YOUR CURRENT SETTINGS: the tuned ones didn't beat them on the held-out markets.")
        else:
            gain = best_te["total"] - base_te["total"]
            verdict.append(f"SUGGESTED: the tuned settings beat yours by {gain:+.1f}c on {n_te} held-out fills "
                           f"({gain / max(n_te, 1):+.2f}c per contract).")
            if best_te["se"] and abs(best_te["mean"] - base_te["mean"]) < 2 * best_te["se"]:
                verdict.append("  ...but the difference is within the noise (less than 2 standard errors). "
                               "Treat it as a lean, not proof.")
        print("\n  " + "\n  ".join(verdict))
        report[g] = {"current": {"pull": cur_pull, "stops": cur_stops, "tuning": base_tr, "held_out": base_te},
                     "best": {"pull": best_p, "stops": best_s, "tuning": best_tr, "held_out": best_te},
                     "verdict": verdict, "markets": len(tickers), "days": [d.name for d in days]}
    print("\nNotes: results are per 1 contract after fees, simulated like paper mode (queue position, real trades"
          "\nto fill, stop sells at the bid less 0.5c plus taker fee). Daily loss limits and market caps aren't"
          "\napplied. Enter any change on the Settings page.")
    if a.json:
        a.json.write_text(json.dumps(report, indent=2, default=str))
        print(f"Full results written to {a.json}")


def config_label(w):
    return f"{int(w['start_s']) // 60}:{int(w['start_s']) % 60:02d}-{int(w['end_s']) // 60}:{int(w['end_s']) % 60:02d}"


def stop_label(k):
    return "hold" if k[0] == 0 else f"{k[0]:g}c/{k[1]}ms"


if __name__ == "__main__":
    main()
