#!/usr/bin/env bash
# tgclone installer for Ubuntu 22.04 / 24.04 (also Debian 12).
#   bash scripts/install_ubuntu.sh            -> install packages + venv
#   bash scripts/install_ubuntu.sh --service  -> also install a systemd service (24x7, auto-restart)
set -euo pipefail

cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

echo "==> 1/4 System packages (python, ffmpeg, tesseract)"
$SUDO apt-get update -y
$SUDO apt-get install -y python3 python3-venv python3-pip git ffmpeg \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin

echo "==> 2/4 Python virtual environment (.venv)"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

echo "==> 3/4 Config files"
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "    created .env (fill API_ID, API_HASH, BOT_TOKEN, ADMINS)"
else
    echo "    .env already exists, not touched"
fi
mkdir -p data

if [ "${1:-}" = "--service" ]; then
    echo "==> 4/4 systemd service (user: $RUN_USER, dir: $APP_DIR)"
    $SUDO tee /etc/systemd/system/tgclone.service >/dev/null <<UNIT
[Unit]
Description=tgclone Telegram cloner
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m tgclone live
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable tgclone >/dev/null
    echo "    service installed. Start it AFTER .env is complete:  sudo systemctl start tgclone"
else
    echo "==> 4/4 skipped service (run again with --service to run 24x7)"
fi

echo
echo "Done. Next:"
echo "  1) nano .env                      # API_ID, API_HASH, BOT_TOKEN, ADMINS"
echo "  2) .venv/bin/python -m tgclone login   # copy SESSION_STRING into .env"
echo "  3) .venv/bin/python -m tgclone live    # then send /start to your bot"
