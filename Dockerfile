# amd64 recommandé (Chrome + auth)
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Europe/Paris \
    DISPLAY=:0 \
    VNC_PORT=5900 \
    NOVNC_PORT=6080 \
    VNC_PW=changeme

# Base + UI minimal + VNC/noVNC + dépendances Chrome
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg git \
    python3 python3-pip python3-venv \
    xvfb fluxbox xterm \
    x11vnc novnc websockify \
    fonts-liberation fonts-dejavu-core \
    libnss3 libxss1 libasound2t64 libgbm1 \
    libgtk-3-0 libu2f-udev \
    supervisor \
  && rm -rf /var/lib/apt/lists/*

# Google Chrome (repo officiel)
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /etc/apt/keyrings/google.gpg && \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone tools (pinne un commit si tu veux de la stabilité)
RUN git clone https://github.com/Smeagolworms4/GoogleFindMyTools.git /app/GoogleFindMyTools

WORKDIR /app/GoogleFindMyTools
# Dépendances utiles si certaines wheels doivent compiler
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential python3-dev libssl-dev libffi-dev pkg-config \
  && rm -rf /var/lib/apt/lists/*

# Création du virtualenv comme dans le README
RUN python3 -m venv venv

# Upgrade pip dans le venv
RUN ./venv/bin/pip install --upgrade pip setuptools wheel

# Install requirements dans le venv
RUN ./venv/bin/pip install -r requirements.txt

# Fichiers runtime
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Persistance (HA addon: /data ; sinon volume docker)
VOLUME ["/data"]
EXPOSE 6080 5900

CMD ["/start.sh"]
