#!/usr/bin/env bash
# One-time VPS bootstrap: system packages, a virtualenv, and systemd services.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/tradingsys}"
SERVICE_USER="${SUDO_USER:-$USER}"

echo "==> installing system packages"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip git caddy
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y python3 python3-pip git caddy
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
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "==> installing systemd units"
for unit in tradingsys-dashboard tradingsys-engine tradingsys-talk; do
  sudo sed "s|__APP_DIR__|$APP_DIR|g; s|__SERVICE_USER__|$SERVICE_USER|g" \
      "deploy/${unit}.service" | sudo tee "/etc/systemd/system/${unit}.service" >/dev/null
done
sudo systemctl daemon-reload

echo
echo "done. next:"
echo "  cd $APP_DIR"
echo "  cp .env.example .env && nano .env"
echo "  sudo systemctl enable --now tradingsys-dashboard tradingsys-engine tradingsys-talk"
echo
echo "NOTE: the engine starts in DRY RUN mode. Nothing is submitted until a venue"
echo "      is connected AND the dry-run switch is turned off."
