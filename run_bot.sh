#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    echo "[INFO] No virtual environment found. Creating one..."
    python3 -m venv venv
    venv/bin/pip install -r requirements.txt
    echo "[INFO] Installation complete!"
fi

echo "Starting Bridge Bot..."

exec venv/bin/python bridge.py
