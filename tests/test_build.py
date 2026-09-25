"""The src/mobile -> mobile_app.html assembly.

The app is written split and served whole, so the thing worth pinning is that
assembly is a pure concatenation: deterministic, lossless, and leaving the
placeholders for the consumers that substitute them. Content is deliberately not
hashed — every UI change would move the hash and the test would only ever be
updated to match, which proves nothing.
"""

import importlib.util
import json
import re
import shutil
import subprocess
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
    assert 'id="i-m-star"' in doc and 'id="i-m-star-fill"' in doc


def test_the_computers_network_is_a_frame_that_cannot_take_the_top_window(doc):
    """The way through for a page that cannot run framed: an app written to be
    the top window (a portal reading top.EPCM through its views) is fetched
    under the computer's other permission and drawn in the same frame on the
    backend's real origin, so it is a page in its own right. There is no key
    for it any more (founder, 2026-09-24): the landing ladder puts a tab there
    by itself, so the frame keeps a sandbox that grants its origin back and
    withholds the top window, and a frame-breaking page throws instead of
    taking the app away."""
    assert 'id="btn-browser-tab"' not in doc
    assert "Use the computer's network for this tab" not in doc
    assert "function syncBrowserLan(" not in doc
    assert '"browse_tab"' in doc or "hasCapStrict(\"browse_tab\")" in doc
    # No window of the device's own is opened any more — the founder asked for
    # the page in the pane, and nothing here should say otherwise.
    assert "Open in its own window" not in doc
    assert 'window.open("", "_blank")' not in doc
    # The mode is one attribute and the frame is replaced to change it: a
    # sandbox list is read when a frame loads, not when it is set. The list is
    # the template's with allow-same-origin added, never one without a sandbox,
    # except for a PDF, whose viewer is a plugin no sandbox lets run.
    frame = doc[doc.index("function browserFrame("):]
    frame = frame[:frame.index("\n}\n")]
    assert ("  if (tab.lan && tab.pdf) tab.frame.removeAttribute(\"sandbox\");\n"
            "  else if (tab.lan) {\n"
            "    tab.frame.setAttribute(\"sandbox\", tab.frame.getAttribute(\"sandbox\")"
            " + \" allow-same-origin\");\n"
            "  }") in frame
    tpl = doc[doc.index('<template id="browser-frame-tpl">'):]
    tpl = tpl[:tpl.index("</template>")]
    assert "allow-top-navigation" not in tpl and "allow-same-origin" not in tpl
    assert "function browserSetLan(" in doc and "function browserFlipLan(" in doc
    # A tab that has loaded nothing yet goes in through the hop that wipes this
    # origin's storage, never straight to the proxied page: the shell may once
    # have been served from this same address, and its pairing token would
    # still be sitting there. Past that hop the pages are ordinary ones —
    # re-entering would wipe what the page itself has since put there.
    assert '"/enter?to=" + encodeURIComponent(' in doc
    assert "target = tab.primed ? proxied : browserEnterUrl(proxied, rec);" in doc


def test_the_pane_carries_the_way_out_of_the_pane(doc):
    """The founder's arrow: this page in the browser the device runs, rather
    than in the pane. A tester had asked where the
    address a webapp printed went — before the pane it opened on their own
    machine, and since the pane it opens in the pane. The key is the way back
    to that, and an address the device cannot reach on its own goes out as the
    computer's own copy of the page (the tab flavour, the one the landing
    ladder already mints), which is what the relay did for a tapped localhost
    link."""
    assert 'id="btn-browser-out" hidden' in doc
    said = "Open this page in your browser"
    assert f'aria-label="{said}"' in doc and f'title="{said}"' in doc
    # An arrow leaving its box, from the side panes' glyph set.
    assert 'id="i-m-external"' in doc and 'href="#i-m-external"' in doc
    # A key inside the address capsule, left of reload, on the phone and
    # docked alike (founder, v0.9.170 follow-up): docked it is not hidden and
    # the more menu carries no row for it.
    wrap = doc[doc.index('<div id="browser-url-wrap">'):]
    wrap = wrap[:wrap.index("</div>")]
    assert wrap.index('id="browser-url"') < wrap.index('id="btn-browser-out"') < wrap.index('id="btn-browser-reload"')
    assert 'data-more="Open in your browser"' not in doc
    assert ':is(#screen-browser, #screen-browser-2).docked #browser-zoom-wrap { display: none; }' in doc
    assert ':is(#screen-browser, #screen-browser-2).docked :is(#browser-zoom-wrap, #btn-browser-out) { display: none; }' not in doc
    assert 'const keys = [...pane.querySelectorAll("[data-more]")].filter((k) => !k.hidden' in doc
    assert "#btn-browser-out[hidden] { display: none; }" in doc
    # Nothing to hand over is the one state it is not in: a tab with no address
    # yet, which is what a new tab is until it lands somewhere.
    assert 'out.hidden = !browserUrlIn(tab);' in doc
    # The press opens a tab of the device's own browser and leaves this one
    # alone — an anchor click, because Safari's window.open returns null on the
    # noopener path (08-links.js's openUrl, for the same reason).
    assert "function browserOpenOutside(" in doc
    assert 'a.target = "_blank";' in doc
    assert "function browserOutPress(" in doc
    assert 'q("btn-browser-out").addEventListener("click"' in doc
    # A private address goes out as the computer's copy of the page, through
    # the same mint and the same Clear-Site-Data hop the landing ladder uses.
    assert "browserEnterUrl(browserProxied(url, rec), rec)" in doc
    assert ("toast(\"This address is only reachable from the computer;\"\n"
            "            + \" update it to open such pages here\");") in doc
    # And a streamed tab has it in its own menu, where the page's other
    # commands are (43-full-browser.js).
    assert 'item("Open in your browser", () => cb.openExternal());' in doc
    assert "openExternal: () => browserOutPress(tab)," in doc


