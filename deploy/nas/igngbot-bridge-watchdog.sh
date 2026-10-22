#!/bin/sh
# IGNGbot QQ <-> OneBot bridge watchdog for the NAS host.
#
# The failure this guards against is not a dead process: QQ and SnowLuma can
# both be RUNNING while SnowLuma keeps a stale "logged out" hook state and the
# OneBot listeners (3000/3001) never come up.  Docker's restart policy cannot
# see that, and a blind container restart re-creates the same startup race.
#
# The recovery therefore mirrors the verified operator sequence:
#   stop the hook runtime -> restart QQ -> wait for auto-login -> start the
#   login-gated SnowLuma wrapper -> wait for OneBot.
#
# Usage: igngbot-bridge-watchdog.sh [--check|--dry-run|--force-recover]
# Cron:  */2 * * * * .../igngbot-bridge-watchdog.sh >> .../cron.log 2>&1
set -u

PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
NAS_ROOT=${NAS_ROOT:-/vol2/1000/Docker/igngbot}
NAPCAT_CONTAINER=${NAPCAT_CONTAINER:-igngbot-napcat}
BOT_CONTAINER=${BOT_CONTAINER:-igngbot-v3}
PROBE=${PROBE:-$NAS_ROOT/watchdog/pt-login-probe.py}
STATE_DIR=${STATE_DIR:-$NAS_ROOT/watchdog/state}
FAIL_FILE=$STATE_DIR/consecutive_failures
COOLDOWN_FILE=$STATE_DIR/last_recovery
FAIL_THRESHOLD=${FAIL_THRESHOLD:-3}
COOLDOWN_SECONDS=${COOLDOWN_SECONDS:-1800}
RECOVERY_WAIT_SECONDS=${RECOVERY_WAIT_SECONDS:-240}

log() {
    printf '%s [watchdog] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

env_value() {
    key=$1
    value=$(sed -n "s/^${key}=//p" "$NAS_ROOT/.env" 2>/dev/null | tail -n 1 | tr -d '\r')
    case "$value" in
        \"*\") value=${value#\"}; value=${value%\"} ;;
        \'*\') value=${value#\'}; value=${value%\'} ;;
    esac
    printf '%s' "$value"
}

HTTP_PORT=$(env_value ONEBOT_HTTP_HOST_PORT)
HTTP_PORT=${HTTP_PORT:-3200}
TOKEN=$(env_value ONEBOT_HTTP_TOKEN)
NTFY_URL=$(env_value NTFY_REPORT_URL)
NTFY_TOKEN=$(env_value NTFY_REPORT_TOKEN)

notify() {
    message=$1
    [ -n "$NTFY_URL" ] || return 0
    if [ -n "$NTFY_TOKEN" ]; then
        curl -fsS -m 10 -H "Authorization: Bearer $NTFY_TOKEN" \
            -H "Title: IGNGbot bridge" -d "$message" "$NTFY_URL" >/dev/null 2>&1 \
            || log "ntfy delivery failed"
    else
        curl -fsS -m 10 -H "Title: IGNGbot bridge" -d "$message" "$NTFY_URL" \
            >/dev/null 2>&1 || log "ntfy delivery failed"
    fi
}

onebot_healthy() {
    if [ -n "$TOKEN" ]; then
        body=$(curl -fsS -m 5 -H "Authorization: Bearer $TOKEN" \
            "http://127.0.0.1:$HTTP_PORT/get_status" 2>/dev/null) || return 1
    else
        body=$(curl -fsS -m 5 "http://127.0.0.1:$HTTP_PORT/get_status" 2>/dev/null) || return 1
    fi
    printf '%s' "$body" | grep -Eq '"status" *: *"ok"' || return 1
    printf '%s' "$body" | grep -Eq '"online" *: *true' || return 1
    printf '%s' "$body" | grep -Eq '"good" *: *true' || return 1
}

qq_logged_in() {
    [ -r "$PROBE" ] || return 1
    docker exec -i "$NAPCAT_CONTAINER" python3 - <"$PROBE" >/dev/null 2>&1
}

bot_connected_state() {
    body=$(docker exec "$BOT_CONTAINER" python3 -c \
        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8788/health', timeout=3).read().decode())" \
        2>/dev/null || true)
    if printf '%s' "$body" | grep -Eq '"connected" *: *true'; then
        printf 'connected'
    elif [ -n "$body" ]; then
        printf 'disconnected'
    else
        printf 'unavailable'
    fi
}

read_int() {
    value=$(cat "$1" 2>/dev/null || true)
    case "$value" in
        ''|*[!0-9]*) printf '0' ;;
        *) printf '%s' "$value" ;;
    esac
}

