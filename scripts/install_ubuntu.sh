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
    echo "    created .env (next: python -m tgclone setup)"
else
    echo "    .env already exists, not touched"
fi
mkdir -p data

if [ "${1:-}" = "--service" ] && [ ! -d /run/systemd/system ]; then
    echo "==> 4/4 systemd is not running here (WSL or container) - service skipped"
    if grep -qi microsoft /proc/version 2>/dev/null; then
        echo "    WSL: enable systemd once, then re-run this script with --service:"
        echo "      printf '[boot]\\nsystemd=true\\n' | sudo tee /etc/wsl.conf"
        echo "      (in Windows PowerShell)  wsl --shutdown   -> reopen Ubuntu"
    fi
    echo "    Or run in background without systemd:"
    echo "      tmux new -s tgclone '.venv/bin/python -m tgclone live'   (detach: Ctrl+B then D)"
elif [ "${1:-}" = "--service" ]; then
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
echo "Done. Next (only the owner does this, once):"
echo "  1) .venv/bin/python -m tgclone setup   # answers 4 questions, writes .env"
echo "  2) .venv/bin/python -m tgclone live    # or: sudo systemctl start tgclone"
echo "  3) Open your bot in Telegram -> /start -> 🔐 Login (everyone logs in inside the bot)"