def test_a_pdf_is_drawn_where_a_plugin_can_run(doc):
    """The founder opened a PDF in a proxy tab and got Chrome's own "This page
    has been blocked": the viewer is a plugin, and a plugin never runs inside
    the sandbox those frames carry. The backend answers such a document with a
    card that names the PDF (browse_pdf_response in app.py), and the pane shows
    it the only two ways there are — the same frame without the sandbox, served
    as a page in its own right, or the computer's own Chrome."""
    assert 'if (d.type === "pockettui-pdf") {' in doc
    assert "function browserPdfShow(" in doc and "function browserPdfLeave(" in doc
    # The tab is on the PDF, not on the card: the address field, the chip, the
    # record and the arrow all read the tab.
    assert ("  browserPush(tab, url);\n"
            '  if (name) { tab.title = name; tab.titleFor = url; }') in doc
    # The other flavour first, taken through the same flip the landing ladder
    # makes rather than a second way of doing it, for this document, with the
    # flavour the tab had remembered so the tab can be given it back.
    assert "function browserFlipLan(" in doc
    assert ("    tab.lanBefore = tab.lan;\n"
            "    tab.pdf = url;\n"
            "    browserFlipLan(tab, true);") in doc
    assert "  if (browserTabAllowed()) {" in doc
    # Then the stream, for that document alone: no host goes on the streamed
    # record for the sake of one file, which is what browserSetFull does not do.
    assert ('    toast("PDFs stream from the computer\'s Chrome");\n'
            "    browserSetFull(tab, true);") in doc
    assert "  if (tab.pdfAsked) {" in doc
    assert ('    toast("Use the arrow in the address field to open this PDF'
            ' in your browser");') in doc
    # And the tab's own flavour back at its next address, in the navigation that
    # is already being made.
    assert "  if (tab.pdf && url !== tab.pdf) browserPdfLeave(tab);" in doc
    # The frame made for the PDF has no sandbox, so it is given up at the next
    # address even when the flavour stays.
    assert ("  if (!!tab.lan !== back || (tab.frame && !tab.frame.hasAttribute(\"sandbox\"))) {\n"
            "    browserFlipLan(tab, back);") in doc
    # A PDF has no shim in it to report its landing, and the arrow reads the
    # answer the user gave rather than the one the document took.
    assert "    if (tab.pdf) return;" in doc
    assert "  if (tab.pdf ? tab.lanBefore : tab.lan) return true;" in doc


def test_the_monitor_key_is_gone_where_there_is_nothing_to_stream_from(doc):
    """Both keys on the address row are hidden in JS by the attribute alone, and
    .icon-btn is display:inline-flex — which the attribute does not undo. A
    computer with no browser to stream from must not draw the key."""
    assert '#btn-browser-full[hidden] { display: none; }' in doc
    assert '#btn-browser-tab' not in doc
    assert 'id="btn-browser-full" hidden' in doc
    assert 'q("btn-browser-full").hidden = !hasCapStrict("browser_full");' in doc


# The addresses the key hands to the computer rather than to this device's
# browser, run as the shell runs them. A table rather than a reading of the
# source: the rule is four lists in a trench coat, and only the answers matter.
ADDRESS_CASES = [
    ("localhost", True),
    ("LocalHost", True),
    ("127.0.0.1", True),
    ("127.1.2.3", True),
    ("0.0.0.0", True),
    ("10.1.2.3", True),
    ("172.20.0.1", True),
    ("172.16.0.1", True),
    ("172.31.255.1", True),
    ("192.168.1.5", True),
    ("169.254.10.1", True),
    ("[::1]", True),
    ("dev.local", True),
    ("printer.lan", True),
    ("wiki.internal", True),
    ("gpu.localnet", True),        # the suffix an institute hands out
    ("mybox", True),               # a name with no dot in it is a LAN name
    ("mybox.", True),              # a root dot is not part of the name
    # Public, every one of them: the device reaches these on its own, and the
    # computer has nothing to add.
    ("172.32.0.1", False),         # just outside 172.16/12
    ("172.15.0.1", False),
    ("11.0.0.1", False),
    ("example.net", False),
    ("box.example.net", False),
    ("8.8.8.8", False),
    ("", False),
    # This computer's own name, which is private-looking and the one address
    # every paired device does reach: the app itself is served from it.
    ("computer.example.net", False),
]


def test_the_way_out_tells_a_private_address_from_a_public_one(doc, tmp_path):
    """Which of the two ways a press takes, run rather than read. The shell
    answers it with 08-links.js's list — the terminal's own, so a link tapped
    there and a page in the pane cannot disagree — plus the institute suffix and
    minus the backend's own name, and those two corrections are what this pins.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    src = "\n".join(_js_chunk(doc, name) for name in (
        "const PRIVATE_HOST_SUFFIXES", "function backendHost",
        "function isPrivateHost", "function browserAddressIsPrivate"))
    harness = f"""
const cfg = {{}};
globalThis.location = {{ href: "https://pockettui.com/" }};
function apiURL(p) {{ return "https://computer.example.net/" + p; }}
{src}
const cases = {json.dumps(ADDRESS_CASES)};
console.log(JSON.stringify(cases.map(([h]) => browserAddressIsPrivate(h))));
"""
    f = tmp_path / "addr.mjs"
    f.write_text(harness, encoding="utf-8")
    out = subprocess.run([node, str(f)], check=True, capture_output=True)
    got = json.loads(out.stdout.decode())
    assert got == [want for _, want in ADDRESS_CASES], list(
        zip([h for h, _ in ADDRESS_CASES], got))


def _js_chunk(doc, head):
    """One top-level declaration out of the assembled shell, brace to brace.

    The fragments are concatenated verbatim, so a declaration starts at column
    zero and ends at the first line that is a lone closing brace — or, for a
    const, at its own semicolon."""
    at = doc.index("\n" + head) + 1
    if head.startswith("const"):
        return doc[at:doc.index(";\n", at) + 1]
    end = doc.index("\n}\n", at) + 3
    return doc[at:end]


def test_the_address_capsule_holds_the_page_mode_and_reload(doc):
    """Back, forward, then the mode key on the row (founder: visible, lit
    while on, a tooltip saying what it does), then Safari's capsule: a glyph
    at its left that only mirrors the tab's mode, the address, reload at its
    right. The capsule glyph opens no menu. The network key that sat beside
    the monitor key is gone (founder, 2026-09-24): the landing ladder puts a
    tab on the computer's network, and the glyph still says so."""
    at = {name: doc.index(f'id="{name}"')
          for name in ("btn-browser-back", "btn-browser-fwd", "browser-url-wrap",
                       "browser-url", "btn-browser-reload", "btn-browser-full")}
    mode = doc.index('<span class="browser-mode" aria-hidden="true">')
    assert (at["btn-browser-back"] < at["btn-browser-fwd"]
            < at["btn-browser-full"]
            < at["browser-url-wrap"] < mode < at["browser-url"]
            < at["btn-browser-reload"])
    assert "browser-mode-wrap" not in doc and "menuRows" not in doc
    assert "#btn-browser-fwd + #btn-browser-full { margin-left: 8px; }" in doc
    assert ".browser-mode { margin-left: 3px; pointer-events: none; cursor: default; }" in doc
    assert 'glyph.setAttribute("href", full ? "#i-m-monitor" : lan ? "#i-m-lan" : "#i-m-globe");' in doc
    # On is the accent over the pressed fill, not only the fill every hover has.
    assert ('.browser-topbar #btn-browser-full[aria-pressed="true"] {\n'
            "  color: var(--umber);\n"
            "  background: linear-gradient(var(--umber-soft), var(--umber-soft)), var(--m-fill2);") in doc
    # Unfocused, the field shows the host; focused, the whole address.
    assert "f.value = document.activeElement === f ? browserFieldUrl : browserHostOf(browserFieldUrl);" in doc


