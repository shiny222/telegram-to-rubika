#!/usr/bin/env bash
# Rubika Bridge - Update Script (Linux)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "[$(date)] Checking for updates..."
git pull

echo ""
echo "✅ Update complete!"
