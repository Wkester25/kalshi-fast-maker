"""Whole program against the fake exchange: feed, paper fills, live order format, web API."""
import asyncio
import socket
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from fastmaker import config, store
from fastmaker.engine import Engine
from fastmaker.web import build
from tests.fake_kalshi import FakeKalshi, point_at, serve


TAPERED = [{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
           {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
           {"start": "0.9000", "end": "1.0000", "step": "0.0010"}]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def pem():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                           serialization.NoEncryption()).decode()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(config, "SECRETS", tmp_path / "secrets")
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(store, "DATA", tmp_path / "data")
    return tmp_path


async def wait_for(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


def test_paper_then_live(home):
    async def go():
        port = free_port()
        point_at(port)
        fake = FakeKalshi(secs_into_window=600, ranges=TAPERED)
        runner = await serve(fake, port)
        config.save_key("test-key-id", pem())
        cfg = config.load()
        cfg.update(env="fake", mode="paper", trading=True)
        cfg["rules"]["crypto"]["enabled"] = True
        eng = Engine(cfg)
        await eng.start()
        try:
            assert await wait_for(lambda: eng.feed.state == "connected"), eng.feed.state
            assert fake.ws_auth[0]["KALSHI-ACCESS-KEY"] == "test-key-id"
            eth = eng.slots["ETH"]
            # ETH's favourite is NO with a 97.0 bid: inside the crypto band, so we bid NO 97.1
            assert await wait_for(lambda: eth.st and eth.st.order is not None), eth.st and eth.st.note
            assert (eth.st.order.side, eth.st.order.price) == ("no", 97.1)
            assert eng.slots["BTC"].st.order is None          # BTC's favourite is at 96.2: below the band
            await asyncio.sleep(0.1)                           # past the simulated paper latency
            await fake.trade(eth.ticker, 3.0, 5, "yes")      # a YES taker buys at 3 = NO sells at 97: through us
            assert await wait_for(lambda: eth.st.held == 1)
            assert eth.st.order is None
            # subscriptions are tracked so finished windows can be unsubscribed
            assert eng.feed._sid_markets

            # ---- live: a real order goes out in V2 format
            await eng.set_mode("live")
            sol = eng.slots["SOL"]
            assert sol.st.order is None                         # SOL is switched off by default
            gold = eng.slots["GOLD"]
            assert await wait_for(lambda: any(o["ticker"] == gold.ticker for o in fake.orders), 3), \
                (gold.st.note, fake.orders)
            o = next(o for o in fake.orders if o["ticker"] == gold.ticker)
            assert o["post_only"] is True and o["client_order_id"].startswith("fmb-")
            # stop all cancels and halts
            await eng.halt("test")
            assert not eng.cfg["trading"] and eng.halted
            await asyncio.sleep(0.3)
            assert all(not (s.st and s.st.order) for s in eng.slots.values())
        finally:
            await eng.stop()
            await runner.cleanup()
    asyncio.run(go())


def test_web_flow(home):
    async def go():
        port = free_port()
        point_at(port)
        fake = FakeKalshi()
        runner = await serve(fake, port)
        cfg = config.load()
        cfg["env"] = "fake"
        eng = Engine(cfg)
        client = TestClient(TestServer(build(eng)))
        await client.start_server()
        try:
            r = await client.get("/", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/setup"
            assert (await client.get("/api/state")).status == 401
            assert (await client.post("/setup", json={"password": "short"})).status == 400
            assert (await client.post("/setup", json={"password": "correct horse"})).status == 200
            st = await (await client.get("/api/state")).json()
            assert st["mode"] == "paper" and st["has_key"] is False and len(st["markets"]) == 15

            # settings: validation and save
            r = await (await client.post("/api/settings", json={"rules": {"crypto": {"lo_c": 99.6, "hi_c": 99.5}}})).json()
            assert not r["ok"]
            r = await (await client.post("/api/settings", json={"rules": {"crypto": {"contracts": 3}},
                                                                "limits": {"daily_loss_usd": 15}})).json()
            assert r["ok"] and config.load()["rules"]["crypto"]["contracts"] == 3

            # live needs a key and the password
            assert (await client.post("/api/mode", json={"mode": "live", "password": "correct horse"})).status == 400
            r = await (await client.post("/api/keys", json={"key_id": "abc", "pem": "not a key"})).json()
            assert "error" in r
            r = await (await client.post("/api/keys", json={"key_id": "abc", "pem": pem()})).json()
            assert r["ok"] and r["cash_usd"] == 1250.0
            assert (config.SECRETS / "kalshi.pem").stat().st_mode & 0o077 == 0
            st = await (await client.get("/api/state")).json()
            assert "BEGIN" not in str(st)                     # the key never goes back to the browser
            assert (await client.post("/api/mode", json={"mode": "live", "password": "nope"})).status == 401
            assert (await client.post("/api/mode", json={"mode": "live", "password": "correct horse"})).status == 200
            assert (await client.post("/api/trading", json={"on": True})).status == 401
            assert (await client.post("/api/trading", json={"on": True, "password": "correct horse"})).status == 200

            # a fresh client (no cookie) is signed out; wrong passwords lock out after 5 tries
            c2 = TestClient(TestServer(build(eng)))
            await c2.start_server()
            for _ in range(5):
                assert (await c2.post("/login", json={"password": "x"})).status == 401
            assert (await c2.post("/login", json={"password": "correct horse"})).status == 429
            await c2.close()
        finally:
            await client.close()
            await eng.stop()
            await runner.cleanup()
    asyncio.run(go())


def test_window_close_settles(home, monkeypatch):
    from fastmaker import engine as engmod

    async def go():
        port = free_port()
        point_at(port)
        fake = FakeKalshi(secs_into_window=600, ranges=TAPERED)
        runner = await serve(fake, port)
        config.save_key("k", pem())
        cfg = config.load()
        cfg.update(env="fake", mode="paper", trading=True)
        cfg["rules"]["crypto"]["enabled"] = True
        eng = Engine(cfg)
        await eng.start()
        try:
            eth = eng.slots["ETH"]
            assert await wait_for(lambda: eth.st and eth.st.order is not None)
            await asyncio.sleep(0.1)
            await fake.trade(eth.ticker, 3.0, 5, "yes")
            assert await wait_for(lambda: eth.st.held == 1)
            old = eth.ticker
            eth.close_ts = time.time()                           # the window ends
            assert await wait_for(lambda: len(eng.pending) == 1, 3)
            p = eng.pending[0]
            assert p.slot is not eth and p.slot.st.held == 1 and p.slot.ticker == old
            fake.markets["KXETH15M"]["result"] = "no"
            monkeypatch.setattr(engmod, "SETTLE_AFTER_S", -10_000)
            await eng.settle_once()
            res = eng.store.recent_results()
            assert len(res) == 1 and res[0]["outcome"] == "won" and abs(res[0]["pnl_c"] - 2.9) < 1e-6
            assert not eng.pending
        finally:
            await eng.stop()
            await runner.cleanup()
    asyncio.run(go())


def test_rejected_price_falls_back_to_whole_cents(home):
    """A market that advertises 0.1c steps but rejects them: no phantom order, then whole cents."""
    async def go():
        port = free_port()
        point_at(port)
        deci = [{"start": "0.0000", "end": "1.0000", "step": "0.0010"}]
        fake = FakeKalshi(secs_into_window=600, ranges=deci, whole_cent_only=True)
        runner = await serve(fake, port)
        config.save_key("k", pem())
        cfg = config.load()
        cfg.update(env="fake", mode="live", trading=True)
        eng = Engine(cfg)
        await eng.start()
        try:
            gold = eng.slots["GOLD"]                      # NO favourite at 96.2 bid
            assert await wait_for(lambda: any(o["ticker"] == gold.ticker for o in fake.orders), 5), \
                (gold.st and gold.st.note)
            o = next(o for o in fake.orders if o["ticker"] == gold.ticker)
            assert abs(float(o["price"]) * 100 - round(float(o["price"]) * 100)) < 1e-6
            assert gold.st.order is not None and gold.st.order.order_id
            assert getattr(fake, "rejected", 0) >= 1
            await asyncio.sleep(0.5)
            assert fake.rejected <= 15                    # one rejection per market, not a storm
        finally:
            await eng.stop()
            await runner.cleanup()
    asyncio.run(go())


def test_post_only_cross_is_retried_and_post_only_kept(home):
    async def go():
        port = free_port()
        point_at(port)
        fake = FakeKalshi(secs_into_window=600, ranges=TAPERED)
        fake.cross_first = True
        runner = await serve(fake, port)
        config.save_key("k", pem())
        cfg = config.load()
        cfg.update(env="fake", mode="live", trading=True)
        eng = Engine(cfg)
        await eng.start()
        try:
            gold = eng.slots["GOLD"]
            assert await wait_for(lambda: any(o["ticker"] == gold.ticker for o in fake.orders), 5)
            assert fake.orders_rejected_cross >= 1
            assert all(o.get("post_only") is True for o in fake.orders)   # never resent as a taker order
            assert gold.place_errors == 0
            ev = eng.store.recent_events(200)
            assert any("would have crossed" in e["msg"] and "post only order would cross" in e["msg"] for e in ev)
            assert not any(e["level"] == "error" and e["coin"] == "GOLD" for e in ev)
        finally:
            await eng.stop()
            await runner.cleanup()
    asyncio.run(go())


def test_switch_saves_even_if_other_fields_are_bad(home):
    """Flipping a rule on must work on its own and the page must never be served stale."""
    async def go():
        cfg = config.load()
        cfg["env"] = "fake"
        point_at(free_port())
        cfg["rules"]["crypto"]["stop_c"] = 99          # an invalid value already saved (stop above the band)
        eng = Engine(cfg)
        client = TestClient(TestServer(build(eng)))
        await client.start_server()
        try:
            await client.post("/setup", json={"password": "correct horse"})
            r = await (await client.post("/api/settings", json={"rules": {"crypto": {"enabled": True}}})).json()
            assert r["ok"] and config.load()["rules"]["crypto"]["enabled"] is True
            st = await (await client.get("/api/state")).json()
            assert st["settings"]["rules"]["crypto"]["enabled"] is True and st["version"]
            page = await client.get("/")
            assert "no-store" in page.headers["Cache-Control"]
            assert "/static/app.js?v=" in await page.text()
        finally:
            await client.close()
            await eng.stop()
    asyncio.run(go())
