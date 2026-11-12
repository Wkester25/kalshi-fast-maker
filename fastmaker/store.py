"""Results and events in SQLite (data/fastmaker.db), and a light market-data recorder (data/rec/)."""
from __future__ import annotations

import csv
import gzip
import logging
import os
import queue
import sqlite3
import threading
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
        self.path = str(path or DATA / "fastmaker.db")
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        # write-ahead log: each event/result write no longer waits on a disk sync inside the trading loop
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS results(
                ts REAL, day TEXT, mode TEXT, coin TEXT, ticker TEXT, side TEXT,
                bought INTEGER, avg_px REAL, sold INTEGER, outcome TEXT, pnl_c REAL);
            CREATE TABLE IF NOT EXISTS events(ts REAL, level TEXT, coin TEXT, msg TEXT);
            CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY, cid TEXT, ticker TEXT, kind TEXT, ts REAL);
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
        rows = self.db.execute("SELECT rowid, ts, mode, coin, ticker, side, bought, avg_px, sold, outcome, pnl_c "
                               "FROM results ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        k = ["id", "ts", "mode", "coin", "ticker", "side", "bought", "avg_px", "sold", "outcome", "pnl_c"]
        return [dict(zip(k, r)) for r in rows]

    def live_results_since(self, ts: float) -> list[dict]:
        rows = self.db.execute("SELECT rowid, ticker, side, bought, avg_px, sold, outcome, pnl_c, coin FROM results "
                               "WHERE mode='live' AND ts>=? ORDER BY ts", (ts,)).fetchall()
        k = ["id", "ticker", "side", "bought", "avg_px", "sold", "outcome", "pnl_c", "coin"]
        return [dict(zip(k, r)) for r in rows]

    def update_result(self, rid: int, **f) -> None:
        cols = ", ".join(f"{k}=?" for k in f)
        self.db.execute(f"UPDATE results SET {cols} WHERE rowid=?", (*f.values(), rid))
        self.db.commit()

    def delete_result(self, rid: int) -> bool:
        n = self.db.execute("DELETE FROM results WHERE rowid=?", (rid,)).rowcount
        self.db.commit()
        return n > 0

    # our own orders, so fills can be recognised after a restart (Kalshi's fill records carry no client id)
    def add_order(self, order_id: str, cid: str, ticker: str, kind: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO orders VALUES(?,?,?,?,?)", (order_id, cid, ticker, kind, time.time()))
        self.db.commit()

    def order_kinds(self, days: float = 7) -> dict[str, str]:
        rows = self.db.execute("SELECT order_id, kind FROM orders WHERE ts>=?", (time.time() - days * 86400,)).fetchall()
        return dict(rows)

    def orders_for(self, ticker: str) -> dict[str, str]:
        return dict(self.db.execute("SELECT order_id, kind FROM orders WHERE ticker=?", (ticker,)).fetchall())

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

    def pnl_since(self, mode: str, ts: float) -> float:
        return self.db.execute("SELECT COALESCE(SUM(pnl_c),0) FROM results WHERE mode=? AND ts>=?",
                               (mode, ts)).fetchone()[0] or 0.0

    def by_coin(self, mode: str, day: str) -> dict[str, float]:
        rows = self.db.execute("SELECT coin, SUM(pnl_c) FROM results WHERE mode=? AND day=? GROUP BY coin",
                               (mode, day)).fetchall()
        return {c: p for c, p in rows}

    def daily(self, mode: str, days: int = 14) -> list[dict]:
        rows = self.db.execute("SELECT day, COUNT(*), SUM(pnl_c), SUM(bought) FROM results WHERE mode=? "
                               "GROUP BY day ORDER BY day DESC LIMIT ?", (mode, days)).fetchall()
        return [{"day": d, "trades": n, "pnl_c": p, "contracts": c} for d, n, p, c in rows]


class Recorder:
    """Market data and order timing to gzipped CSV, one file per stream per UTC hour (data/rec/<day>/<name>-HH.csv.gz),
    so every finished hour is a complete file. Every row carries recv_ms (when this server got it) so replays
    can't look ahead.

    Writing and compression happen on a background thread: the trading loop only puts the row on a queue.
    If the writer ever falls behind, rows are dropped and counted - the bot is never slowed down."""

    FILES = {"book": ["recv_ms", "ticker", "yes_bid", "yes_bid_sz", "yes_ask", "yes_ask_sz"],
             "trade": ["recv_ms", "ts_ms", "ticker", "yes_price", "count", "taker_side"],
             "index": ["recv_ms", "index_id", "value", "avg_60s"],
             "market": ["recv_ms", "coin", "ticker", "open_ts", "close_ts", "strike", "result"],
             # full depth: 'snap' rows list every level of a fresh book (qty = size); 'delta' rows are changes (qty = +/-)
             "depth": ["recv_ms", "ts_ms", "ticker", "kind", "side", "price_c", "qty"],
             # our own orders, millisecond timed, with the market at that instant
             "orders": ["ms", "event", "mode", "coin", "ticker", "side", "price_c", "qty", "client_id", "order_id",
                        "latency_ms", "mkt_bid", "mkt_ask", "note"]}
    SWITCH = {"book": "record", "trade": "record", "index": "record", "market": "record",
              "depth": "record_depth", "orders": "record_orders"}

    def __init__(self, cfg: dict | bool = True):
        self.cfg = cfg if isinstance(cfg, dict) else {"record": bool(cfg), "record_depth": bool(cfg),
                                                       "record_orders": bool(cfg)}
        self.q: queue.Queue = queue.Queue(maxsize=500_000)
        self.dropped = 0
        self.written = 0
        self.files: dict[str, tuple] = {}
        self.day = ""
        self._slot = ""
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("record", True))

    def on(self, name: str) -> bool:
        return bool(self.cfg.get(self.SWITCH[name], True))

    def write(self, name: str, row: list) -> None:
        """Called from the trading loop: never blocks."""
        if not self.on(name):
            return
        if self._thread is None:
            with self._lock:
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
                    self._thread.start()
        try:
            self.q.put_nowait((name, row))
        except queue.Full:
            self.dropped += 1

    # ---- writer thread
    def _run(self):
        last_flush = time.time()
        while True:
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                item = None
            if item == ("__stop__", None):
                break
            now = time.time()
            if item is not None:
                try:
                    self._open()
                    self.files[item[0]][1].writerow(item[1])
                    self.written += 1
                except Exception as e:                    # noqa: BLE001  disk full etc.: count it, keep running
                    self.dropped += 1
                    if self.dropped % 10000 == 1:
                        logging.getLogger("recorder").warning("recording failed: %s", e)
            if now - last_flush > 5:
                last_flush = now
                for fh, _ in list(self.files.values()):
                    try:
                        fh.flush()
                    except OSError:
                        pass
        self._close_files()

    def _open(self):
        t = datetime.now(timezone.utc)
        slot = t.strftime("%Y-%m-%d %H")
        if slot == self._slot:
            return
        self._close_files()
        self._slot, self.day = slot, t.strftime("%Y-%m-%d")
        d = DATA / "rec" / self.day
        d.mkdir(parents=True, exist_ok=True)
        for name, cols in self.FILES.items():
            p = d / f"{name}-{t:%H}.csv.gz"
            new = not p.exists()
            fh = gzip.open(p, "at", newline="", compresslevel=6)
            w = csv.writer(fh)
            if new:
                w.writerow(cols)
            self.files[name] = (fh, w)

    def _close_files(self):
        for fh, _ in self.files.values():
            try:
                fh.close()
            except OSError:
                pass
        self.files = {}
        self._slot = ""                                 # reopen on the next row

    def flush_now(self, timeout: float = 5.0):
        """Wait until queued rows are written (tests, shutdown)."""
        t0 = time.time()
        while not self.q.empty() and time.time() - t0 < timeout:
            time.sleep(0.02)
        time.sleep(0.05)

    def close(self):
        if self._thread is not None:
            try:
                self.q.put(("__stop__", None), timeout=2)
            except queue.Full:
                pass
            self._thread.join(timeout=10)
            self._thread = None
        else:
            self._close_files()

    def stats(self) -> dict:
        return {"queued": self.q.qsize(), "written": self.written, "dropped": self.dropped}


def disk_free_gb() -> float:
    st = os.statvfs(str(DATA)) if hasattr(os, "statvfs") else None
    return st.f_bavail * st.f_frsize / 1e9 if st else 0.0


# ---------------------------------------------------------------- exports (run on a worker thread)
EXPORT_TABLES = {"results": "SELECT datetime(ts,'unixepoch') AS utc, ts, day, mode, coin, ticker, side, bought, avg_px, "
                            "sold, outcome, pnl_c FROM results ORDER BY ts",
                 "events": "SELECT datetime(ts,'unixepoch') AS utc, ts, level, coin, msg FROM events ORDER BY ts"}


def export_table(db_path: str, name: str, out: Path) -> Path:
    """Write one table to CSV with its own read connection (WAL lets it read while the bot writes)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.execute(EXPORT_TABLES[name])
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([c[0] for c in cur.description])
            for row in cur:
                w.writerow(row)
    finally:
        con.close()
    return out


def list_days() -> list[dict]:
    root = DATA / "rec"
    out = []
    if root.exists():
        for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
            files = [f for f in d.iterdir() if f.is_file()]
            out.append({"day": d.name, "files": len(files), "bytes": sum(f.stat().st_size for f in files)})
    return out


def zip_day(day: str, out: Path) -> Path:
    """The day's recordings in one zip. The files are already gzipped, so they're stored, not recompressed."""
    import zipfile
    d = DATA / "rec" / day
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as z:
        for f in sorted(d.iterdir()):
            if f.is_file():
                z.write(f, arcname=f"{day}/{f.name}")
    return out
