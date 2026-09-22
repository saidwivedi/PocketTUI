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


def test_a_tab_is_a_proxy_tab_unless_its_host_is_remembered(doc):
    """The proxy is what a tab is — it runs in the browser being read, so it is
    quick and its sound and video are the device's own — and the stream is where
    the pages the proxy cannot serve go. Which pages those are is remembered by
    host: the key on the address row puts a site on the stream and takes it off
    again, and every tab opened on a remembered host is streamed from its first
    navigation. The switch in Settings is the other way in, for a machine whose
    browser is wanted for everything. The key is a toggle per tab like the
    network key beside it, and it hides that one while it is pressed: a page
    fetched by a browser running on the computer is on the computer's network
    already."""
    assert 'id="btn-browser-full" hidden' in doc
    off = "Stream this site from the computer's Chrome"
    on = ("This site streams from the computer's Chrome; press to use the"
          " lightweight proxy")
    assert f'aria-label="{off}"' in doc and f'title="{off}"' in doc
    assert f'"{on}"' in doc
    assert '+ (host ? " (remembered for " + host + ")" : "");' in doc
    assert "function syncBrowserFull(" in doc and "function browserSetFull(" in doc
    # The default, and the two ways past it: the capability strictly checked,
    # the switch that streams everything, and the hosts that are remembered.
    assert ('  if (!hasCapStrict("browser_full")) return false;\n'
            "  return cfg.browserStreamAll || browserStreamsHost(url);") in doc
    assert "pockettui_browser_stream_all" in doc
    assert "pockettui_browser_stream_hosts" in doc
    assert 'id="browser-stream-toggle"' in doc
    assert ">Stream every tab from the computer's Chrome<" in doc
    # The record is written by the key, on the address the tab is on, and read
    # back by every navigation a proxy tab makes.
    assert "browserRememberStream(browserUrlIn(tab), true);" in doc
    assert "browserRememberStream(browserUrlIn(tab), false);" in doc
    assert ("  } else if (!tab.fullMode && browserStreamsHost(url)) {" in doc)
    # A page the proxy cannot serve hands itself over, and says why.
    assert 'if (d.type === "pockettui-stream") {' in doc
    assert ('"Google wants a real browser here; streaming it from the computer"'
            in doc)
    assert '"Sign-in pages stream from the computer\'s Chrome"' in doc
    assert '"The computer has no browser to stream from"' in doc
    # And the sites on the record are readable, and forgettable, from Settings.
    assert 'id="browser-stream-hosts"' in doc
    assert "function browserSyncStreamHosts(" in doc


def test_a_landing_the_proxy_did_not_serve_hands_its_tab_over(doc):
    """A proxied page that sets location.href to a root-relative path leaves the
    proxy's mount: the frame is sandboxed, so there is no hook to catch it, and
    what lands under `tailscale serve` is the front's bare 404 with the pane's
    address bar still showing the site. Everything the proxy does serve reports
    itself with a pockettui-* message, so a landing that says nothing inside the
    watchdog's window is that escape — and the tab goes to the browser that can
    fetch it, on the address the pane last knew it to be at."""
    assert 'tab.frame.addEventListener("load", () => browserWatchLanding(tab));' in doc
    assert "function browserWatchLanding(" in doc
    assert "const BROWSER_LAND_WAIT = 1200;" in doc
    # Every message from the frame counts as the document speaking, whatever it
    # had to say.
    assert ('if (typeof d.type === "string" && d.type.indexOf("pockettui-") === 0) {\n'
            "    tab.heardAt = Date.now();") in doc
    # Not for a tab that is already streamed, not for a load the pane started and
    # has had no report of yet, and not where there is nothing to hand over to.
    assert "    if (browserIsFull(tab)) return;" in doc
    assert "    if (tab.navigating) return;" in doc
    assert '    if (!url || !hasCapStrict("browser_full")) return;' in doc
    assert '"This site left the proxy; streaming it from the computer"' in doc
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


def test_a_window_a_streamed_page_opens_becomes_a_tab_of_the_pane(doc):
    """window.open in a streamed page makes a target on the computer that the
    pane knows nothing about, so the backend offers it (`newtab`) and the pane
    answers with `show` or `close`. Answered the way a browser does: a tab in
    front, adopting the page that is already loading in that target rather than
    loading it again — and at the cap the tab it was opened from goes there
    instead, because spending it beats the click going nowhere."""
    assert 'if (msg.type === "newtab")' in doc
    assert "function browserPopup(" in doc
    assert "const tab = browserMakeTab(false);" in doc
    assert "tab.fid = String(msg.tab);" in doc
    assert 'tab.target = String(msg.targetId || "");' in doc
    # The pane's own ids are "t<n><rand>" and the computer's popups are "p<n>",
    # so an adopted id can never be one this pane would mint for itself.
    assert 'return "t" + browserFidN + Math.random().toString(36).slice(2, 8);' in doc
    assert 'fullLink.send({ type: "close", tab: msg.tab });' in doc


