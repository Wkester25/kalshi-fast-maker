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
    r = dict(DEFAULT_RULE)
    r.update(k)
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
