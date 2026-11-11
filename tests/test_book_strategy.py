from fastmaker.book import Book
from fastmaker.config import DEFAULT_RULE
from fastmaker.strategy import State, bid_price, decide


def make_book():
    b = Book("T")
    b.snapshot({"yes_dollars_fp": [["0.9700", "120"], ["0.9650", "50"]], "no_dollars_fp": [["0.0200", "80"]]}, 0)
    return b


def test_book_sides_and_deltas():
    b = make_book()
    v = b.view("yes")
    assert (v.bid, v.ask, v.bid_size) == (97.0, 98.0, 120)
    n = b.view("no")
    assert (n.bid, n.ask) == (2.0, 3.0)
    b.delta({"side": "yes", "price_dollars": "0.9700", "delta_fp": "-120"}, 1)
    assert b.view("yes").bid == 96.5
    b.delta({"side": "no", "price_dollars": "0.0150", "delta_fp": "10"}, 2)
    assert b.view("yes").ask == 98.0
    assert b.favourite() == "yes"


def test_own_order_excluded():
    b = make_book()
    b.delta({"side": "yes", "price_dollars": "0.9710", "delta_fp": "3"}, 1)     # our bid at 97.1 x3
    assert b.view("yes").bid == 97.1
    assert b.view("yes", 97.1, 3).bid == 97.0
    b.delta({"side": "yes", "price_dollars": "0.9710", "delta_fp": "5"}, 2)     # someone joins us
    assert b.view("yes", 97.1, 3).bid == 97.1


def test_bid_price():
    assert bid_price(97.0, 98.0, 99.5) == 97.1
    assert bid_price(97.0, 97.1, 99.5) == 97.0          # one-tick spread: join
    assert bid_price(99.4, 99.9, 99.5) == 99.4          # top of band: join


def rule(**k):
    """DEFAULT_RULE with overrides; window keys (start_s, lo_c, ...) go to its single window."""
    from fastmaker.config import WINDOW_KEYS
    r = dict(DEFAULT_RULE)
    r["windows"] = [dict(r["windows"][0], **{x: v for x, v in k.items() if x in WINDOW_KEYS})]
    r.update({x: v for x, v in k.items() if x not in WINDOW_KEYS})
    return r


def test_places_in_band_and_window_only():
    b = make_book()
    st = State("T")
    acts = decide(rule(), st, 600, 0, b.view("yes"), "yes", 1)
    assert [(a.kind, a.side, a.price, a.qty) for a in acts] == [("place", "yes", 97.1, 1)]
    st2 = State("T")
    assert decide(rule(), st2, 300, 0, b.view("yes"), "yes", 1) == []
    st3 = State("T")
    assert decide(rule(lo_c=97.5), st3, 600, 0, b.view("yes"), "yes", 1) == []


def test_never_chases_itself_but_follows_others():
    from fastmaker.strategy import Order
    b = make_book()
    st = State("T", side="yes", order=Order("yes", 97.1, 1, 0))
    b.delta({"side": "yes", "price_dollars": "0.9710", "delta_fp": "1"}, 1)     # our own order in the book
    assert decide(rule(), st, 600, 10, b.view("yes", 97.1, 1), "yes", 1) == []
    b.delta({"side": "yes", "price_dollars": "0.9720", "delta_fp": "4"}, 2)     # someone outbids
    acts = decide(rule(), st, 600, 20, b.view("yes", 97.1, 1), "yes", 1)
    assert [a.kind for a in acts] == ["cancel", "place"] and acts[1].price == 97.3


def test_stop_waits_for_stop_ms():
    b = Book("T")
    b.snapshot({"yes_dollars_fp": [["0.4000", "10"]], "no_dollars_fp": [["0.5500", "10"]]}, 0)
    st = State("T", side="yes", held=2, cost_c=194)
    r = rule(stop_ms=1000)
    assert decide(r, st, 700, 0, b.view("yes"), "yes", 2) == []
    assert decide(r, st, 700, 900, b.view("yes"), "yes", 2) == []
    acts = decide(r, st, 700, 1000, b.view("yes"), "yes", 2)
    assert acts[0].kind == "sell" and acts[0].qty == 2 and st.exiting


def test_pull_on_fast_drop():
    b = make_book()
    st = State("T")
    r = rule(pull_drop_c=1.5, pull_ms=1500)
    decide(r, st, 600, 0, b.view("yes"), "yes", 1)
    from fastmaker.strategy import Order
    st.order = Order("yes", 97.1, 1, 0)
    st.side = "yes"
    b.snapshot({"yes_dollars_fp": [["0.9500", "10"]], "no_dollars_fp": [["0.0400", "10"]]}, 500)   # mid 97.5 -> 95.5
    acts = decide(r, st, 600.5, 500, b.view("yes", 97.1, 1), "yes", 1)
    assert [a.kind for a in acts] == ["cancel"] and st.cooldown_until_ms == 500 + r["pull_cooldown_ms"]


