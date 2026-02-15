#!/bin/bash
set -e

echo "Starting Google Find My UI..."

# Activate global venv
export PATH="/opt/venv/bin:$PATH"

# Ensure data folder exists
AUTH_DIR="${GFM_DATA_PATH:-/data}/Auth"

mkdir -p $AUTH_DIR
mkdir -p ${GFM_CHROME_PROFILE_DIR:-/data/chrome}


# Copy the py code
cp -rf /app/Auth/* $AUTH_DIR/ || true
cp -rf /app/Auth/.* $AUTH_DIR/ 2>/dev/null || true

mv /app/Auth /app/Auth-old
ln -sfn $AUTH_DIR /app/Auth

# Export chrome profile location for the python code
export GFM_CHROME_PROFILE_DIR="/data/chrome"
export DISPLAY=${DISPLAY:-:99}

echo "Python version:"
python --version

echo "Launching server..."
exec uvicorn server_web:app --host 0.0.0.0 --port 8000
