#!/usr/bin/env bash
# One-time VPS bootstrap: system packages, a virtualenv, and systemd services.
#
# Works on x86_64 and aarch64 (Oracle Cloud Always Free runs Ampere ARM).
# The only hard platform requirement is Linux -- the Exness/MT5 broker is
# Windows-only and is simply unavailable on a server, which is expected.

set -euo pipefail

APP_DIR="${APP_DIR:-/srv/tradingsys}"
SERVICE_USER="${SUDO_USER:-$USER}"

echo "==> platform"
UNAME_M="$(uname -m)"
case "$UNAME_M" in
  aarch64|arm64) echo "    arch: aarch64 (Ampere ARM -- Oracle Always Free)" ;;
  x86_64|amd64)  echo "    arch: x86_64" ;;
  *)             echo "    arch: $UNAME_M (untested, continuing)" ;;
esac
echo "    python: $(python3 --version 2>&1)"

echo "==> installing system packages"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip git curl
  # caddy is optional; skip it if your distro does not carry it
  sudo apt-get install -y caddy || echo "    (caddy not installed -- see deploy/README.md)"
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y python3 python3-pip git curl caddy
else
  echo "!! unsupported distro - install python3, venv, git and caddy yourself"
  exit 1
fi

echo "==> creating ${APP_DIR}"
sudo mkdir -p "$APP_DIR"
sudo chown "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

echo "==> installing python dependencies"
cd "$APP_DIR"
python3 -m venv .venv
# pandas and numpy both publish aarch64 wheels, so this compiles nothing.
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "==> installing systemd units"
for unit in tradingsys-dashboard tradingsys-engine tradingsys-talk; do
  if [ ! -f "deploy/${unit}.service" ]; then
    echo "    (missing deploy/${unit}.service, skipping)"
    continue
  fi
  sudo sed "s|__APP_DIR__|$APP_DIR|g; s|__SERVICE_USER__|$SERVICE_USER|g" \
      "deploy/${unit}.service" | sudo tee "/etc/systemd/system/${unit}.service" >/dev/null
done
sudo systemctl daemon-reload

echo
echo "done. next:"
echo "  cd $APP_DIR"
echo "  cp .env.example .env && nano .env"
echo "  sudo systemctl enable --now tradingsys-dashboard"
echo "  sudo systemctl enable --now tradingsys-engine"
echo "  sudo systemctl enable --now tradingsys-talk"
echo
echo "NOTE: the engine starts in DRY RUN mode. Nothing is submitted until a venue"
echo "      is connected AND the dry-run switch is turned off."
echo "NOTE: the Exness broker needs Windows (MetaTrader5). On a server, use"
echo "      paper, Binance, Alpaca, or the generic webhook bridge."