def test_a_tab_is_a_proxy_tab_unless_its_host_is_remembered(doc):
    """The proxy is what a tab is — it runs in the browser being read, so it is
    quick and its sound and video are the device's own — and the stream is where
    the pages the proxy cannot serve go. Which pages those are is remembered by
    host: the key on the address row puts a site on the stream and takes it off
    again, and every tab opened on a remembered host is streamed from its first
    navigation. The switch in Settings is the other way in, for a machine whose
    browser is wanted for everything. The key is a toggle per tab, and a
    streamed tab is never on the computer's network flavour as well: a page
    fetched by a browser running on the computer is on the computer's network
    already."""
    assert 'id="btn-browser-full" hidden' in doc
    off = "Stream this site from the computer's Chrome"
    on = "Streaming from the computer's Chrome — press to go back to the proxy"
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


def test_a_landing_the_proxy_did_not_serve_is_put_back_then_laddered(doc):
    """A proxied page that sets location.href to a root-relative path leaves the
    proxy's mount: the frame is sandboxed, so there is no hook to catch it, and
    what lands under `tailscale serve` is the front's bare 404 with the pane's
    address bar still showing the site. Everything the proxy does serve reports
    itself with a pockettui-* message, so a landing that says nothing inside the
    watchdog's window is that escape. The first one is loaded again in the proxy,
    on the address the pane last knew; only a second one soon after is a landing
    that did not work, and it goes to the landing ladder like every other
    failure. YouTube escapes this way and plays in the proxy, and remembering
    its host put it on the stream for good."""
    assert 'tab.frame.addEventListener("load", () => browserWatchLanding(tab));' in doc
    assert "function browserWatchLanding(" in doc
    assert "const BROWSER_LAND_WAIT = 1200;" in doc
    assert "const BROWSER_ESCAPE_AGAIN = 10000;" in doc
    # Every message from the frame counts as the document speaking, whatever it
    # had to say.
    assert ('if (typeof d.type === "string" && d.type.indexOf("pockettui-") === 0) {\n'
            "    tab.heardAt = Date.now();") in doc
    # Not for a tab that is already streamed, not for a load the pane started and
    # has had no report of yet.
    assert "    if (browserIsFull(tab)) return;" in doc
    assert "    if (tab.navigating) return;" in doc
    # A landing that held clears the count.
    assert ("    if (tab.heardAt >= at - BROWSER_LAND_GRACE) {         // the proxy's own page\n"
            "      // Held for the whole window with nothing newer landing, so whatever left\n"
            "      // before is not leaving on every load.\n"
            "      tab.escapes = 0;") in doc
    body = doc[doc.index("function browserWatchLanding("):]
    body = body[:body.index("\n}\n")]
    # First escape: back into the proxy, silently; second: the ladder, this tab
    # only, with nothing remembered.
    assert "tab.escapes = (again ? tab.escapes : 0) + 1;" in body
    assert "tab.escapedAt = { url: url, at: now };" in body
    assert ("    if (tab.escapes < 2) {\n"
            "      browserNavigateIn(tab, url, false);\n"
            "      return;\n"
            "    }") in body
    assert '    browserLandFailed(tab, "left the proxy twice", true);' in body
    assert "browserSwapMode(tab, true);" not in body
    assert "browserRememberStream" not in body
    assert "This site left the proxy" not in doc
    # The proxy's own hand-off still remembers the host.
    hand = doc[doc.index("function browserHandOff("):]
    hand = hand[:hand.index("\n}\n")]
    assert "browserRememberStream(url, true);" in hand
    # A streamed tab is zoomed on the computer — there is no element here to
    # scale, only a picture of one — and its history is the real browser's.
    assert "tab.full.setZoom(factor);" in doc
    assert 'browserFullSend(tab, delta < 0 ? "back" : "fwd");' in doc
    # Closing a tab closes the page on the computer; closing the pane does not,
    # for the reason a frame keeps its document.
    assert 'browserFullSend(tab, "close");' in doc
    assert "function browserHideFulls(" in doc


def test_media_sites_the_old_watchdog_streamed_are_taken_off_once(doc):
    """The old watchdog remembered a host whenever a page left the proxy, which
    put YouTube on the stream for good. Nothing in the record says which hosts
    were its, so the media sites are taken off by registrable name, once, under a
    version number, and a toast says so only if something went."""
    assert "get browserStreamHostsVer() {" in doc
    assert '"pockettui_browser_stream_hosts_ver"' in doc
    assert ('const BROWSER_MEDIA_SITES = ["youtube.com", "youtu.be", "vimeo.com", "twitch.tv",\n'
            '                             "netflix.com", "spotify.com", "soundcloud.com",\n'
            '                             "dailymotion.com"];') in doc
    mig = doc[doc.index("function browserMigrateStreamHosts("):]
    mig = mig[:mig.index("\n}\n")]
    assert "if (cfg.browserStreamHostsVer >= 1) return;" in mig
    assert 'name === m || name.endsWith("." + m)' in mig
    assert "cfg.browserStreamHostsVer = 1;" in mig
    assert ('if (dropped) toast("YouTube and other media sites now open in the proxy'
            ' again", 6000);') in mig
    # Run once, at load.
    assert "\nbrowserMigrateStreamHosts();\n" in doc


