#!/usr/bin/env bash
# Rebuild vendor/three.js, the 3D viewer's lazily loaded bundle (48-mesh-viewer.js).
# Usage: explore/build_three_vendor.sh [scratch-dir]
# The scratch dir holds node_modules and is never committed; the pins below and
# the esbuild version are the whole of what decides the output.
set -euo pipefail
THREE_VERSION=0.186.1
ESBUILD_VERSION=0.28.2

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${1:-$(mktemp -d)}"
mkdir -p "$WORK"
cd "$WORK"
[ -f package.json ] || echo '{"name":"ptui-three-vendor","private":true}' > package.json
# npm on a network mount can fail once with ENOTEMPTY; a second try clears it.
npm install --no-audit --no-fund --save-exact "three@$THREE_VERSION" "esbuild@$ESBUILD_VERSION" \
  || npm install --no-audit --no-fund --save-exact "three@$THREE_VERSION" "esbuild@$ESBUILD_VERSION"

cat > entry.js <<'JS'
export * from "three";
export { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
export { PLYLoader } from "three/examples/jsm/loaders/PLYLoader.js";
export { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js";
export { MTLLoader } from "three/examples/jsm/loaders/MTLLoader.js";
export { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
export { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
export { PCDLoader } from "three/examples/jsm/loaders/PCDLoader.js";
export { XYZLoader } from "three/examples/jsm/loaders/XYZLoader.js";
JS

npx esbuild entry.js --bundle --minify --format=iife --global-name=THREE3D \
  --legal-comments=none --outfile=bundle.js

{
  cat <<HDR
/*! PocketTUI three.js vendor bundle — built with esbuild $ESBUILD_VERSION
 * (entry re-exports under the THREE3D global; --bundle --minify --format=iife).
 * Loaded lazily by the 3D viewer (48-mesh-viewer.js) on the first model opened,
 * never at boot. Rebuild with explore/build_three_vendor.sh.
 * Pinned package versions:
 *   three $THREE_VERSION (MIT), with these addons from three/examples/jsm:
 *   OrbitControls, PLYLoader, OBJLoader, MTLLoader, STLLoader, GLTFLoader,
 *   PCDLoader, XYZLoader
 */
HDR
  cat bundle.js
} > "$REPO/vendor/three.js"
ls -l "$REPO/vendor/three.js"
