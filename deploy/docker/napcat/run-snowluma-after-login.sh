#!/bin/sh
# Wait until QQ has restored its account, then exec SnowLuma.
#
# The hook must be attached to a QQ process that is already logged in.  If it
# is attached earlier and QQ restores its session without emitting the event
# SnowLuma expects, the hook keeps a stale "logged out" state forever and
# OneBot never listens.  Keeping SnowLuma stopped until the PT probe sees the
# account avoids that startup race.
set -eu

PROBE=/opt/igngbot/pt-login-probe.py
NODE=/usr/local/bin/node
APP=/app/snowluma/index.mjs
PROBE_OUT=/tmp/igngbot-pt-login-probe.out

log() {
    printf '%s [wait-qq-login] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

if [ ! -r "$PROBE" ]; then
    log "probe missing: $PROBE"
    exit 1
fi
if [ ! -x "$NODE" ] || [ ! -f "$APP" ]; then
    log "SnowLuma runtime missing: node=$NODE app=$APP"
    exit 1
fi

attempt=0
while :; do
    if /usr/bin/python3 "$PROBE" >"$PROBE_OUT" 2>&1; then
        log "QQ login detected; starting SnowLuma"
        exec "$NODE" "$APP"
    fi
    attempt=$((attempt + 1))
    if [ $((attempt % 15)) -eq 1 ]; then
        log "waiting for QQ login (probe attempt $attempt); scan via VNC if a QR screen is shown"
        if [ -s "$PROBE_OUT" ]; then
            log "probe detail: $(tr '\n' ' ' <"$PROBE_OUT" | cut -c1-300)"
        fi
    fi
    sleep 2
done