def test_a_page_that_did_not_land_is_tried_on_the_computers_network_once(doc):
    """The landing ladder that replaced the network key (founder, 2026-09-24).
    Whatever used to raise the hint bar, or stream a page that left the proxy
    twice, now asks the ladder: a proxy tab is loaded again, silently and once,
    in the computer's network flavour; a tab already there, or a computer that
    cannot give it, gets the hint bar as before. Nothing is written for the
    host, the pane's record or the tabs the page opens, and the flavour lasts
    until the tab is sent to another host."""
    fail = _js_chunk(doc, "function browserLandFailed(")
    assert "  if (tab.lanTrying) return;" in fail
    assert ("  if (browserLanNext(tab)) browserLanRetry(tab, reason, escaped);\n"
            "  else browserLandLast(tab, reason, escaped);") in fail
    nxt = _js_chunk(doc, "function browserLanNext(")
    assert ("  return !demoMode && !tab.lan && !tab.pdf && !browserIsFull(tab)"
            " && browserTabAllowed();") in nxt
    retry = _js_chunk(doc, "async function browserLanRetry(")
    # The user's own navigation, a closed tab and a torn-down pane all win over
    # a retry that was waiting for its token; the stale watchdog timer is
    # retired with the frame.
    assert "  const rec = browserTabTokenFresh() || await browserEnsureTabToken();" in retry
    assert "  if (!browserAlive(tab, gen) || tab.navs !== navs) return;" in retry
    assert "  if (!rec) { browserLandLast(tab, reason, escaped); return; }" in retry
    assert "  tab.landGen++;\n  browserSetLan(tab, true);" in retry
    for body in (fail, nxt, retry):
        assert "browserRememberStream" not in body
        assert "localStorage" not in body and "cfg." not in body
    # Every failure the hint bar was raised on goes through the ladder now, and
    # the hint itself is only ever its last step (browserLandLast).
    assert doc.count("browserHint(tab, ") == doc.count("browserHint(tab, reason)") == 2
    assert 'browserLandFailed(tab, "status " + d.status' in doc
    assert 'if (ladder) browserLandFailed(tab, "error " + (code || "?"));' in doc
    assert 'if (d.busted) browserLandFailed(tab, "top navigation blocked");' in doc
    assert 'else if (d.empty) browserLandFailed(tab, "empty page");' in doc
    # A new host starts on the proxy again; back, forward and reload do not.
    assert ("  if (push && tab.lan && !tab.pdf\n"
            "      && browserHostOf(url) !== browserHostOf(browserUrlIn(tab))) {\n"
            "    browserFlipLan(tab, false);\n"
            "  }\n"
            "  tab.navs++;") in doc
    # No inheritance by the tabs a page opens, and nothing in the record.
    assert "function browserMakeTab() {" in doc
    assert "browserMakeTab(tab.lan)" not in doc
    assert "{ url: u, lan: true }" not in doc
    # No step from a streamed tab down to the flavour is left anywhere.
    assert "browserFlipLan(tab, true);" in doc
    assert doc.count("browserFlipLan(tab, true);") == 1        # the PDF's


def test_a_window_a_streamed_page_opens_becomes_a_tab_of_the_pane(doc):
    """window.open in a streamed page makes a target on the computer that the
    pane knows nothing about, so the backend offers it (`newtab`) and the pane
    answers with `show` or `close`. Answered the way a browser does: a tab in
    front, adopting the page that is already loading in that target rather than
    loading it again — and at the cap the tab it was opened from goes there
    instead, because spending it beats the click going nowhere."""
    assert 'if (msg.type === "newtab")' in doc
    assert "function browserPopup(" in doc
    assert "const tab = browserMakeTab();" in doc
    assert "tab.fid = String(msg.tab);" in doc
    assert 'tab.target = String(msg.targetId || "");' in doc
    # The pane's own ids are "t<n><rand>" and the computer's popups are "p<n>",
    # so an adopted id can never be one this pane would mint for itself.
    assert 'return "t" + browserFidN + Math.random().toString(36).slice(2, 8);' in doc
    assert 'fullLink.send({ type: "close", tab: msg.tab });' in doc


def test_a_popup_opens_in_the_mode_its_site_would_get(doc):
    """A window a streamed page opens is adopted streamed only when its own site
    would be: a remembered host, the stream-all switch, or a sign-in page the
    opener's session lives behind. Anything else (a YouTube link on a streamed
    site) is closed on the computer and opened as a proxy tab, where it has
    sound. The sign-in list mirrors the backend's BROWSE_HANDOFF_HOSTS."""
    body = doc[doc.index("function browserPopup("):]
    body = body[:body.index("\n}\n")]
    assert ('if (url && url !== "about:blank" && !browserFullWanted(url) '
            "&& !browserSignInPage(url)) {") in body
    decide = body.index("!browserFullWanted(url)")
    assert body.index('fullLink.send({ type: "close", tab: msg.tab });') > decide
    assert "browserOpenFrom(from, url);" in body
    assert body.index("browserOpenFrom(from, url);") < body.index("tab.fullMode = true;")
    assert "const BROWSER_SIGNIN_HOSTS = {" in doc
    assert "BROWSE_HANDOFF_HOSTS in" in doc
    app = (REPO / "app.py").read_text()
    block = app[app.index("BROWSE_HANDOFF_HOSTS: dict = {"):]
    block = block[:block.index("}")]
    js = doc[doc.index("const BROWSER_SIGNIN_HOSTS = {"):]
    js = js[:js.index("};")]
    assert (sorted(re.findall(r'"([^"]+)":', block))
            == sorted(re.findall(r'"([^"]+)":', js)))


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
    assert re.search(r"\.tab-icon \{[^}]*width: 14px;", css)
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


