"""The src/mobile -> mobile_app.html assembly.

The app is written split and served whole, so the thing worth pinning is that
assembly is a pure concatenation: deterministic, lossless, and leaving the
placeholders for the consumers that substitute them. Content is deliberately not
hashed — every UI change would move the hash and the test would only ever be
updated to match, which proves nothing.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "mobile"


def _load_build_mobile():
    spec = importlib.util.spec_from_file_location(
        "build_mobile", REPO / "build_mobile.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_mobile"] = mod
    spec.loader.exec_module(mod)
    return mod


build_mobile = _load_build_mobile()


@pytest.fixture(scope="module")
def doc():
    return build_mobile.assemble()


def test_assembly_is_deterministic(doc):
    assert build_mobile.assemble() == doc


def test_every_fragment_is_a_file():
    assert (SRC / "index.src.html").is_file()
    assert (SRC / "boot-theme.js").is_file()
    assert (SRC / "styles.css").is_file()
    for frag in build_mobile.JS_FRAGMENTS:
        assert (SRC / "js" / frag).is_file(), frag


def test_fragment_list_matches_the_directory():
    """A fragment on disk that no one includes is dead code that still looks live."""
    on_disk = {
        str(p.relative_to(SRC / "js"))
        for p in (SRC / "js").rglob("*.js")
    }
    assert on_disk == set(build_mobile.JS_FRAGMENTS)


def test_no_include_markers_survive(doc):
    assert "@include" not in doc


def test_every_fragment_appears_verbatim(doc):
    """Concatenation, not transformation: each fragment's bytes are in the output."""
    for rel in ["boot-theme.js", "styles.css"] + [
            "js/" + f for f in build_mobile.JS_FRAGMENTS]:
        text = (SRC / rel).read_text(encoding="utf-8")
        assert text in doc, rel


def test_fragments_appear_in_listed_order(doc):
    positions = [
        doc.index((SRC / "js" / f).read_text(encoding="utf-8"))
        for f in build_mobile.JS_FRAGMENTS
    ]
    assert positions == sorted(positions)


def test_document_structure(doc):
    assert doc.startswith("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n")
    assert doc.rstrip().endswith("</html>")
    for tag in ("<head>", "</head>", "<body>", "</body>", "<style>", "</style>"):
        assert doc.count(tag) == 1, tag
    # One inline script for the pre-paint theme, one for the app.
    assert doc.count("</script>") == doc.count("<script")


def test_style_block_holds_the_whole_stylesheet(doc):
    css = (SRC / "styles.css").read_text(encoding="utf-8")
    assert css.startswith("<style>\n")
    assert css.rstrip().endswith("</style>")
    head = doc[:doc.index("</head>")]
    assert css in head


def test_app_script_is_one_block(doc):
    """All the js fragments land inside a single <script> element."""
    body = doc[doc.index("<body>"):]
    first = (SRC / "js" / build_mobile.JS_FRAGMENTS[0]).read_text(encoding="utf-8")
    last = (SRC / "js" / build_mobile.JS_FRAGMENTS[-1]).read_text(encoding="utf-8")
    start, end = body.index(first), body.index(last)
    assert start < end
    # Nothing closes the script between the first fragment and the last.
    assert "</script>" not in body[start:end]


def test_placeholders_survive_assembly(doc):
    """app.py and build_mobile.py substitute these on the assembled document."""
    assert "__BACKEND_URL__" in doc
    assert doc.count("__CACHE_VERSION__") >= 2


def test_report_dialog_is_wired(doc):
    """The report sheet, its Settings row, and the endpoint it posts to."""
    assert 'id="sheet-report"' in doc
    assert 'id="btn-report"' in doc
    assert "https://pockettui.com/api/report" in doc


def test_browser_zoom_scales_the_frame_not_the_page(doc):
    """The pane's zoom is a transform on the iframe, never the page's own.

    A proxied document told to zoom itself reports rects with the factor in
    them and applies it again to any length written back from one, which puts
    every rect-positioned overlay (jQuery .offset() into a select2 list, a
    date picker, a tooltip) at factor times where it belongs. Scaling the
    frame element leaves the page a CSS pixel of its own and still reflows it,
    because the frame is laid out at 1/factor of the pane.
    """
    assert "function browserApplyZoom(" in doc
    assert "frame.style.transform = factor === 1" in doc
    assert "pockettui-zoom" not in doc
    css = (SRC / "styles.css").read_text(encoding="utf-8")
    rule = re.search(r"#browser-frame \{[^}]*\}", css).group(0)
    assert "transform-origin: 0 0;" in rule


