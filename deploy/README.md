# Autonomous Trading System — VPS deployment

Runs the engine 24/7 on a Linux VPS, with the dashboard reachable over HTTPS
from your phone, and alerts reaching you on Telegram.

## What you need

| | |
|---|---|
| A VPS | ~$5/mo. Ubuntu 22.04 or 24.04 recommended |
| A domain or subdomain | optional but strongly recommended (needed for HTTPS) |
| SSH access | password or key |
| A Telegram bot | message @BotFather, send /newbot |

## 1. Prepare the bot

1. Message **@BotFather**, send `/newbot`, follow the prompts, copy the **token**
2. Message **@userinfobot**, copy your numeric **chat id**

## 2. One-time server setup

```bash
sudo bash deploy/install.sh
```

That installs Python, the system packages, a virtualenv with the dependencies,
and three systemd services.

## 3. Clone and configure

```bash
sudo mkdir -p /srv/tradingsys
sudo chown "$USER":"$USER" /srv/tradingsys
cd /srv/tradingsys
git clone <your-repo-url> .
cp .env.example .env
nano .env
```

`.env` is the only place secrets live on the server, and it is gitignored.

```ini
TELEGRAM_BOT_TOKEN=1234567890:AA...
TELEGRAM_CHAT_ID=123456789
DASHBOARD_TOKEN=some-long-random-string
DASHBOARD_USER=trader
ALPACA_KEY_ID=...
ALPACA_SECRET_KEY=...
```

`DASHBOARD_TOKEN` matters once the dashboard is public — see step 5.

## 4. Start the services

```bash
sudo systemctl enable --now tradingsys-dashboard
sudo systemctl enable --now tradingsys-engine
sudo systemctl enable --now tradingsys-talk
```

| Service | What it does |
|---|---|
| `tradingsys-dashboard` | the web UI, bound to 127.0.0.1:8642 |
| `tradingsys-engine` | the multi-agent loop, dry-run by default |
| `tradingsys-talk` | the Telegram command channel |

Check them:

```bash
systemctl status tradingsys-engine
journalctl -u tradingsys-engine -f
```

## 5. HTTPS in front (Caddy)

`deploy/Caddyfile` terminates TLS and puts Basic auth over the whole
dashboard — important, because an unauthenticated trading console on the public
internet would let anyone place orders.

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Then browse to `https://trading.yourdomain.com` using the
`DASHBOARD_USER` / `DASHBOARD_TOKEN` pair from `.env`.

With no domain, an SSH tunnel is safer than exposing the port:

```bash
ssh -L 8642:127.0.0.1:8642 user@your-vps-ip
# then open http://127.0.0.1:8642 locally
```

## 6. Trade it

The engine boots **dry-run**: it computes signals, journals forward trades, and
sends alerts, but submits nothing. To enable real orders you must connect a
venue *and* flip the switch deliberately:

```bash
py run.py agents --cycles 1        # sanity-check the signals first
```

Then in the dashboard: **Setup → Broker → Save connection → Test connection**,
and only after that **Setup → Risk → turn off "Dry run"**.

Even then, the Alpaca live endpoint requires `ALPACA_LIVE_CONFIRMED=yes` in the
server's environment. That triple lock — credentials, dry-run off, and an env
assertion — exists so nothing goes live by accident.

## Operating it from Telegram

Start the `talk` service, then message your bot:

```
/status      equity, positions, risk, uptime
/kill        emergency stop — flatten and halt
/resume      clear the kill switch
/forward     predicted vs realised performance
/set risk 0.005
/help
```

Only your chat id may issue commands. Any other sender is ignored and counted.

## Updating

```bash
cd /srv/tradingsys
git pull
sudo systemctl restart tradingsys-dashboard tradingsys-engine
```
