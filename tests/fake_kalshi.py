"""A small fake Kalshi exchange (REST + WebSocket) for tests and demos. Not a market simulator:
books wander a little, trades print now and then, orders are recorded."""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timezone

from aiohttp import web

from fastmaker import config


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeKalshi:
    def __init__(self, secs_into_window: float = 600, seed: int = 7, ranges=None, whole_cent_only=False):
        self.whole_cent_only = whole_cent_only
        self.rng = random.Random(seed)
        self.open_ts = time.time() - secs_into_window
        self.close_ts = self.open_ts + 900
        self.orders: list[dict] = []
        self.rest_fills: list[dict] = []
        self.foreign_orders: list[dict] = []             # another bot's orders on the same account
        self.cancels: list[str] = []
        self.ws_auth: list[dict] = []
        self.books: dict[str, dict] = {}
        self.clients: list[web.WebSocketResponse] = []
        self.subs: dict[web.WebSocketResponse, set] = {}
        self.sid = 0
        self.markets = {}
        for i, (coin, (series, group)) in enumerate(config.MARKETS.items()):
            t = f"{series}-{int(self.close_ts)}"
            fav_yes = i % 3 != 1
            lvl = [96.2, 97.0, 97.4, 98.1, 98.3, 95.5, 98.6][i % 7]
            self.markets[series] = {"ticker": t, "open_time": iso(self.open_ts), "close_time": iso(self.close_ts),
                                    "floor_strike": 100.0 + i, "exchange_index": 0, "result": "", "status": "open"}
            if ranges:
                self.markets[series]["price_ranges"] = ranges
            yes_bid = lvl if fav_yes else round(100 - lvl - 1, 1)
            self.books[t] = {"yes": {yes_bid: 150.0, round(yes_bid - 0.5, 1): 60.0},
                             "no": {round(100 - yes_bid - 1, 1): 90.0, round(100 - yes_bid - 1.5, 1): 40.0}}

    # ------------------------------------------------------------- REST
    def app(self) -> web.Application:
        a = web.Application()
        a.router.add_get("/trade-api/v2/markets", self.list_markets)
        a.router.add_get("/trade-api/v2/markets/{t}", self.get_market)
        a.router.add_get("/trade-api/v2/portfolio/balance", self.balance)
        a.router.add_get("/trade-api/v2/portfolio/fills", self.empty_fills)
        a.router.add_get("/trade-api/v2/portfolio/orders", self.resting_orders)
        a.router.add_post("/trade-api/v2/portfolio/events/orders", self.place)
        a.router.add_delete("/trade-api/v2/portfolio/events/orders/{oid}", self.cancel)
        a.router.add_delete("/trade-api/v2/portfolio/events/orders", self.cancel_all)
        a.router.add_get("/trade-api/ws/v2", self.ws)
        return a

    async def resting_orders(self, r):
        live = [dict(o, client_order_id=o.get("client_order_id", "")) for o in self.orders + self.foreign_orders
                if o["order_id"] not in self.cancels]
        return web.json_response({"orders": live, "cursor": ""})

    async def fill(self, ticker, order_id, cid, side, yes_price, count):
        msg = {"type": "fill", "sid": 9, "msg": {
            "trade_id": f"t{order_id}-{time.time()}", "order_id": order_id, "client_order_id": cid,
            "market_ticker": ticker, "side": side, "action": "buy", "yes_price_dollars": f"{yes_price / 100:.4f}",
            "count_fp": f"{count:.2f}", "fee_cost": "0.0000"}, "sending_ts_ms": int(time.time() * 1000)}
        for ws in list(self.clients):
            await ws.send_json(msg)

    async def empty_fills(self, r):
        """REST fills, in Kalshi's REST shape (no client order id)."""
        t, since = r.query.get("ticker"), float(r.query.get("min_ts") or 0)
        out = [f for f in self.rest_fills if (not t or f["ticker"] == t) and f["ts"] >= since]
        return web.json_response({"fills": out, "cursor": ""})

    def add_rest_fill(self, ticker, order_id, book_side, yes_price, count, fee=0.0):
        n = len(self.rest_fills) + 1
        self.rest_fills.append({"fill_id": f"rf{n}", "trade_id": f"rf{n}", "order_id": order_id, "ticker": ticker,
                                "market_ticker": ticker, "book_side": book_side,
                                "outcome_side": "yes" if book_side == "bid" else "no", "count_fp": f"{count:.2f}",
                                "yes_price_dollars": f"{yes_price / 100:.4f}",
                                "no_price_dollars": f"{(100 - yes_price) / 100:.4f}", "fee_cost": f"{fee:.4f}",
                                "is_taker": False, "ts": int(time.time())})

    async def cancel_all(self, r):
        self.cancels.append("*")
        return web.json_response({})

    async def list_markets(self, r):
        m = self.markets.get(r.query.get("series_ticker"))
        return web.json_response({"markets": [m] if m else []})

    async def get_market(self, r):
        for m in self.markets.values():
            if m["ticker"] == r.match_info["t"]:
                return web.json_response({"market": m})
        return web.json_response({"error": {"message": "not found"}}, status=404)

    async def balance(self, r):
        self.balance_calls = getattr(self, "balance_calls", 0) + 1
        if "KALSHI-ACCESS-SIGNATURE" not in r.headers:
            return web.json_response({"error": {"message": "unauthorized"}}, status=401)
        return web.json_response({"balance": 125000, "balance_dollars": "1250.00"})

    async def place(self, r):
        body = await r.json()
        if getattr(self, "cross_first", False) and body["ticker"] not in getattr(self, "_crossed", set()):
            self._crossed = getattr(self, "_crossed", set()) | {body["ticker"]}
            self.orders_rejected_cross = getattr(self, "orders_rejected_cross", 0) + 1
            return web.json_response({"error": {"code": "invalid_order", "message": "invalid order",
                                                "details": "post only order would cross the book"}}, status=400)
        if self.whole_cent_only and abs(float(body["price"]) * 100 - round(float(body["price"]) * 100)) > 1e-6:
            self.rejected = getattr(self, "rejected", 0) + 1
            return web.json_response({"error": {"code": "invalid_price", "message": "invalid price"}}, status=400)
        oid = f"o{len(self.orders) + 1}"
        self.orders.append(dict(body, order_id=oid))
        return web.json_response({"order": {"order_id": oid, "fill_count_fp": "0.00"}})

    async def cancel(self, r):
        self.cancels.append(r.match_info["oid"])
        return web.json_response({})

    # ------------------------------------------------------------- WebSocket
    async def ws(self, r):
        self.ws_auth.append({k: r.headers.get(k) for k in ("KALSHI-ACCESS-KEY", "KALSHI-ACCESS-SIGNATURE")})
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(r)
        self.clients.append(ws)
        self.subs[ws] = set()
        async for m in ws:
            if m.type != web.WSMsgType.TEXT:
                continue
            d = json.loads(m.data)
            if d.get("cmd") == "subscribe":
                p = d["params"]
                for ch in p["channels"]:
                    self.sid += 1
                    await ws.send_json({"id": d["id"], "type": "subscribed", "msg": {"channel": ch, "sid": self.sid}})
                for t in p.get("market_tickers") or []:
                    self.subs[ws].add(t)
                    await ws.send_json(self.snapshot(t))
        self.clients.remove(ws)
        return ws

    def snapshot(self, t):
        b = self.books[t]
        f = lambda d: [[f"{p / 100:.4f}", f"{q:.2f}"] for p, q in sorted(d.items())]   # noqa: E731
        return {"type": "orderbook_snapshot", "sid": 1, "seq": 1, "sending_ts_ms": int(time.time() * 1000) - 2,
                "msg": {"market_ticker": t, "yes_dollars_fp": f(b["yes"]), "no_dollars_fp": f(b["no"])}}

    async def send(self, t, msg):
        msg["sending_ts_ms"] = int(time.time() * 1000) - 2
        for ws in list(self.clients):
            if t in self.subs.get(ws, ()):
                try:
                    await ws.send_json(msg)
                except ConnectionError:
                    pass

    async def delta(self, t, side, price, dq):
        b = self.books[t][side]
        b[price] = max(b.get(price, 0) + dq, 0)
        if b[price] <= 0:
            b.pop(price)
        await self.send(t, {"type": "orderbook_delta", "sid": 1, "msg": {
            "market_ticker": t, "price_dollars": f"{price / 100:.4f}", "delta_fp": f"{dq:.2f}", "side": side,
            "ts_ms": int(time.time() * 1000)}})

    async def trade(self, t, yes_price, count, taker_side):
        await self.send(t, {"type": "trade", "sid": 2, "msg": {
            "market_ticker": t, "yes_price_dollars": f"{yes_price / 100:.4f}",
            "no_price_dollars": f"{(100 - yes_price) / 100:.4f}", "count_fp": f"{count:.2f}",
            "taker_side": taker_side, "ts_ms": int(time.time() * 1000)}})

    async def wander(self, stop: asyncio.Event):
        """Light random activity for demos."""
        while not stop.is_set():
            t = self.rng.choice(list(self.books))
            side = self.rng.choice(["yes", "no"])
            px = self.rng.choice(list(self.books[t][side]))
            await self.delta(t, side, px, self.rng.choice([-20, -10, 10, 25]))
            if self.rng.random() < .3:
                yb = max(self.books[t]["yes"] or {50: 0})
                await self.trade(t, yb, self.rng.choice([1, 5, 40]), "no")
            await asyncio.sleep(.05)


async def serve(fake: FakeKalshi, port: int) -> web.AppRunner:
    runner = web.AppRunner(fake.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


def point_at(port: int) -> None:
    config.ENDPOINTS["fake"] = {"rest": [f"http://127.0.0.1:{port}/trade-api/v2"],
                                "ws": [f"ws://127.0.0.1:{port}/trade-api/ws/v2"]}
