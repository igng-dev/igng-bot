#!/bin/sh
set -eu
CHAT_ANALYST_PACKAGE_DIR=${CHAT_ANALYST_PACKAGE_DIR:-/app/chat-analyst-dsh}
DSH_BIN=${DSH_BIN:-/app/yunying-dsh/node_modules/.bin/dsh}
export DSH_HOME=${DSH_HOME:-/data/chat-analyst-dsh}
export DSH_TELEMETRY_DISABLED=1
mkdir -p "$DSH_HOME"
if [ ! -f "$DSH_HOME/profiles/chat-analyst/package.json" ]; then
  "$DSH_BIN" plugin --profile chat-analyst add "$CHAT_ANALYST_PACKAGE_DIR"
fi
node --input-type=module - "$DSH_HOME/profiles/chat-analyst/package.json" <<'JS'
import { readFileSync } from 'node:fs';
const manifest = JSON.parse(readFileSync(process.argv[2], 'utf8'));
if (!manifest.dsh?.profile?.bundles?.includes('@igng/chat-analyst-dsh')) throw new Error('unexpected chat analyst profile');
JS
cd "$CHAT_ANALYST_PACKAGE_DIR"
exec "$DSH_BIN" --profile chat-analyst "$@"
