#!/usr/bin/env bash
# ============================================================
#  ONE-COMMAND VPS DEPLOY
#  Run on the server (Ubuntu/Debian, x86_64 or aarch64):
#
#     curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/deploy/vps_deploy.sh | bash
#
#  Or if you already cloned the repo:
#     bash deploy/vps_deploy.sh
#
#  What it does:
#    1. installs system packages (python3, venv, git, caddy)
#    2. creates the app dir and a virtualenv
#    3. installs python deps
#    4. writes .env if missing (never overwrites)
#    5. installs + enables the three systemd units
#    6. prints the resulting URLs and the next steps
# ============================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/tradingsys}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
BRANCH="${BRANCH:-main}"

# ---------------------------------------------------------------- banner
echo
echo "  ============================================================"
echo "   AUTONOMOUS TRADING SYSTEM -- VPS deployment"
echo "   arch: $(uname -m)    user: ${SERVICE_USER}"
echo "   target: ${APP_DIR}"
echo "  ============================================================"
echo

# ------------------------------------------------------------ 1. packages
echo "[1/6] installing system packages"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-venv python3-pip git curl caddy
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y python3 python3-pip git curl caddy
else
  echo "  !! unsupported distro -- install python3, venv, git, caddy yourself" >&2
  exit 1
fi
echo "    done"

# --------------------------------------------------- 2. app dir + virtualenv
echo "[2/6] preparing ${APP_DIR}"
sudo mkdir -p "$APP_DIR"
sudo chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
cd "$APP_DIR"

if [ ! -f "run.py" ]; then
  echo "    cloning ..."
  if [ -n "${REPO_URL:-}" ]; then
    git clone --branch "$BRANCH" "$REPO_URL" .
  else
    echo "    !! no run.py here and REPO_URL is not set." >&2
    echo "       clone the repo into ${APP_DIR} first, e.g." >&2
    echo "       git clone --branch $BRANCH https://github.com/<you>/<repo> $APP_DIR" >&2
    exit 1
  fi
fi

python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
echo "    done"

# -------------------------------------------------------- 3. python deps
echo "[3/6] installing python dependencies"
./.venv/bin/pip install --quiet -r requirements.txt
python3 -c "import pandas, numpy; print(f'    pandas {pandas.__version__}, numpy {numpy.__version__}')"
echo "    done"

# ------------------------------------------------------------- 4. secrets
echo "[4/6] configuration"
if [ ! -f .env ]; then
  cp .env.example .env
  cat <<'NOTE'

    Created .env with placeholders. EDIT IT BEFORE THE ENGINE TRADES:

        nano .env

    Minimum to be useful:
        TELEGRAM_BOT_TOKEN   (from @BotFather)
        TELEGRAM_CHAT_ID     (from @userinfobot)
        DASHBOARD_USER       (any login name)
        DASHBOARD_TOKEN      (a long random password)

    Leave broker keys blank to stay in simulated PAPER mode.

NOTE
else
  echo "    .env already exists -- leaving it alone"
fi
echo "    done"

# ---------------------------------------------------- 5. systemd services
echo "[5/6] installing systemd services"
for unit in tradingsys-dashboard tradingsys-engine tradingsys-talk; do
  [ -f "deploy/${unit}.service" ] || continue
  sudo sed "s|__APP_DIR__|${APP_DIR}|g; s|__SERVICE_USER__|${SERVICE_USER}|g" \
      "deploy/${unit}.service" | sudo tee "/etc/systemd/system/${unit}.service" >/dev/null
  echo "    installed ${unit}"
done
sudo systemctl daemon-reload

# the engine unit runs `serve`, which is headless -- correct for a server
sudo sed -i "s|ExecStart=.*run.py agents.*|ExecStart=${APP_DIR}/.venv/bin/python run.py serve --cycle-seconds 300 --interval 1m|" \
    /etc/systemd/system/tradingsys-engine.service 2>/dev/null || true
echo "    done"

# ------------------------------------------------------------ 6. start them
echo "[6/6] enabling services"
sudo systemctl enable --now tradingsys-dashboard
sudo systemctl enable --now tradingsys-engine
sudo systemctl enable --now tradingsys-talk 2>/dev/null || echo "    (talk skipped -- set TELEGRAM_* in .env first)"
echo "    done"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<SUMMARY

  ============================================================
   DEPLOYED
  ============================================================

   Services:
     systemctl status tradingsys-dashboard
     systemctl status tradingsys-engine
     journalctl -u tradingsys-engine -f

   Dashboard (local only for now):
     http://127.0.0.1:8642/

   NEXT -- expose it over HTTPS so you can watch from your phone:
     1. point a domain (or subdomain) at ${IP}
     2. replace the hostname in deploy/Caddyfile
     3. sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
     4. sudo caddy validate --config /etc/caddy/Caddyfile
     5. sudo systemctl reload caddy
     then open https://trading.yourdomain.com

   The engine runs headless and writes state; the dashboard only reads it,
   so closing a browser tab will NOT stop trading.

   IMPORTANT: the engine starts in DRY RUN. Nothing is submitted until you
   connect a venue in the dashboard AND turn off Setup -> Risk -> Dry run.
   ============================================================

SUMMARY