def test_the_window_controls_sit_on_the_tool_row(doc):
    """Safari's tool row: the address in the middle, the page's and the pane's
    keys at its right end (star, zoom, the more menu, then expand and close
    after a short rule), and the tabs in a band of their own under it."""
    at = {name: doc.index(f'id="{name}"')
          for name in ("browser-url-wrap", "btn-browser-star",
                       "browser-zoom-wrap", "btn-browser-zoom",
                       "btn-browser-expand", "btn-browser-close",
                       "browser-tabs", "browser-tab-row", "btn-browser-newtab")}
    bar = doc.index('class="topbar browser-topbar"')
    tools = doc.index('<span class="browser-tools">')
    assert (bar < at["browser-url-wrap"] < tools < at["btn-browser-star"]
            < at["browser-zoom-wrap"] < at["btn-browser-zoom"]
            < at["btn-browser-expand"] < at["btn-browser-close"]
            < at["browser-tabs"] < at["browser-tab-row"] < at["btn-browser-newtab"])
    assert 'id="browser-tab-actions"' not in doc
    # Tabs are as wide as their names, capped, and shrink only when the row
    # would overflow; the row is as wide as its tabs, so the "+" follows the
    # last one, and a row too wide for the pane scrolls inside itself.
    css = (SRC / "styles.css").read_text(encoding="utf-8")
    chip = re.search(r"\.tab-chip \{[^}]*\}", css).group(0)
    assert "flex: 0 1 auto;" in chip and "min-width: 56px;" in chip and "max-width: 180px;" in chip
    row = re.search(r"#browser-tab-row \{[^}]*\}", css).group(0)
    assert "flex: 0 1 auto;" in row and "overflow-x: auto;" in row
    plus = re.search(r"#btn-browser-newtab \{[^}]*\}", css).group(0)
    assert "margin-left: 4px;" in plus and "flex: 0 0 26px;" in plus


def test_the_bookmarks_bar_is_a_row_of_links_not_a_second_row_of_tabs(doc):
    """Two rows of cards a few pixels apart read as one thing twice. The strip
    above the address row is the pane's tabs; this one is links, each one only
    the name of the page it saves — no star glyph before it."""
    assert 'el("span", { class: "bm-name" }, b.title || browserMarkName(url))' in doc
    assert 'svgIcon("i-star"), el("span", { class: "bm-name" }' not in doc
    # The title alone is what is capped and ellipsised; the link's own padding
    # sits outside it.
    assert ".bm-name {" in doc and "text-overflow: ellipsis;" in doc
    # No card: the border and the filled background the chips used are gone.
    body = doc[doc.index(".bm-chip {"):doc.index(".bm-del {")]
    assert "border: 1px solid" not in body and "var(--card-2)" not in body


