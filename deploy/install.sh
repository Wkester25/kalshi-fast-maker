#!/usr/bin/env bash
# Install Fast Maker as a service on a Linux server (Debian/Ubuntu/Fedora/Arch...).
#   sudo bash deploy/install.sh         install to /opt/fastmaker and start it
#   sudo bash deploy/install.sh --port 9000
set -euo pipefail

PORT=8080
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/fastmaker
SVC_USER=fastmaker

echo "==> Python"
if ! command -v python3 >/dev/null; then
  if command -v apt-get >/dev/null; then apt-get update -q && apt-get install -yq python3 python3-venv
  elif command -v dnf >/dev/null; then dnf install -yq python3
  else echo "Install Python 3.10+ first."; exit 1; fi
fi
python3 - <<'EOF' || { echo "Python 3.10 or newer is needed."; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF
python3 -m venv --help >/dev/null 2>&1 || { command -v apt-get >/dev/null && apt-get install -yq python3-venv; }

echo "==> Files to $DEST"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --home "$DEST" --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$DEST"
# copy the program; never overwrite settings, keys or data that are already there
tar -C "$SRC" --exclude=./config.json --exclude=./secrets --exclude=./data --exclude=./.venv \
    --exclude='__pycache__' -cf - . | tar -C "$DEST" -xf -
mkdir -p "$DEST/secrets" "$DEST/data"
chmod 700 "$DEST/secrets"

echo "==> Python packages"
[[ -d "$DEST/.venv" ]] || python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q --upgrade pip
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"
chown -R "$SVC_USER:$SVC_USER" "$DEST"

if [[ ! -f "$DEST/config.json" ]] || ! grep -q '"password_hash": "[0-9a-f]' "$DEST/config.json"; then
  echo "==> Dashboard password"
  sudo -u "$SVC_USER" "$DEST/.venv/bin/python" "$DEST/run.py" --set-password --port "$PORT"
else
  sudo -u "$SVC_USER" "$DEST/.venv/bin/python" - "$PORT" <<EOF
import sys; sys.path.insert(0, "$DEST")
from fastmaker import config
c = config.load(); c["web"]["port"] = int(sys.argv[1]); config.save(c)
EOF
fi

echo "==> Service"
sed "s#/opt/fastmaker#$DEST#g; s#^User=.*#User=$SVC_USER#" "$SRC/deploy/fastmaker.service" > /etc/systemd/system/fastmaker.service
systemctl daemon-reload
systemctl enable --now fastmaker
systemctl restart fastmaker
sleep 2
systemctl --no-pager --lines=5 status fastmaker || true

# make the clock right: decisions are timed to the window
if command -v timedatectl >/dev/null; then timedatectl set-ntp true 2>/dev/null || true; fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<EOF

Done. Open  http://${IP:-<server-ip>}:${PORT}  and add your Kalshi API key on the "API key" page.

  Logs:      journalctl -u fastmaker -f
  Restart:   sudo systemctl restart fastmaker
  Password:  sudo -u $SVC_USER $DEST/.venv/bin/python $DEST/run.py --set-password

The dashboard is plain HTTP. If the server is on the open internet, either firewall the port to your
own IP, or bind it locally and use an SSH tunnel (see README "Security").
EOF
