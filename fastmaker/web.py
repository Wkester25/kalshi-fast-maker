"""The web page: login, dashboard, switches, settings and API keys. Served by aiohttp on the same
event loop as the engine, so every change takes effect immediately."""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

from aiohttp import web

from . import __version__, config
from .kalshi import Signer

STATIC = Path(__file__).parent / "static"
COOKIE = "fm_session"
SESSION_DAYS = 30
_fails: dict[str, list[float]] = {}


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_token(cfg: dict) -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    # tie the session to the current password, so changing it signs everyone out
    return exp + "." + _sign(cfg["web"]["session_secret"], exp + cfg["web"]["password_hash"])


def _token_ok(cfg: dict, tok: str | None) -> bool:
    if not tok or "." not in tok:
        return False
    exp, sig = tok.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig, _sign(cfg["web"]["session_secret"], exp + cfg["web"]["password_hash"]))


def _rule_from(cur: dict, r: dict) -> tuple[dict, str]:
    """Merge a posted rule into the current one and check it. Returns (rule, error)."""
    new = dict(cur)
    try:
        for k in ("stop_ms", "pull_ms", "pull_cooldown_ms"):
            if k in r:
                new[k] = int(float(r[k]))
        for k in ("stop_c", "pull_drop_c"):
            if k in r:
                new[k] = float(r[k])
        for k in ("enabled", "pull"):
            if k in r:
                new[k] = bool(r[k])
        if "windows" in r:
            wins = []
            for w in r["windows"]:
                wins.append({"start_s": _secs(w["start_s"]), "end_s": _secs(w["end_s"]), "lo_c": float(w["lo_c"]),
                             "hi_c": float(w["hi_c"]), "contracts": int(float(w["contracts"]))})
            new["windows"] = sorted(wins, key=lambda w: w["start_s"])
    except (TypeError, ValueError, KeyError):
        return cur, "numbers only (times as m:ss or seconds)"
    wins = new["windows"]
    if not 1 <= len(wins) <= config.MAX_WINDOWS:
        return cur, f"use 1 to {config.MAX_WINDOWS} windows"
    for n, w in enumerate(wins, 1):
        if not (0 <= w["start_s"] < w["end_s"] <= 899):
            return cur, f"window {n}: start must be before end, between 0:00 and 14:59"
        if not (50 <= w["lo_c"] < w["hi_c"] <= 99.9):
            return cur, f"window {n}: price band 50-99.9c, lowest below highest"
        if not 1 <= w["contracts"] <= 1000:
            return cur, f"window {n}: contracts 1-1000"
    for a, b in zip(wins, wins[1:]):
        if b["start_s"] <= a["end_s"]:
            return cur, "windows overlap - each one must start after the previous one ends"
    if new["stop_c"] != 0 and not 1 <= new["stop_c"] < min(w["lo_c"] for w in wins):
        return cur, "the stop must be 0 (hold) or between 1c and every window's price band"
    return new, ""


def _secs(v) -> int:
    """'9:00' or '540' -> 540."""
    t = str(v).strip()
    if ":" in t:
        m, sec = t.split(":", 1)
        return int(m) * 60 + int(float(sec))
    return int(float(t))


def _locked(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 300]
    return len(_fails[ip]) >= 5


