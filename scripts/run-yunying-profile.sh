#!/bin/sh
# Use DSH's own plugin/profile persistence; no hand-written replacement launcher.
set -eu
YUNYING_PACKAGE_DIR=${YUNYING_PACKAGE_DIR:-/app/yunying-dsh}
DSH_BIN=${DSH_BIN:-$YUNYING_PACKAGE_DIR/node_modules/.bin/dsh}
export DSH_HOME=${DSH_HOME:-/data/dsh}
export DSH_TELEMETRY_DISABLED=1
mkdir -p "$DSH_HOME"
if [ ! -f "$DSH_HOME/profiles/yunying/package.json" ]; then
  "$DSH_BIN" plugin --profile yunying add "$YUNYING_PACKAGE_DIR"
fi
# Refuse an unrelated pre-existing profile; do not overwrite an operator's configuration.
node --input-type=module - "$DSH_HOME/profiles/yunying/package.json" <<'JS'
import {readFileSync} from 'node:fs';
const manifest=JSON.parse(readFileSync(process.argv[2],'utf8'));
if(!manifest.dsh?.profile?.bundles?.includes('@igng/yunying-dsh'))throw new Error('existing yunying profile is missing its native bundle');
JS
cd "$YUNYING_PACKAGE_DIR"
exec "$DSH_BIN" --profile yunying "$@"