def test_the_browser_topbar_carries_one_zoom_key_and_a_star(doc):
    """Zoom is a key and a panel, and the bar's other new key is the bookmark.

    The two zoom keys are gone from the bar itself — the panel under the one
    key is the only place a minus and a plus are left, which is what frees the
    slots on a bar that is 360px wide docked.
    """
    assert 'id="btn-browser-zoom"' in doc
    assert 'id="btn-browser-star"' in doc
    assert "btn-browser-zoom-out" not in doc and "btn-browser-zoom-in" not in doc
    for ident in ("browser-zoom-minus", "browser-zoom-pct", "browser-zoom-plus"):
        assert f'id="{ident}"' in doc, ident
    # The panel still draws the pair's glyphs, so the sprite keeps them.
    assert 'id="i-zoom-out"' in doc and 'id="i-zoom-in"' in doc
    assert 'id="i-star"' in doc and 'id="i-star-fill"' in doc


def test_the_topbar_offers_the_page_as_a_tab_through_the_computer(doc):
    """The way out for a page that cannot run framed: an app written to be the
    top window (a portal reading top.EPCM through its views) needs a top-level
    tab, which the backend serves under its unsandboxed token. Hidden until the
    computer says it has the mode, like the star."""
    assert 'id="btn-browser-tab" hidden' in doc
    assert 'aria-label="Open in a tab through the computer"' in doc
    assert '"browse_tab"' in doc or "hasCapStrict(\"browse_tab\")" in doc
    # Opened blank inside the click, sent somewhere once the token is in hand.
    assert 'window.open("", "_blank")' in doc
    # And sent to the hop that wipes this origin's storage, never straight to
    # the proxied page: the shell may once have been served from this same
    # address, and its pairing token would still be sitting there.
    assert '"/enter?to=" + encodeURIComponent(' in doc
    assert "function browserCloseTabs(" in doc
    # Asked to close, not closed: cross-origin, with the opener nulled, the
    # shell's own close() on that window is refused.
    assert 'w.postMessage("pockettui-close", browserOrigin())' in doc


def test_the_topbar_offers_an_empty_tab_through_the_computer(doc):
    """A tab has no address bar of ours — the browser's own types into the
    laptop — so an empty tab starts on the backend's start page, which is the
    one field in a tab that goes through the computer. Same token, same
    flavour, so it is shown and hidden with the button beside it."""
    assert 'id="btn-browser-newtab" hidden' in doc
    assert 'aria-label="New tab through the computer"' in doc
    assert '$("btn-browser-newtab").hidden = tab;' in doc
    assert 'rec.prefix + "/b/" + rec.token + "/start"' in doc
    # Both buttons open the blank window inside the click and send it on once
    # the token is in hand.
    assert "function browserOpenTab(" in doc
    assert "browserOpenTab(browserStartUrl)" in doc


def test_vendor_script_tags_survive(doc):
    for name in ("xterm.js", "addon-fit.js", "addon-webgl.js"):
        assert f'src="vendor/{name}?v=__CACHE_VERSION__"' in doc
    assert 'href="vendor/xterm.css?v=__CACHE_VERSION__"' in doc


def test_no_fragment_starts_mid_statement():
    """Each cut sits on a section banner, so fragments stay independently readable."""
    banner = re.compile(r"^// ={10,}\n|^// -{4,} ")
    for frag in build_mobile.JS_FRAGMENTS:
        text = (SRC / "js" / frag).read_text(encoding="utf-8")
        assert banner.match(text), frag


def test_emit_runtime_writes_the_flat_set(tmp_path, doc):
    build_mobile.emit_runtime(tmp_path, doc, "// sw")
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {"mobile_app.html", "sw.js", "icon-192.png", "icon-512.png"}
    assert (tmp_path / "mobile_app.html").read_text(encoding="utf-8") == doc


def test_root_runtime_copy_is_current(doc):
    """The gitignored root copy is what app.py serves from a checkout."""
    root = REPO / "mobile_app.html"
    if not root.exists():
        pytest.skip("no build has been run in this checkout")
    assert root.read_text(encoding="utf-8") == doc
