#!/usr/bin/env python3
"""Build a deployable static PWA shell for PocketTUI.

  - Assembles src/mobile/ into the single-file mobile_app.html.
  - Substitutes __BACKEND_URL__ -> chosen backend.
  - Emits index.html, sw.js, manifest.json, the icons and vendor/.

The app source lives split under src/mobile/ (markup skeleton, the pre-paint
theme script, the stylesheet, and the main script in ordered fragments). The
browser is still served one self-contained document, so assembly is plain
concatenation: index.src.html carries `@include` marker lines that are replaced
by the named fragment's bytes, unchanged. Nothing is minified, rewritten or
reordered — the assembled file is what a single mobile_app.html would have been.

The shell is published to a public host while the backend it talks to stays on
a private tailnet, so every asset reference is relative and every API/WS call
goes to the backend URL.

That URL is deliberately NOT baked in by default: a public build ships empty, and
the app asks for the backend on first run and keeps it in localStorage. This
keeps the owner's private hostname out of the published HTML.

Usage:
  python build_mobile.py                     # public build, no backend baked in
  python build_mobile.py --backend URL       # bake one in
  python build_mobile.py --backend-file      # bake in .backend_url's contents
  python build_mobile.py --assemble-only     # only refresh mobile_app.html + sw.js
  python build_mobile.py --emit-runtime DIR  # write the flat runtime set into DIR

Baking a backend in is opt-in, so a stray .backend_url can never leak a private
hostname into a published build.
"""

import argparse
import shutil
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
BUILD_DIR = HERE / "mobile_build"
SRC_DIR = HERE / "src" / "mobile"
ASSETS_DIR = HERE / "assets"
VENDOR_DIR = HERE / "vendor"
BACKEND_FILE = HERE / ".backend_url"

# The assembled runtime copies, written at the repo root because that is the
# flat layout app.py serves from and install.sh/deploy ship.
TEMPLATE = HERE / "mobile_app.html"
SW_TEMPLATE = HERE / "sw.js"

ICONS = ("icon-192.png", "icon-512.png")

# The main script's fragments, in the order they are concatenated. Listed
# explicitly rather than globbed: the order is the program, and a directory
# listing is not a place to encode it.
JS_FRAGMENTS = (
    "01-helpers.js",
    "02-debug-log.js",
    "03-a2hs-hint.js",
    "04-theme.js",
    "05-settings.js",
    "06-session-list.js",
    "07-terminal.js",
    "08-links.js",
    "09-image-viewer.js",
    "10-demo-shell.js",
    "demo/11-fake-fs.js",
    "demo/12-output.js",
    "demo/13-line-editor.js",
    "demo/14-commands.js",
    "demo/15-agent-tui.js",
    "demo/16-lifecycle.js",
    "17-key-bar.js",
    "18-speech-recognition.js",
    "19-voice-capture.js",
    "20-edge-swipe.js",
    "21-cell-geometry.js",
    "22-drag-scroll.js",
    "23-long-press-select.js",
    "24-pinch-zoom.js",
    "25-keyboard-geometry.js",
    # Numbered past boot but concatenated before it: boot is the program's
    # last word, and these views are wired by then like everything else.
    "28-file-explorer.js",
    "29-editor.js",
    "26-boot.js",
)


def assemble() -> str:
    """src/mobile/ -> the one-file document, by concatenation only.

    index.src.html is copied through line by line; a line that is exactly
    `@include NAME` is replaced by that fragment's bytes. `@include js` expands
    to every JS_FRAGMENTS entry in order. Placeholders (__CACHE_VERSION__,
    __BACKEND_URL__) pass through untouched — substituting them is the caller's
    job, here and in app.py, on the assembled result.
    """
    def read(rel: Path) -> str:
        return rel.read_text(encoding="utf-8")

    out = []
    for line in read(SRC_DIR / "index.src.html").splitlines(keepends=True):
        if not line.startswith("@include "):
            out.append(line)
            continue
        what = line[len("@include "):].strip()
        if what == "js":
            for frag in JS_FRAGMENTS:
                out.append(read(SRC_DIR / "js" / frag))
        else:
            out.append(read(SRC_DIR / what))
    return "".join(out)