def test_the_tab_strip_sits_under_the_tool_row(doc):
    """Where Safari puts it: the tool row, then the tabs, then the bookmarks,
    then the page."""
    assert (doc.index('class="topbar browser-topbar"') < doc.index('id="browser-tabs"')
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
    assert 'title="New tab"><svg><use href="#i-m-plus"/></svg>' in doc
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


def test_a_session_switch_leaves_the_pane_one_empty_tab(doc):
    """The rail's stash puts every open strip away with the session being left,
    and the panes then drop their tabs: without that the next session's globe
    reopened the last session's tabs. Only the stash path asks for it; a pane
    closed and reopened in the same session keeps its pages. A pane that is
    closed at the switch but still holds pages is stashed too, as a record
    that restores without opening the pane."""
    assert ('    view.browser = browserStash("browser");\n'
            '    view.browser2 = browserStash("browser#2");\n') in doc
    assert "    browserTeardown(true);\n" in doc
    assert doc.count("browserTeardown(true)") == 1
    pane = doc[doc.index("function browserTeardown(fresh = false) {\n  const wasDocked"):]
    pane = pane[:pane.index("\n}\n")]
    assert ("  if (fresh) {\n"
            "    browserTabs = [browserNewTab()];\n"
            "    browserActive = 0;") in pane
    assert ("function browserTeardown(fresh = false) {\n"
            "  for (const pane of Object.values(browserPanes)) pane.teardown(fresh);") in doc
    # The closed pane's half: held pages are stashed and the stash is taken on
    # a switch even when nothing else is up.
    assert "  held: () => browserTabs.some((t) => t.idx >= 0 || !!t.title)," in doc
    assert "  return pane && (pane.isOpen() || pane.held()) ? pane.stash() : null;" in doc
    assert "  if (browserAnyOpen() || browserAnyHeld()) {" in doc
    assert ("    } else if (browserAnyHeld() && name !== currentSession && currentSession) {\n"
            "      stashFileView(currentSession, null);") in doc
    # And a record left closed goes back closed: no open, no history entry.
    restore = doc[doc.index("function browserRestore(s) {"):]
    restore = restore[:restore.index("\n}\n")]
    assert "  if (!s.docked) { syncBrowserNav(); return; }" in restore
    assert "pushState" not in restore


def test_the_pane_offers_the_stream_where_a_proxied_page_did_not_work(doc):
    """A bar over the page, naming the key that would fix it.

    The proxy serves pages it cannot make work, and until now the only thing
    that said so was a toast about the load. The bar is the offer: the monitor
    glyph so the key is recognised on the row afterwards, the words, one press
    that does what the key does, and a cross. Inside the wrap, so it covers the
    page it is about and never the address row; a class rather than an id,
    because the second pane is a copy of this markup.
    """
    assert 'class="browser-hint" hidden' in doc
    assert doc.index('class="browser-hint" hidden') > doc.index('id="browser-wrap"')
    assert doc.index('class="browser-hint" hidden') < doc.index('id="browser-frame-tpl"')
    assert 'class="browser-hint-glyph" aria-hidden="true"><use href="#i-m-monitor"/>' in doc
    assert ">Stream</button>" in doc
    assert 'class="browser-hint-x"' in doc
    assert "Not working here? Stream this site from the computer's Chrome" in doc
    # The ways a page says it did not work, and the one bar they all raise once
    # the landing ladder's network step is spent.
    assert "function browserHint(" in doc and "function browserShowHint(" in doc
    assert "const ladder = !BROWSE_HINT_SKIP[code];" in doc
    assert "if (BROWSE_WALL_CODES[d.status]) {" in doc
    assert 'if (d.type === "pockettui-health") {' in doc
    # Never twice for a site, never on a streamed tab, never where there is no
    # browser to stream from — and down again on the tab's next page.
    assert "browserHintShown[host]" in doc
    assert "if (browserIsFull(tab)) return;" in doc
    assert "if (tab === browserHintFor) browserHideHint();" in doc
    # And the first proxy page this device opens says what the key is for, once.
    assert "function browserHintTip(" in doc
    assert 'BROWSER_HINT_SEEN_KEY = "pockettui_browser_hint_seen"' in doc


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


def _term_tap_handler(doc):
    """The terminal's tap rule, from its window constant to the end of the
    #term-host click listener (the touchend pair reader sits in between)."""
    at = doc.index("\nconst TERM_DOUBLE_TAP_MS") + 1
    click = doc.index('$("term-host").addEventListener("click"', at)
    return doc[at:doc.index("\n});\n", click) + 5]


# Each case is (touch, alternate buffer, steps) -> where the caret is after
# every step. A step is ("tap", ms) for a touchstart/touchend pair at that event
# time, ("drag", ms) for one that travelled, or ("click", ms) for the click the
# browser synthesizes. Real iOS may deliver a pair's clicks late or drop the
# second one, so the pair is read from the touches alone. The alternate buffer
# is where Claude Code runs, so it must not change where a tap goes.
TERM_TAP_CASES = [
    ("touch single tap", True, False,
     [("tap", 0), ("click", 10)], [None, "compose"]),
    ("touch taps far apart", True, False,
     [("tap", 0), ("click", 10), ("tap", 900), ("click", 910)],
     [None, "compose", "compose", "compose"]),
    ("touch double tap", True, False,
     [("tap", 0), ("click", 10), ("tap", 200), ("click", 210)],
     [None, "compose", "term", "term"]),
    ("touch triple tap starts a new pair", True, False,
     [("tap", 0), ("click", 10), ("tap", 200), ("click", 210),
      ("tap", 400), ("click", 410)],
     [None, "compose", "term", "term", "term", "compose"]),
    ("touch double tap past the window is two singles", True, False,
     [("tap", 0), ("click", 10), ("tap", 600), ("click", 610)],
     [None, "compose", "compose", "compose"]),
    ("touch second click swallowed", True, False,
     [("tap", 0), ("tap", 100), ("click", 110)], [None, "term", "term"]),
    ("touch clicks both late", True, False,
     [("tap", 0), ("tap", 150), ("click", 250), ("click", 750)],
     [None, "term", "term", "term"]),
    ("touch drag is not half a pair", True, False,
     [("tap", 0), ("click", 10), ("drag", 150), ("click", 160)],
     [None, "compose", "compose", "compose"]),
    ("touch in the alternate buffer", True, True,
     [("tap", 0), ("click", 10)], [None, "compose"]),
    ("touch double tap in the alternate buffer", True, True,
     [("tap", 0), ("click", 10), ("tap", 200), ("click", 210)],
     [None, "compose", "term", "term"]),
    ("real keyboard", False, False, [("click", 0)], ["term"]),
    ("real keyboard double tap", False, False,
     [("click", 0), ("click", 200)], ["term", "term"]),
]


@pytest.mark.parametrize("name,touch,alt,steps,want", TERM_TAP_CASES,
                         ids=[c[0] for c in TERM_TAP_CASES])
def test_a_terminal_tap_on_touch_types_into_the_box(doc, tmp_path, name,
                                                    touch, alt, steps, want):
    """Run rather than read: on touch a single tap puts the caret in the
    composer and a double tap, read from touchend event times, puts it in the
    terminal, whichever buffer is up (Claude Code lives in the alternate one)
    and however late or few the clicks are; beside a real keyboard every tap
    goes to the terminal. A lone touchend focuses nothing, and a double tap
    leaves the box's text where it was."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    harness = f"""
let now = 10000;
Date.now = () => now;
let focused = null, composeOpen = true;
const on = {{}};
const selectEndedAt = 0, dragScrolled = false, edgeSwipe = false, termGesture = false;
const box = {{ id: "compose-text", className: "", tagName: "TEXTAREA", value: "half a line",
  focus() {{ focused = "compose"; document.activeElement = box; }} }};
const helper = {{ id: "", className: "xterm-helper-textarea", tagName: "TEXTAREA" }};
const document = {{ activeElement: null }};
const host = {{ addEventListener(ev, fn) {{ (on[ev] = on[ev] || []).push(fn); }} }};
function $(id) {{ return id === "term-host" ? host : id === "compose-text" ? box : null; }}
const dbgLines = [];
function dbg(...p) {{ dbgLines.push(p.join(" ")); }}
function touchOnly() {{ return {json.dumps(touch)}; }}
function setCompose() {{ throw new Error("the docked strip is already open"); }}
const term = {{
  buffer: {{ active: {{ type: {json.dumps("alternate" if alt else "normal")} }} }},
  focus() {{ focused = "term"; document.activeElement = helper; }},
}};
{_term_tap_handler(doc)}
const fire = (ev, e) => (on[ev] || []).forEach(fn => fn(e));
const pt = (x) => ({{ clientX: x, clientY: 50 }});
const got = [];
for (const [kind, ms] of {json.dumps(steps)}) {{
  now = 10000 + ms;
  const t = 5000 + ms;
  if (kind === "click") fire("click", {{}});
  else {{
    fire("touchstart", {{ timeStamp: t - 40, touches: [pt(50)] }});
    if (kind === "drag") fire("touchmove", {{ timeStamp: t - 20, touches: [pt(90)] }});
    fire("touchend", {{ timeStamp: t, touches: [] }});
  }}
  got.push(focused);
}}
console.log(JSON.stringify({{ got, value: box.value, dbgLines }}));
"""
    f = tmp_path / "tap.mjs"
    f.write_text(harness, encoding="utf-8")
    out = json.loads(subprocess.run([node, str(f)], check=True,
                                    capture_output=True).stdout.decode())
    assert out["got"] == want
    assert out["value"] == "half a line"
    lines = out["dbgLines"]
    if not touch:
        assert lines == []
        return
    # Every accepted touch says its gap and how it was read; every pair says
    # where the caret landed; every click says whether the pair swallowed it.
    taps = [s for s in steps if s[0] == "tap"]
    assert len([l for l in lines if l.startswith("tap: touch gap=")]) == len(taps)
    for l in lines:
        if l.startswith("tap: terminal (double tap)"):
            assert "active=textarea.xterm-helper-textarea" in l
    clicks = [l for l in lines if l.startswith(("tap: composer", "tap: click suppressed"))]
    assert len(clicks) == len([s for s in steps if s[0] == "click"])
    # And every touch that was not taken says why.
    drags = [s for s in steps if s[0] == "drag"]
    assert [l for l in lines if l.startswith("tap: touch rejected")] == \
        ["tap: touch rejected moved(40px)"] * len(drags)


def test_a_terminal_tap_keeps_every_gesture_guard(doc):
    handler = _term_tap_handler(doc)
    assert handler.count("Date.now() - selectEndedAt < 350") == 2
    assert handler.count("!term || dragScrolled || edgeSwipe || termGesture") == 1
    # The touchend reader checks the same guards one by one, naming each.
    for why in ('"fingers="', '"selectEnded "', '"noterm"', '"dragScrolled"',
                '"edgeSwipe"', '"termGesture="', '"multi"', '"moved("', '"long("'):
        assert f'reject({why}' in handler
    assert 'host.addEventListener("touchcancel", (e) => {' in handler
    assert "e.detail" not in handler
    # The pair is timed by the touch's own event time, not by when the click
    # got round to being dispatched.
    assert "termTapAt = pair ? null : e.timeStamp;" in handler
    assert 'host.addEventListener("touchend", (e) => {' in handler
    assert "}, { passive: true });" in handler


@pytest.mark.parametrize("name,touch,active,stopped", [
    ("touch, box focused", True, "compose", True),
    ("touch, pair already moved focus to xterm", True, "term", False),
    ("touch, nothing focused", True, None, False),
    ("laptop, box focused", False, "compose", False),
])
def test_a_tap_with_the_box_focused_keeps_xterm_off_the_press(doc, tmp_path, name,
                                                              touch, active, stopped):
    """With the caret in the box, the tap's mousedown is held on the host in
    the capture phase, so xterm's own handler (preventDefault + focus its
    textarea) never runs and the box keeps focus: no blur, no keyboard flap.
    A pair has focused xterm from touchend before its mousedowns arrive, so
    they pass; the laptop is never touched."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    harness = f"""
