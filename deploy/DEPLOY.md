# Deploy to a VPS — run it on a server, not your PC

You need this because the engine must run when your laptop is shut. Three
services on a Linux box, the dashboard as a remote viewer.

---

## Step 1 — push this code to GitHub

Run on **this PC** (PowerShell, from `tradingsys/`):

```powershell
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

If you have no GitHub repo yet: create an empty one at
<https://github.com/new> (no README, no .gitignore — the local repo already has
both), then use the URL it gives you.

`.env` and `state/` are gitignored, so nothing secret is ever pushed.

## Step 2 — rent the server

Whatever you like, e.g. **Hetzner CX22** (~EUR 4/mo, 4 vCPU / 8 GB), or
**Oracle Cloud Always Free** (2 Arm cores / 12 GB, free). Choose a region near
your data source.

Note the server's public IP as you create it.

## Step 3 — point a domain at it (recommended)

In your DNS provider, add an A record:

```
trading.yourdomain.com   A   <server-ip>
```

This is what lets Caddy issue a free HTTPS certificate, which is what lets you
open the dashboard from your phone. **This is the only step that costs money and
takes time**, so start it before step 4 — DNS propagates while you do the rest.

No domain? Skip to the SSH tunnel note at the bottom.

## Step 4 — one command on the server

SSH in and run:

```bash
sudo bash -c 'APP_DIR=/srv/tradingsys REPO_URL=https://github.com/<you>/<repo>.git bash -s' \
  < <(curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/deploy/vps_deploy.sh)
```

Or, if you prefer to clone first:

```bash
sudo mkdir -p /srv/tradingsys && sudo chown $USER:$USER /srv/tradingsys
git clone https://github.com/<you>/<repo>.git /srv/tradingsys
cd /srv/tradingsys && bash deploy/vps_deploy.sh
```

That script installs Python, git and Caddy, builds a virtualenv, installs the
Python deps, writes `.env` if it is missing (never overwriting), installs the
three systemd services and starts them.

## Step 5 — fill in `.env`

```bash
nano /srv/tradingsys/.env
```

```ini
TELEGRAM_BOT_TOKEN=...        # from @BotFather
TELEGRAM_CHAT_ID=...          # from @userinfobot
DASHBOARD_USER=trader
DASHBOARD_TOKEN=a-long-random-password-you-choose

# leave broker keys blank to stay in simulated PAPER mode
```

Then:

```bash
sudo systemctl restart tradingsys-engine
```

## Step 6 — HTTPS + password

```bash
# on the server
cd /srv/tradingsys
sed -i 's/trading.example.com/trading.yourdomain.com/' deploy/Caddyfile
caddy hash-password --plaintext "a-long-random-password-you-choose"
# put the resulting $2a$14$... hash into the basicauth line in deploy/Caddyfile
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Open **https://trading.yourdomain.com**. Log in with `DASHBOARD_USER` /
`DASHBOARD_TOKEN`.

## Step 7 — stop using this PC for trading

Your dashboard on this machine can keep showing the *same* numbers, because both
read the same state files. But the trading run is now on the VPS. Locally:

```powershell
py tradingsys\run.py serve    # optional: a second, independent instance
```

To point the local dashboard at the VPS instead, nothing needs changing — both
read their own `state/`.

---

## What runs where

| Service | Command | Purpose |
|---|---|---|
| `tradingsys-engine` | `run.py serve` | trades headless, writes state, runs Telegram in-process |
| `tradingsys-dashboard` | `run.py dashboard` | web UI on `127.0.0.1:8642`, Caddy proxies to it |
| `tradingsys-talk` | `run.py talk` | Telegram command channel (if the engine's own is disabled) |

Check them:

```bash
systemctl status tradingsys-engine
journalctl -u tradingsys-engine -f     # live logs
```

## Going live

The engine boots in **dry run**. Nothing is submitted until you both connect a
venue in the dashboard *and* turn off `Setup -> Risk -> Dry run`. Alpaca live
additionally needs `ALPACA_LIVE_CONFIRMED=yes` in the server's environment.

## Updating after you change code

On this PC:

```powershell
git push
```

On the server:

```bash
cd /srv/tradingsys && git pull
sudo systemctl restart tradingsys-engine tradingsys-dashboard
```

## No domain? Use an SSH tunnel

Safer than exposing the port directly:

```bash
ssh -L 8642:127.0.0.1:8642 user@your-server
```

Then open <http://127.0.0.1:8642> on this PC and it reaches the VPS.

---

## Files this adds

```
deploy/
  vps_deploy.sh                 one-command server setup
  tradingsys-engine.service     runs `run.py serve` (headless, correct for a server)
  tradingsys-dashboard.service  runs the web UI
  tradingsys-talk.service       runs the Telegram channel
  Caddyfile                     HTTPS + Basic auth + SSE-aware proxying
  install.sh                    lower-level install helper
  DEPLOY.md                     this file
```

## Known limits

- Tested only on this Windows box. The code is platform-neutral and the ARM
  check passes, but nothing here has been booted under systemd on a real server.
- `tradingsys-talk.service` is redundant if `tradingsys-engine` already runs
  Telegram in-process (it does). Run one or the other, not both, or two pollers
  will fight over `getUpdates`.
