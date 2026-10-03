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


def test_report_sheet_can_attach_the_debug_recording(doc):
    """The founder, 2026-09-26: a report carries the debug log. Turning Debug
    log off asks whether to send it, and the sheet has an attach row that is
    hidden unless there is a recording."""
    sheet = doc[doc.index('<div class="sheet" id="sheet-report">'):]
    sheet = sheet[:sheet.index("</div>\n\n")]
    row = sheet[sheet.index('id="report-log-row"') - 30:]
    assert '<div class="toggle-row" id="report-log-row" hidden>' in row
    assert '<input id="report-log" type="checkbox" checked>' in row
    # After the diagnostics switch, before the details block.
    assert sheet.index('id="report-diag"') < sheet.index('id="report-log-row"') \
        < sheet.index('id="report-diag-details"')
    assert "#report-log-row[hidden] { display: none; }" in doc
    assert 'const DBG_REC_KEY = "pockettui_dbg_rec";' in doc
    # The toggle's off tap offers the log; its on tap starts a fresh recording.
    handler = doc[doc.index('$("dbg-toggle").addEventListener("change"'):]
    handler = handler[:handler.index("\n});\n")]
    assert "setDebug(e.target.checked, true);" in handler
    assert "if (!e.target.checked) offerDebugLog();" in handler
    offer = _js_chunk(doc, "async function offerDebugLog(")
    assert 'title: "Send the debug log to support?"' in offer
    assert 'confirmLabel: "Send log…"' in offer and "danger: false" in offer
    assert "openReport({ fromOff: true });" in offer
    # Any way out of the sheet but Send drops a log the turn-off path handed it.
    show = _js_chunk(doc, "function showSheet(")
    assert 'if (dbgRecHeld && !(on && id === "sheet-report")) dbgRecDiscard();' in show
    send = _js_chunk(doc, "async function sendReport(")
    assert '$("report-log").checked ? reportLogRec : null' in send
    assert "const REPORT_TIMEOUT = 30000;" in doc
    # Boot's re-enable carries the recording on (no fresh flag).
    assert "if (cfg.debug) setDebug(true);" in doc



def test_report_send_is_never_silent(doc, tmp_path):
    """The founder, 2026-09-27: Send with an empty box and a log attached did
    nothing at all. With a log attached the description is optional and a
    stand-in message goes out; without one, an empty box says so in red."""
    sheet = doc[doc.index('<div class="sheet" id="sheet-report">'):]
    assert ('<label for="report-msg" class="opt">What happened '
            '<span id="report-msg-opt" hidden>optional</span></label>') in sheet
    opener = _js_chunk(doc, "async function openReport(")
    assert '$("report-msg-opt").hidden = !reportLogRec;' in opener
    send = _js_chunk(doc, "async function sendReport(")
    assert "if (!message) { $(\"report-msg\").focus(); return; }" not in send
    empty = send[send.index("if (!message) {"):]
    empty = empty[:empty.index("return;")]
    assert 'showReportError("Please describe what went wrong.");' in empty
    assert ('$("report-msg").addEventListener("input", () => '
            '$("report-error").classList.remove("show"));') in doc
    code = _js_chunk(doc, "const REPORT_LOG_ONLY_MESSAGE") + "\n" + \
        _js_chunk(doc, "function reportMessage(")
    out = _node_json(tmp_path, "msg.mjs", code + """
console.log(JSON.stringify({
  logOnly: reportMessage("  \\n ", true),
  none: reportMessage("", false),
  typed: reportMessage(" it broke ", true),
  typedNoLog: reportMessage("it broke", false),
}));
""")
    assert out["logOnly"] == "Debug log sent from Settings (no description given)."
    assert out["none"] == ""
    assert out["typed"] == "it broke" and out["typedNoLog"] == "it broke"


def _dbg_rec_code(doc):
    at = doc.index("\nconst DBG_REC_KEY") + 1
    end = doc.index("\nfunction dbgRecResume(", at)
    return doc[at:doc.index("\n}\n", end) + 3]


def test_debug_recording_caps_persists_and_resumes(doc, tmp_path):
    """Newest lines win on both caps, the header survives them, storage is
    written only on save (the flush), a reload resumes rather than restarts,
    and a discard only ever removes the recording of a setting that is off."""
    code = _dbg_rec_code(doc)
    out = _node_json(tmp_path, "rec.mjs", f"""
const ls = {{}};
let writes = 0;
const localStorage = {{ getItem: (k) => (k in ls ? ls[k] : null),
  setItem: (k, v) => {{ writes++; ls[k] = v; }}, removeItem: (k) => {{ delete ls[k]; }} }};
let dbgOn = true;
function buildVersion() {{ return "v-test"; }}
function reportLogHeader() {{ return ["PocketTUI debug log", "started: now", ""]; }}
{code}
const got = {{}};
dbgRecStart();
got.startWrites = writes;
for (let i = 0; i < 4100; i++) dbgRecPush("line " + i);
got.writesBeforeSave = writes;
dbgRecSave(); dbgRecSave();
got.writesAfterSave = writes;
let r = dbgRecLoad();
got.count = r.lines.length;
got.first = r.lines[0];
got.head = r.head;
// The char cap: long lines evict by size well before 4000 of them.
dbgRecStart();
for (let i = 0; i < 400; i++) dbgRecPush(String(i).padEnd(1000, "."));
dbgRecSave();
r = dbgRecLoad();
got.bigCount = r.lines.length;
got.bigChars = r.chars <= DBG_REC_CHARS;
got.bigLast = r.lines[r.lines.length - 1].slice(0, 3);
// A reload: memory gone, storage kept, the same recording carries on.
dbgRec = null;
dbgRecResume();
got.resumed = dbgRec.lines.length === got.bigCount + 1 &&
  dbgRec.lines[dbgRec.lines.length - 1].startsWith("--- resumed after reload");
got.text = dbgRecText({{ head: ["h"], lines: ["a", "b"] }});
// Discard while on does nothing; while off, it removes the stored copy.
dbgRecSave();
dbgRecDiscard();
got.keptWhileOn = DBG_REC_KEY in ls;
dbgOn = false; dbgRecHeld = true;
dbgRecDiscard();
got.goneWhenOff = !(DBG_REC_KEY in ls) && !dbgRecHeld;
// A corrupt store reads as none.
ls[DBG_REC_KEY] = "{{nope";
got.corrupt = dbgRecLoad();
console.log(JSON.stringify(got));
""")
    assert out["startWrites"] == 1
    assert out["writesBeforeSave"] == 1
    assert out["writesAfterSave"] == 2
    assert out["count"] == 4000 and out["first"] == "line 100"
    assert out["head"] == ["PocketTUI debug log", "started: now", ""]
    assert 250 <= out["bigCount"] < 400 and out["bigChars"] and out["bigLast"] == "399"
    assert out["resumed"] is True
    assert out["text"] == "h\na\nb"
    assert out["keptWhileOn"] is True and out["goneWhenOff"] is True
    assert out["corrupt"] is None


def test_report_body_fits_the_log_under_the_endpoint_limits(doc, tmp_path):
    """A log that would take the report over the endpoint's byte or char cap
    loses its oldest lines at send time; the header stays and the cut is
    marked, so the endpoint never refuses it or cuts the header off."""
    consts = "\n".join(_js_chunk(doc, c) for c in (
        "const REPORT_MAX_BODY", "const REPORT_MAX_LOG"))
    body = _js_chunk(doc, "function reportBody(")
    out = _node_json(tmp_path, "body.mjs", f"""
{consts}
{body}
const fields = {{ message: "m", email: "", diag: "", website: "" }};
const got = {{}};
got.none = JSON.parse(reportBody(fields, null));
got.small = JSON.parse(reportBody(fields, {{ head: ["H"], lines: ["a", "b"] }})).log;
// Non-ASCII: under the char cap, over the byte cap as UTF-8.
const wide = [];
for (let i = 0; i < 3000; i++) wide.push(i + " " + "日本語".repeat(40));
let raw = reportBody(fields, {{ head: ["HEAD"], lines: wide }});
let log = JSON.parse(raw).log;
got.wideBytes = new TextEncoder().encode(raw).length <= REPORT_MAX_BODY;
got.wideHead = log.split("\\n")[0];
got.wideMarker = / earlier lines dropped to fit the report]$/.test(log.split("\\n")[1]) &&
  log.split("\\n")[1].startsWith("[");
got.wideLast = log.endsWith("2999 " + "日本語".repeat(40));
got.wideKeptBytes = new TextEncoder().encode(raw).length;
// ASCII over the char cap.
const ascii = [];
for (let i = 0; i < 3000; i++) ascii.push(String(i).padEnd(100, "x"));
log = JSON.parse(reportBody(fields, {{ head: ["HEAD"], lines: ascii }})).log;
got.asciiChars = log.length <= REPORT_MAX_LOG;
got.asciiHead = log.startsWith("HEAD\\n[");
got.asciiKept = log.length;
console.log(JSON.stringify(got));
""")
    assert "log" not in out["none"]
    assert out["small"] == "H\na\nb"
    assert out["wideBytes"] and out["wideHead"] == "HEAD" and out["wideMarker"] and out["wideLast"]
    # Only what had to go: the kept part fills most of the byte cap.
    assert out["wideKeptBytes"] > 300 * 1024
    assert out["asciiChars"] and out["asciiHead"] and out["asciiKept"] > 250 * 1024


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


