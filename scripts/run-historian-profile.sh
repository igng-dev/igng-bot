#!/bin/sh
set -eu
HISTORIAN_PACKAGE_DIR=${HISTORIAN_PACKAGE_DIR:-/app/historian-dsh}
DSH_BIN=${DSH_BIN:-/app/yunying-dsh/node_modules/.bin/dsh}
export DSH_HOME=${DSH_HOME:-/data/historian-dsh}
export DSH_TELEMETRY_DISABLED=1
mkdir -p "$DSH_HOME"
if [ ! -f "$DSH_HOME/profiles/server-historian/package.json" ]; then
  "$DSH_BIN" plugin --profile server-historian add "$HISTORIAN_PACKAGE_DIR"
fi
node --input-type=module - "$DSH_HOME/profiles/server-historian/package.json" <<'JS'
import { readFileSync } from 'node:fs';
const manifest = JSON.parse(readFileSync(process.argv[2], 'utf8'));
if (!manifest.dsh?.profile?.bundles?.includes('@igng/server-historian-dsh')) throw new Error('unexpected historian profile');
JS
cd "$HISTORIAN_PACKAGE_DIR"
exec "$DSH_BIN" --profile server-historian "$@"
