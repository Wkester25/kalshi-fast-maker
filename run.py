#!/usr/bin/env python3
"""Fast Maker: start the trading engine and the web dashboard.

    python run.py                    start (dashboard on the port in config.json, default 8080)
    python run.py --set-password     set or reset the dashboard password from the terminal
    python run.py --port 9000        use another port (saved)
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import signal
import sys

from aiohttp import web

from fastmaker import config
from fastmaker.engine import Engine
from fastmaker.web import build


async def main(cfg: dict) -> None:
    engine = Engine(cfg)
    app = build(engine)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    host, port = cfg["web"]["host"], int(cfg["web"]["port"])
    await web.TCPSite(runner, host, port).start()
    logging.info("dashboard on http://%s:%s  (%s mode, trading %s)", host, port, cfg["mode"],
                 "ON" if cfg["trading"] else "off")
    if not cfg["web"]["password_hash"]:
        logging.info("first visit asks you to choose a dashboard password")
    await engine.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:                 # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass
    try:
        await stop.wait()
    finally:
        logging.info("stopping: cancelling resting orders")
        await engine.stop()
        await runner.cleanup()


def cli() -> None:
    ap = argparse.ArgumentParser(description="Kalshi Fast Maker")
    ap.add_argument("--set-password", action="store_true", help="set the dashboard password and exit")
    ap.add_argument("--port", type=int, help="dashboard port (saved to config.json)")
    ap.add_argument("--host", help="dashboard address, e.g. 127.0.0.1 to allow only local/SSH-tunnel access")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    cfg = config.load()
    if a.port or a.host:
        if a.port:
            cfg["web"]["port"] = a.port
        if a.host:
            cfg["web"]["host"] = a.host
        config.save(cfg)
    if a.set_password:
        while True:
            pw = getpass.getpass("New dashboard password (8+ characters): ")
            if len(pw) >= 8 and pw == getpass.getpass("Repeat: "):
                break
            print("Too short or didn't match - try again.")
        cfg["web"]["password_hash"] = config.hash_password(pw)
        config.save(cfg)
        print("Password saved. Restart the service if it is running.")
        return
    try:
        asyncio.run(main(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(cli())
