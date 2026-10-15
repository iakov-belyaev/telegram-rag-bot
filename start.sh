#!/usr/bin/env bash
# telegram-rag-bot launcher: venv check, deps, then the unified engine.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "❌ Error: Virtual environment .venv not found! Create it with: python3.11 -m venv .venv"
    exit 1
fi
source .venv/bin/activate

if [ ! -f ".env" ]; then
    echo "❌ Error: .env file missing! Copy .env.example and fill it in."
    exit 1
fi

echo "🚀 Starting telegram-rag-bot v2 (main.py)..."
exec python main.py
