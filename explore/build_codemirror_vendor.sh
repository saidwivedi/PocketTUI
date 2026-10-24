#!/usr/bin/env bash
# Rebuild vendor/codemirror.js, the editor's lazily loaded bundle (29-editor.js).
# Usage: explore/build_codemirror_vendor.sh [scratch-dir]
# The scratch dir holds node_modules and is never committed. The pins are the
# header comment's versions; the dependencies esbuild pulls in are pinned to the
# versions THIRD_PARTY_NOTICES.md lists, so a rebuild does not drift them.
set -euo pipefail
ESBUILD_VERSION=0.28.2
PINS=(
  @codemirror/state@6.7.1 @codemirror/view@6.43.9
  @codemirror/commands@6.11.0 @codemirror/language@6.12.4
  @codemirror/lang-python@6.2.1 @codemirror/lang-javascript@6.2.5
  @codemirror/lang-json@6.0.2 @codemirror/lang-markdown@6.5.2
  @codemirror/lang-html@6.4.12 @codemirror/lang-css@6.3.1
  @codemirror/lang-yaml@6.1.3 @codemirror/legacy-modes@6.5.3
  @codemirror/theme-one-dark@6.1.3 @replit/codemirror-vim@6.4.0
  @codemirror/search@6.7.1 @lezer/highlight@1.2.3
)
DEPS=(
  @codemirror/autocomplete@6.20.3 @lezer/common@1.5.2 @lezer/lr@1.4.10
  @lezer/python@1.1.19 @lezer/javascript@1.5.4 @lezer/json@1.0.3
  @lezer/markdown@1.7.2 @lezer/html@1.3.13 @lezer/css@1.3.6 @lezer/yaml@1.0.4
  style-mod@4.1.3 w3c-keyname@2.2.8 crelt@1.0.7 @marijn/find-cluster-break@1.0.4
)

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${1:-$(mktemp -d)}"
mkdir -p "$WORK"
cd "$WORK"
[ -f package.json ] || echo '{"name":"ptui-codemirror-vendor","private":true}' > package.json
# npm on a network mount can fail once with ENOTEMPTY; a second try clears it.
npm install --no-audit --no-fund --save-exact "${PINS[@]}" "${DEPS[@]}" "esbuild@$ESBUILD_VERSION" \
  || npm install --no-audit --no-fund --save-exact "${PINS[@]}" "${DEPS[@]}" "esbuild@$ESBUILD_VERSION"

# WidgetType, StateField, StateEffect, gutter and GutterMarker serve the
# workspace editor's inline git marks (47-workspace.js); the rest is the set the
# editor has always used.
cat > entry.js <<'JS'
export { EditorState, Compartment, RangeSetBuilder, StateField, StateEffect } from "@codemirror/state";
export {
  EditorView, Decoration, ViewPlugin, WidgetType, keymap, lineNumbers, gutter, GutterMarker,
  drawSelection, dropCursor, highlightActiveLine, highlightActiveLineGutter, highlightSpecialChars,
} from "@codemirror/view";
export {
  HighlightStyle, StreamLanguage, bracketMatching, defaultHighlightStyle, foldGutter, foldKeymap,
  indentOnInput, indentUnit, syntaxHighlighting, syntaxTree,
} from "@codemirror/language";
export { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
export { python } from "@codemirror/lang-python";
export { javascript } from "@codemirror/lang-javascript";
export { json } from "@codemirror/lang-json";
export { markdown } from "@codemirror/lang-markdown";
export { html } from "@codemirror/lang-html";
export { css } from "@codemirror/lang-css";
export { yaml } from "@codemirror/lang-yaml";
export { shell } from "@codemirror/legacy-modes/mode/shell";
export { oneDark } from "@codemirror/theme-one-dark";
export { vim, Vim, getCM } from "@replit/codemirror-vim";
export { tags } from "@lezer/highlight";
JS

npx esbuild entry.js --bundle --minify --format=iife --global-name=CM6 \
  --legal-comments=none --outfile=bundle.js

{
  cat <<HDR
/*! PocketTUI CodeMirror 6 vendor bundle — built with esbuild $ESBUILD_VERSION
 * (entry re-exports under the CM6 global; --bundle --minify --format=iife).
 * Loaded lazily by the editor (29-editor.js) on first open, never at boot.
 * Rebuild with explore/build_codemirror_vendor.sh.
 * Pinned package versions:
 *   @codemirror/state 6.7.1          @codemirror/view 6.43.9
 *   @codemirror/commands 6.11.0      @codemirror/language 6.12.4
 *   @codemirror/lang-python 6.2.1    @codemirror/lang-javascript 6.2.5
 *   @codemirror/lang-json 6.0.2      @codemirror/lang-markdown 6.5.2
 *   @codemirror/lang-html 6.4.12     @codemirror/lang-css 6.3.1
 *   @codemirror/lang-yaml 6.1.3      @codemirror/legacy-modes 6.5.3 (mode/shell)
 *   @codemirror/theme-one-dark 6.1.3 @replit/codemirror-vim 6.4.0
 *   @codemirror/search 6.7.1 (peer dependency of the vim mode)
 *   @lezer/highlight 1.2.3 (tags, for the palette-built highlight style)
 */
HDR
  cat bundle.js
} > "$REPO/vendor/codemirror.js"
ls -l "$REPO/vendor/codemirror.js"
