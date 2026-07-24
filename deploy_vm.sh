#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/deploy/igngbot-v3}"
SERVICE_NAME="${SERVICE_NAME:-igngbot-v3}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
APP_USER="${APP_USER-deploy}"

run_as_app_user() {
  if [ "$(id -un)" = "$APP_USER" ]; then
    "$@"
  else
    sudo -u "$APP_USER" "$@"
  fi
}

cd "$APP_DIR"

SEARXNG_COMPOSE_FILE="$APP_DIR/deploy/searxng/docker-compose.yml"
SEARXNG_IMAGE="${SEARXNG_IMAGE:-dockerproxy.net/searxng/searxng:latest}"
SEARXNG_CONFIG_DIR="$APP_DIR/deploy/searxng/searxng"
SEARXNG_SETTINGS_FILE="$SEARXNG_CONFIG_DIR/settings.yml"
mkdir -p "$SEARXNG_CONFIG_DIR"
sudo chown -R "$APP_USER:$APP_USER" "$SEARXNG_CONFIG_DIR"
if [ ! -f "$SEARXNG_SETTINGS_FILE" ]; then
  SEARXNG_SECRET_KEY="$(openssl rand -hex 32)"
  cat > "$SEARXNG_SETTINGS_FILE" <<EOF
use_default_settings: true

server:
  secret_key: "$SEARXNG_SECRET_KEY"
  limiter: false
  image_proxy: true

search:
  formats: [html, json]

engines:
  - name: bing
    disabled: false
  - name: baidu
    disabled: false
EOF
elif ! grep -q '^  limiter:' "$SEARXNG_SETTINGS_FILE"; then
  printf '\n  limiter: false\n' >> "$SEARXNG_SETTINGS_FILE"
fi
if ! grep -q '^  formats:.*json' "$SEARXNG_SETTINGS_FILE"; then
  cat >> "$SEARXNG_SETTINGS_FILE" <<'EOF'

search:
  formats: [html, json]
EOF
fi
if ! grep -q '^  - name: bing$' "$SEARXNG_SETTINGS_FILE"; then
  cat >> "$SEARXNG_SETTINGS_FILE" <<'EOF'

engines:
  - name: bing
    disabled: false
  - name: baidu
    disabled: false
EOF
fi
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && [ -f "$SEARXNG_COMPOSE_FILE" ]; then
  if sudo SEARXNG_IMAGE="$SEARXNG_IMAGE" docker compose -f "$SEARXNG_COMPOSE_FILE" up -d; then
    echo "SearXNG container is running."
  else
    echo "Warning: failed to start SearXNG; bot deployment will continue." >&2
  fi
else
  echo "Warning: Docker Compose is unavailable; SearXNG was not started." >&2
fi

if [ ! -x ".venv/bin/python" ]; then
  run_as_app_user "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
fi
run_as_app_user "$APP_DIR/.venv/bin/pip" install --upgrade pip
run_as_app_user "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR/.venv"

sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=IGNGbot v3
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
sudo systemctl restart "${SERVICE_NAME}.service"
sudo systemctl status "${SERVICE_NAME}.service" --no-pager
