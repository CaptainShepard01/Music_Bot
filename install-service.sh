#!/usr/bin/env bash
set -euo pipefail

UNIT=music-bot.service
DEST=/etc/systemd/system/$UNIT

export BOT_USER=$(whoami)
export BOT_DIR=$(realpath "$(dirname "$0")")
export BOT_PYTHON="$BOT_DIR/.venv/bin/python"

if [ ! -f "$BOT_PYTHON" ]; then
    echo "Error: virtual environment not found at $BOT_PYTHON"
    echo "Run: python -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

if [ ! -f "$BOT_DIR/.env" ]; then
    echo "Error: .env file not found. Copy .env.example to .env and add your token."
    exit 1
fi

envsubst < "$BOT_DIR/$UNIT" | sudo tee "$DEST" > /dev/null
sudo systemctl daemon-reload

echo "Installed $DEST"
echo ""
echo "  Start now:        sudo systemctl start music-bot"
echo "  Enable on boot:   sudo systemctl enable music-bot"
echo "  Follow logs:      journalctl -u music-bot -f"
