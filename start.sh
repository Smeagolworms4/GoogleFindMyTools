#!/bin/bash
set -e

echo "Starting Google Find My UI..."

# Activate global venv
export PATH="/opt/venv/bin:$PATH"

# Ensure data folder exists
mkdir -p /data/Auth
mkdir -p /data/chrome


# Copy the py code
cp -rf /app/Auth/* /data/Auth/ || true
cp -rf /app/Auth/.* /data/Auth/ 2>/dev/null || true

mv /app/Auth /app/Auth-old
ln -sfn /data/Auth /app/Auth

# Export chrome profile location for the python code
export GFM_CHROME_PROFILE_DIR="/data/chrome"
export DISPLAY=${DISPLAY:-:99}

echo "Python version:"
python --version

echo "Launching server..."
exec uvicorn server_web:app --host 0.0.0.0 --port 8000
