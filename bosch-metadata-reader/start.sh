#!/bin/bash
# start.sh
# Opens two separate Terminal windows:
#   1. uvicorn server:app --reload
#   2. python3 run_all_cameras.py (which opens one window per camera)

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(which python3)"

osascript -e "tell application \"Terminal\" to do script \"cd '$DIR' && uvicorn server:app --reload\""
osascript -e "tell application \"Terminal\" to do script \"cd '$DIR' && $PYTHON run_all_cameras.py\""
