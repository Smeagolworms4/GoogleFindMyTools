FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# -------------------------------------------------------
# Base system + X stack + noVNC + WM + tools
# -------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg \
    python3 python3-venv python3-pip \
    xvfb x11vnc \
    novnc websockify \
    fonts-liberation \
    openbox \
    xdotool \
    && rm -rf /var/lib/apt/lists/*

# -------------------------------------------------------
# Google Chrome
# -------------------------------------------------------
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /etc/apt/keyrings/google.gpg && \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

# -------------------------------------------------------
# App
# -------------------------------------------------------
WORKDIR /app
COPY . /app

# -------------------------------------------------------
# Virtual environment (global)
# -------------------------------------------------------
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir fastapi uvicorn httpx websockets
RUN pip install --no-cache-dir -r /app/requirements.txt

# -------------------------------------------------------
# Pre-download undetected_chromedriver at build time
# -------------------------------------------------------
ENV XDG_CACHE_HOME=/opt/uc-cache
RUN mkdir -p /opt/uc-cache && \
    python - <<EOF
import undetected_chromedriver as uc
driver = uc.Chrome(headless=True)
driver.quit()
print("Chromedriver pre-installed")
EOF

# -------------------------------------------------------
# Runtime configuration
# -------------------------------------------------------

# Repo
ENV GFM_TOOLS_DIR=/app

# Auth persistence
ENV GFM_SECRETS_PATH=/data/Auth/secrets.json
ENV GFM_AUTH_DONE_MARKER=/data/Auth/.auth_done.json
ENV GFM_AUTH_WAIT_SECONDS=900

# Display
ENV DISPLAY=:99

# Chrome profile persistence
ENV GFM_CHROME_PROFILE_DIR=/data/chrome

# Avoid killing chrome between flows
ENV GFM_DONT_KILL_CHROME=1

# Use preinstalled driver
ENV GFM_CHROMEDRIVER_PATH=/opt/uc-cache/undetected_chromedriver

# -------------------------------------------------------
EXPOSE 8000

RUN chmod +x /app/start.sh

CMD ["/app/start.sh"]
