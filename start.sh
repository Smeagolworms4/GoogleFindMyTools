#!/bin/bash
set -e

echo "Starting Google Find My UI..."

# Activate global venv
export PATH="/opt/venv/bin:$PATH"

# Ensure data folder exists
mkdir -p /data/Auth

echo "Python version:"
python --version

echo "Launching server..."
exec uvicorn server_web:app --host 0.0.0.0 --port 8000
