"""Run the full program against the fake exchange (for looking at the UI):  python -m tests.demo_server [port]"""
import asyncio
import sys
import tempfile
from pathlib import Path

from fastmaker import config, store


async def main(port: int):
    home = Path(tempfile.mkdtemp())
    config.CONFIG, config.SECRETS, config.DATA, store.DATA = home / "c.json", home / "s", home / "d", home / "d"
    from tests.fake_kalshi import FakeKalshi, point_at, serve
    from tests.test_end_to_end import pem
    from aiohttp import web
    from fastmaker.engine import Engine
    from fastmaker.web import build
    xport = port + 1
    point_at(xport)
    fake = FakeKalshi(secs_into_window=610)
    await serve(fake, xport)
    config.save_key("demo-key-0000", pem())
    cfg = config.load()
    cfg.update(env="fake", mode="paper", trading=True)
    eng = Engine(cfg)
    r = web.AppRunner(build(eng))
    await r.setup()
    await web.TCPSite(r, "127.0.0.1", port).start()
    await eng.start()
    stop = asyncio.Event()
    asyncio.create_task(fake.wander(stop))
    print("demo on", port, flush=True)
    await asyncio.Event().wait()

asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8090))