def _files_last_store(doc):
    at = doc.index("\nconst FILES_LAST_KEY") + 1
    end = doc.index("\nfunction dropProfileFilesLast(", at)
    return doc[at:doc.index("\n}\n", end) + 3]


def test_the_explorer_folder_is_remembered_per_session(doc, tmp_path):
    """Founder, 2026-09-25: reopening the explorer in a session goes back to the
    folder it was left at. Kept per session under the tmux id and creation
    time, so a rename keeps it, another session never sees it, a session that
    is gone (killed) loses it with the next list, and each computer (profile)
    has its own map."""
    store = _files_last_store(doc)
    out = _node_json(tmp_path, "store.mjs", f"""
const ls = {{}};
const localStorage = {{ getItem: (k) => (k in ls ? ls[k] : null), setItem: (k, v) => {{ ls[k] = v; }} }};
let profile = "p1";
function unreadProfileKey() {{ return profile; }}
{store}
const got = {{}};
keepFilesLast([{{ name: "work", sid: 3, created: 100 }}, {{ name: "play", sid: 4, created: 101 }}]);
rememberFilesDir("work", "/h/proj/src");
got.work = filesLastDir("work");
got.play = filesLastDir("play");
got.unknown = filesLastDir("nobody");
rememberFilesDir("nobody", "/x");
// Renamed: same tmux session, new name.
keepFilesLast([{{ name: "job", sid: 3, created: 100 }}, {{ name: "play", sid: 4, created: 101 }}]);
got.renamed = filesLastDir("job");
got.oldName = filesLastDir("work");
// Another computer's list: its own map, and it does not prune this one's.
profile = "p2";
keepFilesLast([{{ name: "job", sid: 3, created: 555 }}]);
got.otherProfile = filesLastDir("job");
profile = "p1";
keepFilesLast([{{ name: "job", sid: 3, created: 100 }}, {{ name: "play", sid: 4, created: 101 }}]);
got.back = filesLastDir("job");
// A tmux restart reuses $3 for a new session: different creation time.
keepFilesLast([{{ name: "job", sid: 3, created: 900 }}]);
got.reused = filesLastDir("job");
// Killed: the next list no longer names it.
keepFilesLast([{{ name: "a", sid: 7, created: 1 }}]);
rememberFilesDir("a", "/a");
forgetFilesDir("a");
got.forgot = filesLastDir("a");
rememberFilesDir("a", "/a2");
keepFilesLast([]);
got.pruned = JSON.parse(ls[FILES_LAST_KEY]).p1;
// A demo row has no sid and gets no key.
keepFilesLast([{{ name: "demo" }}]);
rememberFilesDir("demo", "/d");
got.demo = filesLastDir("demo");
console.log(JSON.stringify(got));
""")
    assert out == {"work": "/h/proj/src", "play": "", "unknown": "", "renamed": "/h/proj/src",
                   "oldName": "", "otherProfile": "", "back": "/h/proj/src", "reused": "",
                   "forgot": "", "pruned": {}, "demo": ""}
    # The list payload feeds it, a kill drops it at once, a forgotten computer
    # takes its map with it.
    assert "  keepFileViews(names);\n  keepFilesLast(sessions);\n" in doc
    assert "    dropFileView(name);\n    forgetFilesDir(name);\n" in doc
    assert "  dropProfileUnread(id);\n  dropProfileFilesLast(id);\n" in doc
    # Written from the markup's own pane, over a terminal, on the working tree.
    assert ('  if (id === "files" && filesOrigin === "screen-term" && !filesRef && currentSession\n'
            '      && root.classList.contains("active")) {\n'
            '    rememberFilesDir(currentSession, data.path);') in doc


# name, remembered folder, what the disk says about it, docked
# -> the folder opened, whether the memory was forgotten, synced cwd, held folder
OPEN_AT_CWD_CASES = [
    ("reopen restores the folder", "/h/p/deep", "ok", True,
     ["/h/p/deep"], False, "/h/p", "/h/p/deep"),
    ("reopen full screen", "/h/p/deep", "ok", False,
     ["/h/p/deep"], False, "", ""),
    ("new session follows the terminal", "", "ok", True,
     ["/h/p"], False, "/h/p", ""),
    ("remembered folder is the cwd", "/h/p", "ok", True,
     ["/h/p"], False, "/h/p", ""),
    ("missing folder falls back", "/h/p/gone", "not_found", True,
     ["/h/p"], True, "/h/p", ""),
    ("network failure falls back, memory kept", "/h/p/deep", "network", True,
     ["/h/p"], False, "/h/p", ""),
]


@pytest.mark.parametrize("name,held,disk,docked,opened,forgot,synced,heldAt",
                         OPEN_AT_CWD_CASES, ids=[c[0] for c in OPEN_AT_CWD_CASES])
def test_opening_the_explorer_goes_back_to_the_remembered_folder(
        doc, tmp_path, name, held, disk, docked, opened, forgot, synced, heldAt):
    fn = _js_chunk(doc, "async function filesOpenAtCwd(opts) {")
    out = _node_json(tmp_path, "open.mjs", f"""
const id = "files";
const filesPanes = {{}};
let currentSession = "work";
let filesPath = "", filesDocked = false, filesSyncedCwd = "", filesHeldAt = "stale";
const opened = [];
let forgot = false;
async function fetchPaneCwd() {{ return "/h/p"; }}
function filesLastDir(s) {{ return s === "work" ? {json.dumps(held)} : ""; }}
function forgetFilesDir(s) {{ forgot = true; }}
async function fsList(p) {{
  const disk = {json.dumps(disk)};
  if (disk === "ok") return {{ path: p }};
  const e = new Error(disk === "network" ? "Failed to fetch" : disk);
  if (disk !== "network") e.code = disk;
  throw e;
}}
async function openExplorer(p) {{ opened.push(p); filesPath = p; filesDocked = {json.dumps(docked)}; return true; }}
{fn}
await filesOpenAtCwd();
console.log(JSON.stringify({{ opened, forgot, filesSyncedCwd, filesHeldAt }}));
""")
    assert out == {"opened": opened, "forgot": forgot,
                   "filesSyncedCwd": synced, "filesHeldAt": heldAt}


# Following after a reopen at a remembered folder: steps of the tick's cwd
# answer (or "nav" for the user browsing one level in) -> folder after each.
FOLLOW_CASES = [
    ("unchanged cwd does not yank the pane", ["/h/p", "/h/p"], ["/h/p/deep", "/h/p/deep"]),
    ("a cd moves it, and it follows from there", ["/h/p/new", "/h/p/other"],
     ["/h/p/new", "/h/p/other"]),
    ("browsed away, a cd does not move it", ["nav", "/h/p/new"], ["/h/p/deep/sub", "/h/p/deep/sub"]),
]


@pytest.mark.parametrize("name,steps,want", FOLLOW_CASES, ids=[c[0] for c in FOLLOW_CASES])
def test_following_after_a_reopen_waits_for_the_terminal_to_move(doc, tmp_path, name, steps, want):
    chunk = (_js_chunk(doc, "function filesFollowsCwd() {") + "\nlet filesCwdBusy = false;\n"
             + _js_chunk(doc, "async function followPaneCwd() {"))
    assert "\nlet filesCwdBusy = false;\nasync function followPaneCwd() {" in doc
    out = _node_json(tmp_path, "follow.mjs", f"""
const cls = () => ({{ classList: {{ contains: () => false }} }});
function $(id) {{ return cls(); }}
function q(id) {{ return cls(); }}
let filesDocked = true, filesRef = "";
let filesStack = ["/h/p/deep"], filesPath = "/h/p/deep";
let filesSyncedCwd = "/h/p", filesHeldAt = "/h/p/deep";
let answer = "";
async function fetchPaneCwd() {{ return answer; }}
async function loadDir(p) {{ filesPath = p; if (!filesStack.length) filesStack.push(p); return true; }}
{chunk}
const got = [];
for (const s of {json.dumps(steps)}) {{
  if (s === "nav") {{ filesPath = "/h/p/deep/sub"; filesStack.push(filesPath); }}
  else {{ answer = s; await followPaneCwd(); }}
  got.push(filesPath);
}}
console.log(JSON.stringify(got));
""")
    assert out == want