def emit_runtime(target: Path, html: str, sw: str) -> None:
    """Write the flat runtime set (what app.py serves) into target."""
    target.mkdir(parents=True, exist_ok=True)
    (target / "mobile_app.html").write_text(html, encoding="utf-8")
    (target / "sw.js").write_text(sw, encoding="utf-8")
    for icon in ICONS:
        shutil.copy(ASSETS_DIR / icon, target / icon)


def manifest_json() -> str:
    """The same manifest app.py serves live, as a static file."""
    return r"""{
  "name": "PocketTUI",
  "short_name": "PocketTUI",
  "start_url": "./",
  "scope": "./",
  "display": "standalone",
  "orientation": "any",
  "background_color": "#FAF8F3",
  "theme_color": "#FAF8F3",
  "icons": [
    { "src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable" },
    { "src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable" }
  ]
}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    # Baking a backend in is opt-in. Omitting the flag produces the public
    # build, so the private hostname can never leak into a published page just
    # because a local .backend_url happens to exist.
    parser.add_argument("--backend", default="",
                        help="Bake in a backend URL. Omit for a public build; "
                             "the app then asks for it on first run.")
    parser.add_argument("--backend-file", action="store_true",
                        help=f"Bake in the URL from {BACKEND_FILE.name} "
                             "(personal deploys).")
    parser.add_argument("--assemble-only", action="store_true",
                        help="Only refresh the runtime copies at the repo root; "
                             "skip mobile_build/.")
    parser.add_argument("--emit-runtime", metavar="DIR",
                        help="Also write the flat runtime set (mobile_app.html, "
                             "sw.js, icons) into DIR. Used by install.sh when "
                             "installing from a checkout.")
    args = parser.parse_args()

    if args.backend_file:
        if not BACKEND_FILE.exists():
            print(f"ERROR: {BACKEND_FILE} not found", file=sys.stderr)
            return 1
        backend = BACKEND_FILE.read_text().strip()
    else:
        backend = args.backend.strip()

    for path in (SRC_DIR / "index.src.html", SRC_DIR / "sw.js", ASSETS_DIR,
                 VENDOR_DIR):
        if not path.exists():
            print(f"ERROR: {path} not found", file=sys.stderr)
            return 1

    # The runtime copies are the assembled source, placeholders intact: app.py
    # substitutes __CACHE_VERSION__ itself when it serves them, and a checkout
    # run straight from run.sh reads these. Written on every build so they can
    # never drift behind src/.
    template = assemble()
    sw_template = (SRC_DIR / "sw.js").read_text(encoding="utf-8")
    emit_runtime(HERE, template, sw_template)
    if args.emit_runtime:
        emit_runtime(Path(args.emit_runtime).resolve(), template, sw_template)

    if args.assemble_only:
        print(f"Assembled -> {TEMPLATE}")
        print(f"            {SW_TEMPLATE}")
        return 0

    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    cache_version = time.strftime("%Y%m%d-%H%M%S")
    backend = backend.rstrip("/")

    html = template.replace("__BACKEND_URL__", backend)
    html = html.replace("__CACHE_VERSION__", cache_version)
    (BUILD_DIR / "index.html").write_text(html, encoding="utf-8")

    sw = sw_template.replace("__CACHE_VERSION__", cache_version)
    (BUILD_DIR / "sw.js").write_text(sw, encoding="utf-8")

    (BUILD_DIR / "manifest.json").write_text(manifest_json(), encoding="utf-8")

    for icon in ICONS:
        shutil.copy(ASSETS_DIR / icon, BUILD_DIR / icon)
    shutil.copytree(VENDOR_DIR, BUILD_DIR / "vendor", dirs_exist_ok=True)

    print(f"Built -> {BUILD_DIR}")
    print(f"Backend baked in: {backend or '(none — app asks on first run)'}")
    print(f"Cache version:    {cache_version}")
    for f in sorted(BUILD_DIR.rglob("*")):
        if f.is_file():
            print(f"  {str(f.relative_to(BUILD_DIR)):24s} {f.stat().st_size:>8d} B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
