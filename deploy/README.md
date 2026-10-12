# Autonomous Trading System — VPS deployment

Runs the engine 24/7 on a Linux VPS, with the dashboard reachable over HTTPS
from your phone, and alerts reaching you on Telegram.

Verified on Ubuntu 22.04/24.04, **both x86_64 and aarch64** (Oracle Cloud
Always Free runs Ampere ARM). The only hard requirement is Linux.

---

## Option A — free: Oracle Cloud Always Free

**What you actually get** (corrected June 2026):

| | |
|---|---|
| Compute | **2 Arm OCPUs + 12 GB RAM** |
| Storage | 200 GB block |
| Transfer | 10 TB/month |
| Cost | Free for the life of the account — permanent, not a trial |

Oracle **halved** this on 15 June 2026 (4 OCPU / 24 GB → 2 OCPU / 12 GB) with no
announcement. Any article still quoting 24 GB predates that.

### The four catches, all real

1. **Arm capacity errors.** Popular regions are frequently out of stock;
   provisioning fails with an out-of-capacity error until capacity frees —
   sometimes days. Retry, or pick a quieter region.
2. **Your home region is fixed at signup and cannot be changed.** This is the
   only irreversible decision on their platform. Choose near your broker
   (a US region for Alpaca).
3. **A card is required** at signup.
4. **Idle instances can be reclaimed.** A running trading engine keeps the
   instance busy, so this is a non-issue — but don't provision and forget it.

### Provisioning

1. Sign up at <https://www.oracle.com/cloud/free/> and set your home region
   deliberately.
2. Compute → Instances → **Create instance**
   - Image: **Ubuntu 24.04** (Canonical's ARM build)
   - Shape: **Ampere** → `VM.Standard.A1.Flex` → **2 OCPUs, 12 GB RAM**
     (anything larger is billed — stay exactly at 2/12)
   - Add your SSH public key
3. If creation fails with *out of host capacity*, retry or change region.
4. Open port 22 in the instance's security list (OCI denies everything by
   default, including SSH) and, if you use Caddy, 80 and 443.

Then continue at **Install** below.

## Option B — paid, recommended before real money

**Hetzner CX22** (~€4/mo, 4 vCPU / 8 GB, x86_64) or **Vultr** ($6/mo). No
capacity errors, no reclaim policy, x86 so every package has a build.

My honest advice: learn on the free Oracle instance, then move to ~$4/month
before it touches a funded account. A free instance reclaimed mid-session costs
more than a decade of Hetzner.

## Do not use

| Provider | Why |
|---|---|
| **Render** | Free services sleep after 15 min idle — an idle engine misses entries |
| **Railway** | $5 trial, then $1/month — not genuinely free |
| **Fly.io** | Removed its free allowance in 2024 |
| **AWS / Azure** | 12-month *trials*, then billed — wrong for something that must stay up |
| **Google Cloud e2-micro** | Genuinely free forever, but 1 GB RAM and 3 US regions only. Workable backup. |

---

## Install

```bash
# on the server
git clone <your-repo-url> /srv/tradingsys
cd /srv/tradingsys
sudo bash deploy/install.sh
```

`install.sh` detects the architecture, installs Python/git/caddy, creates a
virtualenv, pip-installs the requirements (pandas and numpy both have aarch64
wheels, so nothing is compiled), and installs three systemd units.

## Configure

```bash
cp .env.example .env
nano .env
```

`.env` is the only place secrets live on the server and is gitignored.

```ini
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
DASHBOARD_USER=trader
DASHBOARD_TOKEN=<long random string>
ALPACA_KEY_ID=...
ALPACA_SECRET_KEY=...
```

## Start

```bash
sudo systemctl enable --now tradingsys-dashboard
sudo systemctl enable --now tradingsys-engine
sudo systemctl enable --now tradingsys-talk
```

| Service | Does |
|---|---|
| `tradingsys-dashboard` | the web UI, bound to `127.0.0.1:8642` |
| `tradingsys-engine` | the multi-agent loop, dry-run |
| `tradingsys-talk` | the Telegram command channel |

```bash
systemctl status tradingsys-engine
journalctl -u tradingsys-engine -f
```

## HTTPS + Basic auth (Caddy)

An unauthenticated trading console on the public internet would let anyone
place orders. `deploy/Caddyfile` terminates TLS and gates the whole UI:

```bash
# generate a bcrypt hash of your dashboard password
sudo caddy hash-password --plaintext "$DASHBOARD_TOKEN"

# then edit /etc/caddy/Caddyfile with your domain and that hash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Browse `https://trading.yourdomain.com`, log in with `DASHBOARD_USER` /
`DASHBOARD_TOKEN`.

**No domain?** An SSH tunnel is safer than exposing the port:

```bash
ssh -L 8642:127.0.0.1:8642 ubuntu@<server-ip>
# then open http://127.0.0.1:8642 locally
```

## Go live

The engine boots **dry-run**. Real orders need: credentials, the dry-run switch
off, and an env assertion:

```bash
py run.py agents --cycles 1        # sanity-check signals first
# then Setup -> Broker -> Save + Test connection
# then Setup -> Risk -> turn off "Dry run"
```

Alpaca's live endpoint additionally needs `ALPACA_LIVE_CONFIRMED=yes` in the
server's environment. That triple lock exists so nothing goes live by accident.

**On a server the Exness broker is unavailable** — MetaTrader5 is Windows-only
with no ARM/Linux build. Use paper, Binance, Alpaca, or the generic webhook
bridge. Everything else works identically.

## Update

```bash
cd /srv/tradingsys
git pull
sudo systemctl restart tradingsys-dashboard tradingsys-engine tradingsys-talk
```

## Verify the deployment

```bash
py scripts/check_arm.py      # ARM compatibility of the tree
py -m unittest discover -s tests   # full suite on the server
systemctl is-active tradingsys-dashboard tradingsys-engine tradingsys-talk
```
