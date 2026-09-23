"""The features guide: src/features/catalog.json and what render_features.py
makes of it.

The catalogue is hand-kept beside a capability map that grows with every
feature, so the cheap thing to check is that the two still meet: every flag the
server reports is either the `needs` of some record or named below as one no
record should carry. The rest guards the deploy: the pictures a shown card
points at exist, and the page renders with its cards and one script.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as A  # noqa: E402

FEATURES = ROOT / "src" / "features"
SHOTS = ROOT / "assets" / "feature_shots"

# Capability flags no record carries as `needs`, one reason each.
CAPS_WITHOUT_RECORD = {
    # PDF page-one thumbnails are part of the grid previews record
    # (files.thumbnails, needs thumbs); the flag only says which tool draws them.
    "pdf_thumbs",
}


def catalog():
    return json.loads((FEATURES / "catalog.json").read_text(encoding="utf-8"))


def test_catalog_parses_with_unique_ids():
    recs = catalog()
    assert recs, "catalog.json is empty"
    ids = [r["id"] for r in recs]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, "duplicate ids: %s" % dupes


def test_every_capability_is_in_the_catalog():
    needs = {r["needs"] for r in catalog() if r.get("needs")}
    caps = set(A.server_capabilities())
    missing = sorted(caps - needs - CAPS_WITHOUT_RECORD)
    assert not missing, "capabilities with no record (add one, or allowlist with a reason): %s" % missing
    stale = sorted(CAPS_WITHOUT_RECORD - caps)
    assert not stale, "allowlisted flags the server no longer reports: %s" % stale


def test_shown_records_have_both_shots():
    if not SHOTS.is_dir():
        pytest.skip("assets/feature_shots/ is not here (gitignored; run feature_shots.mjs)")
    missing = []
    for r in catalog():
        if r.get("show") and r.get("shot"):
            for theme in ("light", "dark"):
                f = SHOTS / ("%s-%s.webp" % (r["shot"], theme))
                if not f.is_file():
                    missing.append(f.name)
    assert not missing, "missing screenshots: %s" % sorted(set(missing))


def test_render_writes_page_and_markdown(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "render_features.py"), "--out", str(tmp_path)],
                   check=True, capture_output=True)
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    md = (tmp_path / "features.md").read_text(encoding="utf-8")
    assert page.count("<script") == 1
    assert "data:image" not in page
    you = page[page.index('id="p-you"'):page.index('id="p-agent"')]
    # A card is an <article class="c"> with a picture, or an <li class="c"> in
    # the group's text-only "Also" list.
    cards = re.findall(r'<(?:article|li) id="[^"]+" class="c"', you)
    assert len(cards) == sum(1 for r in catalog() if r.get("show"))
    with_agent = [r for r in catalog() if r.get("agent")]
    for r in with_agent:
        assert "### %s\n" % r["title"] in md
    assert "## 4. Documented HTTP routes" in md