def test_a_closed_explorer_leaves_no_rows_for_the_next_open(doc, tmp_path):
    """Founder, v0.9.192: reopening showed the folder the pane was closed on,
    then jumped to the terminal's cwd once that listing landed (seconds on the
    cluster mount). Those rows were leftovers, not a restore. Every way out
    empties the rows and crumbs, so an open shows nothing until its own
    folder lands."""
    fn = _js_chunk(doc, "function filesClearListing() {")
    out = _node_json(tmp_path, "clear.mjs", f"""
const els = {{ "files-list": {{ innerHTML: "<div>old</div>" }},
  "files-crumbs": {{ innerHTML: "<b>paper</b>" }}, "files-empty": {{ style: {{ display: "block" }} }} }};
function q(id) {{ return els[id]; }}
let stopped = 0, filesEntries = [1, 2];
function stopThumbs() {{ stopped++; }}
function filesStopLoad() {{}}
{fn}
filesClearListing();
console.log(JSON.stringify([els["files-list"].innerHTML, els["files-crumbs"].innerHTML,
  els["files-empty"].style.display, filesEntries.length, stopped]));
""")
    assert out == ["", "", "none", 0, 1]
    for head in ("function closeExplorer() {", "function closeDockedFiles() {",
                 "function filesTeardown() {"):
        assert "  filesClearListing();\n" in _js_chunk(doc, head), head


# (x, y, menu w, h, pane box right, bottom) in a 1400x900 window -> left, top
MENU_PLACE_CASES = [
    ("opens at the pointer", 300, 200, 170, 130, 1400, 900, [300, 200]),
    ("flips left at the right edge", 1350, 200, 170, 130, 1400, 900, [1180, 200]),
    ("flips up at the bottom edge", 300, 850, 170, 130, 1400, 900, [300, 720]),
    ("flips both near the bottom-right row", 1380, 880, 170, 130, 1400, 900, [1210, 750]),
    ("flips up at a top-row pane's bottom", 900, 420, 170, 130, 1400, 450, [900, 290]),
    ("clamped inside the window after a flip", 100, 200, 170, 130, 200, 900, [6, 200]),
    ("taller than the room either way", 300, 60, 170, 130, 1400, 150, [300, 6]),
    ("exactly fits against the edge", 1224, 764, 170, 130, 1400, 900, [1224, 764]),
]


@pytest.mark.parametrize("name,x,y,w,h,right,bottom,want", MENU_PLACE_CASES,
                         ids=[c[0] for c in MENU_PLACE_CASES])
def test_the_right_click_menu_sits_at_the_pointer(doc, tmp_path, name, x, y, w, h,
                                                  right, bottom, want):
    """Founder, 2026-09-25: a right-click's actions open next to the file like
    a desktop file manager, not in a sheet in the middle. Top-left at the
    pointer, flipped left/up past the pane's edge, clamped 6px inside the
    window."""
    fn = _js_chunk(doc, "function filesMenuPlace(")
    out = _node_json(tmp_path, "place.mjs", f"""
{fn}
const p = filesMenuPlace({x}, {y}, {w}, {h}, {{ right: {right}, bottom: {bottom} }}, 1400, 900, 6);
console.log(JSON.stringify([p.left, p.top]));
""")
    assert out == want


def test_a_right_click_raises_the_menu_and_a_long_press_the_sheet(doc, tmp_path):
    """Same rows, same visibility rules; only a point decides the shape."""
    fn = _js_chunk(doc, "function filesShowActions(")
    out = _node_json(tmp_path, "route.mjs", f"""
const els = {{}};
function $(id) {{ return els[id] = els[id] || {{ style: {{}}, textContent: "" }}; }}
const FILES_HTML_RE = /\\.html?$/i;
function hasCap() {{ return false; }}
const shown = [];
function showSheet(on, id) {{ shown.push("sheet " + id); }}
function filesShowMenu(at) {{ shown.push("menu " + at.x + "," + at.y); }}
{fn}
filesShowActions({{ name: "a.html", type: "file" }}, {{ x: 5, y: 7 }});
filesShowActions({{ name: "docs", type: "dir" }});
console.log(JSON.stringify({{ shown, ctxEdit: els["ctx-file-edit"].style.display,
  ctxDl: els["ctx-file-download"].style.display, dl: els["btn-file-download"].style.display,
  edit: els["btn-file-edit"].style.display }}));
""")
    assert out == {"shown": ["menu 5,7", "sheet sheet-file-actions"], "ctxEdit": "",
                   "ctxDl": "", "dl": "none", "edit": "none"}
    wire = _js_chunk(doc, "function wireRow(")
    assert "if (pressedByTouch) { openFileActions(entry); return; }" in wire
    assert 'pressedByTouch = ev.pointerType === "touch";' in wire
    assert "openFileActions(entry, { x: ev.clientX, y: ev.clientY, pane: root });" in wire
    # Escape is claimed on the window, ahead of the explorer's own Escape.
    assert ('window.addEventListener("keydown", (e) => {\n'
            '  if (e.key !== "Escape" || !filesMenuOpen()) return;') in doc
    assert "function closeFilesMenus() { showViewMenu(false); showRefMenu(false); filesHideMenu(); }" in doc
    assert '<div id="file-ctx-menu" class="ctx-menu" role="menu" hidden>' in doc


# ---------------------------------------------------------------------------
# Folder upload
# ---------------------------------------------------------------------------
# A fake of the File and Directory Entries API, for the walk and the drop:
# a directory's reader hands out at most `page` entries per readEntries call,
# the way Chrome stops at 100, and counts the calls.
FAKE_ENTRIES = """
let readCalls = 0;
function fileEntry(name) {
  return { name, isFile: true, isDirectory: false,
           file: (ok) => ok({ name, size: 1 }) };
}
function dirEntry(name, kids, page = 100) {
  return { name, isFile: false, isDirectory: true, createReader() {
    let at = 0;
    return { readEntries(ok) {
      readCalls++;
      const out = kids.slice(at, at + page); at += out.length;
      setTimeout(() => ok(out), 0);
    } };
  } };
}
"""


def test_a_dropped_folder_is_walked_page_by_page_under_its_own_name(doc, tmp_path):
    """Chrome answers readEntries 100 at a time; a walk that stopped at the
    first page would drop the rest of a big folder without a word. relPath
    starts with the dropped folder's name, and a folder with nothing in it is
    reported for the upload to make."""
    fn = _js_chunk(doc, "async function walkDroppedDir(")
    out = _node_json(tmp_path, "walk.mjs", FAKE_ENTRIES + fn + """
const many = Array.from({ length: 250 }, (_, i) => fileEntry("f" + i + ".jpg"));
const tree = dirEntry("photos", [
  ...many,
  dirEntry("2026", [fileEntry("a.jpg"), dirEntry("empty", [])]),
  dirEntry("blank", []),
]);
const into = await walkDroppedDir(tree, "", { items: [], empty: [], unreadable: 0 });
const rels = into.items.map((it) => it.relPath);
console.log(JSON.stringify({ n: rels.length, first: rels[0], last: rels[rels.length - 1],
  f249: rels.includes("photos/f249.jpg"), nested: rels.includes("photos/2026/a.jpg"),
  empty: into.empty, readCalls, named: into.items[0].file.name }));
""")
    assert out == {"n": 251, "first": "photos/f0.jpg", "last": "photos/2026/a.jpg",
                   "f249": True, "nested": True,
                   "empty": ["photos/2026/empty", "photos/blank"],
                   # photos: 3 full-or-part pages + the empty one; 2026: 2;
                   # each empty folder: 1.
                   "readCalls": 4 + 2 + 1 + 1, "named": "f0.jpg"}


DROP_HANDLER_STUBS = """
let serverDirs = false;
const calls = [];
function hasCapStrict(name) { return name === "upload_dirs" && serverDirs; }
function demoApiOn() { return false; }
function clearDropHints() {}
function isUploadDrag() { return true; }
function toast(m) { calls.push(["toast", m]); }
function holdToast(m) {}
function hideToast() {}
function uploadFiles(items, empty) {
  calls.push(["upload", items.map((it) => it.relPath), empty || []]);
}
let dropHandler = null;
const filesDropZone = { addEventListener(type, fn) { if (type === "drop") dropHandler = fn; } };
function dataTransfer(entries) {
  return { items: entries.map((e) => ({ kind: "file", webkitGetAsEntry: () => e,
                                        getAsFile: () => (e.isFile ? { name: e.name } : null) })) };
}
function drop(entries) {
  return dropHandler({ dataTransfer: dataTransfer(entries), preventDefault() {} });
}
"""


def _drop_code(doc):
    at = doc.index('\nfilesDropZone.addEventListener("drop", (ev) => {') + 1
    return doc[at:doc.index("\n});\n", at) + 5]


def test_a_folder_drop_uploads_its_tree_where_the_server_takes_folders(doc, tmp_path):
    code = "\n".join([_js_chunk(doc, "function droppedEntries("),
                      _js_chunk(doc, "async function walkDroppedDir("),
                      _drop_code(doc),
                      _js_chunk(doc, "async function uploadDropped(")])
    out = _node_json(tmp_path, "drop.mjs", FAKE_ENTRIES + DROP_HANDLER_STUBS + code + """
const mixed = () => [fileEntry("notes.txt"),
                     dirEntry("photos", [fileEntry("a.jpg"), dirEntry("x", [])])];
// An older server: the files in a mixed drop go, the folder is left out, and
// a drop of nothing but folders says why nothing happened.
drop(mixed());
drop([dirEntry("photos", [fileEntry("a.jpg")])]);
serverDirs = true;
drop(mixed());
await new Promise((r) => setTimeout(r, 50));
console.log(JSON.stringify(calls));
""")
    assert out == [
        ["upload", ["notes.txt"], []],
        ["toast", "Update the server to upload folders"],
        ["upload", ["notes.txt", "photos/a.jpg"], ["photos/x"]],
    ]