write_int() {
    printf '%s\n' "$2" >"$1" 2>/dev/null || true
}

recover() {
    fails=$1
    now=$(date +%s)
    write_int "$COOLDOWN_FILE" "$now"
    if qq_logged_in; then qq_state=online; else qq_state=needs-login; fi
    log "recovering: consecutive=$fails qq=$qq_state bot=$(bot_connected_state)"
    notify "OneBot 3001 不可达 ${fails} 次，QQ=${qq_state}，开始有序恢复"

    docker exec "$NAPCAT_CONTAINER" supervisorctl stop snowluma >/dev/null 2>&1 \
        || log "stop snowluma failed"
    docker exec "$NAPCAT_CONTAINER" supervisorctl restart qq >/dev/null 2>&1 \
        || log "restart qq failed"
    docker exec "$NAPCAT_CONTAINER" supervisorctl start snowluma >/dev/null 2>&1 \
        || log "start snowluma failed"

    elapsed=0
    while [ "$elapsed" -lt "$RECOVERY_WAIT_SECONDS" ]; do
        sleep 2
        elapsed=$((elapsed + 2))
        if onebot_healthy; then
            write_int "$FAIL_FILE" 0
            log "recovered after ${elapsed}s; bot=$(bot_connected_state)"
            notify "OneBot 已恢复（约 ${elapsed}s）；bot 最多 5 分钟后重连"
            exit 0
        fi
    done

    if qq_logged_in; then qq_state=online; else qq_state=needs-login; fi
    log "recovery failed within ${RECOVERY_WAIT_SECONDS}s; qq=$qq_state"
    notify "OneBot 恢复失败：${RECOVERY_WAIT_SECONDS}s 内未监听。QQ=${qq_state}。请检查 VNC/扫码。"
    exit 1
}

case "${1:-}" in
    --check)
        if onebot_healthy; then
            printf 'healthy\n'
            exit 0
        fi
        printf 'unhealthy\n'
        exit 1
        ;;
    --dry-run)
        log "dry-run: onebot=$(onebot_healthy && printf healthy || printf unhealthy) qq=$(qq_logged_in && printf online || printf needs-login) bot=$(bot_connected_state)"
        exit 0
        ;;
    --force-recover)
        mkdir -p "$STATE_DIR" || exit 1
        exec 9>"$STATE_DIR/lock" || exit 1
        flock -n 9 || { log "another watchdog run is active"; exit 0; }
        recover "$FAIL_THRESHOLD"
        ;;
    "")
        ;;
    *)
        printf 'usage: %s [--check|--dry-run|--force-recover]\n' "$0" >&2
        exit 2
        ;;
esac

mkdir -p "$STATE_DIR" || exit 1
exec 9>"$STATE_DIR/lock" || exit 1
flock -n 9 || exit 0

if onebot_healthy; then
    previous=$(read_int "$FAIL_FILE")
    write_int "$FAIL_FILE" 0
    if [ "$previous" -gt 0 ]; then
        log "OneBot healthy again after $previous failed checks"
    fi
    exit 0
fi

fails=$(( $(read_int "$FAIL_FILE") + 1 ))
write_int "$FAIL_FILE" "$fails"
log "OneBot unhealthy (consecutive=$fails)"
[ "$fails" -lt "$FAIL_THRESHOLD" ] && exit 0

now=$(date +%s)
last=$(read_int "$COOLDOWN_FILE")
if [ $((now - last)) -lt "$COOLDOWN_SECONDS" ]; then
    log "recovery cooldown active for $((COOLDOWN_SECONDS - (now - last)))s"
    exit 0
fi

recover "$fails"