def test_what_a_streamed_page_asks_is_answered_by_the_pane(doc):
    """A dialog, an HTTP challenge and a file input each stop the page on the
    computer until an answer goes back over the pane's socket.

    The dialogs are the app's own sheets — a page's confirm() is the same
    question in the same words as every other question the app asks — with the
    alert's second answer taken away, since an alert has none. The other two are
    drawn over the picture of the page they belong to: with two panes open and
    tabs behind them there would be no saying whose challenge, or whose file
    dialog, a sheet in the middle of the window was.
    """
    assert 'else if (msg.type === "dialog") view.dialog(msg);' in doc
    assert 'else if (msg.type === "auth") view.auth(msg);' in doc
    assert 'else if (msg.type === "filechooser") view.chooser(msg);' in doc
    assert 'send({ type: "dialog", tab: tabId, accept: accept, text: typed });' in doc
    assert 'await appConfirm(text, { confirmLabel: "OK", okOnly: true, danger: false });' in doc
    assert '$("btn-confirm-cancel").hidden = !!opts.okOnly;' in doc
    assert 'await appConfirm("Leave this page?",' in doc
    # A queue rather than one sheet over another: appConfirm and appPrompt share
    # one resolver, and a second question raised over the first would leave the
    # first page stopped with nobody left to answer it. A background tab's
    # dialog waits for its tab and marks its chip meanwhile.
    assert "function fbAskTurn(" in doc
    assert "tab.chip.classList.toggle(\"ask\", !!(tab.full && tab.full.asking()));" in doc
    # The challenge sheet and the file bar, in the view's own markup.
    assert '<div class="fb-auth" hidden></div><div class="fb-ask" hidden></div>' in doc
    assert 'type: "password"' in doc
    assert 'send({ type: "auth", tab: tabId, cancel: true });' in doc
    # A file input needs a gesture and the page's own press was spent on the
    # canvas, so the bar's button is the gesture that opens the picker.
    assert "function pickFiles(" in doc and "input.click();" in doc
    assert 'send({ type: "files", tab: tabId, paths: paths });' in doc
    assert 'apiURL("api/browser/upload?name=" + encodeURIComponent(file.name))' in doc
    # As many as the op will pass on, said in both files.
    app_py = (REPO / "app.py").read_text(encoding="utf-8")
    assert "const FB_FILES_MAX = 32;" in doc
    assert "BROWSER_FILES_MAX = 32" in app_py


def test_a_download_the_computers_browser_makes_comes_to_this_device(doc):
    """The browser is on the computer, so that is where the file lands. The pane
    says so while it happens and then brings it over — small ones without being
    asked, since the device that asked is the device it was wanted on, and big
    ones on a yes, because that transfer is itself worth a question."""
    assert "function browserDownloaded(" in doc
    assert 'if (msg.type === "download")' in doc
    assert "const BROWSER_DL_MAX = 200 * 1024 * 1024;" in doc
    assert ("if (total && total <= BROWSER_DL_MAX) { downloadFile(path, name, total); return; }"
            in doc)
    assert 'toast("Download cancelled");' in doc


def test_a_chip_carries_its_pages_icon_and_says_when_it_was_given_up(doc):
    """Two things a streamed tab knows about itself that a chip can show: the
    icon the page sent, and that the computer gave its page up to stay inside
    the memory cap — which keeps the tab, so showing it again loads it. An
    absent favicon key is "nothing new" rather than "no icon"."""
    assert 'if (typeof msg.favicon === "string") tab.icon = msg.favicon;' in doc
    assert 'if (typeof msg.discarded === "boolean") tab.discarded = msg.discarded;' in doc
    assert 'icon = el("img", { class: "tab-icon", alt: "" });' in doc
    assert 'tab.chip.classList.toggle("dim", !!tab.discarded);' in doc
    css = (SRC / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"\.tab-icon \{[^}]*width: 16px;", css)
    # The browser swapped under its tabs to stay inside the cap is a line, not
    # an overlay: the tab each pane was showing is already coming back.
    assert 'if (msg.code === "restarted")' in doc


def test_a_folded_row_stops_streaming_and_the_profile_can_be_cleared(doc):
    """A row the column folds away behind the other one's expand is
    display:none, which is nowhere to paint a picture arriving many times a
    second — so the stream stops there exactly as it does when the pane closes.
    And the one place the browsing session lives is a directory on the computer,
    which Settings can throw away once the panes have given up their pages."""
    assert "onFold: (hidden) => browserSetFolded(hidden)," in doc
    assert 'if (inst && inst.onFold) inst.onFold(cls === "side-hidden");' in doc
    assert "function browserSetFolded(" in doc
    assert 'id="btn-browser-clear"' in doc
    assert ">Clear browsing data<" in doc
    assert "async function browserClearProfile(" in doc
    assert 'apiURL("api/browser/reset")' in doc
    # Refused while a tab is open there, so the tabs go first — and a 409 in the
    # moment after that is the race, not the answer.
    assert "for (const pane of Object.values(browserPanes)) pane.clearFulls();" in doc
    assert "if (r.status !== 409) break;" in doc
    assert 'toast("Browser profile cleared");' in doc


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
