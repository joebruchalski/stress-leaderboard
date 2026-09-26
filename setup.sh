#!/usr/bin/env bash
# Sets up the Python environment and launches the Streamlit dashboard.
# Safe to re-run: skips creating the venv if it already exists.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -d .venv ]; then
    echo "Creating virtual environment (.venv)..."
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

echo ""
echo "Setup complete. Launching the dashboard..."
echo "(First run will prompt for your Garmin Connect login and calendar file/URL.)"
echo "Next time, skip setup with: source .venv/bin/activate && streamlit run dashboard.py"
echo ""

streamlit run dashboard.py
