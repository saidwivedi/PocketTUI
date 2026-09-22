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
    rule = re.search(r"\.browser-frame \{[^}]*\}", css).group(0)
    assert "transform-origin: 0 0;" in rule


def test_the_browser_pane_carries_one_zoom_key_and_a_star(doc):
    """Zoom is a key and a panel, and the pane's other new key is the bookmark.

    The two zoom keys are gone from the row itself — the panel under the one
    key is the only place a minus and a plus are left, which is what frees the
    slots on a pane that is 360px wide docked.
    """
    assert 'id="btn-browser-zoom"' in doc
    assert 'id="btn-browser-star"' in doc
    assert "btn-browser-zoom-out" not in doc and "btn-browser-zoom-in" not in doc
    for ident in ("browser-zoom-minus", "browser-zoom-pct", "browser-zoom-plus"):
        assert f'id="{ident}"' in doc, ident
    # The panel still draws the pair's glyphs, so the sprite keeps them.
    assert 'id="i-zoom-out"' in doc and 'id="i-zoom-in"' in doc
    assert 'id="i-star"' in doc and 'id="i-star-fill"' in doc


def test_the_topbar_toggles_a_tab_onto_the_computers_own_network(doc):
    """The way through for a page that cannot run framed: an app written to be
    the top window (a portal reading top.EPCM through its views) is fetched
    under the computer's other permission and drawn in the same frame without
    the sandbox, so it is a page in its own right. A toggle per tab, left of
    reload, and hidden until the computer says it can grant that — like the
    star. What it is called says nothing about how it is fetched: that is the
    computer's business, not the reader's."""
    assert 'id="btn-browser-tab" hidden' in doc
    # A glyph nobody has met before says nothing on its own, so the key says
    # what pressing it would do, and once pressed what it did — the tooltip and
    # the label carrying the same words either way (syncBrowserLan).
    off = "Use the computer's network for this tab"
    on = "This tab uses the computer's network"
    assert f'aria-label="{off}"' in doc and f'title="{off}"' in doc
    assert f'"{on}"' in doc
    assert 'btn.setAttribute("aria-label", said);' in doc
    assert 'btn.setAttribute("title", said);' in doc
    # A toggle says so to a reader who cannot see the tint, not only to one
    # who can.
    assert 'aria-pressed="false"' in doc
    assert '"browse_tab"' in doc or "hasCapStrict(\"browse_tab\")" in doc
    # No window of the device's own is opened any more — the founder asked for
    # the page in the pane, and nothing here should say otherwise.
    assert "Open in its own window" not in doc
    assert 'window.open("", "_blank")' not in doc
    # The mode is one attribute and the frame is replaced to change it: a
    # sandbox list is read when a frame loads, not when it is set.
    assert 'tab.frame.removeAttribute("sandbox")' in doc
    assert "function browserSetLan(" in doc and "function syncBrowserLan(" in doc
    # A tab that has loaded nothing yet goes in through the hop that wipes this
    # origin's storage, never straight to the proxied page: the shell may once
    # have been served from this same address, and its pairing token would
    # still be sitting there. Past that hop the pages are ordinary ones —
    # re-entering would wipe what the page itself has since put there.
    assert '"/enter?to=" + encodeURIComponent(' in doc
    assert "target = tab.primed ? proxied : browserEnterUrl(proxied, rec);" in doc


def test_the_toggles_order_in_the_address_row(doc):
    """Left of reload, where a browser keeps the keys that act on the page
    rather than on the address — and all of them before the field, which now
    has the whole rest of the row to itself. Which browser the tab is comes
    before which network it is on: it is the larger of the two choices, and it
    is the one that decides whether the other is offered at all."""
    at = {name: doc.index(f'id="{name}"')
          for name in ("btn-browser-back", "btn-browser-fwd", "btn-browser-full",
                       "btn-browser-tab", "btn-browser-reload",
                       "browser-url-wrap", "browser-url")}
    assert (at["btn-browser-back"] < at["btn-browser-fwd"] < at["btn-browser-full"]
            < at["btn-browser-tab"] < at["btn-browser-reload"]
            < at["browser-url-wrap"] < at["browser-url"])


