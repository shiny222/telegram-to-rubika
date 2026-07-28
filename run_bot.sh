#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# Telegram -> Rubika Bridge (Linux version)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "[INFO] No virtual environment found. Creating one..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    echo "[INFO] Installation complete!"
else
    source venv/bin/activate
fi

while true; do
    clear
    echo "Starting Bridge Bot..."

    python bridge.py

    echo ""
    echo "Bot crashed or stopped! Restarting in 10 seconds..."
    sleep 10
done
