#!/bin/sh
set -eu
MODERATION_PACKAGE_DIR=${MODERATION_PACKAGE_DIR:-/app/moderation-dsh}
DSH_BIN=${DSH_BIN:-/app/yunying-dsh/node_modules/.bin/dsh}
export DSH_HOME=${DSH_HOME:-/data/moderation-dsh}
export DSH_TELEMETRY_DISABLED=1
mkdir -p "$DSH_HOME"
if [ ! -f "$DSH_HOME/profiles/work-moderation/package.json" ]; then
  "$DSH_BIN" plugin --profile work-moderation add "$MODERATION_PACKAGE_DIR"
fi
node --input-type=module - "$DSH_HOME/profiles/work-moderation/package.json" <<'JS'
import { readFileSync } from 'node:fs';
const manifest = JSON.parse(readFileSync(process.argv[2], 'utf8'));
if (!manifest.dsh?.profile?.bundles?.includes('@igng/work-moderation-dsh')) throw new Error('unexpected work moderation profile');
JS
cd "$MODERATION_PACKAGE_DIR"
exec "$DSH_BIN" --profile work-moderation "$@"
