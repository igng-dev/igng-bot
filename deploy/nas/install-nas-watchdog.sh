#!/bin/sh
# Install the IGNGbot bridge watchdog in the NAS user's crontab.
# Run this on the NAS as the Docker-capable operator user (no sudo required).
set -eu

NAS_ROOT=${NAS_ROOT:-/vol2/1000/Docker/igngbot}
WATCHDOG="$NAS_ROOT/watchdog/igngbot-bridge-watchdog.sh"
LOG="$NAS_ROOT/watchdog/cron.log"

mkdir -p "$NAS_ROOT/watchdog/state"
chmod 700 "$NAS_ROOT/watchdog/state" 2>/dev/null || true
chmod 755 "$WATCHDOG" 2>/dev/null || true

LINE="*/2 * * * * $WATCHDOG >> $LOG 2>&1"
CURRENT=$(crontab -l 2>/dev/null || true)
if printf '%s\n' "$CURRENT" | grep -Fq "$WATCHDOG"; then
    echo "watchdog cron already installed"
else
    {
        printf '%s\n' "$CURRENT"
        printf '%s\n' "$LINE"
    } | crontab -
    echo "watchdog cron installed"
fi

"$WATCHDOG" --check
