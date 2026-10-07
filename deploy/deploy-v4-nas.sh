#!/usr/bin/env bash
# Authorized operator commands only. This script does not run during tests or normal development.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${COMPOSE_DIR:-$REPO_DIR/deploy/docker}"
NAS_TARGET="${NAS_TARGET:-nas}"
BUILD_HOST="${BUILD_HOST:-ubuntu-vm}"
BUILD_DIR="${BUILD_DIR:-/home/deploy/igngbot-v4-build}"
NAS_ROOT="${NAS_ROOT:-/vol2/1000/Docker/igngbot}"
V4_VERSION="${V4_VERSION:-$(git -C "$REPO_DIR" rev-parse --short=12 HEAD)}"
BOT_V4_IMAGE="${BOT_V4_IMAGE:-igngbot-v4:$V4_VERSION}"
BROKER_V4_IMAGE="${BROKER_V4_IMAGE:-igngbot-broker:$V4_VERSION}"
YUNYING_IMAGE="${YUNYING_IMAGE:-igngbot-yunying:$V4_VERSION}"
export COMPOSE_DIR BUILD_HOST BUILD_DIR NAS_ROOT NAS_TARGET BOT_V4_IMAGE BROKER_V4_IMAGE YUNYING_IMAGE
nas() { ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$NAS_TARGET" "$@"; }
build() { ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$BUILD_HOST" "$@"; }
compose="docker compose --env-file .env --env-file .env.v4 -f docker-compose.yml -f docker-compose.v4.yml"
require_config() {
  test -f "$COMPOSE_DIR/.env" && test -f "$COMPOSE_DIR/.env.v4" || { echo 'Existing .env and configured .env.v4 are required' >&2; exit 1; }
  grep -Eq '^YUNYING_BROKER_SECRET=.{32,}$' "$COMPOSE_DIR/.env.v4" || { echo 'YUNYING_BROKER_SECRET (at least 32 characters) is required in .env.v4' >&2; exit 1; }
  case "$BOT_V4_IMAGE $BROKER_V4_IMAGE $YUNYING_IMAGE" in *:local*|*:latest*) echo 'Use fixed source-version image tags' >&2; exit 1;; esac
}
sync_config() {
  require_config
  nas "mkdir -p '$NAS_ROOT/dsh-runtime' '$NAS_ROOT/v4-backups' '$NAS_ROOT/broker-work'; chmod 700 '$NAS_ROOT/v4-backups'; chown 1000:1001 '$NAS_ROOT/dsh-runtime' '$NAS_ROOT/broker-work'"
  nas "if [ ! -s '$NAS_ROOT/.v4-rollback-image' ]; then docker inspect --format '{{.Config.Image}}' \$(cd '$NAS_ROOT' && docker compose ps -q bot) > '$NAS_ROOT/.v4-rollback-image'; test -s '$NAS_ROOT/.v4-rollback-image'; cp '$NAS_ROOT/docker-compose.yml' '$NAS_ROOT/v4-backups/docker-compose.v3.yml'; cp '$NAS_ROOT/.env' '$NAS_ROOT/v4-backups/.env.v3'; chmod 600 '$NAS_ROOT/v4-backups/.env.v3'; fi"
  scp -q -o BatchMode=yes -o StrictHostKeyChecking=yes "$COMPOSE_DIR/docker-compose.yml" "$COMPOSE_DIR/docker-compose.v4.yml" "$NAS_TARGET:$NAS_ROOT/"
  local staged_env
  staged_env="$(mktemp)"
  chmod 600 "$staged_env"
  sed '/^BOT_V4_IMAGE=/d; /^BROKER_V4_IMAGE=/d; /^YUNYING_IMAGE=/d' "$COMPOSE_DIR/.env.v4" > "$staged_env"
  printf '\nBOT_V4_IMAGE=%s\nBROKER_V4_IMAGE=%s\nYUNYING_IMAGE=%s\n' "$BOT_V4_IMAGE" "$BROKER_V4_IMAGE" "$YUNYING_IMAGE" >> "$staged_env"
  scp -q -o BatchMode=yes -o StrictHostKeyChecking=yes "$staged_env" "$NAS_TARGET:$NAS_ROOT/.env.v4"
  local env_archive
  env_archive="${XDG_CACHE_HOME:-$HOME/.cache}/igngbot-deploy-private"
  mkdir -p "$env_archive"
  chmod 700 "$env_archive"
  mv "$staged_env" "$env_archive/env-$(date -u +%Y%m%dT%H%M%SZ)-$V4_VERSION"
  nas "chmod 600 '$NAS_ROOT/.env.v4'"
}
images() {
  require_config
  BOT_IMAGE="$BOT_V4_IMAGE" bash "$REPO_DIR/deploy/deploy-nas.sh" image
  build "cd '$BUILD_DIR' && docker build --platform linux/amd64 -f deploy/docker/Dockerfile.broker -t '$BROKER_V4_IMAGE' ."
  ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$BUILD_HOST" "docker save '$BROKER_V4_IMAGE' | zstd -3 -T0" | nas "zstd -d -c | docker load"
  build "cd '$BUILD_DIR' && docker build --platform linux/amd64 -f deploy/docker/Dockerfile.dsh -t '$YUNYING_IMAGE' ."
  ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$BUILD_HOST" "docker save '$YUNYING_IMAGE' | zstd -3 -T0" | nas "zstd -d -c | docker load"
}
up() {
  sync_config
  images
  # Stop the sole legacy consumer before schema migration and V4 startup.
  nas "cd '$NAS_ROOT' && $compose stop bot yunying broker"
  nas "cd '$NAS_ROOT' && BOT_V4_IMAGE='$BOT_V4_IMAGE' BROKER_V4_IMAGE='$BROKER_V4_IMAGE' YUNYING_IMAGE='$YUNYING_IMAGE' $compose run --rm --no-deps --entrypoint python bot -m igngbot_v4.migrate"
  nas "cd '$NAS_ROOT' && BOT_V4_IMAGE='$BOT_V4_IMAGE' BROKER_V4_IMAGE='$BROKER_V4_IMAGE' YUNYING_IMAGE='$YUNYING_IMAGE' $compose up -d --no-build bot broker yunying"
  nas "cd '$NAS_ROOT' && $compose up -d --wait --wait-timeout 180 --no-build bot broker yunying && $compose ps"
}
rollback() {
  # Restore retired legacy contracts with the reviewed V4 image before V3 starts.
  # Official DSH data and current message/Memory records remain intact.
  nas "cd '$NAS_ROOT' && $compose stop bot yunying broker"
  nas "cd '$NAS_ROOT' && $compose run -T --rm --no-deps --entrypoint python bot -c 'from igngbot_v4.migrate import connect; from igngbot_v4.retire import Retirement, exists; c=connect(); print(Retirement(c).restore(apply=True) if exists(c, \"yunying_legacy_tables\") else \"No retirement archive\"); c.close()'"
  nas "cd '$NAS_ROOT' && test -s .v4-rollback-image && $compose stop bot yunying && cp v4-backups/docker-compose.v3.yml docker-compose.yml && BOT_IMAGE=\$(cat .v4-rollback-image) docker compose --env-file .env -f docker-compose.yml up -d --no-build bot && docker compose --env-file .env -f docker-compose.yml ps bot"
}
case "${1:-}" in
  sync) sync_config;;
  images) images;;
  up) up;;
  status) nas "cd '$NAS_ROOT' && $compose ps";;
  logs) nas "cd '$NAS_ROOT' && $compose logs -f --tail=100 bot broker yunying";;
  rollback) rollback;;
  *) echo 'Usage: deploy-v4-nas.sh sync|images|up|status|logs|rollback' >&2; exit 1;;
esac