def test_a_tab_is_the_computers_own_browser_by_default(doc):
    """Full mode is what a tab is: wherever the computer has a browser to stream
    from, a new tab runs in it and the proxy is the fallback — no browser found,
    the preference in Settings, or a tab stepped down by the key on its row. The
    key is a toggle per tab like the network key beside it, and it hides that one
    while it is pressed: a page fetched by a browser running on the computer is
    on the computer's network already."""
    assert 'id="btn-browser-full" hidden' in doc
    off = "Run this tab in the computer's Chrome"
    on = ("This tab runs in the computer's Chrome; press to use the lightweight"
          " proxy instead")
    assert f'aria-label="{off}"' in doc and f'title="{off}"' in doc
    assert f'"{on}"' in doc
    assert "function syncBrowserFull(" in doc and "function browserSetFull(" in doc
    # The default, and the one way out of it: the capability strictly checked,
    # and the preference that asks for the proxy anyway.
    assert ('return hasCapStrict("browser_full") && !cfg.browserPreferProxy;'
            in doc)
    assert "pockettui_browser_prefer_proxy" in doc
    assert 'id="browser-proxy-toggle"' in doc
    assert ">Prefer the lightweight proxy<" in doc
    # The network key has nothing to offer a streamed tab, so it goes while one
    # is on screen.
    assert ("btn.hidden = !hasCapStrict(\"browse_tab\") || browserTabBlocked\n"
            "               || browserIsFull(browserTab());") in doc
    # A streamed tab is zoomed on the computer — there is no element here to
    # scale, only a picture of one — and its history is the real browser's.
    assert "tab.full.setZoom(factor);" in doc
    assert 'browserFullSend(tab, delta < 0 ? "back" : "fwd");' in doc
    # Closing a tab closes the page on the computer; closing the pane does not,
    # for the reason a frame keeps its document.
    assert 'browserFullSend(tab, "close");' in doc
    assert "function browserHideFulls(" in doc


def test_the_window_controls_sit_in_the_tab_row(doc):
    """A browser window's top row: tabs at one end, the window's own controls
    at the other. Bookmark, zoom, and docked the pane's expand and close all
    act on the window or on the page as a whole, so they belong up there rather
    than on the address row, which is left to the address and the keys that
    move it."""
    at = {name: doc.index(f'id="{name}"')
          for name in ("browser-tabs", "browser-tab-row", "btn-browser-newtab",
                       "browser-tab-actions", "btn-browser-star",
                       "browser-zoom-wrap", "btn-browser-zoom",
                       "btn-browser-expand", "btn-browser-close")}
    bar = doc.index('class="topbar browser-topbar"')
    # Every one of them inside the group, the group inside the tab row, and the
    # whole of it above the address row.
    assert (at["browser-tabs"] < at["browser-tab-row"] < at["btn-browser-newtab"]
            < at["browser-tab-actions"] < at["btn-browser-star"]
            < at["browser-zoom-wrap"] < at["btn-browser-zoom"]
            < at["btn-browser-expand"] < at["btn-browser-close"] < bar)
    # The group is flush right on the band by a margin, not by a spacer: the
    # tabs and the "+" stay together at the left and a strip too wide for the
    # pane scrolls inside its own box rather than pushing the keys off.
    css = (SRC / "styles.css").read_text(encoding="utf-8")
    rule = re.search(r"#browser-tab-actions \{[^}]*\}", css).group(0)
    assert "margin-left: auto;" in rule
    # The panel hangs off a key in the tab row now, so that row has to out-stack
    # the address row below it and the scrim that dims the page for it.
    band = re.search(r"#browser-tabs \{[^}]*\}", css).group(0)
    assert "position: relative;" in band and "z-index: 55;" in band


