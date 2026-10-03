#!/usr/bin/env bash
# Deploy / operate the IGNGbot NAS compose stack.
#
# The NAS cannot build the Python image reliably (no outbound access to
# deb.debian.org or pypi.org), and the workstation has no Docker daemon, so the
# bot image is built on the build host (the Ubuntu VM) and shipped as a
# compressed archive. The media service is plain Node with no dependencies and
# is small enough to build on the NAS itself.
#
# Usage:
#   ./deploy-nas.sh image     build the bot image on the build host, import on NAS
#   ./deploy-nas.sh sync      sync compose files + vendored media service to the NAS
#   ./deploy-nas.sh up        sync, import image, and start the stack
#   ./deploy-nas.sh status    show service status and recent logs
#   ./deploy-nas.sh down      stop the stack (data and volumes are preserved)
#   ./deploy-nas.sh logs      follow bot logs
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${COMPOSE_DIR:-$REPO_DIR/deploy/docker}"
SITE_DIR="${SITE_DIR:-$HOME/项目/IGNG站点}"
NAS_TARGET="${NAS_TARGET:-nas}"
BUILD_HOST="${BUILD_HOST:-ubuntu-vm}"
BUILD_DIR="${BUILD_DIR:-/home/deploy/igngbot-v3-build}"
NAS_ROOT="${NAS_ROOT:-/vol2/1000/Docker/igngbot}"
BOT_IMAGE="${BOT_IMAGE:-igngbot-v3:local}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

nas() { ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$NAS_TARGET" "$@"; }
build() { ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$BUILD_HOST" "$@"; }

require_env() {
  [ -f "$COMPOSE_DIR/.env" ] || die "$COMPOSE_DIR/.env is missing; copy .env.example and fill it in"
}

cmd_sync() {
  require_env
  log "Ensuring remote directory $NAS_ROOT"
  nas "mkdir -p '$NAS_ROOT/media' '$NAS_ROOT/bot-runtime' '$NAS_ROOT/models'"

  log "Syncing compose project files"
  scp -q -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "$COMPOSE_DIR/docker-compose.yml" \
    "$COMPOSE_DIR/.env" \
    "$NAS_TARGET:$NAS_ROOT/"

  log "Vendoring media service from $SITE_DIR"
  local media_src="$SITE_DIR/services/igng-bot-media"
  [ -f "$media_src/server.js" ] || die "media service source not found at $media_src"
  scp -q -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "$media_src/Dockerfile" \
    "$media_src/Dockerfile.frpc" \
    "$media_src/package.json" \
    "$media_src/server.js" \
    "$media_src/.dockerignore" \
    "$NAS_TARGET:$NAS_ROOT/media/"

  # The upstream Dockerfile references the bare node image, which the NAS
  # registry mirror cannot resolve. Rewrite only that FROM line on the NAS copy;
  # the site repository stays the untouched source of truth.
  nas "set -eu
    f='$NAS_ROOT/media/Dockerfile'
    sed -i 's|^FROM node:|FROM dockerproxy.net/library/node:|' \"\$f\"
    grep -q '^FROM dockerproxy.net/library/node:' \"\$f\""

  # frpc binary and tunnel config come from the NAS itself: the config already
  # exists there and embeds the tunnel auth token, so it is never copied around.
  log "Refreshing frpc binary and tunnel config"
  nas "set -eu
    src=/fs/1000/ftp/IGNGcloud/.service-bot
    test -f \"\$src/frpc.toml\" || { echo 'missing frpc.toml at' \"\$src\" >&2; exit 1; }
    cp \"\$src/frpc.toml\" '$NAS_ROOT/frpc.toml'
    chmod 600 '$NAS_ROOT/frpc.toml'
    if [ -x /var/apps/frpc/target/app/frpc ]; then
      cp /var/apps/frpc/target/app/frpc '$NAS_ROOT/media/frpc'
      chmod 755 '$NAS_ROOT/media/frpc'
    else
      echo 'frpc binary not found; reusing existing copy if present' >&2
      test -x '$NAS_ROOT/media/frpc'
    fi"

  log "Sync complete"
}

cmd_image() {
  log "Syncing source tree to build host $BUILD_HOST:$BUILD_DIR"
  build "mkdir -p '$BUILD_DIR'"
  # Only the files the image needs are copied; runtime data, secrets and git
  # metadata stay behind so no credential can leak into the build context.
  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude 'runtime' --exclude '__pycache__' \
    --exclude 'node_modules' --exclude '*.pyc' --exclude '.pytest_cache' --exclude 'secrets' --exclude '.env*' \
    --exclude '_upstream_astrbot' --exclude '老程序*' --exclude 'tests' \
    "$REPO_DIR/" "$BUILD_HOST:$BUILD_DIR/"

  log "Building $BOT_IMAGE on $BUILD_HOST"
  build "cd '$BUILD_DIR' && docker build --platform linux/amd64 -f deploy/docker/Dockerfile -t '$BOT_IMAGE' ."

  log "Streaming image to the NAS (compressed)"
  ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$BUILD_HOST" \
    "docker save '$BOT_IMAGE' | zstd -3 -T0" \
    | nas "cat > '$NAS_ROOT/.bot-image.tar.zst'"

  log "Loading image on the NAS"
  nas "zstd -d -c '$NAS_ROOT/.bot-image.tar.zst' | docker load && rm -f '$NAS_ROOT/.bot-image.tar.zst'"
}

cmd_up() {
  cmd_sync
  cmd_image
  log "Starting stack"
  nas "cd '$NAS_ROOT' && docker compose up -d --build"
  cmd_status
}

cmd_status() {
  nas "cd '$NAS_ROOT' && docker compose ps"
  echo
  log "Recent bot logs"
  nas "cd '$NAS_ROOT' && docker compose logs --tail=30 bot" || true
}

cmd_down() {
  log "Stopping stack (volumes and data are preserved)"
  nas "cd '$NAS_ROOT' && docker compose down"
}

cmd_logs() {
  nas "cd '$NAS_ROOT' && docker compose logs -f --tail=100 bot"
}

case "${1:-}" in
  image)  cmd_image ;;
  sync)   cmd_sync ;;
  up)     cmd_up ;;
  status) cmd_status ;;
  down)   cmd_down ;;
  logs)   cmd_logs ;;
  *)      sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
