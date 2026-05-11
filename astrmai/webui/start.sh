#!/bin/bash

cd "$(dirname "$0")"

echo "[AstrMai WebUI] Checking Python installation..."
if ! command -v python3 &> /dev/null; then
    echo "python3 could not be found. Please install Python 3."
    exit 1
fi

if [ ! -d "venv" ]; then
    echo "[AstrMai WebUI] Creating virtual environment..."
    python3 -m venv venv
fi

echo "[AstrMai WebUI] Activating virtual environment..."
source venv/bin/activate

echo "[AstrMai WebUI] Installing dependencies..."
pip install -r requirements.txt -q

# Load environment variables from .env if exists
if [ -f .env ]; then
    export $(cat .env | xargs)
fi

echo "[AstrMai WebUI] Starting AstrMai WebUI server..."
uvicorn backend.server:app --host 0.0.0.0 --port 8765 --workers 1
