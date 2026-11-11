"""Settings, markets and secrets. Everything lives next to the program:

    config.json          settings (written by the web page; safe to edit by hand while stopped)
    secrets/kalshi.json  API key id          (chmod 600)
    secrets/kalshi.pem   API private key     (chmod 600)
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets as _secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
SECRETS = ROOT / "secrets"
DATA = ROOT / "data"

# Kalshi 15-minute series. group decides which rule trades it.
MARKETS = {
    "BTC": ("KXBTC15M", "crypto"), "ETH": ("KXETH15M", "crypto"), "SOL": ("KXSOL15M", "crypto"),
    "DOGE": ("KXDOGE15M", "crypto"), "HYPE": ("KXHYPE15M", "crypto"), "XRP": ("KXXRP15M", "crypto"),
    "BNB": ("KXBNB15M", "crypto"),
    "GOLD": ("KXGOLD15M", "commodity"), "SILVER": ("KXSILVER15M", "commodity"), "WTI": ("KXWTI15M", "commodity"),
    "BRENT": ("KXBRENT15M", "commodity"), "NATGAS": ("KXNATGAS15M", "commodity"),
    "PLATINUM": ("KXPLATINUM15M", "commodity"), "PALLADIUM": ("KXPALLADIUM15M", "commodity"),
    "COPPER": ("KXCOPPER15M", "commodity"),
}
# CF Benchmarks index per crypto market (shown on the dashboard; crypto settles on its 60 s average)
CF_INDEX = {"BTC": "BRTI", "ETH": "ETHUSD_RTI", "SOL": "SOLUSD_RTI", "DOGE": "DOGEUSD_RTI",
            "HYPE": "HYPEUSD_RTI", "XRP": "XRPUSD_RTI", "BNB": "BNBUSD_RTI"}

ENDPOINTS = {   # first entry is used; the rest are fallbacks (Kalshi docs, Nov 2026)
    "real": {"rest": ["https://api.elections.kalshi.com/trade-api/v2", "https://external-api.kalshi.com/trade-api/v2"],
             "ws": ["wss://external-api-ws.kalshi.com/trade-api/ws/v2", "wss://api.elections.kalshi.com/trade-api/ws/v2"]},
    "demo": {"rest": ["https://demo-api.kalshi.co/trade-api/v2", "https://external-api.demo.kalshi.co/trade-api/v2"],
             "ws": ["wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2", "wss://demo-api.kalshi.co/trade-api/ws/v2"]},
}
WS_SIGN_PATH = "/trade-api/ws/v2"

# One rule per group. Times are seconds into the 15-minute window; prices in cents.
DEFAULT_RULE = {
    "enabled": True,
    "start_s": 540, "end_s": 720,        # bid only inside this part of the window
    "lo_c": 97.0, "hi_c": 99.5,          # the favourite's best bid must be in [lo, hi)
    "contracts": 1,
    "stop_c": 50.0, "stop_ms": 1000,     # sell if our side's bid stays below stop_c this long
    "pull": True, "pull_drop_c": 1.5, "pull_ms": 1500, "pull_cooldown_ms": 5000,
}
DEFAULTS = {
    "env": "real",
    "mode": "paper",                     # paper | live
    "trading": False,                    # master switch
    "markets": {k: k not in ("SOL", "COPPER") for k in MARKETS},
    "rules": {
        "crypto": dict(DEFAULT_RULE, enabled=False),   # newer data didn't support crypto; paper-test before live
        "commodity": dict(DEFAULT_RULE, end_s=780, lo_c=96.0),
    },
    "limits": {"daily_loss_usd": 20.0, "max_open_crypto": 6, "max_open_commodity": 7,
               "max_order_usd": 25.0, "stop_after_losses": 6},
    "news_skip": {"enabled": True, "times_et": ["08:30", "10:00"], "weekdays_only": True},
    "record": True,
    "web": {"host": "0.0.0.0", "port": 8080, "password_hash": "", "session_secret": ""},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    try:
        raw = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        raw = {}
    cfg = _merge(DEFAULTS, raw)
    if not cfg["web"]["session_secret"]:
        cfg["web"]["session_secret"] = _secrets.token_hex(32)
        save(cfg)
    return cfg


def save(cfg: dict) -> None:
    tmp = CONFIG.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # holds the password hash
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(cfg, indent=2))
    os.replace(tmp, CONFIG)                     # atomic: a crash never leaves half a file


# ------------------------------------------------------------ web password
def hash_password(pw: str) -> str:
    salt = _secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return salt.hex() + ":" + h.hex()


def check_password(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000)
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, AttributeError):
        return False


# ------------------------------------------------------------ API key
def key_paths():
    return SECRETS / "kalshi.json", SECRETS / "kalshi.pem"


def load_key() -> tuple[str, str] | None:
    j, p = key_paths()
    try:
        kid = json.loads(j.read_text()).get("key_id", "").strip()
        pem = p.read_text()
        return (kid, pem) if kid and pem else None
    except (OSError, ValueError):
        return None


def save_key(key_id: str, pem: str) -> None:
    SECRETS.mkdir(exist_ok=True)
    os.chmod(SECRETS, 0o700)
    j, p = key_paths()
    for path, text in ((j, json.dumps({"key_id": key_id.strip()})), (p, pem.strip() + "\n")):
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
