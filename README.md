# Kalshi Fast Maker

A small rebuild of the Late Maker bot for a fast Linux server. It runs one strategy (the early favourite maker), takes its market data from Kalshi's WebSocket instead of polling, and you control it from a web page.

About 1,800 lines of Python plus the web page, with no optimiser or calibration files to keep in sync.

## What it does

During each 15-minute window, between `start` and `end` seconds in, it looks at the market's favourite. If that side's best bid is inside the price band, it rests a post-only buy one tick above that bid, never at or through the offer. It moves up only when someone else outbids it, so it never chases its own order. It holds to settlement.

Each rule can have **several trading windows** (Settings → the rule's card → *+ Add window*). Each window has its own time range, price band, contract count and **market stop** (0 = hold to settlement), for example 9:00–12:00 at 97–99.5¢ ×1 with a 50¢ stop plus 14:30–14:55 at 98–99.5¢ ×2 holding. Windows can't overlap. Contracts keep the stop of the window that bought them, even after that window ends. The fast pull is shared by all of a rule's windows. Outside every window, resting bids are cancelled and held contracts are kept.

Defaults (one window each):

| Rule | Window | Band | Default |
|---|---|---|---|
| Commodity | 9:00 to 13:00 | 96 to 99.5¢ | on |
| Crypto | 9:00 to 12:00 | 97 to 99.5¢ | **off** |

The crypto rule is off because the independent review of our backtests found the crypto early edge disappears (−0.3¢ per contract) on newer data. It also found that a 1.2-second clock look-ahead had inflated my earlier backtest numbers. The commodity rule held up at about +0.4¢ per contract. Turn crypto on in paper mode if you want to collect evidence on it.

### Where the fast connection helps

The old bot polled once a second. Here, every decision runs on each WebSocket book update, typically a few milliseconds after Kalshi sends it:

- **Market stop.** If our side's bid stays below `stop_c` for `stop_ms` (default 50¢ for 1,000 ms), it sells everything right away. The old bot noticed this up to a second late. Set the stop to **0** to never sell and always hold to settlement.
- **Fast pull.** If our side's mid drops by `pull_drop_c` within `pull_ms` (default 1.5¢ within 1.5 s), it cancels the resting bid before the move fills us, then pauses for `pull_cooldown_ms`. This targets the adverse selection we found: fills that arrive just as the price turns are the losing ones.
- **Cancel on feed loss.** If the WebSocket drops, resting bids are cancelled and the stale books are ignored until fresh snapshots arrive.
- The self-bidding bug can't happen here: our own resting order is subtracted from the book before any decision.

The pull thresholds are a sensible starting point, not a fitted result. The recorder (below) collects what's needed to fit them properly.

## Install on the server

Copy this folder to the server (scp, git or rsync), then:

```bash
cd kalshi-fast-maker
sudo bash deploy/install.sh         # or: sudo bash deploy/install.sh --port 9000
```

The script installs Python if it's missing, copies the program to `/opt/fastmaker` with its own venv and user, and asks you for a dashboard password. It then starts it as a systemd service that restarts on failure and cancels resting orders when stopped. Re-running it updates the program and keeps your settings, key and data.

Then open `http://<server-ip>:8080`.

### Add the API key (one page, no files to copy around)

1. On kalshi.com go to **Account & security → API keys → Create new API key**.
2. In the dashboard, open **API key**. Paste the Key ID, and paste the private key or drop the downloaded file on the box.
3. Click **Save & test**. It shows your balance and the round-trip time.

The key is saved to `/opt/fastmaker/secrets/` (owner-only, chmod 600) and is never sent back to the browser. The WebSocket reconnects with the new key immediately. A key is needed even for paper mode, because Kalshi's WebSocket requires authentication.

### Run without installing (for trying it out)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py          # first visit to http://localhost:8080 asks for a password
```

## Using it

- **Paper** is the default. It uses the same decisions as live. A paper fill counts only when a real Kalshi trade prints at or through our price, after the queue that was ahead of us when we joined.
- **Live** needs your password to switch on and again to start trading.
- **Stop all** switches trading off and cancels every resting order on the account.
- **Position size** (Settings, plus the *Size ×* box on the dashboard): a multiplier applied to every window's contracts (rounded down, at least 1). Optionally **reinvest a % of profit**: size grows by (bankroll + reinvest% × profit) / bankroll, where profit is counted in the current mode from when reinvesting was switched on. Losses shrink it the same way. The max-per-order and daily loss limits still cap every order.
- **Settings** take effect the moment you save. Limits are the daily loss limit, the maximum per order, the maximum markets open at once per group, and a stop after N losses in a day. Windows around 8:30 and 10:00 ET news are skipped by default.
- Paper and live results are kept separately. The dashboard shows today's results, all-time, per contract, by day, every trade and an activity log.

Suggested path: run paper for at least a week (several hundred trades). Go live with 1 contract on commodities only if paper per-contract stays positive after fees.

## Data for later analysis

`data/rec/<day>/` holds gzipped CSVs: top-of-book changes, trades, CF Benchmarks index values and market results. Every row is stamped with the time this server received it (`recv_ms`). Backtests on this data therefore can't look ahead the way the old logs allowed, which was the flaw the review found. It uses about 50–150 MB per day; turn it off with `"record": false` in `config.json`.

Results and the event log are in `data/fastmaker.db` (SQLite).

## Security

The dashboard is plain HTTP and protected by the password: it uses a 30-day session cookie, locks out for 5 minutes after 5 wrong tries, and changing the password signs everyone out. On a server open to the internet, do one of the following:

- Firewall the port to your own IP: `sudo ufw allow from <your-ip> to any port 8080`.
- Or bind it locally and use an SSH tunnel: `sudo -u fastmaker /opt/fastmaker/.venv/bin/python /opt/fastmaker/run.py --host 127.0.0.1` once (this saves it), then restart the service. From your PC run `ssh -L 8080:localhost:8080 you@server` and open `http://localhost:8080`.
- Or put it behind Caddy or nginx with HTTPS.

## Commands

```bash
journalctl -u fastmaker -f                 # live log
sudo systemctl restart fastmaker           # restart (orders are cancelled on stop)
sudo -u fastmaker /opt/fastmaker/.venv/bin/python /opt/fastmaker/run.py --set-password
```

## Files

```
run.py                 start / set password
fastmaker/config.py    settings, markets, endpoints, key storage
fastmaker/kalshi.py    signed REST + the WebSocket feed
fastmaker/book.py      order book from snapshot + deltas (own order excluded)
fastmaker/strategy.py  the rule as pure decisions (no network)
fastmaker/engine.py    windows, decisions, paper/live orders, fills, settlement
fastmaker/store.py     results/events (SQLite) + market recorder
fastmaker/web.py       dashboard server + API
fastmaker/static/      the web page
deploy/                install script + systemd unit
tests/                 unit tests + a fake Kalshi exchange (python -m pytest)
```

## Not verified yet

These need checking against the real exchange in paper mode before you trust live mode:

- The tests run against a fake exchange built from Kalshi's documented message formats, not the real one.
- The first real session should confirm three things: the feed connects, books match the Kalshi site, and paper fills look sensible.
- Live mode should start at 1 contract.