def test_price_grid():
    from fastmaker.strategy import parse_ranges, step_at
    tapered = parse_ranges({"price_ranges": [{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
                                             {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
                                             {"start": "0.9000", "end": "1.0000", "step": "0.0010"}]})
    assert step_at(tapered, "yes", 97.0) == 0.1 and step_at(tapered, "no", 97.0) == 0.1   # NO 97 = YES 3
    assert step_at(tapered, "yes", 50.0) == 1.0
    assert parse_ranges({}) == [(0.0, 100.0, 1.0)] and parse_ranges(None) == [(0.0, 100.0, 1.0)]
    whole = parse_ranges({})
    assert step_at(whole, "no", 97.0) == 1.0
    assert bid_price(97.0, 98.0, 99.5, 1.0) == 97.0          # whole cents, 1c spread: join
    assert bid_price(96.0, 98.0, 99.5, 1.0) == 97.0
    assert bid_price(96.3, 98.0, 99.5, 1.0) == 97.0          # off-grid book level still yields a grid price
    acts = decide(rule(), State("T"), 600, 0, make_book().view("yes"), "yes", 1, whole)
    assert acts[0].price == 97.0


def test_multiple_windows():
    from fastmaker.config import window
    from fastmaker.strategy import Order
    r = rule(windows=[window(120, 240, 98.0, 99.5, 1), window(540, 720, 96.0, 99.5, 3)])
    b = make_book()                                   # YES 97/98
    st = State("T")
    assert decide(r, st, 60, 0, b.view("yes"), "yes") == [] and st.note == "next window at 2:00"
    assert decide(r, st, 180, 0, b.view("yes"), "yes") == []            # 97 is below window 1's 98c band
    acts = decide(r, st, 600, 0, b.view("yes"), "yes")                  # window 2: 96-99.5, 3 contracts
    assert acts[0].kind == "place" and acts[0].qty == 3
    st.order = Order("yes", 97.1, 3, 0)
    acts = decide(r, st, 300, 0, b.view("yes", 97.1, 3), "yes")         # between windows: cancel
    assert [a.kind for a in acts] == ["cancel"]
    st.order = None
    decide(r, st, 800, 0, b.view("yes"), "yes")
    assert st.note == "done for this market"


def test_old_config_becomes_one_window(tmp_path, monkeypatch):
    import json
    from fastmaker import config
    monkeypatch.setattr(config, "CONFIG", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({"rules": {"crypto": {
        "enabled": True, "start_s": 600, "end_s": 700, "lo_c": 98.0, "hi_c": 99.0, "contracts": 2, "stop_c": 40}}}))
    c = config.load()
    assert c["rules"]["crypto"]["windows"] == [{"start_s": 600, "end_s": 700, "lo_c": 98.0, "hi_c": 99.0, "contracts": 2}]
    assert "lo_c" not in c["rules"]["crypto"] and c["rules"]["crypto"]["stop_c"] == 40
    assert c["rules"]["commodity"]["windows"][0]["end_s"] == 780                 # untouched default


def test_stop_zero_holds():
    b = Book("T")
    b.snapshot({"yes_dollars_fp": [["0.0500", "10"]], "no_dollars_fp": [["0.9000", "10"]]}, 0)   # crashed to 5c
    st = State("T", side="yes", held=1, cost_c=97)
    r = rule(stop_c=0, stop_ms=0)
    for t in (0, 5000, 60000):
        assert decide(r, st, 700, t, b.view("yes"), "yes", 1) == []
    assert not st.exiting and "no stop" in st.note


def test_rebuild_result_from_kalshi_fills():
    from fastmaker.engine import rebuild_result
    f = lambda book, yes_px, n, fee=0: {"book_side": book, "yes_price_dollars": f"{yes_px / 100:.4f}",   # noqa: E731
                                        "count_fp": f"{n}.00", "fee_cost": f"{fee:.4f}"}
    # bought 2 NO at 96.3 (ask side of the YES book), won
    r = rebuild_result([f("ask", 3.7, 2)], "no", "no")
    assert r["bought"] == 2 and r["outcome"] == "won" and abs(r["pnl_c"] - 7.4) < 1e-6
    # bought 1 YES at 97, sold it at 40 (taker fee 2c), market then went NO
    r = rebuild_result([f("bid", 97, 1), f("ask", 40, 1, 0.02)], "yes", "no")
    assert (r["bought"], r["sold"], r["outcome"]) == (1, 1, "sold") and abs(r["pnl_c"] - (40 - 2 - 97)) < 1e-6
    assert rebuild_result([], "yes", "yes") is None
