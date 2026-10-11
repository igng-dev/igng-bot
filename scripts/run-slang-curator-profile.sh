#!/bin/sh
set -eu
SLANG_PACKAGE_DIR=${SLANG_PACKAGE_DIR:-/app/slang-curator-dsh}
DSH_BIN=${DSH_BIN:-/app/yunying-dsh/node_modules/.bin/dsh}
export DSH_HOME=${DSH_HOME:-/data/slang-dsh}
export DSH_TELEMETRY_DISABLED=1
mkdir -p "$DSH_HOME"
if [ ! -f "$DSH_HOME/profiles/slang-curator/package.json" ]; then
  "$DSH_BIN" plugin --profile slang-curator add "$SLANG_PACKAGE_DIR"
fi
node --input-type=module - "$DSH_HOME/profiles/slang-curator/package.json" <<'JS'
import { readFileSync } from 'node:fs';
const manifest = JSON.parse(readFileSync(process.argv[2], 'utf8'));
if (!manifest.dsh?.profile?.bundles?.includes('@igng/slang-curator-dsh')) throw new Error('unexpected slang curator profile');
JS
cd "$SLANG_PACKAGE_DIR"
exec "$DSH_BIN" --profile slang-curator "$@"
