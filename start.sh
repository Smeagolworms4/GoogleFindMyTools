#!/usr/bin/env bash
set -euo pipefail

# Persistance : on stocke Auth/ dans /data/Auth (volume)
mkdir -p /data/Auth

# Si Auth/ existe déjà dans /app, on copie vers /data (1ère exécution)
if [ -d "/app/GoogleFindMyTools/Auth" ] && [ ! -L "/app/GoogleFindMyTools/Auth" ]; then
  # Si /data/Auth est vide, copie; sinon on garde /data
  if [ -z "$(ls -A /data/Auth 2>/dev/null || true)" ]; then
    cp -a /app/GoogleFindMyTools/Auth/. /data/Auth/ || true
  fi
  rm -rf /app/GoogleFindMyTools/Auth
fi

# Symlink vers le volume persistant
ln -sfn /data/Auth /app/GoogleFindMyTools/Auth

echo "Auth dir -> /data/Auth"
echo "noVNC: http://<host>:6080/vnc.html"
echo "VNC:   <host>:5900  (password: ${VNC_PW})"
echo "secrets.json will be at: /data/Auth/secrets.json"

exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