let now = 10000;
Date.now = () => now;
let composeOpen = true;
const caps = {{}};
const selectEndedAt = 0, dragScrolled = false, edgeSwipe = false, termGesture = false;
const box = {{ id: "compose-text", className: "", tagName: "TEXTAREA", focus() {{}} }};
const helper = {{ id: "", className: "xterm-helper-textarea", tagName: "TEXTAREA" }};
const document = {{ activeElement: {{ compose: box, term: helper }}[{json.dumps(active)}] || null }};
const host = {{ addEventListener(ev, fn, opt) {{ if (opt === true) caps[ev] = fn; }} }};
function $(id) {{ return id === "term-host" ? host : id === "compose-text" ? box : null; }}
const dbgLines = [];
function dbg(...p) {{ dbgLines.push(p.join(" ")); }}
function touchOnly() {{ return {json.dumps(touch)}; }}
function setCompose() {{}}
const term = {{ focus() {{}} }};
{_term_tap_handler(doc)}
let prevented = false, stopped = false;
caps.mousedown({{ preventDefault() {{ prevented = true; }}, stopPropagation() {{ stopped = true; }} }});
console.log(JSON.stringify({{ prevented, stopped, still: document.activeElement === box, dbgLines }}));
"""
    f = tmp_path / "md.mjs"
    f.write_text(harness, encoding="utf-8")
    out = json.loads(subprocess.run([node, str(f)], check=True,
                                    capture_output=True).stdout.decode())
    assert out["prevented"] is stopped and out["stopped"] is stopped
    assert out["dbgLines"] == (["tap: mousedown kept in composer"] if stopped else [])
    if active == "compose":
        assert out["still"]


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


def test_a_page_that_escapes_twice_ends_in_the_stream_after_the_network(doc):
    """The watchdog's old hand-off, kept as the ladder's last rung for a page
    that leaves the proxy twice: the computer's network first, and where that
    did not keep it either (or cannot be had), this tab is streamed with the
    old toast. Every other failure still ends at the bar, and nothing is put
    on the host record."""
    assert '    browserLandFailed(tab, "left the proxy twice", true);' in doc
    last = _js_chunk(doc, "function browserLandLast(")
    assert ('  if ((escaped || tab.lanEscaped) && url && hasCapStrict("browser_full")) {'
            in last)
    assert '"This page left the proxy twice; streaming it from the computer"' in last
    assert "browserSwapMode(tab, true);" in last
    assert "browserRememberStream" not in last
    assert last.rstrip().endswith("browserHint(tab, reason);\n}")
    retry = _js_chunk(doc, "async function browserLanRetry(")
    assert "  if (!rec) { browserLandLast(tab, reason, escaped); return; }" in retry
    assert "  tab.lanEscaped = !!escaped;" in retry
    # The mark goes with the flavour.
    flip = _js_chunk(doc, "function browserFlipLan(")
    assert "  if (!on) tab.lanEscaped = false;" in flip



def _node_json(tmp_path, name, harness):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    f = tmp_path / name
    f.write_text(harness, encoding="utf-8")
    return json.loads(subprocess.run([node, str(f)], check=True,
                                     capture_output=True).stdout.decode())


def _explorer_popstate(doc):
    at = doc.index('\nwindow.addEventListener("popstate", () => {') + 1
    return doc[at:doc.index("\n});\n", at) + 5]


def test_the_cross_over_a_docked_file_closes_the_file_not_the_pane(doc, tmp_path):
    """Founder, 2026-09-25: closing a PDF opened in the docked explorer closed
    the whole pane. The editor's, the reader's and the viewer's cross put the
    file away (the back arrow's and Escape's close) and leave the listing it
    came from; only the listing's own cross closes the pane."""
    loop = _js_chunk(doc, 'for (const name of ["screen-editor", "screen-reader", "viewer"]) {')
    out = _node_json(tmp_path, "cross.mjs", f"""
const calls = [];
const btns = {{}};
function mk(cls) {{ const b = {{ cls, on: {{}},
  addEventListener(ev, fn) {{ this.on[ev] = fn; }} }}; return b; }}
function $(name) {{
  btns[name] = {{ ".dock-expand": [mk("expand")], ".dock-close": [mk("close")] }};
  return {{ querySelectorAll: (sel) => btns[name][sel] || [] }};
}}
let filesViewOwner = "files#2";
const filesPanes = {{}};
function refit() {{}}
function closeDockedFileView() {{ calls.push("file"); return true; }}
function closeDockedFiles(id) {{ calls.push("pane " + id); }}
{loop}
for (const name of ["screen-editor", "screen-reader", "viewer"]) btns[name][".dock-close"][0].on.click();
console.log(JSON.stringify(calls));
""")
    assert out == ["file", "file", "file"]
    for view in ("screen-editor", "screen-reader", "viewer"):
        at = doc.index(f'id="{view}"')
        bar = doc[at:doc.index('dock-close"', at) + 200]
        assert 'aria-label="Close this file"' in bar
    # The listing's own cross is still the pane's way out.
    assert 'id="btn-files-close"' in doc
    assert ('for (const btn of root.querySelectorAll(".dock-close")) {\n'
            '  btn.addEventListener("click", () => closeDockedFiles());') in doc


