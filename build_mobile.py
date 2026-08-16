#!/usr/bin/env python3
"""Build a deployable static PWA shell for PocketTUI.

  - Reads mobile_app.html, substitutes __BACKEND_URL__ -> chosen backend.
  - Emits index.html, sw.js, manifest.json, the icons and vendor/.

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
TEMPLATE = HERE / "mobile_app.html"
SW_TEMPLATE = HERE / "sw.js"
VENDOR_DIR = HERE / "vendor"
BACKEND_FILE = HERE / ".backend_url"


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
    args = parser.parse_args()

    if args.backend_file:
        if not BACKEND_FILE.exists():
            print(f"ERROR: {BACKEND_FILE} not found", file=sys.stderr)
            return 1
        backend = BACKEND_FILE.read_text().strip()
    else:
        backend = args.backend.strip()

    for path in (TEMPLATE, SW_TEMPLATE, VENDOR_DIR):
        if not path.exists():
            print(f"ERROR: {path} not found", file=sys.stderr)
            return 1

    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    cache_version = time.strftime("%Y%m%d-%H%M%S")
    backend = backend.rstrip("/")

    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("__BACKEND_URL__", backend)
    html = html.replace("__CACHE_VERSION__", cache_version)
    (BUILD_DIR / "index.html").write_text(html, encoding="utf-8")

    sw = SW_TEMPLATE.read_text(encoding="utf-8").replace("__CACHE_VERSION__", cache_version)
    (BUILD_DIR / "sw.js").write_text(sw, encoding="utf-8")

    (BUILD_DIR / "manifest.json").write_text(manifest_json(), encoding="utf-8")

    for icon in ("icon-192.png", "icon-512.png"):
        shutil.copy(HERE / icon, BUILD_DIR / icon)
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