def test_the_bookmarks_bar_is_a_row_of_links_not_a_second_row_of_tabs(doc):
    """Two rows of cards a few pixels apart read as one thing twice. The strip
    above the address row is the pane's cards; this one is links, each with the
    star that saved it where a desktop browser would put a favicon — there are
    none to fetch through a proxy."""
    assert 'svgIcon("i-star"), el("span", { class: "bm-name" }' in doc
    # The title alone is what is capped and ellipsised; the star and the link's
    # own padding sit outside it.
    assert ".bm-name {" in doc and "text-overflow: ellipsis;" in doc
    # No card: the border and the filled background the chips used are gone.
    body = doc[doc.index(".bm-chip {"):doc.index(".bm-del {")]
    assert "border: 1px solid" not in body and "var(--card-2)" not in body


def test_the_tab_strip_sits_above_the_address_row(doc):
    """Where every desktop browser puts it: the pane's top edge, then the
    address row, then the bookmarks, then the page."""
    assert (doc.index('id="browser-tabs"') < doc.index('class="topbar browser-topbar"')
            < doc.index('id="browser-bookmarks"') < doc.index('id="browser-wrap"'))


def test_the_pane_has_tabs_of_its_own(doc):
    """The pane is a browser: a strip of chips above the page, the lone tab
    included, and the button that opens another after the last of them.

    Every tab owns what the pane used to own once — its stack, where in it the
    frame is, and its own frame, kept in the wrap while the tab is off screen
    so coming back to it is not a reload. The frame is cut from the template
    the markup keeps, which is where the sandbox list lives; a frame built any
    other way would be a frame with other powers.
    """
    assert 'id="browser-tabs"' in doc
    assert 'id="btn-browser-newtab"' in doc
    assert 'title="New tab"' in doc
    # A "+" again: the network glyph moved to the key it now names, and a new
    # tab is the one thing a "+" has always meant.
    assert ('title="New tab"><svg><use href="#i-plus"/></svg>' in doc
            or 'title="New tab"><svg><use href="#i-plus"/>' in doc)
    assert 'id="browser-frame-tpl"' in doc
    assert "function browserNewTab(" in doc and "function browserTab(" in doc
    assert "function browserShowTab(" in doc and "function browserCloseTab(" in doc
    assert "const BROWSER_TAB_MAX = 8;" in doc
    # The landing goes to the tab whose frame reported it, not to the one on
    # screen: a tab loading in the background keeps its own stack and name.
    assert "browserTabs.find((t) => t.frame && e.source === t.frame.contentWindow)" in doc
    # The laptop start page is gone with the button that opened it: the "+"
    # opens a tab in here now.
    assert "/start" not in doc and "browserStartUrl" not in doc
    # A tab remembers which of the modes it was left in, so a reload brings it
    # back the way it was: on the computer's own network, or running in the
    # computer's own browser (browserSeedMode).
    assert "function browserSeedMode(" in doc
    assert "tab.lan = !!(rec && rec.lan);" in doc
    assert "tab.fullMode = full || tab.lan ? false : null;" in doc


def test_every_pane_tab_carries_the_proxys_own_sandbox_list(doc):
    """The iframe attribute and the CSP header have to agree word for word, or
    the stricter of the two wins and the page loses a capability it was
    granted. One template, so there is one list to agree with."""
    sandbox = ("allow-scripts allow-forms allow-popups allow-modals "
               "allow-downloads allow-popups-to-escape-sandbox")
    assert f'sandbox="{sandbox}"' in doc
    assert doc.count('sandbox="allow-scripts') == 1
    app_py = (REPO / "app.py").read_text(encoding="utf-8")
    assert f'BROWSE_SANDBOX = ("sandbox allow-scripts' in app_py
    # Spelled out here as well as in app.py, because this is the pinning: the
    # markup and the header are two files that must say the same thing.
    assert f"sandbox {sandbox}" in re.sub(r'"\s*\n\s*"', "", app_py)


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
