"""Results and events in SQLite (data/fastmaker.db), and a light market-data recorder (data/rec/)."""
from __future__ import annotations

import csv
import gzip
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DATA

ET = timezone(timedelta(hours=-5))      # display/day boundaries; DST handled loosely (day = US Eastern standard)


def et_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), ET).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path: Path | None = None):
        DATA.mkdir(exist_ok=True)
        self.db = sqlite3.connect(str(path or DATA / "fastmaker.db"), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS results(
                ts REAL, day TEXT, mode TEXT, coin TEXT, ticker TEXT, side TEXT,
                bought INTEGER, avg_px REAL, sold INTEGER, outcome TEXT, pnl_c REAL);
            CREATE TABLE IF NOT EXISTS events(ts REAL, level TEXT, coin TEXT, msg TEXT);
            CREATE INDEX IF NOT EXISTS r_day ON results(day, mode);
            CREATE INDEX IF NOT EXISTS e_ts ON events(ts);
        """)
        self.db.commit()

    def event(self, level: str, coin: str, msg: str) -> None:
        self.db.execute("INSERT INTO events VALUES(?,?,?,?)", (time.time(), level, coin, msg))
        self.db.commit()

    def result(self, mode, coin, ticker, side, bought, avg_px, sold, outcome, pnl_c) -> None:
        now = time.time()
        self.db.execute("INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (now, et_day(now), mode, coin, ticker, side, bought, avg_px, sold, outcome, pnl_c))
        self.db.commit()

    def recent_events(self, n: int = 60) -> list[dict]:
        rows = self.db.execute("SELECT ts, level, coin, msg FROM events ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        return [{"ts": r[0], "level": r[1], "coin": r[2], "msg": r[3]} for r in rows]

    def recent_results(self, n: int = 40) -> list[dict]:
        rows = self.db.execute("SELECT ts, mode, coin, side, bought, avg_px, sold, outcome, pnl_c FROM results "
                               "ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        k = ["ts", "mode", "coin", "side", "bought", "avg_px", "sold", "outcome", "pnl_c"]
        return [dict(zip(k, r)) for r in rows]

    def summary(self, mode: str, day: str | None = None) -> dict:
        q = "SELECT COUNT(*), COALESCE(SUM(pnl_c),0), SUM(outcome='lost'), SUM(outcome='sold'), " \
            "COALESCE(SUM(bought),0) FROM results WHERE mode=?"
        args: list = [mode]
        if day:
            q += " AND day=?"
            args.append(day)
        n, pnl, lost, sold, contracts = self.db.execute(q, args).fetchone()
        return {"trades": n or 0, "pnl_c": pnl or 0.0, "lost": lost or 0, "stopped": sold or 0,
                "contracts": contracts or 0, "per_contract_c": (pnl / contracts) if contracts else 0.0}

    def by_coin(self, mode: str, day: str) -> dict[str, float]:
        rows = self.db.execute("SELECT coin, SUM(pnl_c) FROM results WHERE mode=? AND day=? GROUP BY coin",
                               (mode, day)).fetchall()
        return {c: p for c, p in rows}

    def daily(self, mode: str, days: int = 14) -> list[dict]:
        rows = self.db.execute("SELECT day, COUNT(*), SUM(pnl_c), SUM(bought) FROM results WHERE mode=? "
                               "GROUP BY day ORDER BY day DESC LIMIT ?", (mode, days)).fetchall()
        return [{"day": d, "trades": n, "pnl_c": p, "contracts": c} for d, n, p, c in rows]


class Recorder:
    """Top-of-book changes, trades and index values to gzipped CSV, one set of files per UTC day.
    Every row carries recv_ms (when this server received it) so later replays can't look ahead."""

    FILES = {"book": ["recv_ms", "ticker", "yes_bid", "yes_bid_sz", "yes_ask", "yes_ask_sz"],
             "trade": ["recv_ms", "ts_ms", "ticker", "yes_price", "count", "taker_side"],
             "index": ["recv_ms", "index_id", "value", "avg_60s"],
             "market": ["recv_ms", "coin", "ticker", "open_ts", "close_ts", "strike", "result"]}

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.day = ""
        self.files: dict[str, tuple] = {}
        self.last_flush = 0.0

    def _open(self):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day == self.day:
            return
        self.close()
        self.day = day
        d = DATA / "rec" / day
        d.mkdir(parents=True, exist_ok=True)
        for name, cols in self.FILES.items():
            p = d / f"{name}.csv.gz"
            new = not p.exists()
            fh = gzip.open(p, "at", newline="")
            w = csv.writer(fh)
            if new:
                w.writerow(cols)
            self.files[name] = (fh, w)

    def write(self, name: str, row: list) -> None:
        if not self.enabled:
            return
        self._open()
        self.files[name][1].writerow(row)
        now = time.time()
        if now - self.last_flush > 5:
            self.last_flush = now
            for fh, _ in self.files.values():
                fh.flush()

    def close(self):
        for fh, _ in self.files.values():
            try:
                fh.close()
            except OSError:
                pass
        self.files = {}


def disk_free_gb() -> float:
    st = os.statvfs(str(DATA)) if hasattr(os, "statvfs") else None
    return st.f_bavail * st.f_frsize / 1e9 if st else 0.0
