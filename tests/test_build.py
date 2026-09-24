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
    on = "This tab uses the computer's network — press to switch it off"
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


def test_the_pane_carries_the_way_out_of_the_pane(doc):
    """The founder's arrow: this page in the browser the device runs, rather
    than in the pane. A tester had asked where the
    address a webapp printed went — before the pane it opened on their own
    machine, and since the pane it opens in the pane. The key is the way back
    to that, and an address the device cannot reach on its own goes out as the
    computer's own copy of the page (the tab flavour, the one the network key
    already mints), which is what the relay did for a tapped localhost link."""
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
    # the same mint and the same Clear-Site-Data hop the network key uses.
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
    # The other flavour first, taken through the same flip the network key makes
    # rather than a second way of doing it — for this document, with the answer
    # the key held remembered so the tab can be given it back.
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
    assert "  if (!!tab.lan !== back) browserFlipLan(tab, back);" in doc
    # A PDF has no shim in it to report its landing, and the arrow reads the
    # answer the user gave rather than the one the document took.
    assert "    if (tab.pdf) return;" in doc
    assert "  if (tab.pdf ? tab.lanBefore : tab.lan) return true;" in doc
    assert "      const lan = t.pdf ? t.lanBefore : t.lan;" in doc


def test_the_monitor_key_is_gone_where_there_is_nothing_to_stream_from(doc):
    """Both keys on the address row are hidden in JS by the attribute alone, and
    .icon-btn is display:inline-flex — which the attribute does not undo. A
    computer with no browser to stream from must not draw the key."""
    assert '#btn-browser-full[hidden] { display: none; }' in doc
    assert '#btn-browser-tab[hidden] { display: none; }' in doc
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
    """Back, forward, then the two mode keys on the row (founder: visible, lit
    while on, a tooltip saying what they do), then Safari's capsule: a glyph
    at its left that only mirrors the tab's mode, the address, reload at its
    right. One control per mode: the capsule glyph opens no menu. Which browser
    the tab is comes before which network it is on."""
    at = {name: doc.index(f'id="{name}"')
          for name in ("btn-browser-back", "btn-browser-fwd", "browser-url-wrap",
                       "browser-url", "btn-browser-reload",
                       "btn-browser-full", "btn-browser-tab")}
    mode = doc.index('<span class="browser-mode" aria-hidden="true">')
    assert (at["btn-browser-back"] < at["btn-browser-fwd"]
            < at["btn-browser-full"] < at["btn-browser-tab"]
            < at["browser-url-wrap"] < mode < at["browser-url"]
            < at["btn-browser-reload"])
    assert "browser-mode-wrap" not in doc and "menuRows" not in doc
    assert ":is(#btn-browser-full, #btn-browser-tab) { display: none; }" not in doc
    assert ".browser-mode { margin-left: 3px; pointer-events: none; cursor: default; }" in doc
    assert 'glyph.setAttribute("href", full ? "#i-m-monitor" : lan ? "#i-m-lan" : "#i-m-globe");' in doc
    # On is the accent over the pressed fill, not only the fill every hover has.
    assert ('.browser-topbar :is(#btn-browser-full, #btn-browser-tab)[aria-pressed="true"] {\n'
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
    browser is wanted for everything. The key is a toggle per tab like the
    network key beside it, and it hides that one while it is pressed: a page
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


def test_a_landing_the_proxy_did_not_serve_is_put_back_then_streamed(doc):
    """A proxied page that sets location.href to a root-relative path leaves the
    proxy's mount: the frame is sandboxed, so there is no hook to catch it, and
    what lands under `tailscale serve` is the front's bare 404 with the pane's
    address bar still showing the site. Everything the proxy does serve reports
    itself with a pockettui-* message, so a landing that says nothing inside the
    watchdog's window is that escape. The first one is loaded again in the proxy,
    on the address the pane last knew; only a second one soon after streams the
    tab, for that tab alone — YouTube escapes this way and plays in the proxy,
    and remembering its host put it on the stream for good."""
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
    # First escape: back into the proxy, silently; second: the stream, this tab
    # only, with nothing remembered.
    assert "tab.escapes = (again ? tab.escapes : 0) + 1;" in body
    assert "tab.escapedAt = { url: url, at: now };" in body
    assert ("    if (tab.escapes < 2) {\n"
            "      browserNavigateIn(tab, url, false);\n"
            "      return;\n"
            "    }") in body
    assert '    if (!hasCapStrict("browser_full")) return;' in body
    assert '"This page left the proxy twice; streaming it from the computer"' in body
    assert "browserSwapMode(tab, true);" in body
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


def test_the_network_key_shows_wherever_the_flavour_is_available(doc):
    """The LOCAL NETWORK key is shown on proxy and streamed tabs alike wherever
    the computer can give the unsandboxed flavour, and hidden only while a PDF
    holds it. On a streamed tab it reads un-pressed and steps the tab down to a
    proxy tab with the flavour on, in one navigation, forgetting the host."""
    sync = doc[doc.index("function syncBrowserLan("):]
    sync = sync[:sync.index("\n}\n")]
    assert ("btn.hidden = !browserTabAllowed() || !!(browserTab() && browserTab().pdf);"
            in sync)
    assert "browserIsFull(browserTab())\n" not in sync.split("btn.hidden")[1].split(";")[0]
    assert "const on = !full && !!(browserTab() && browserTab().lan);" in sync
    assert '"Use the local-network proxy for this tab"' in sync
    press = doc[doc.index('q("btn-browser-tab").addEventListener("click"'):]
    press = press[:press.index("\n});\n")]
    assert ("    browserRememberStream(url, false);\n"
            "    browserSwapMode(tab, false);\n"
            "    browserFlipLan(tab, true);\n"
            "    if (url) browserNavigateIn(tab, url, false);") in press
    assert press.count("browserNavigateIn(") == 1


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
    # The four ways a page says it did not work, and the one bar they all raise.
    assert "function browserHint(" in doc and "function browserShowHint(" in doc
    assert "if (!BROWSE_HINT_SKIP[code]) browserHint(tab" in doc
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
