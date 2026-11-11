"""Kalshi access: request signing, REST (markets, orders, balance) and the WebSocket feed.

Order formats are carried over from the Late Maker bot, which traded real money with them.
WebSocket formats follow Kalshi's docs (orderbook_snapshot/delta with *_dollars_fp levels,
trade, fill, cfbenchmarks_value).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import datetime

import aiohttp
from yarl import URL

from . import config

log = logging.getLogger("kalshi")


class KalshiError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def iso_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def dollars_c(x) -> float:
    """'0.9730' (dollars) -> 97.3 (cents)."""
    return round(float(x) * 100, 2)


# ------------------------------------------------------------------ signing
class Signer:
    def __init__(self, pem: str):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        text = pem.strip()
        if "PRIVATE KEY" not in text:
            raise ValueError("that isn't a private key (expected a file starting with -----BEGIN ... PRIVATE KEY-----)")
        if "ENCRYPTED" in text:
            raise ValueError("the key is password-protected; export it without a password")
        key = serialization.load_pem_private_key(text.encode(), password=None)
        if isinstance(key, Ed25519PrivateKey):
            self._sign = key.sign
        else:
            pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)
            self._sign = lambda m: key.sign(m, pss, hashes.SHA256())

    def headers(self, key_id: str, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        sig = self._sign((ts + method.upper() + path.split("?")[0]).encode())
        return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


# ------------------------------------------------------------------- REST
class Rest:
    def __init__(self, env: str = "real"):
        self.env = env
        self.bases = config.ENDPOINTS[env]["rest"]
        self.base = self.bases[0]
        self.key_id, self.signer, self.key_error = "", None, "no API key loaded"
        self.session: aiohttp.ClientSession | None = None
        self.rtt_ms: float | None = None                   # last measured REST round trip
        self._shards: dict[str, int] = {}
        self.post_only = True
        self.load_key()

    def load_key(self) -> None:
        k = config.load_key()
        if not k:
            self.key_id, self.signer, self.key_error = "", None, "no API key loaded"
            return
        try:
            self.signer = Signer(k[1])
            self.key_id, self.key_error = k[0], None
        except Exception as e:                              # noqa: BLE001
            self.key_id, self.signer, self.key_error = "", None, f"API key problem: {e}"

    @property
    def has_key(self) -> bool:
        return self.signer is not None

    async def open(self):
        if self.session is None or self.session.closed:
            conn = aiohttp.TCPConnector(limit=32, keepalive_timeout=60, ttl_dns_cache=300)
            self.session = aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=8))

    async def close(self):
        if self.session:
            await self.session.close()

    async def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                      auth: bool = True, timeout: float = 8.0) -> dict:
        await self.open()
        if auth and not self.signer:
            raise KalshiError(self.key_error or "no API key")
        last = None
        for b in [self.base] + [x for x in self.bases if x != self.base]:
            hdr = {"Accept": "application/json"}
            if auth:
                hdr.update(self.signer.headers(self.key_id, method, URL(b).path + path))
            for attempt in range(3):
                t0 = time.perf_counter()
                try:
                    async with self.session.request(method, b + path, params=params, json=body, headers=hdr,
                                                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                        raw = await r.read()
                        self.rtt_ms = (time.perf_counter() - t0) * 1000
                        self.base = b
                        if r.status == 429 and attempt < 2:     # rate limited: not processed, safe to retry
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue
                        if r.status < 400:
                            return json.loads(raw) if raw else {}
                        try:
                            d = json.loads(raw or b"{}")
                            err = d.get("error") or d
                            if isinstance(err, dict):   # keep Kalshi's code and details: they say *why*
                                parts = [str(err.get(k)) for k in ("code", "message", "details") if err.get(k)]
                                msg = " | ".join(dict.fromkeys(parts)) or str(err)[:200]
                            else:
                                msg = str(err)[:200]
                        except ValueError:
                            msg = raw[:160].decode(errors="replace")
                        raise KalshiError(f"HTTP {r.status}: {msg}", r.status)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last = e
                    break
        raise KalshiError(f"cannot reach Kalshi ({last})")

    # ---- market data
    async def open_market(self, series: str) -> dict | None:
        now = time.time()
        d = await self.request("GET", "/markets", {"series_ticker": series, "status": "open", "limit": 10},
                               auth=False)
        best = None
        for m in d.get("markets") or []:
            try:
                o, c = iso_ts(m["open_time"]), iso_ts(m["close_time"])
            except (KeyError, ValueError):
                continue
            if o <= now + 1 < c and (best is None or c < iso_ts(best["close_time"])):
                best = m
        return best

    async def market(self, ticker: str) -> dict | None:
        try:
            return (await self.request("GET", f"/markets/{ticker}", auth=False)).get("market")
        except KalshiError as e:
            if e.status == 404:
                return None
            raise

    # ---- account
    async def balance(self) -> dict:
        return await self.request("GET", "/portfolio/balance")

    async def shard_cash(self) -> dict[int, float]:
        b = await self.balance()
        out = {}
        for x in b.get("balance_breakdown") or []:
            try:
                out[int(x["exchange_index"])] = float(x["balance"]) * 100
            except (KeyError, TypeError, ValueError):
                pass
        if not out:
            v = b.get("balance_dollars")
            out[0] = float(v) * 100 if v not in (None, "") else float(b.get("balance", 0))
        return out

    async def market_shard(self, ticker: str) -> int:
        if ticker not in self._shards:
            m = await self.market(ticker) or {}
            try:
                self._shards[ticker] = int(m.get("exchange_index"))
            except (TypeError, ValueError):
                self._shards[ticker] = 0
        return self._shards[ticker]

    async def ensure_shard_cash(self, ticker: str, need_c: float) -> tuple[bool, str]:
        """Kalshi keeps cash per exchange shard and orders aren't funded from the others."""
        dst = await self.market_shard(ticker)
        cash = await self.shard_cash()
        have = cash.get(dst, 0.0)
        if have >= need_c:
            return True, ""
        want, moved = need_c * 4 - have, 0
        for src, amt in sorted(cash.items(), key=lambda kv: -kv[1]):
            if src == dst or amt <= 0 or moved >= want:
                continue
            step = int(min(amt, want - moved))
            await self.request("POST", "/portfolio/intra_exchange_instance_transfer", body={
                "source": "event_contract", "destination": "event_contract",
                "source_exchange_shard": src, "destination_exchange_shard": dst,
                "amount": step * 100, "client_transfer_id": str(uuid.uuid4())})
            moved += step
        if moved <= 0:
            return False, f"not enough cash (shard {dst} has ${have / 100:.2f})"
        return True, f"moved ${moved / 100:.2f} to shard {dst}"

    # ---- orders (V2 books are quoted in YES: buying NO at p = asking YES at 100 - p)
    async def place_bid(self, ticker: str, side: str, count: int, price_c: float, expires_ts: float,
                        client_id: str) -> dict:
        yes_px = round(price_c if side == "yes" else 100 - price_c, 4)
        body = {"ticker": ticker, "side": "bid" if side == "yes" else "ask", "count": f"{int(count)}.00",
                "price": f"{yes_px / 100:.4f}", "time_in_force": "good_till_canceled",
                "self_trade_prevention_type": "taker_at_cross", "expiration_time": int(expires_ts),
                "client_order_id": client_id}
        if self.post_only:
            body["post_only"] = True
        try:
            r = await self.request("POST", "/portfolio/events/orders", body=body, timeout=4)
        except KalshiError as e:
            msg = str(e).lower()
            # the market moved onto our price before the order arrived: post-only refuses to trade as taker
            if e.status == 400 and any(w in msg for w in ("cross", "post_only", "post only", "would match", "taker")):
                return {"order_id": "", "fill_count": 0, "rejected": f"would have crossed the spread ({e})"}
            raise                                   # never resend without post-only
        r = r.get("order", r) if isinstance(r, dict) else {}
        fc = r.get("fill_count_fp") or r.get("fill_count") or 0
        return {"order_id": r.get("order_id", ""), "fill_count": int(float(fc))}

    async def sell_ioc(self, ticker: str, side: str, count: int, min_price_c: float, client_id: str = "") -> dict:
        """Reduce-only immediate sell of what we hold, down to min_price_c."""
        yes_px = round(min_price_c if side == "yes" else 100 - min_price_c, 4)
        body = {"ticker": ticker, "side": "ask" if side == "yes" else "bid", "count": f"{int(count)}.00",
                "price": f"{yes_px / 100:.4f}", "time_in_force": "immediate_or_cancel", "reduce_only": True,
                "self_trade_prevention_type": "taker_at_cross", "client_order_id": client_id or str(uuid.uuid4())}
        r = await self.request("POST", "/portfolio/events/orders", body=body, timeout=4)
        r = r.get("order", r) if isinstance(r, dict) else {}
        got = int(float(r.get("fill_count_fp") or r.get("fill_count") or 0))
        afp = r.get("average_fill_price")
        avg = None
        if afp not in (None, ""):
            avg = float(afp) * 100 if side == "yes" else 100 - float(afp) * 100
        fee = r.get("average_fee_paid")
        return {"order_id": r.get("order_id", ""), "fill_count": got, "avg_price_c": avg,
                "fees_c": got * float(fee) * 100 if fee not in (None, "") else 0.0}

    async def cancel(self, order_id: str, ticker: str) -> dict:
        # the ticker routes the cancel to the right exchange shard
        return await self.request("DELETE", f"/portfolio/events/orders/{order_id}", {"market_ticker": ticker},
                                  timeout=4)

    async def cancel_mine(self, prefix: str = "fm") -> tuple[int, list[str]]:
        """Cancel only this program's resting orders (client ids start with `prefix`); other bots on the
        same account are left alone. Returns (cancelled, errors)."""
        done, errs, cursor = 0, [], None
        for _ in range(10):
            params = {"status": "resting", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = await self.request("GET", "/portfolio/orders", params, timeout=6)
            for o in r.get("orders") or []:
                if not str(o.get("client_order_id") or "").startswith(prefix):
                    continue
                try:
                    await self.cancel(o["order_id"], o.get("ticker", ""))
                    done += 1
                except KalshiError as e:
                    if e.status != 404:
                        errs.append(f"{o.get('ticker')}: {e}")
            cursor = r.get("cursor")
            if not cursor:
                break
        return done, errs


# ------------------------------------------------------------------ WebSocket
class Feed:
    """One authenticated WebSocket connection. Reconnects on its own; calls on_message(dict, recv_ms)
    for every data message and on_state(str) when the connection changes."""

    def __init__(self, rest: Rest, on_message, on_state):
        self.rest, self.on_message, self.on_state = rest, on_message, on_state
        self.urls = config.ENDPOINTS[rest.env]["ws"]
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.state = "stopped"
        self.markets: set[str] = set()
        self.indices: list[str] = []
        self._id = 0
        self._stop = False
        self.msgs = 0
        self.skew_ms: float | None = None                  # Kalshi send time minus our receive time (median-ish)
        self._skews: list[float] = []
        self._cmd_markets: dict[int, list[str]] = {}       # subscribe command id -> markets it asked for
        self._sid_markets: dict[int, set[str]] = {}        # Kalshi subscription id -> markets still wanted

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def run(self):
        backoff = 1.0
        url_i = 0
        while not self._stop:
            if not self.rest.has_key:
                self._set("no API key - add one on the Keys page")
                await asyncio.sleep(2)
                continue
            url = self.urls[url_i % len(self.urls)]
            try:
                await self.rest.open()
                hdr = self.rest.signer.headers(self.rest.key_id, "GET", config.WS_SIGN_PATH)
                self._set("connecting")
                async with self.rest.session.ws_connect(url, headers=hdr, heartbeat=10, max_msg_size=0,
                                                        timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                    self.ws = ws
                    self._set("connected")
                    backoff = 1.0
                    await self._subscribe_all()
                    async for m in ws:
                        if m.type == aiohttp.WSMsgType.TEXT:
                            recv = time.time() * 1000
                            try:
                                d = json.loads(m.data)
                            except ValueError:
                                continue
                            self.msgs += 1
                            st = d.get("sending_ts_ms")
                            if st:
                                self._skews.append(st - recv)
                                if len(self._skews) >= 200:
                                    s = sorted(self._skews)
                                    self.skew_ms = s[len(s) // 2]
                                    self._skews.clear()
                            typ = d.get("type")
                            if typ == "error":
                                log.warning("websocket error: %s", d.get("msg"))
                            elif typ == "subscribed":
                                ms = self._cmd_markets.get(d.get("id"))
                                sid = (d.get("msg") or {}).get("sid")
                                if ms and sid is not None:
                                    self._sid_markets[sid] = set(ms)
                                continue
                            try:
                                self.on_message(d, recv)
                            except Exception:                    # noqa: BLE001
                                log.exception("message handler failed")
                        elif m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except Exception as e:                               # noqa: BLE001
                log.warning("websocket %s: %s", url, e)
                url_i += 1
            self.ws = None
            self._cmd_markets.clear()
            self._sid_markets.clear()
            if self._stop:
                break
            self._set(f"reconnecting in {backoff:.0f} s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        self._set("stopped")

    def _set(self, s: str):
        if s != self.state:
            self.state = s
            self.on_state(s)

    async def _send(self, obj: dict):
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(json.dumps(obj))

    async def _subscribe_markets(self, tickers: list[str]):
        i = self._next_id()
        self._cmd_markets[i] = list(tickers)
        await self._send({"id": i, "cmd": "subscribe",
                          "params": {"channels": ["orderbook_delta", "trade"], "market_tickers": list(tickers)}})

    async def _subscribe_all(self):
        if self.markets:
            await self._subscribe_markets(sorted(self.markets))
        await self._send({"id": self._next_id(), "cmd": "subscribe", "params": {"channels": ["fill"]}})
        if self.indices:
            await self._send({"id": self._next_id(), "cmd": "subscribe",
                              "params": {"channels": ["cfbenchmarks_value"], "index_ids": self.indices}})

    async def add_markets(self, tickers: list[str]):
        new = [t for t in tickers if t not in self.markets]
        self.markets.update(new)
        if new and self.ws is not None:
            await self._subscribe_markets(new)          # a snapshot follows for each new market

    async def drop_markets(self, tickers: list[str]):
        """Forget finished markets; unsubscribe a subscription once none of its markets are wanted."""
        gone = []
        for t in tickers:
            self.markets.discard(t)
        for sid, ms in list(self._sid_markets.items()):
            ms.difference_update(tickers)
            if not ms:
                gone.append(sid)
                del self._sid_markets[sid]
        if gone and self.ws is not None:
            await self._send({"id": self._next_id(), "cmd": "unsubscribe", "params": {"sids": gone}})

    async def reconnect(self):
        """Drop the connection so it comes back with the current key."""
        if self.ws is not None:
            await self.ws.close()

    async def stop(self):
        self._stop = True
        if self.ws is not None:
            await self.ws.close()