# (viewer shown, stack depth) -> what the explorer's popstate does.
POPSTATE_CASES = [
    ("viewer over a subfolder", True, 2, ["hide", 'push {"files":true,"path":"/h/a"}'], 2),
    ("viewer over the entry folder", True, 1, ["hide", 'push {"files":true}'], 1),
    ("no viewer in a subfolder", False, 2, ["apply /h"], 1),
    ("no viewer at the entry folder", False, 1, ["closeExplorer"], 1),
]


@pytest.mark.parametrize("name,shown,depth,want,left", POPSTATE_CASES,
                         ids=[c[0] for c in POPSTATE_CASES])
def test_back_over_the_full_screen_viewer_closes_only_the_picture(doc, tmp_path, name,
                                                                  shown, depth, want, left):
    """The media viewer is an overlay with no history entry, so a back (iOS
    swipe, Android back, the browser's button) over it used to climb a folder
    or close the explorer with the picture still up. It now puts the picture
    away and re-pushes the folder's entry; without the viewer, back is as
    before."""
    handler = _explorer_popstate(doc)
    out = _node_json(tmp_path, "pop.mjs", f"""
const calls = [];
const on = {{}};
const window = {{ addEventListener(ev, fn) {{ on[ev] = fn; }} }};
const cls = (set) => ({{ contains: (c) => set.includes(c) }});
const els = {{
  "viewer": {{ classList: cls({json.dumps(["show"] if shown else [])}) }},
  "screen-editor": {{ classList: cls([]) }},
  "screen-reader": {{ classList: cls([]) }},
}};
function $(id) {{ return els[id]; }}
function q(id) {{ return {{ classList: cls([]) }}; }}
const id = "files";
const root = {{ classList: cls(["active"]) }};
let filesDocked = false, filesClosing = false;
let filesStack = {json.dumps(["/h", "/h/a"][:depth])};
let filesPath = filesStack[filesStack.length - 1];
function dockedFileView() {{ return null; }}
function editorPopped() {{ calls.push("editor"); }}
function closeReader() {{ calls.push("reader"); }}
function hideImage() {{ calls.push("hide"); }}
function filesEntryState() {{
  return filesPath === filesStack[0] ? {{ files: true }} : {{ files: true, path: filesPath }};
}}
const location = {{ href: "x" }};
const history = {{ pushState(s) {{ calls.push("push " + JSON.stringify(s)); }} }};
function closeExplorer() {{ calls.push("closeExplorer"); }}
function cachedListing(p) {{ return {{ path: p }}; }}
function applyListing(d) {{ calls.push("apply " + d.path); }}
function loadDir(p) {{ calls.push("load " + p); }}
{handler}
on.popstate();
console.log(JSON.stringify({{ calls, left: filesStack.length }}));
""")
    assert out["calls"] == want
    assert out["left"] == left


def test_a_full_screen_file_view_gives_the_listing_its_scroll_back(doc, tmp_path):
    """The full-screen listing scrolls the page, and the editor or reader over
    it takes the page, so closing one used to land at the top of the folder.
    Where the listing was is kept when a view covers it and put back when the
    view goes; the reader handing its screen to the editor keeps the first
    reading, and a docked view never touches the page's scroll."""
    assert "\nlet filesCoveredScroll = null;\n" in doc
    chunk = ("let filesCoveredScroll = null;\n" + _js_chunk(doc, "function dockFileView(")
             + "\n" + _js_chunk(doc, "function undockFileView("))
    out = _node_json(tmp_path, "scroll.mjs", f"""
const log = [];
let active = true, docked = false;
const window = {{ scrollY: 640, scrollTo(x, y) {{ log.push(y); this.scrollY = y; }} }};
const filesPanes = {{ files: {{ isOpen: () => active, setActive: (on) => {{ active = on; }},
  isDocked: () => docked }} }};
function filesActive() {{ return filesPanes.files; }}
function filesSetViewOwner() {{}}
const mk = () => {{ const s = new Set(); return {{ classList: {{
  add: (c) => s.add(c), remove: (c) => s.delete(c), contains: (c) => s.has(c) }} }}; }};
const reader = mk(), editor = mk();
{chunk}
dockFileView(reader);            // covers the listing at 640
window.scrollY = 0;              // the reader's page
dockFileView(editor);            // Edit: the listing is already covered
undockFileView(editor);          // back to the listing
const full = [active, log.slice()];
docked = true; log.length = 0;
dockFileView(reader); undockFileView(reader);
console.log(JSON.stringify({{ full, docked: log }}));
""")
    assert out["full"] == [True, [640]]
    assert out["docked"] == []
