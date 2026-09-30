#!/bin/bash

# Navigate to project directory
cd "$(dirname "$0")"

# 1. Check if virtual environment exists and activate it
if [ -d ".venv" ]; then
    source .venv/bin/activate
else
    echo "❌ Error: Virtual environment .venv not found!"
    exit 1
fi

# 2. Check if .env file exists
if [ ! -f ".env" ]; then
    echo "❌ Error: .env file missing!"
    exit 1
fi

# 3. Launch the unified engine
echo "🚀 Starting Telegram Engine..."
python start.py