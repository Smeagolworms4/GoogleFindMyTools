FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# -------------------------------------------------------
# Base system + X stack + noVNC + WM + Chromium (NO SNAP)
# -------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg \
    python3 python3-venv python3-pip \
    xvfb x11vnc \
    novnc websockify \
    openbox \
    xdotool \
    fonts-liberation \
    \
    chromium \
    chromium-driver \
    \
    # Chromium runtime deps (safe even if already pulled)
    libnss3 libnspr4 \
    libgbm1 \
    libasound2 \
    libatk-bridge2.0-0 libatk1.0-0 \
    libgtk-3-0 \
    libxss1 \
    libxshmfence1 \
    libdrm2 \
    libu2f-udev \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# -------------------------------------------------------
# App
# -------------------------------------------------------
WORKDIR /app
COPY . /app

# -------------------------------------------------------
# Virtual environment
# -------------------------------------------------------
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir fastapi uvicorn httpx websockets
RUN pip install --no-cache-dir -r /app/requirements.txt

# optional cache dir
ENV XDG_CACHE_HOME=/opt/uc-cache
RUN mkdir -p /opt/uc-cache

# -------------------------------------------------------
# Runtime configuration
# -------------------------------------------------------
ENV GFM_TOOLS_DIR=/app

ENV GFM_SECRETS_PATH=/data/Auth/secrets.json
ENV GFM_AUTH_DONE_MARKER=/data/Auth/.auth_done.json
ENV GFM_AUTH_WAIT_SECONDS=900

ENV DISPLAY=:99
ENV GFM_CHROME_PROFILE_DIR=/data/chrome
ENV GFM_DONT_KILL_CHROME=1

# Container decides which binary is used
# Debian bookworm provides /usr/bin/chromium and /usr/bin/chromedriver
ENV CHROME_BIN=/usr/bin/chromium
ENV GFM_CHROME_BIN=/usr/bin/chromium
ENV GFM_CHROMEDRIVER_PATH=/usr/bin/chromedriver

EXPOSE 8000
RUN chmod +x /app/start.sh
CMD ["/app/start.sh"]