def build(engine) -> web.Application:
    cfg = engine.cfg

    @web.middleware
    async def no_stale(request: web.Request, handler):
        """Browsers (phones especially) must never run an old copy of the page after an update."""
        resp = await handler(request)
        if not request.path.startswith("/favicon"):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    @web.middleware
    async def auth(request: web.Request, handler):
        open_paths = ("/login", "/setup", "/static/", "/favicon.svg")
        if request.path.startswith(open_paths):
            return await handler(request)
        if not cfg["web"]["password_hash"]:
            if request.path.startswith("/api/"):
                return web.json_response({"error": "set a password first"}, status=401)
            raise web.HTTPFound("/setup")
        if not _token_ok(cfg, request.cookies.get(COOKIE)):
            if request.path.startswith("/api/"):
                return web.json_response({"error": "signed out"}, status=401)
            raise web.HTTPFound("/login")
        return await handler(request)

    def _set_cookie(resp: web.Response):
        resp.set_cookie(COOKIE, _make_token(cfg), max_age=SESSION_DAYS * 86400, httponly=True, samesite="Strict")

    def _need_password(body: dict) -> bool:
        return config.check_password(str(body.get("password", "")), cfg["web"]["password_hash"])

    # ---- pages
    async def index(_):
        html = (STATIC / "index.html").read_text()
        for f in ("app.js", "style.css"):             # versioned URLs: an update always loads the new files
            html = html.replace(f"/static/{f}", f"/static/{f}?v={__version__}")
        return web.Response(text=html, content_type="text/html")

    async def login_page(_):
        if not cfg["web"]["password_hash"]:
            raise web.HTTPFound("/setup")
        return web.FileResponse(STATIC / "login.html")

    async def setup_page(_):
        if cfg["web"]["password_hash"]:
            raise web.HTTPFound("/login")
        return web.FileResponse(STATIC / "login.html")

    async def login(request):
        ip = request.remote or "?"
        if _locked(ip):
            return web.json_response({"error": "too many tries - wait 5 minutes"}, status=429)
        body = await request.json()
        if not _need_password(body):
            _fails.setdefault(ip, []).append(time.time())
            return web.json_response({"error": "wrong password"}, status=401)
        resp = web.json_response({"ok": True})
        _set_cookie(resp)
        return resp

    async def setup(request):
        if cfg["web"]["password_hash"]:
            return web.json_response({"error": "a password is already set"}, status=400)
        pw = str((await request.json()).get("password", ""))
        if len(pw) < 8:
            return web.json_response({"error": "use at least 8 characters"}, status=400)
        cfg["web"]["password_hash"] = config.hash_password(pw)
        config.save(cfg)
        resp = web.json_response({"ok": True})
        _set_cookie(resp)
        return resp

    async def logout(_):
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE)
        return resp

    # ---- api
    async def state(_):
        st = engine.status()
        st["settings"] = {k: cfg[k] for k in ("markets", "rules", "limits", "news_skip", "env", "record")}
        st["version"] = __version__
        return web.json_response(st)

    async def trading(request):
        body = await request.json()
        on = bool(body.get("on"))
        if on and cfg["mode"] == "live" and not _need_password(body):
            return web.json_response({"error": "enter your password to trade live"}, status=401)
        await engine.set_trading(on)
        return web.json_response({"ok": True})

    async def mode(request):
        body = await request.json()
        m = body.get("mode")
        if m not in ("paper", "live"):
            return web.json_response({"error": "mode must be paper or live"}, status=400)
        if m == "live":
            if not engine.rest.has_key:
                return web.json_response({"error": "add an API key first"}, status=400)
            if not _need_password(body):
                return web.json_response({"error": "wrong password"}, status=401)
        await engine.set_mode(m)
        return web.json_response({"ok": True})

    async def settings(request):
        body = await request.json()
        errs = []
        if "markets" in body:
            cfg["markets"] = {k: bool(body["markets"].get(k, cfg["markets"].get(k))) for k in config.MARKETS}
        if "rules" in body:
            for g in ("crypto", "commodity"):
                r = body["rules"].get(g)
                if not r:
                    continue
                if set(r) <= {"enabled", "pull"}:          # plain switches: nothing to validate
                    for k in r:
                        cfg["rules"][g][k] = bool(r[k])
                    continue
                new, err = _rule_from(cfg["rules"][g], r)
                if err:
                    errs.append(f"{g}: {err}")
                else:
                    cfg["rules"][g] = new
        if "limits" in body:
            try:
                for k, lo, hi in (("daily_loss_usd", 1, 1e6), ("max_order_usd", 1, 1e6), ("max_open_crypto", 1, 15),
                                  ("max_open_commodity", 1, 15), ("stop_after_losses", 1, 1000)):
                    if k in body["limits"]:
                        v = float(body["limits"][k])
                        if not lo <= v <= hi:
                            raise ValueError(k)
                        cfg["limits"][k] = int(v) if k.startswith(("max_open", "stop_after")) else v
            except (TypeError, ValueError) as e:
                errs.append(f"limit out of range: {e}")
        if "news_skip" in body:
            ns = body["news_skip"]
            cfg["news_skip"]["enabled"] = bool(ns.get("enabled", cfg["news_skip"]["enabled"]))
            if "times_et" in ns:
                times = [t.strip() for t in str(ns["times_et"]).replace(";", ",").split(",") if t.strip()]
                if all(len(t) == 5 and t[2] == ":" and t.replace(":", "").isdigit() for t in times):
                    cfg["news_skip"]["times_et"] = times
                else:
                    errs.append("news times like 08:30, 10:00")
        if "env" in body and body["env"] in ("real", "demo") and body["env"] != cfg["env"]:
            cfg["env"] = body["env"]
            errs.append("exchange changed - restart the program for it to take effect")
        config.save(cfg)
        return web.json_response({"ok": not errs, "errors": errs})

    async def keys(request):
        body = await request.json()
        kid = str(body.get("key_id", "")).strip()
        pem = str(body.get("pem", "")).strip()
        if not kid or not pem:
            return web.json_response({"error": "paste both the key ID and the private key"}, status=400)
        try:
            Signer(pem)                                   # fails with a clear message for the wrong file
        except Exception as e:                            # noqa: BLE001
            return web.json_response({"error": str(e)}, status=400)
        config.save_key(kid, pem)
        engine.rest.load_key()
        await engine.feed.reconnect()
        engine.event("info", "", "API key saved")
        return await key_test(request)

    async def key_test(_):
        if not engine.rest.has_key:
            return web.json_response({"ok": False, "error": engine.rest.key_error})
        t0 = time.perf_counter()
        try:
            cash = await engine.rest.shard_cash()
        except Exception as e:                            # noqa: BLE001
            return web.json_response({"ok": False, "error": str(e)})
        return web.json_response({"ok": True, "cash_usd": round(sum(cash.values()) / 100, 2),
                                  "rtt_ms": round((time.perf_counter() - t0) * 1000), "key_id": engine.rest.key_id[:8] + "…"})

    async def results_recheck(_):
        if not engine.rest.has_key:
            return web.json_response({"error": "add an API key first"}, status=400)
        return web.json_response(await engine.recheck_results())

    async def results_delete(request):
        body = await request.json()
        try:
            rid = int(body.get("id"))
        except (TypeError, ValueError):
            return web.json_response({"error": "which row?"}, status=400)
        if not engine.store.delete_result(rid):
            return web.json_response({"error": "row not found"}, status=404)
        engine._today_key = None
        engine.event("warn", "", f"result row {rid} removed by hand")
        return web.json_response({"ok": True})

    async def stop_all(_):
        await engine.halt("Stop all pressed")
        return web.json_response({"ok": True})

    async def password(request):
        body = await request.json()
        if not _need_password(body):
            return web.json_response({"error": "current password is wrong"}, status=401)
        new = str(body.get("new", ""))
        if len(new) < 8:
            return web.json_response({"error": "use at least 8 characters"}, status=400)
        cfg["web"]["password_hash"] = config.hash_password(new)
        config.save(cfg)
        resp = web.json_response({"ok": True})
        _set_cookie(resp)
        return resp

    app = web.Application(middlewares=[no_stale, auth])
    app.router.add_get("/", index)
    app.router.add_get("/login", login_page)
    app.router.add_get("/setup", setup_page)
    app.router.add_post("/login", login)
    app.router.add_post("/setup", setup)
    app.router.add_post("/logout", logout)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/trading", trading)
    app.router.add_post("/api/mode", mode)
    app.router.add_post("/api/settings", settings)
    app.router.add_post("/api/keys", keys)
    app.router.add_post("/api/keys/test", key_test)
    app.router.add_post("/api/stop-all", stop_all)
    app.router.add_post("/api/results/recheck", results_recheck)
    app.router.add_post("/api/results/delete", results_delete)
    app.router.add_post("/api/password", password)
    app.router.add_static("/static/", STATIC)
    async def favicon(_):
        return web.FileResponse(STATIC / "favicon.svg")

    app.router.add_get("/favicon.svg", favicon)
    return app