# user agent, maxTouchPoints, input has webkitdirectory -> row offered
PICKER_CASES = [
    ("desktop chrome", "Mozilla/5.0 (X11; Linux x86_64) Chrome/140", 0, True, True),
    ("mac safari", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605", 0, True, True),
    ("iphone", "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604", 5, True, False),
    ("ipad as mac", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605", 5, True, False),
    ("android chrome", "Mozilla/5.0 (Linux; Android 14) Chrome/140 Mobile", 5, True, True),
    ("no property", "Mozilla/5.0 (X11; Linux x86_64) Firefox/50", 0, False, False),
]


@pytest.mark.parametrize("name,ua,touch,prop,want", PICKER_CASES,
                         ids=[c[0] for c in PICKER_CASES])
def test_the_folder_picker_row_is_offered_only_where_it_picks_a_folder(
        doc, tmp_path, name, ua, touch, prop, want):
    """iOS Safari has webkitdirectory and ignores it, so the property alone is
    not the test."""
    code = _js_chunk(doc, "function a2hsPlatform(") + _js_chunk(doc, "function folderPickerWorks(")
    out = _node_json(tmp_path, "picker.mjs", f"""
// Node has a navigator of its own, which plain assignment cannot replace.
Object.defineProperty(globalThis, "navigator", {{
  value: {{ userAgent: {json.dumps(ua)}, maxTouchPoints: {touch} }} }});
globalThis.document = {{ createElement: () => ({json.dumps(prop)} ? {{ webkitdirectory: false }} : {{}}) }};
{code}
console.log(JSON.stringify(folderPickerWorks()));
""")
    assert out is want
    # And the row is hidden where the server cannot make a tree's folders.
    assert ('  $("btn-files-upload-dir").hidden = !(hasCapStrict("upload_dirs") '
            '&& folderPickerWorks());') in doc
    assert ('<input type="file" id="files-upload-dir-input" webkitdirectory multiple '
            'style="display:none">') in doc


def test_a_tree_upload_asks_about_existing_files_once(doc, tmp_path):
    """The first file already there raises one question and its answer holds
    for the batch; a file in the way of a folder is a failure, not a replace;
    empty folders are made level by level; one summary at the end."""
    code = _js_chunk(doc, "async function uploadTree(") + _js_chunk(doc, "async function uploadTreeFile(")
    out = {}
    for answer in (True, False):
        out[answer] = _node_json(tmp_path, f"tree{answer}.mjs", f"""
const filesPath = "/x", id = "files";
const disk = new Set(["/x/p/a.txt", "/x/p/b.txt"]);
const posts = [], mkdirs = [], asked = [], toasts = [], held = [];
function joinPath(d, n) {{ return d + "/" + n; }}
function apiURL(p) {{ return p; }}
function authHeaders() {{ return {{}}; }}
function rejectToken() {{}}
function holdToast(m) {{ held.push(m); }}
function hideToast() {{}}
function toast(m) {{ toasts.push(m); }}
function loadDir() {{}}
function filesRefreshPeers() {{}}
async function appConfirm(msg, opts) {{ asked.push(opts.title); return {json.dumps(answer)}; }}
async function fsPost(route, body) {{ mkdirs.push(body.path); return {{ ok: true, status: 200 }}; }}
async function fetch(url) {{
  const q = new URLSearchParams(url.split("?")[1]);
  const path = q.get("path");
  posts.push(path + (q.get("overwrite") ? " !" : "") + (q.get("mkdirs") ? "" : " NO-MKDIRS"));
  if (path.startsWith("/x/p/blocked")) return {{ ok: false, status: 409, json: async () => ({{ error: "not_a_directory" }}) }};
  if (disk.has(path) && !q.get("overwrite")) return {{ ok: false, status: 409, json: async () => ({{ error: "exists" }}) }};
  return {{ ok: true, status: 200 }};
}}
{code}
const it = (relPath) => ({{ file: {{}}, relPath }});
await uploadTree([it("p/a.txt"), it("p/new.txt"), it("p/b.txt"), it("p/blocked/c.txt")],
                 ["p/e1/e2"]);
console.log(JSON.stringify({{ posts, mkdirs, asked, toasts, held }}));
""")
    yes, no = out[True], out[False]
    assert yes["asked"] == no["asked"] == ["Some of these files already exist"]
    # Once the answer is yes, the rest go with overwrite=1 up front.
    assert yes["posts"] == ["/x/p/a.txt", "/x/p/a.txt !", "/x/p/new.txt !", "/x/p/b.txt !",
                            "/x/p/blocked/c.txt !"]
    assert yes["toasts"] == ["Uploaded 3 files, 1 failed"]
    assert no["posts"] == ["/x/p/a.txt", "/x/p/new.txt", "/x/p/b.txt", "/x/p/blocked/c.txt"]
    assert no["toasts"] == ["Uploaded 1 file, 2 skipped, 1 failed"]
    assert yes["mkdirs"] == ["/x/p", "/x/p/e1", "/x/p/e1/e2"]
    assert yes["held"] == ["Uploading 1/4", "Uploading 2/4", "Uploading 3/4", "Uploading 4/4"]


def test_plain_files_keep_the_flat_upload_and_a_ref_is_read_only(doc):
    fn = _js_chunk(doc, "async function uploadFiles(")
    assert 'if (demoApiOn()) { toast("Not in the demo"); return; }' in fn
    assert 'if (refFor(filesPath)) { toast("Read-only on " + refFor(filesPath)); return; }' in fn
    assert 'if (empty.length || items.some((it) => it.relPath.includes("/"))) {' in fn
    assert 'if (items.length === 1) { if (done) toast("Uploaded " + items[0].file.name); }' in fn
    assert 'if (confirm(f.name + " already exists here. Replace it?")) return uploadFile(it, true);' in doc


def test_the_explorer_head_has_a_refresh_key(doc):
    """For pointers, which have no pull-down: first of the folder's keys, on
    every pane (the second is a clone of the first's markup), spinning while
    the listing reloads, at a ref as well since fsList reads the ref."""
    tools = doc[doc.index('<span class="files-tools">'):]
    assert tools.index('id="btn-files-refresh"') < tools.index('id="btn-files-add"')
    assert ('<button class="icon-btn" id="btn-files-refresh" aria-label="Refresh"\n'
            '            title="Refresh"><svg><use href="#i-m-reload"/></svg></button>') in doc
    at = doc.index('q("btn-files-refresh").addEventListener("click", async () => {')
    handler = doc[at:doc.index("\n});\n", at)]
    assert 'btn.classList.add("spin");' in handler
    assert "loadDir(filesPath)" in handler
    assert 'btn.classList.remove("spin");' in handler
    assert ".icon-btn.spin svg { animation: spin" in doc


def _explorer_load(doc):
    return "\n".join(_js_chunk(doc, h) for h in (
        "function filesStopLoad() {", "async function loadDir(path, primed) {"))


def test_the_newest_folder_load_wins(doc, tmp_path):
    """On the cluster mount a listing can take a minute. A second tap while the
    first is pending aborts it, and the older answer, landing late, never
    paints over the folder asked for last; the pane is marked loading until
    the latest lands. A primed listing is painted without asking again."""
    out = _node_json(tmp_path, "load.mjs", """
let filesLoadSeq = 0, filesLoadCtrl = null;
const cls = new Set();
const root = { classList: { add: (c) => cls.add(c), remove: (c) => cls.delete(c),
                            contains: (c) => cls.has(c) } };
const painted = [], errors = [], asked = [], aborted = [];
function fsList(path, signal) {
  asked.push(path);
  return new Promise((res, rej) => {
    // The first folder answers late and ignores the abort, as a server
    // mid-scan does; the listing must still be dropped.
    setTimeout(() => res({ path }), path === "/slow" ? 60 : 10);
    signal.addEventListener("abort", () => aborted.push(path));
  });
}
function applyListing(d) { painted.push(d.path); }
function showListError(e) { errors.push(String(e)); }
""" + _explorer_load(doc) + """
const first = loadDir("/slow");
const during = cls.has("files-loading");
const second = loadDir("/fast");
const results = await Promise.all([first, second]);
const after = cls.has("files-loading");
await new Promise((r) => setTimeout(r, 80));
const primed = await loadDir("/p", { path: "/p" });
console.log(JSON.stringify({ results, painted, errors, asked, aborted, during, after, primed }));
""")
    assert out == {"results": [False, True], "painted": ["/fast", "/p"], "errors": [],
                   "asked": ["/slow", "/fast"], "aborted": ["/slow"],
                   "during": True, "after": False, "primed": True}


def test_closing_the_explorer_drops_a_listing_still_on_its_way(doc):
    assert "  filesStopLoad();\n" in _js_chunk(doc, "function filesClearListing() {")


def _open_at_cwd_harness(doc, body):
    store_at = doc.index("\nconst FILES_LAST_KEY") + 1
    end = doc.index("\nfunction dropProfileFilesLast(", store_at)
    store = doc[store_at:doc.index("\n}\n", end) + 3]
    return """
const ls = {};
const localStorage = { getItem: (k) => (k in ls ? ls[k] : null), setItem: (k, v) => { ls[k] = v; } };
function unreadProfileKey() { return "p"; }
""" + store + """
let id = "files", currentSession = "", filesHeldAt = "", filesDocked = false;
let filesPath = "", filesSyncedCwd = "";
const cwds = {}, listed = [], opened = [];
let firstPane = { open: false, at: "" };
const filesPanes = { files: { isOpen: () => firstPane.open, path: () => firstPane.at } };
async function fetchPaneCwd() { return cwds[currentSession]; }
async function fsList(path) { listed.push(path); return { path, entries: [] }; }
async function openExplorer(path, opts, primed) {
  opened.push([currentSession, path, !!primed]);
  filesPath = path;
  return true;
}
""" + _js_chunk(doc, "async function filesOpenAtCwd(opts) {") + body


def test_the_reopen_lists_the_remembered_folder_once(doc, tmp_path):
    """The journal showed each reopen listing the remembered folder twice: the
    probe, then the open. The probe's listing is the one painted."""
    out = _node_json(tmp_path, "reopen.mjs", _open_at_cwd_harness(doc, """
keepFilesLast([{ name: "a", sid: 1, created: 5 }]);
currentSession = "a"; cwds.a = "/home/u";
rememberFilesDir("a", "/mnt/cifs/paper");
await filesOpenAtCwd();
console.log(JSON.stringify({ listed, opened }));
"""))
    assert out == {"listed": ["/mnt/cifs/paper"], "opened": [["a", "/mnt/cifs/paper", True]]}


def test_one_sessions_folder_never_opens_in_another(doc, tmp_path):
    """Founder rule: each session has one remembered folder of its own, and a
    session with nothing remembered opens at its terminal's cwd. Neither the
    store, nor a pane left closed at the other session's folder, nor an old
    server whose two sessions share a creation second may hand B what A left."""
    out = _node_json(tmp_path, "isolate.mjs", _open_at_cwd_harness(doc, """
const got = {};
keepFilesLast([{ name: "a", sid: 1, created: 5 }, { name: "b", sid: 2, created: 5 }]);
cwds.a = "/home/u/a"; cwds.b = "/home/u/b";
currentSession = "a";
rememberFilesDir("a", "/mnt/cifs/paper");
await filesOpenAtCwd();
// The first pane is closed, still holding A's folder, when B opens a copy.
firstPane = { open: false, at: "/mnt/cifs/paper" };
currentSession = "b";
await filesOpenAtCwd();
id = "files#2";
await filesOpenAtCwd();
id = "files";
got.b = filesLastDir("b");
// An old server sends no sid, and both sessions were made in the same second.
keepFilesLast([{ name: "a", created: 7 }, { name: "b", created: 7 }]);
rememberFilesDir("a", "/mnt/cifs/old");
got.oldA = filesLastDir("a");
got.oldB = filesLastDir("b");
console.log(JSON.stringify({ got, listed, opened }));
"""))
    assert out["opened"] == [["a", "/mnt/cifs/paper", True],
                             ["b", "/home/u/b", False],
                             ["b", "/home/u/b", False]]
    assert out["listed"] == ["/mnt/cifs/paper"]
    assert out["got"] == {"b": "", "oldA": "", "oldB": ""}


def test_the_branch_question_is_asked_once_per_repo_in_flight(doc, tmp_path):
    """The journal showed /api/git/branches twice for one folder, each a minute
    of git over the mount. Folders tapped through while it is pending wait for
    that answer, which holds for any folder inside the repo it names; a folder
    outside it is asked about when the answer lands."""
    out = _node_json(tmp_path, "repo.mjs", """
let filesPath = "/r/a", filesRepo = null, filesRepoAsked = "", filesRepoBusy = false;
let filesHome = "/home/u";
const asked = [], waiting = [];
function demoApiOn() { return false; }
function hasCap() { return true; }
function syncRefBar() {}
function rejectToken() {}
function authHeaders() { return {}; }
function apiURL(u) { return u; }
function fetch(u) {
  asked.push(decodeURIComponent(u.split("path=")[1]));
  return new Promise((res) => waiting.push((body) => res({ status: 200, ok: true,
                                                           json: async () => body })));
}
""" + _js_chunk(doc, "function insideRoot(path, root) {") + _js_chunk(doc, "async function syncRepo() {") + """
const tick = () => new Promise((r) => setTimeout(r, 5));
const p1 = syncRepo();
filesPath = "/r/a/b";
syncRepo();
waiting.shift()({ root: "/r", current: "main", branches: ["main"] });
await p1;
const inRepo = { asked: asked.slice(), root: filesRepo && filesRepo.root };
filesPath = "/s/x";
const p2 = syncRepo();
filesPath = "/t/y";
waiting.shift()({ root: "/s", current: "main", branches: [] });
await p2; await tick();
console.log(JSON.stringify({ inRepo, asked }));
""")
    assert out == {"inRepo": {"asked": ["/r/a"], "root": "/r"},
                   "asked": ["/r/a", "/s/x", "/t/y"]}


# ---- the anonymous usage summary (45-usage.js) ------------------------------

USAGE_HOOKS = (
    ("function openExplorer(path, opts) {", "explorer"),
    ("function openBrowser(url, tabs, at, id, opts) {", "browser"),
    ("function toggleDiffPane() {", "diff"),
    ("function sideClaim(id, opts) {", "side2"),
    ("function startRecording() {", "voice"),
    ("function openSettings(firstRun, tab) {", "settings"),
    ("async function openReader(path, pane) {", "reader"),
    ("async function openEditor(path, opts) {", "editor"),
    ("function showImage(path, pane) {", "viewer"),
    ("async function openPdf(path) {", "viewer"),
    ("function openSearch() {", "search"),
    ("async function createAndOpenSession(base, exact) {", "newsess"),
)


def test_usage_counter_is_declared_before_any_fragment_counts(doc):
    """The counters are called at load (sideBootOpen, boot opening a session),
    so they must be declared ahead of every call site; and the sender reads
    every fragment it depends on at load, so it comes after all of them."""
    assert doc.index("\nfunction usageCount(") < doc.index('usageCount("')
    assert doc.index("\nconst usageCounts") < doc.index("usageSeen.add(")
    frags = list(build_mobile.JS_FRAGMENTS)
    at = frags.index("45-usage.js")
    for dep in ("01-helpers.js", "02-debug-log.js", "03-a2hs-hint.js", "31-wide-layout.js",
                "35-report.js", "36-server-version.js"):
        assert frags.index(dep) < at, dep
    # Nothing earlier reads the sender's own bindings at load: they appear in
    # no fragment before it.
    before = "".join((SRC / "js" / f).read_text(encoding="utf-8") for f in frags[:at])
    for name in ("USAGE_URL", "USAGE_TEXT_VERSION", "usageStart", "usageSent", "usageHidden"):
        assert name not in before, name


@pytest.mark.parametrize("head,key", USAGE_HOOKS, ids=[h[0] for h in USAGE_HOOKS])
def test_each_way_in_is_counted(doc, head, key):
    assert f'usageCount("{key}")' in _js_chunk(doc, head)


def test_sessions_seen_and_reconnects_are_counted(doc):
    opener = _js_chunk(doc, "function openTerminal(name, resumed) {")
    assert "if (!demoMode) usageSeen.add(name);" in opener
    retry = _js_chunk(doc, "function scheduleReconnect() {")
    # After both early returns: a hidden page and an offline one do not retry.
    assert retry.index("usageReconnects += 1;") > retry.index("if (netOffline()) {")
    assert _js_chunk(doc, "function sideClaim(id, opts) {").count('usageCount("side2")') == 1


def test_the_cloudflare_beacon_is_gone_and_the_about_rows_exist(doc):
    assert "cloudflareinsights" not in doc
    about = doc[doc.index('id="panel-about"'):]
    about = about[:about.index('id="dbg-toggle"')]
    assert '<label for="usage-toggle">Usage statistics</label>' in about
    assert '<input id="usage-toggle" type="checkbox">' in about
    assert '<label for="usage-id-toggle">Count me as a returning user</label>' in about
    assert '<input id="usage-id-toggle" type="checkbox">' in about
    assert "Your terminal is never routed through our servers." in about


def _usage_harness(doc, text_version=None):
    """The real usage code under node: the 01 counters, cfg's three usage keys,
    parseUA, and the whole 45 fragment, over stubbed browser globals. The DOM is
    a map of elements with a class list and click listeners, showSheet keeps
    the one shown, and the load-time timer is parked in `timers` rather than
    run. `text_version` rewrites the question's version constant, standing in
    for a later build that changed the wording."""
    counters = doc[doc.index("\nconst usageCounts") + 1:]
    counters = counters[:counters.index("\n", counters.index("function usageCount(")) + 1]
    keys = doc[doc.index("  // The anonymous usage summary (45-usage.js)."):]
    keys = keys[:keys.index("\n};\n")]
    frag = (SRC / "js" / "45-usage.js").read_text(encoding="utf-8")
    assert frag in doc
    if text_version is not None:
        assert "const USAGE_TEXT_VERSION = 1;" in frag
        frag = frag.replace("const USAGE_TEXT_VERSION = 1;",
                            f"const USAGE_TEXT_VERSION = {text_version};")
    gate = "\n".join(_js_chunk(doc, h) for h in (
        "function testGateActive(", "function testGateLocked("))
    return f"""
const els = {{}};
function $(id) {{
  if (!els[id]) {{
    const cls = new Set(), on = {{}};
    els[id] = {{ id, on, classList: {{ contains: (c) => cls.has(c),
      toggle: (c, v) => {{ if (v) cls.add(c); else cls.delete(c); }} }},
      addEventListener: (t, fn) => {{ on[t] = fn; }}, click() {{ if (on.click) on.click({{}}); }} }};
  }}
  return els[id];
}}
let shown = null, shows = 0;
function showSheet(v, id = "sheet-settings") {{
  shown = v ? id : null;
  if (v) shows += 1;
  $("sheet-scrim").classList.toggle("show", v);
}}
let setupMode = false, voiceStep = false, rowSyncs = 0;
function syncUsageRows() {{ rowSyncs += 1; }}
function dbg() {{}}
// The public build's gate (02-debug-log.js): empty, so never locked.
const TEST_GATE = "";
{gate}
const timers = [];
const setTimeout = (fn, ms) => {{ timers.push({{ fn, ms }}); }};
const ls = {{}};
const localStorage = {{ getItem: (k) => (k in ls ? ls[k] : null),
  setItem: (k, v) => {{ ls[k] = String(v); }}, removeItem: (k) => {{ delete ls[k]; }} }};
const listeners = {{}};
const on = (t, fn) => {{ (listeners[t] = listeners[t] || []).push(fn); }};
const fire = (t, e) => (listeners[t] || []).forEach((fn) => fn(e || {{}}));
const document = {{ visibilityState: "visible", addEventListener: on }};
const window = {{ addEventListener: on }};
const beacons = [];
const navigator = {{ userAgent: "", maxTouchPoints: 0,
  sendBeacon: (url, blob) => {{ beacons.push({{ url, blob }}); return true; }} }};
let location = {{ hostname: "pockettui.com" }};
let SAME_ORIGIN = false;
let demoMode = false;
let paired = true;
function needsSetup() {{ return !paired; }}
let currentSession = null;
let appV = "0.9.210", serverVersion = "0.9.209";
function appVersion() {{ return appV; }}
let wide = false;
function isWideLayout() {{ return wide; }}
let pwa = false;
function a2hsInstalled() {{ return pwa; }}
{counters}
const cfg = {{
{keys}
}};
{_js_chunk(doc, "const REPORT_BROWSERS")}
{_js_chunk(doc, "function parseUA(")}
{frag}
"""


UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
UA_ANDROID = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0.0.0 Mobile Safari/537.36")
UA_MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
UA_OLD_IPAD = ("Mozilla/5.0 (iPad; CPU OS 12_5 like Mac OS X) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/12.1 Mobile/15E148 Safari/604.1")
UA_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0 Safari/537.36"
UA_CROS = "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 Chrome/129.0 Safari/537.36"
UA_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"


def test_usage_payload_carries_only_the_allowed_fields(doc, tmp_path):
    uas = {"iphone": [UA_IPHONE, 5], "android": [UA_ANDROID, 5], "mac": [UA_MAC, 0],
           "ipad": [UA_MAC, 5], "oldipad": [UA_OLD_IPAD, 5], "win": [UA_WIN, 0],
           "cros": [UA_CROS, 0], "linux": [UA_LINUX, 0], "odd": ["curl/8", 0]}
    out = _node_json(tmp_path, "payload.mjs", _usage_harness(doc) + f"""
const got = {{}};
usageCount("explorer"); usageCount("explorer"); usageCount("voice"); usageCount("nope");
usageSeen.add("a"); usageSeen.add("b"); usageSeen.add("a");
usageReconnects = 3;
got.plain = usagePayload(usageStart + 12900);
cfg.usageId = "0b8f5e1c-2d3a-4b5c-9d6e-7f8091a2b3c4";
got.withId = usagePayload(usageStart + 1000).id;
cfg.usageId = "";
got.shellHosted = usageShell();
location = {{ hostname: "example.net" }};
got.shellOther = usageShell();
SAME_ORIGIN = true;
got.shellSelf = usageShell();
got.os = {{}};
for (const [k, [ua, mtp]] of Object.entries({json.dumps(uas)})) {{
  navigator.userAgent = ua; navigator.maxTouchPoints = mtp;
  got.os[k] = usagePayload().os;
}}
appV = "0.9.1 <b>/x"; serverVersion = "a".repeat(40);
wide = true; pwa = true;
const p = usagePayload();
got.app = p.app; got.srv = p.srv; got.layout = p.layout; got.pwa = p.pwa;
console.log(JSON.stringify(got));
""")
    plain = out["plain"]
    assert sorted(plain) == sorted(["v", "app", "srv", "shell", "os", "layout", "pwa",
                                    "secs", "rc", "seen", "f"])
    assert plain["v"] == 1 and plain["app"] == "0.9.210" and plain["srv"] == "0.9.209"
    assert plain["shell"] == "hosted" and plain["layout"] == "phone" and plain["pwa"] == 0
    assert plain["secs"] == 12 and plain["rc"] == 3 and plain["seen"] == 2
    assert plain["f"] == {"explorer": 2, "browser": 0, "diff": 0, "side2": 0, "voice": 1,
                          "settings": 0, "reader": 0, "editor": 0, "viewer": 0,
                          "search": 0, "newsess": 0}
    assert out["withId"] == "0b8f5e1c-2d3a-4b5c-9d6e-7f8091a2b3c4"
    assert (out["shellHosted"], out["shellOther"], out["shellSelf"]) == ("hosted", "other", "self")
    assert out["os"] == {"iphone": "ios", "android": "android", "mac": "mac", "ipad": "ipados",
                         "oldipad": "ipados", "win": "windows", "cros": "chromeos",
                         "linux": "linux", "odd": "other"}
    assert out["app"] == "0.9.1bx" and out["srv"] == "a" * 20
    assert out["layout"] == "desktop" and out["pwa"] == 1


def test_usage_sends_once_per_stint_and_only_when_it_should(doc, tmp_path):
    out = _node_json(tmp_path, "stint.mjs", _usage_harness(doc) + """
const got = {};
const back = (s) => { usageStart = Date.now() - s * 1000; };
back(6);
got.first = usageFlush();
got.second = usageFlush();
got.afterTwo = beacons.length;
// Both backgrounding events on one hide: one beacon.
usageResume(); back(6);
document.visibilityState = "hidden"; fire("visibilitychange"); fire("pagehide");
got.afterHide = beacons.length;
// Coming back starts a new stint with zeroed counters.
usageCount("diff"); usageSeen.add("x"); usageReconnects = 4;
document.visibilityState = "visible"; fire("visibilitychange");
got.zeroed = usageCounts.diff === 0 && usageSeen.size === 0 && usageReconnects === 0 && !usageSent;
back(7);
fire("pagehide");
got.afterReturn = beacons.length;
// A bfcache restore is a new stint too, and the open session is part of it.
currentSession = "s1";
fire("pageshow", { persisted: true });
got.seenOnResume = usageSeen.size;
back(6);
cfg.usageOff = true; got.off = usageFlush(); cfg.usageOff = false;
demoMode = true; got.demo = usageFlush(); demoMode = false;
paired = false; got.unpaired = usageFlush(); paired = true;
back(4); got.short = usageFlush();
got.suppressed = beacons.length;
const b = beacons[0];
got.url = b.url; got.type = b.blob.type;
got.body = JSON.parse(await b.blob.text());
// No beacon: the no-cors text/plain fetch carries it.
const fetches = [];
globalThis.fetch = (u, o) => { fetches.push({ u, o }); return Promise.resolve(); };
navigator.sendBeacon = () => false;
usageResume(); back(6); usageFlush();
got.fetch = fetches.map((x) => ({ u: x.u, method: x.o.method, mode: x.o.mode,
  keepalive: x.o.keepalive, ct: x.o.headers["Content-Type"], v: JSON.parse(x.o.body).v }));
console.log(JSON.stringify(got));
""")
    assert out["first"] is True and out["second"] is False and out["afterTwo"] == 1
    assert out["afterHide"] == 2
    assert out["zeroed"] is True
    assert out["afterReturn"] == 3
    assert out["seenOnResume"] == 1
    assert not any(out[k] for k in ("off", "demo", "unpaired", "short"))
    assert out["suppressed"] == 3
    assert out["url"] == "https://pockettui.com/api/wave" and out["type"] == "text/plain"
    assert out["body"]["v"] == 1 and out["body"]["secs"] >= 5
    assert out["fetch"] == [{"u": "https://pockettui.com/api/wave", "method": "POST",
                             "mode": "no-cors", "keepalive": True, "ct": "text/plain", "v": 1}]


def test_usage_id_lifecycle_and_consent(doc, tmp_path):
    out = _node_json(tmp_path, "consent.mjs", _usage_harness(doc) + """
const got = {};
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
got.dueNone = usageConsentDue();
ls.pockettui_usage_consent = "{nope";
got.dueBad = usageConsentDue();
ls.pockettui_usage_consent = JSON.stringify({ consent: true, at: "x", v: 0 });
got.dueOld = usageConsentDue();
delete ls.pockettui_usage_consent;
usageGrant();
const id1 = ls.pockettui_usage_id;
const rec1 = JSON.parse(ls.pockettui_usage_consent);
got.grant = { uuid: UUID.test(id1), consent: rec1.consent, v: rec1.v,
  at: !isNaN(Date.parse(rec1.at)), due: usageConsentDue(), cfgId: cfg.usageId === id1 };
usageRevoke();
const rec2 = JSON.parse(ls.pockettui_usage_consent);
got.revoke = { gone: !("pockettui_usage_id" in ls), consent: rec2.consent, v: rec2.v,
  due: usageConsentDue() };
usageGrant();
got.regrant = UUID.test(ls.pockettui_usage_id) && ls.pockettui_usage_id !== id1;
usageSetOff(true);
got.offDeletes = !("pockettui_usage_id" in ls) && ls.pockettui_usage_off === "1" &&
  JSON.parse(ls.pockettui_usage_consent).consent === false;
usageSetOff(false);
got.onAgain = !("pockettui_usage_off" in ls) && !("pockettui_usage_id" in ls);
// The fallback for a plain-http LAN shell, where randomUUID does not exist.
const real = crypto.randomUUID;
Object.defineProperty(crypto, "randomUUID", { value: undefined, configurable: true });
const minted = new Set();
for (let i = 0; i < 50; i++) minted.add(usageMintId());
Object.defineProperty(crypto, "randomUUID", { value: real, configurable: true });
got.fallback = minted.size === 50 && [...minted].every((u) => UUID.test(u));
ls.pockettui_usage_id = "not-a-uuid";
got.badIdIgnored = cfg.usageId === "";
console.log(JSON.stringify(got));
""")
    assert out["dueNone"] is True and out["dueBad"] is True and out["dueOld"] is True
    assert out["grant"] == {"uuid": True, "consent": True, "v": 1, "at": True,
                            "due": False, "cfgId": True}
    assert out["revoke"] == {"gone": True, "consent": False, "v": 1, "due": False}
    assert out["regrant"] is True
    assert out["offDeletes"] is True and out["onAgain"] is True
    assert out["fallback"] is True and out["badIdIgnored"] is True


ASK_JS = """
const got = {};
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const fresh = () => { usageAsked = false; shown = null; shows = 0;
  $("sheet-scrim").classList.toggle("show", false); };
"""


def test_usage_question_shows_once_per_load_and_only_on_a_quiet_screen(doc, tmp_path):
    out = _node_json(tmp_path, "ask.mjs", _usage_harness(doc) + ASK_JS + """
got.timer = timers.map((t) => t.ms);
// Each guard on its own, from a fresh load where the question is due.
cfg.usageOff = true; got.off = usageAskIfDue("t"); cfg.usageOff = false;
demoMode = true; got.demo = usageAskIfDue("t"); demoMode = false;
paired = false; got.unpaired = usageAskIfDue("t"); paired = true;
setupMode = true; got.setup = usageAskIfDue("t"); setupMode = false;
voiceStep = true; got.voice = usageAskIfDue("t"); voiceStep = false;
showSheet(true, "sheet-settings"); got.overSheet = usageAskIfDue("t"); showSheet(false);
got.noneShown = shows === 1 && shown === null;
fresh();
// The boot timer, once nothing stands in the way.
timers[0].fn();
got.boot = { shown, shows };
got.second = usageAskIfDue("pairing");
got.stillOne = shows;
// The cross stores nothing.
$("btn-usage-close").click();
got.closed = { shown, consent: "pockettui_usage_consent" in ls, due: usageConsentDue() };
// Not due: nothing to ask.
fresh();
ls.pockettui_usage_consent = JSON.stringify({ consent: false, at: "x", v: 1 });
got.notDue = usageAskIfDue("t");
console.log(JSON.stringify(got));
""")
    assert out["timer"] == [800]
    for k in ("off", "demo", "unpaired", "setup", "voice", "overSheet"):
        assert out[k] is False, k
    assert out["noneShown"] is True
    assert out["boot"] == {"shown": "sheet-usage", "shows": 1}
    assert out["second"] is False and out["stillOne"] == 1
    assert out["closed"] == {"shown": None, "consent": False, "due": True}
    assert out["notDue"] is False


def test_usage_question_answers(doc, tmp_path):
    out = _node_json(tmp_path, "answer.mjs", _usage_harness(doc) + ASK_JS + """
usageAskIfDue("t");
$("btn-usage-no").click();
const no = JSON.parse(ls.pockettui_usage_consent);
got.no = { shown, consent: no.consent, v: no.v, at: !isNaN(Date.parse(no.at)),
  id: "pockettui_usage_id" in ls, off: "pockettui_usage_off" in ls,
  due: usageConsentDue(), rows: rowSyncs };
fresh();
got.askedAgain = usageAskIfDue("t");
// A yes after an earlier no.
delete ls.pockettui_usage_consent;
fresh();
usageAskIfDue("t");
$("btn-usage-yes").click();
const yes = JSON.parse(ls.pockettui_usage_consent);
got.yes = { shown, consent: yes.consent, v: yes.v, uuid: UUID.test(ls.pockettui_usage_id),
  due: usageConsentDue(), rows: rowSyncs };
console.log(JSON.stringify(got));
""")
    assert out["no"] == {"shown": None, "consent": False, "v": 1, "at": True,
                         "id": False, "off": False, "due": False, "rows": 1}
    assert out["askedAgain"] is False
    assert out["yes"] == {"shown": None, "consent": True, "v": 1, "uuid": True,
                          "due": False, "rows": 2}


def test_usage_question_returns_when_the_wording_version_rises(doc, tmp_path):
    out = _node_json(tmp_path, "reask.mjs", _usage_harness(doc, text_version=2) + ASK_JS + """
// A yes to the old wording: the id it minted must not outlive a no to the new.
ls.pockettui_usage_consent = JSON.stringify({ consent: true, at: "x", v: 1 });
ls.pockettui_usage_id = "0b8f5e1c-2d3a-4b5c-9d6e-7f8091a2b3c4";
got.due = usageConsentDue();
got.asked = usageAskIfDue("t");
$("btn-usage-no").click();
const rec = JSON.parse(ls.pockettui_usage_consent);
got.rec = { consent: rec.consent, v: rec.v };
got.idGone = !("pockettui_usage_id" in ls) && !("id" in usagePayload());
got.after = usageConsentDue();
console.log(JSON.stringify(got));
""")
    assert out == {"due": True, "asked": True, "rec": {"consent": False, "v": 2},
                   "idGone": True, "after": False}


USAGE_SHEET_LINES = (
    "The app already sends one anonymous summary per use: app version, device type, "
    "time used and which panes were opened. Never what was in them.",
    "Saying yes adds a random id so returning use can be counted. It is never linked "
    "to your name, address, hostnames, folders, commands or text.",
    "Change your mind any time in Settings, About.",
)


def test_usage_sheet_markup_and_the_pairing_trigger(doc):
    at = doc.index('<div class="sheet" id="sheet-usage"')
    sheet = doc[at:doc.index("\n</div>\n", at)]
    assert "<h2" in sheet and "Help count PocketTUI users</h2>" in sheet
    for line in USAGE_SHEET_LINES:
        assert f"<p>{line}</p>" in sheet, line
    assert ('<a href="https://pockettui.com/#privacy" target="_blank" rel="noopener">'
            "What is sent</a>") in sheet
    buttons = re.findall(r'<button type="button" class="([^"]*)" id="btn-usage-(yes|no)">([^<]*)<', sheet)
    assert {(b[1], b[2]) for b in buttons} == {("yes", "Share anonymous usage"), ("no", "Not now")}
    assert len({b[0] for b in buttons}) == 1
    assert "primary" not in buttons[0][0] and "primary" not in sheet
    assert 'id="btn-usage-close"' in sheet
    assert '"sheet-usage"' in _js_chunk(doc, "const SHEET_IDS")
    confirm = doc[doc.index('$("btn-voice-confirm").addEventListener("click"'):]
    confirm = confirm[:confirm.index("\n});\n")]
    assert confirm.index('usageAskIfDue("pairing")') > confirm.index("showSheet(false);")


# ---- the usage console (src/console/) --------------------------------------

CONSOLE_SRC = REPO / "src" / "console" / "index.html"


def test_console_page_is_one_self_contained_file():
    """No build step and nothing fetched from elsewhere: the deployed page is the
    file written, and the only network call is the console API itself."""
    page = CONSOLE_SRC.read_text(encoding="utf-8")
    assert "<title>PocketTUI usage</title>" in page
    assert "<script src=" not in page
    assert "<link" not in page
    assert "http://" not in page
    assert 'const API = "https://pockettui.com/api/console";' in page
    assert 'const STORE = "pockettui_console";' in page
    assert "url: location.href" in page


def test_console_is_built_beside_the_shell_but_not_into_the_runtime(tmp_path, doc, monkeypatch):
    build = tmp_path / "mobile_build"
    monkeypatch.setattr(build_mobile, "BUILD_DIR", build)
    monkeypatch.setattr(sys, "argv", ["build_mobile.py", "--version", "0.9.999"])
    assert build_mobile.main() == 0
    assert (build / "console.html").read_bytes() == CONSOLE_SRC.read_bytes()
    runtime = tmp_path / "runtime"
    # The runtime set is what install.sh and the tarball carry to a self-hosted
    # install; the console is the founder's page and stays out of it.
    build_mobile.emit_runtime(runtime, doc, "// sw")
    assert not any("console" in p.name for p in runtime.rglob("*"))


def test_console_about_row_markup(doc):
    about = doc[doc.index('id="panel-about"'):]
    about = about[:about.index('id="dbg-toggle"')]
    row = about[about.index('<div class="toggle-row" id="console-row" hidden>'):]
    row = row[:row.index("</div>")]
    assert "<label>Usage console</label>" in row
    assert '<button type="button" id="btn-console">Open</button>' in row
    assert about.index('id="console-row"') > about.index('id="usage-id-toggle"')


def test_console_row_shows_with_a_stored_url_or_the_test_gate(doc, tmp_path):
    """syncUsageRows (05) unhides the row exactly when the console's stored
    object carries a string url, or the test gate is active (then Open falls
    back to console.html beside the shell); Open is a synchronous anchor click
    and a stored url wins."""
    at = doc.index('$("btn-console").addEventListener("click"')
    opener = doc[at:doc.index("\n});\n", at) + 4]
    out = _node_json(tmp_path, "console-row.mjs", f"""
const els = {{}};
function $(id) {{
  if (!els[id]) els[id] = {{ id, hidden: true, checked: false, disabled: false, on: {{}},
    addEventListener(t, fn) {{ this.on[t] = fn; }} }};
  return els[id];
}}
const ls = {{}};
const localStorage = {{ getItem: (k) => (k in ls ? ls[k] : null) }};
const clicks = [];
const document = {{
  createElement: () => ({{ style: {{}}, click() {{ clicks.push({{ href: this.href, target: this.target, rel: this.rel }}); }}, remove() {{}} }}),
  body: {{ appendChild() {{}} }},
}};
const window = {{ open() {{ throw new Error("window.open used"); }} }};
const cfg = {{ usageOff: false, usageId: "" }};
let gate = false;
function testGateActive() {{ return gate; }}
const location = {{ href: "https://apps.example.net/ptui-test/index.html?x=1#y" }};
{_js_chunk(doc, "function syncUsageRows(")}
{_js_chunk(doc, "function usageConsoleUrl(")}
{_js_chunk(doc, "function syncConsoleRow(")}
const btn = $("btn-console");
{opener}
const got = {{}};
const cases = {{
  none: null,
  junk: "not json",
  nourl: JSON.stringify({{ key: "k" }}),
  numurl: JSON.stringify({{ key: "k", url: 5 }}),
  jsurl: JSON.stringify({{ key: "k", url: "javascript:alert(1)" }}),
  prod: JSON.stringify({{ key: "k", url: "https://pockettui.com/console/" }}),
  test: JSON.stringify({{ url: "https://apps.saidwivedi.in/pockettui/console.html" }}),
}};
for (const [name, v] of Object.entries(cases)) {{
  for (const k of Object.keys(ls)) delete ls[k];
  if (v !== null) ls.pockettui_console = v;
  $("console-row").hidden = name !== "none";
  syncUsageRows();
  got[name] = $("console-row").hidden;
}}
for (const g of [true, false]) {{
  for (const k of Object.keys(ls)) delete ls[k];
  gate = g;
  $("console-row").hidden = !g;
  syncUsageRows();
  got[g ? "gate_none" : "nogate_none"] = $("console-row").hidden;
}}
gate = true;
btn.on.click();
ls.pockettui_console = cases.prod;
btn.on.click();
gate = false;
btn.on.click();
got.clicks = clicks;
console.log(JSON.stringify(got));
""")
    prod = {"href": "https://pockettui.com/console/", "target": "_blank", "rel": "noopener"}
    gated = {"href": "https://apps.example.net/ptui-test/console.html",
             "target": "_blank", "rel": "noopener"}
    assert out.pop("clicks") == [gated, prod, prod]
    assert out == {"none": True, "junk": True, "nourl": True, "numurl": True,
                   "jsurl": True, "prod": False, "test": False,
                   "gate_none": False, "nogate_none": True}


# ---- the test deployment's password gate ------------------------------------

def _build_with(tmp_path, monkeypatch, *args):
    build = tmp_path / "mobile_build"
    monkeypatch.setattr(build_mobile, "BUILD_DIR", build)
    monkeypatch.setattr(sys, "argv", ["build_mobile.py", *args])
    assert build_mobile.main() == 0
    return (build / "index.html").read_text(encoding="utf-8")


def test_a_build_without_a_gate_carries_an_empty_one(tmp_path, monkeypatch):
    """The public build: the placeholder becomes "" and never survives as-is."""
    html = _build_with(tmp_path, monkeypatch, "--version", "0.9.999")
    assert html.count('const TEST_GATE = "";') == 1
    assert "__TEST_GATE__" not in html


def test_a_gated_build_embeds_the_hash(tmp_path, monkeypatch):
    gate = "ab" * 32
    html = _build_with(tmp_path, monkeypatch, "--test-gate", gate)
    assert html.count(f'const TEST_GATE = "{gate}";') == 1
    assert 'const TEST_GATE = "";' not in html


def test_the_self_hosted_shell_substitutes_an_empty_gate():
    src = (REPO / "app.py").read_text(encoding="utf-8")
    assert 'html = html.replace("__TEST_GATE__", "")' in src


def test_the_gate_panel_is_hidden_markup(doc):
    assert doc.count('<div id="test-gate" hidden>') == 1
    panel = doc[doc.index('<div id="test-gate" hidden>'):]
    panel = panel[:panel.index("</form>")]
    assert '<label for="test-gate-input">Test build password</label>' in panel
    assert '<input type="password" id="test-gate-input"' in panel
    assert '<p id="test-gate-err" hidden></p>' in panel
    assert '<button type="submit" id="btn-test-gate">Continue</button>' in panel


def test_boot_waits_for_the_gate(doc):
    """Nothing boots before the gate: the demo, the pairing link, the first run
    and the list all live inside bootApp, and the gate decides when it runs."""
    boot = _js_chunk(doc, "function bootApp(")
    for part in ('openDemo();', 'location.hash.indexOf("#pair=") === 0',
                 'openSettings(true);', 'loadSessions().then('):
        assert part in boot
    assert "\nif (testGateLocked()) openTestGate(bootApp);\nelse bootApp();\n" in doc
    assert "  if (testGateLocked()) return false;" in _js_chunk(doc, "function usageAskIfDue(")


def _gate_code(doc):
    return "\n".join(_js_chunk(doc, h) for h in (
        "function testGateActive(", "async function testGateCheck("))


@pytest.mark.parametrize("value,active", [
    ("", False), ("__TEST_GATE__", False), ("ab" * 32, True),
    ("a" * 63, False), ("AB" * 32, False)])
def test_the_gate_is_active_only_for_a_hash(doc, tmp_path, value, active):
    out = _node_json(tmp_path, "active.mjs", f"""
const TEST_GATE = {json.dumps(value)};
{_gate_code(doc)}
console.log(JSON.stringify(testGateActive()));
""")
    assert out is active


def test_the_gate_checks_the_salted_sha256(doc, tmp_path):
    import hashlib
    gate = hashlib.sha256(b"pockettui-test-gate|right horse").hexdigest()
    out = _node_json(tmp_path, "check.mjs", f"""
const TEST_GATE = {json.dumps(gate)};
{_gate_code(doc)}
console.log(JSON.stringify([await testGateCheck("right horse"),
                            await testGateCheck("wrong horse"),
                            await testGateCheck("")]));
""")
    assert out == [True, False, False]
