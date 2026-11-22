#!/bin/bash
# Usage: ./run.sh [--test] [--headed] [--debug] [--traces] [--learn] [--notify-test] [--keep-scanning]
# Headless by default; all runtime flags are CLI args.

if [ ! -d "venv" ]; then
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    playwright install chromium
else
    source venv/bin/activate
fi

python main.py "$@"