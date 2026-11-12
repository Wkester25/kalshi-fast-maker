"""The tuner on synthetic markets with a known answer."""
import csv
import gzip
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import tune  # noqa: E402

COINS = ["GOLD", "SILVER", "WTI", "BRENT", "NATGAS", "PLATINUM", "PALLADIUM"]


def make_data(root: Path, n_windows=10, seed=3, pull_case=False):
    """Commodity markets: the YES favourite sits at 97/98 from 9:00, a trade fills a 97.1 bid at 9:30.
    One in four then collapses (85 for 3 s, then 5) and settles NO; the rest dip to 93 and settle YES."""
    rng = random.Random(seed)
    day = root / "rec" / "2026-11-10"
    day.mkdir(parents=True)
    cols = {"market": ["recv_ms", "coin", "ticker", "open_ts", "close_ts", "strike", "result"],
            "book": ["recv_ms", "ticker", "yes_bid", "yes_bid_sz", "yes_ask", "yes_ask_sz"],
            "trade": ["recv_ms", "ts_ms", "ticker", "yes_price", "count", "taker_side"]}
    rows = {k: [] for k in cols}
    t0 = 1794300000
    k = 0
    for w in range(n_windows):
        for c in COINS:
            o = t0 + w * 900
            cl = o + 900
            tk = f"KX{c}15M-{w}"
            crash = (k % 4 == 0)
            k += 1
            ms = lambda s: int((o + s) * 1000)  # noqa: E731
            b = lambda s, yb, ya: rows["book"].append([ms(s), tk, yb, 50, ya, 50])  # noqa: E731
            b(500, 96.0, 97.0)
            b(540.5, 97.0, 98.0)
            if crash and pull_case:                                           # the market turns just before
                b(569.6, 96.2, 96.4)                                          # the fill (still inside the band):
                rows["trade"].append([ms(570), "", tk, 96.0, 200, "no"])      # only a fast pull dodges it
            else:
                rows["trade"].append([ms(570), "", tk, 97.0, 200, "no"])      # sells into our 97.1 bid
            if crash:
                b(600, 85.0, 86.0)
                b(603, 5.0, 6.0)
                res = "no"
            else:
                b(600, 93.0 + rng.random(), 95.0)
                b(610, 97.0, 98.0)
                res = "yes"
            rows["market"].append([ms(0), c, tk, o, cl, "", ""])
            rows["market"].append([ms(910), c, tk, o, cl, "", res])
    for name, rs in rows.items():
        with gzip.open(day / f"{name}-00.csv.gz", "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols[name])
            w.writerows(rs)
    cfg = {"news_skip": {"enabled": False},
           "rules": {"commodity": {"enabled": True, "pull": False,
                                   "windows": [{"start_s": 540, "end_s": 780, "lo_c": 96.0, "hi_c": 99.5,
                                                "contracts": 1, "stop_c": 0, "stop_ms": 1000}]},
                     "crypto": {"enabled": False}}}
    (root / "config.json").write_text(json.dumps(cfg))
    return root


def test_exit_pnl_matches_the_bot():
    path = [(1000, 85.0), (4000, 5.0)]
    assert tune.exit_pnl(path, 97.1, False, 0, 0, 97, 0, 900_000) == -97.1          # hold, lost
    v = tune.exit_pnl(path, 97.1, False, 90, 1000, 97, 0, 900_000)                    # 85 for 3 s -> sold ~84.5
    assert abs(v - (84.5 - tune.taker_fee_c(84.5) - 97.1)) < 1e-9
    v = tune.exit_pnl(path, 97.1, False, 90, 5000, 97, 0, 900_000)                    # too slow: sold at 5
    assert abs(v - (4.5 - tune.taker_fee_c(4.5) - 97.1)) < 1e-9


def test_tuner_finds_the_stop(tmp_path):
    root = make_data(tmp_path)
    out = tmp_path / "r.json"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "tune.py"), "--data", str(root / "rec"),
                        "--config", str(root / "config.json"), "--workers", "2", "--json", str(out)],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr + r.stdout
    rep = json.loads(out.read_text())["commodity"]
    best_stop = rep["best"]["stops"][0]
    assert 86 <= best_stop[0] < 96 and best_stop[1] <= 3000, (best_stop, r.stdout)    # sells the 3 s at ~85
    assert rep["best"]["held_out"]["total"] > rep["current"]["held_out"]["total"]     # beats holding
    assert rep["current"]["tuning"]["n"] + rep["current"]["held_out"]["n"] == 70      # every market filled once
    assert "SUGGESTED" in r.stdout or "NOT ENOUGH" in r.stdout


def test_tuner_finds_the_pull(tmp_path):
    root = make_data(tmp_path, n_windows=20, pull_case=True)
    out = tmp_path / "r.json"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "tune.py"), "--data", str(root / "rec"),
                        "--config", str(root / "config.json"), "--workers", "2", "--json", str(out)],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr + r.stdout
    rep = json.loads(out.read_text())["commodity"]
    pull = rep["best"]["pull"]
    assert pull is not None and pull[0] <= 1.0, (pull, r.stdout)                        # 1.2c drop in 0.4 s
    assert rep["best"]["held_out"]["total"] > -5                                         # crashes avoided
    assert "SUGGESTED" in r.stdout
