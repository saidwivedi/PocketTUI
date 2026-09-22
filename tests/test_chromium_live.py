"""The one test that starts a real browser, when this machine has one.

It is the only proof that the pieces fit: the pipe really is on fds 3 and 4, the
flags really do produce a browser that does not announce itself as automated,
and `close()` really does leave nothing behind — including the systemd scope,
which would otherwise keep its name and collide with the next launch.

Skipped, not failed, where there is no Chromium: most machines running the
suite are not the machine running the browser.
"""

import asyncio
import base64
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromium as C  # noqa: E402

FOUND = C.find_chromium(force=True)
pytestmark = pytest.mark.skipif(FOUND is None, reason="no Chromium on this machine")

PAGE = "data:text/html,<title>ok</title><p>hello"


def unit_alive(unit: str) -> bool:
    out = subprocess.run(["systemctl", "--user", "is-active", unit],
                         capture_output=True, text=True)
    return out.stdout.strip() == "active"


def test_launch_navigate_evaluate_close(tmp_path, monkeypatch):
    # The profile and Chrome's own log land under HOME, so HOME is the temp dir
    # and this test never touches the profile the real browser pane uses.
    monkeypatch.setenv("HOME", str(tmp_path))

    async def main():
        browser = await C.launch(FOUND, C.load_browser_config())
        try:
            assert browser.pid > 0
            assert (tmp_path / ".pockettui" / "chromium-profile").is_dir()
            assert oct(os.stat(tmp_path / ".pockettui" / "chromium-profile").st_mode)[-3:] == "700"

            target = await browser.cdp.send("Target.createTarget",
                                            {"url": "about:blank"})
            sess = await browser.cdp.attach(target["targetId"])
            await sess.send("Page.enable")

            loaded = asyncio.get_running_loop().create_future()
            sess.on("Page.loadEventFired",
                    lambda params: loaded.done() or loaded.set_result(True))
            await sess.send("Page.navigate", {"url": PAGE})
            await asyncio.wait_for(loaded, 10)

            async def value(expr):
                res = await sess.send("Runtime.evaluate",
                                      {"expression": expr, "returnByValue": True})
                return res["result"]["value"]

            assert await value("document.title") == "ok"
            assert await value("navigator.webdriver") is False
            ua = await value("navigator.userAgent")
            assert "Headless" not in ua and "Chrome/" in ua
            assert "Headless" not in browser.user_agent
        finally:
            unit = browser.unit
            await browser.close()
            await browser.close()  # idempotent

        assert browser.closed.done()
        gone = time.monotonic() + 5
        while time.monotonic() < gone:
            try:
                os.kill(browser.pid, 0)
            except OSError:
                break
            await asyncio.sleep(0.1)
        with pytest.raises(OSError):
            os.kill(browser.pid, 0)
        if unit:
            assert not unit_alive(unit)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Tabs and the stream out of them
# ---------------------------------------------------------------------------

HI = "data:text/html,<body style='margin:0;background:#eee'><h1>hi</h1>"
PICKER = ("data:text/html,<body style='margin:0'>"
          "<select id=s style='position:absolute;left:10px;top:10px;width:140px'>"
          "<option>alpha</option><option>beta</option><option>gamma</option>"
          "</select>")


class Sink:
    """What the pane's socket is, as far as a tab is concerned."""

    def __init__(self) -> None:
        self.frames: list = []
        self.msgs: list = []

    def put_json(self, msg):
        self.msgs.append(msg)

    def put_frame(self, header, body):
        self.frames.append((header, body))

    def kinds(self, after=0):
        return [h["kind"] for h, _ in self.frames[after:]]


async def wait_frame(sink, kind, after=0, timeout=10, match=None):
    """The first frame of `kind` past `after`, or an assertion."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for header, body in sink.frames[after:]:
            if header["kind"] == kind and (match is None or match(header)):
                return header, body
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {kind} frame in {timeout}s; got {sink.kinds(after)}")


def live(fn, home):
    """Run one browser test with its own profile under a temp HOME."""
    C._find_cache = None

    async def main():
        fb = C.FullBrowser(C.load_browser_config())
        try:
            await fn(fb)
        finally:
            await fb.shutdown()

    asyncio.run(main())
    C._find_cache = None


def test_open_data_url_receives_cast_frame_then_still(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", HI, css_w=400, css_h=300, dpr=1, zoom=1)
        await tab.show()

        header, data = await wait_frame(sink, "cast")
        assert header["fmt"] == "jpeg" and data[:2] == b"\xff\xd8"
        assert (header["w"], header["h"]) == (400, 300)
        assert (header["cssW"], header["cssH"]) == (400, 300)
        assert header["tab"] == "t1" and header["seq"] >= 1
        tab.ack(header["seq"])

        # The settled picture: a screenshot taken once the input stops, which
        # is the only frame that can show what the screencast skipped.
        seen = len(sink.frames)
        tab.note_input("mouseMoved")
        still, data = await wait_frame(sink, "still", after=seen)
        assert still["fmt"] == "jpeg" and data[:2] == b"\xff\xd8"
        assert (still["w"], still["h"]) == (400, 300)

    live(body, tmp_path)


def test_a_quiet_page_gets_a_sharp_frame_without_input(tmp_path, monkeypatch):
    """The founder's report: a page that loads and sits there looked blurred.

    A screencast frame comes back at the CSS size whatever pixel ratio the page
    is rendered at, so on a 2x client every frame is half the resolution of the
    page. Nobody touches this tab, so nothing on the input path runs: the sharp
    frame has to come from the quiet itself.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", HI, css_w=400, css_h=300, dpr=2, zoom=1)
        await tab.show()

        cast, _ = await wait_frame(sink, "cast")
        assert (cast["w"], cast["h"]) == (400, 300)     # the soft one
        tab.ack(cast["seq"])

        still, data = await wait_frame(sink, "still", timeout=3)
        assert (still["w"], still["h"]) == (800, 600)   # the page's own pixels
        assert data[:2] == b"\xff\xd8"
        assert tab.info()["stills"] == 1

        # And one is all a page at rest is worth: a capture holds the stream
        # for the length of an encode, and the picture has not changed.
        await asyncio.sleep(2.0)
        assert sink.kinds().count("still") == 1, sink.kinds()
        assert tab.info()["stills"] == 1

    live(body, tmp_path)


# A result page the way a search engine draws one: blue links, grey text, and
# a hover colour on every row, so a pointer moving over it makes the page paint.
RESULTS_PAGE = "data:text/html," + urllib.parse.quote(
    "<title>results</title><style>body{margin:0;padding:24px;font:16px/1.5 Arial}"
    "a{color:rgb(26,13,171);display:block;padding:6px}"
    "a:hover{background:rgb(232,240,254);text-decoration:underline}"
    "p{color:rgb(84,84,84);margin:0 0 12px}</style>"
    + "".join(f"<a href='https://example.net/{i}'>Result number {i} about "
              f"something</a><p>A couple of lines of grey text under result {i}, "
              f"with enough words in it that the encoder has edges to spend "
              f"bytes on.</p>" for i in range(1, 13)))


async def client_acks(tab, sink, delay):
    """The client: every frame acknowledged `delay` after it arrived, which is
    the link's distance plus a decode and a paint."""
    seen = 0
    try:
        while True:
            while seen < len(sink.frames):
                header = sink.frames[seen][0]
                at = sink.arrived[seen]
                seen += 1
                wait = at + delay - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                tab.ack(header["seq"])
            await asyncio.sleep(0.005)
    except asyncio.CancelledError:
        return


class TimedSink(Sink):
    def __init__(self) -> None:
        super().__init__()
        self.arrived: list = []

    def put_frame(self, header, body):
        self.arrived.append(time.monotonic())
        super().put_frame(header, body)


def test_a_quiet_google_like_page_stays_at_level_0_with_60ms_acks(tmp_path, monkeypatch):
    """The founder's report: a results page on a Retina laptop, a 59 ms link,
    and the stream at the bottom of the ladder, soft for as long as he looked.
    A link with 60 ms in it and a pointer moving over the page is no reason to
    give up a rung, and the page gets its full-resolution picture the moment
    the pointer stops — and keeps it."""
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        sink = TimedSink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", RESULTS_PAGE, css_w=1200, css_h=800,
                            dpr=2, zoom=1)
        await tab.show()
        acker = asyncio.ensure_future(client_acks(tab, sink, 0.06))
        try:
            await wait_frame(sink, "cast")
            levels = set()
            end = time.monotonic() + 5.0
            n = 0
            while time.monotonic() < end:
                n += 1
                y = 30 + (n * 23) % 700
                tab.note_input("mouseMoved")
                await tab.session.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved", "x": 200 + (n * 17) % 400, "y": y,
                    "button": "none", "buttons": 0, "modifiers": 0})
                levels.add(tab._level)
                await asyncio.sleep(0.03)
            casts = sink.kinds().count("cast")
            assert casts >= 20, sink.kinds()
            assert levels == {0}, (levels, tab.info())

            seen = len(sink.frames)
            still, _ = await wait_frame(sink, "still", after=seen, timeout=3)
            assert (still["w"], still["h"]) == (2400, 1600)
            # And the still is the last thing drawn: its echo is not sent.
            await asyncio.sleep(1.5)
            after = [(h["kind"], h["w"]) for h, _ in sink.frames[seen:]]
            assert after[-1] == ("still", 2400), after
            assert [k for k, _w in after].count("still") == 1, after
            assert tab._level == 0
            info = tab.info()
            assert info["dpr"] == 2 and info["quality"] == 70
            assert "overMs" in info and "dropShare" in info
        finally:
            acker.cancel()

    live(body, tmp_path)


def test_a_tab_forced_to_the_bottom_rung_still_gets_a_2x_still(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        C._LEVEL_MEMORY["p1"] = (len(C.STREAM_LEVELS) - 1, time.monotonic())
        tab = await fb.open("p1", "t1", RESULTS_PAGE, css_w=600, css_h=400,
                            dpr=2, zoom=1)
        assert tab._level == len(C.STREAM_LEVELS) - 1
        await tab.show()
        cast, _ = await wait_frame(sink, "cast")
        # The CSS size: the bottom rung no longer shrinks the viewport.
        assert (cast["w"], cast["h"]) == (600, 400)
        still, data = await wait_frame(sink, "still", timeout=3)
        assert (still["w"], still["h"]) == (1200, 800)
        assert data[:2] == b"\xff\xd8"
        C._LEVEL_MEMORY.clear()

    live(body, tmp_path)


def test_resize_changes_frame_dimensions(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", HI, css_w=400, css_h=300, dpr=1, zoom=1)
        await tab.show()
        header, _ = await wait_frame(sink, "cast")
        assert (header["w"], header["h"]) == (400, 300)
        tab.ack(header["seq"])

        seen = len(sink.frames)
        await tab.resize(640, 480, 1)
        header, _ = await wait_frame(sink, "cast", after=seen,
                                     match=lambda h: h["w"] == 640)
        assert (header["w"], header["h"]) == (640, 480)
        assert (header["cssW"], header["cssH"]) == (640, 480)

    live(body, tmp_path)


def test_select_popup_appears_in_popup_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", PICKER, css_w=400, css_h=300, dpr=1, zoom=1)
        await tab.show()
        header, before = await wait_frame(sink, "cast")
        tab.ack(header["seq"])
        seen = len(sink.frames)

        # A <select>'s menu is the browser's own window drawn over the page's
        # surface: it is in a screenshot and in no screencast frame at all, so
        # without the popup frames the user taps a dropdown and sees nothing.
        tab.note_input("mousePressed")
        for kind in ("mousePressed", "mouseReleased"):
            await tab.session.send("Input.dispatchMouseEvent", {
                "type": kind, "x": 70, "y": 20, "button": "left",
                "clickCount": 1, "buttons": 1 if kind == "mousePressed" else 0})

        popup, after = await wait_frame(sink, "popup", after=seen)
        assert popup["fmt"] == "jpeg" and after[:2] == b"\xff\xd8"
        assert (popup["w"], popup["h"]) == (400, 300)
        assert after != before

    live(body, tmp_path)


def test_two_panes_two_live_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        left, right = Sink(), Sink()
        fb.attach_pane("p1", left)
        fb.attach_pane("p2", right)
        one = await fb.open("p1", "t1", HI, css_w=320, css_h=240, dpr=1, zoom=1)
        two = await fb.open("p2", "t1", HI, css_w=480, css_h=360, dpr=1, zoom=1)
        await one.show()
        await two.show()

        a, _ = await wait_frame(left, "cast")
        b, _ = await wait_frame(right, "cast")
        assert (a["w"], a["h"]) == (320, 240)
        assert (b["w"], b["h"]) == (480, 360)
        assert one.live and two.live
        assert one.target_id != two.target_id

        targets = await fb.browser.cdp.send("Target.getTargets")
        pages = [t for t in targets["targetInfos"] if t["type"] == "page"]
        assert {one.target_id, two.target_id} <= {t["targetId"] for t in pages}

        # A second tab in one pane takes the canvas from the first: the pane
        # has one canvas, so it has one live tab.
        three = await fb.open("p1", "t2", HI, css_w=320, css_h=240, dpr=1, zoom=1)
        await three.show()
        assert three.live and not one.live and two.live
        assert fb.snapshot()["running"] is True
        assert len(fb.snapshot()["tabs"]) == 3

    live(body, tmp_path)


def test_shutdown_kills_browser_within_6s(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    C._find_cache = None
    seen = {}

    async def main():
        fb = C.FullBrowser(C.load_browser_config())
        sink = Sink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", HI, css_w=400, css_h=300, dpr=1, zoom=1)
        await tab.show()
        await wait_frame(sink, "cast")
        seen["pid"] = fb.browser.pid
        seen["unit"] = fb.browser.unit
        started = time.monotonic()
        await fb.shutdown()
        seen["took"] = time.monotonic() - started
        assert fb.snapshot()["running"] is False
        assert fb.snapshot()["tabs"] == []

    asyncio.run(main())
    C._find_cache = None
    # The service unit is KillMode=process, so systemd will not take the
    # browser down with the server: the backend has to, and it has to be
    # quicker than the shutdown it is holding up.
    assert seen["took"] < 6
    gone = time.monotonic() + 3
    while time.monotonic() < gone:
        try:
            os.kill(seen["pid"], 0)
        except OSError:
            break
        time.sleep(0.1)
    with pytest.raises(OSError):
        os.kill(seen["pid"], 0)
    if seen["unit"]:
        assert not unit_alive(seen["unit"])


# ---------------------------------------------------------------------------
# What it costs, measured on the real thing
# ---------------------------------------------------------------------------

def rss_sum_mb(pid: int, unit: str = "") -> float:
    """The number the watchdog does *not* use, for the comparison below."""
    total = 0
    for p in C.ProcessTree.pids(pid, unit or None):
        kb = C.ProcReader().rss_kb(p)
        if kb:
            total += kb
    return total / 1024.0


def test_measure_real_tree_reports_pss_not_rss(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(C, "WATCHDOG_TICK_S", 3600)

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", HI, css_w=400, css_h=300, dpr=1, zoom=1)
        await tab.show()
        await wait_frame(sink, "cast")

        m = C.ProcessTree.measure(fb.browser.pid, fb.browser.unit)
        rss = rss_sum_mb(fb.browser.pid, fb.browser.unit)
        print(f"\none tab: {m.procs} procs, PSS {m.mb:.0f} MB, RSS sum {rss:.0f} MB")
        # A browser with a page in it is hundreds of megabytes and not gigabytes:
        # summed RSS counts every shared mapping once per process and lands
        # several times higher, which is the whole reason PSS is what the cap is
        # enforced on.
        assert 50 < m.mb < 1200, f"{m.mb} MB for one tab"
        assert m.procs >= 3
        assert m.approx is False
        assert rss > m.mb

        # And the same numbers arrive through the watchdog, into the snapshot
        # the pane reads.
        measured = await fb.watchdog.tick()
        assert measured.mb == pytest.approx(m.mb, rel=0.5)
        snap = fb.snapshot()
        assert snap["memMb"] == round(measured.mb)
        assert snap["memApprox"] is False and snap["procs"] >= 3
        assert snap["softMb"] == round(C.caps_mb(fb.config)[0])
        assert snap["discards"] == 0 and snap["restarts"] == 0

    live(body, tmp_path)


@pytest.mark.skipif(shutil.which("systemd-run") is None,
                    reason="no systemd-run on this machine")
def test_scope_launch_when_systemd_run_available(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    if not C.systemd_scope_available():
        pytest.skip("no user manager to put a scope in")

    async def main():
        browser = await C.launch(FOUND, C.load_browser_config())
        unit = browser.unit
        try:
            assert unit and unit.startswith("pockettui-browser-")
            listed = subprocess.run(
                ["systemctl", "--user", "list-units", "--no-legend", "--plain",
                 "pockettui-browser-*"], capture_output=True, text=True)
            assert unit in listed.stdout, listed.stdout
            # The browser is in the scope, which is what makes the scope worth
            # having: the kill that comes with it reaches every renderer.
            assert unit in C.ProcReader().cgroup(browser.pid)
        finally:
            await browser.close()
        gone = subprocess.run(
            ["systemctl", "--user", "list-units", "--no-legend", "--plain",
             "pockettui-browser-*"], capture_output=True, text=True)
        assert unit not in gone.stdout, gone.stdout

    asyncio.run(main())


def test_soft_cap_discards_a_hidden_tab_for_real(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # The ticking loop is parked: this test drives the watchdog itself, and a
    # cap of 1 MB is over the hard cap too — two automatic ticks would restart
    # the browser in the middle of it.
    monkeypatch.setattr(C, "WATCHDOG_TICK_S", 3600)
    C._find_cache = None

    async def main():
        config = C.load_browser_config()
        config["memory_mb"] = 1        # no browser is ever under this
        fb = C.FullBrowser(config)
        try:
            sink = Sink()
            fb.attach_pane("p1", sink)
            one = await fb.open("p1", "t1", HI, css_w=320, css_h=240, dpr=1, zoom=1)
            await one.show()
            await wait_frame(sink, "cast")
            two = await fb.open("p1", "t2", PICKER, css_w=320, css_h=240,
                                dpr=1, zoom=1)
            await two.show()
            assert two.live and not one.live
            was = one.target_id
            sink.msgs.clear()

            measured = await fb.watchdog.tick()
            assert measured.mb > 1
            assert one.discarded and not two.discarded and two.live
            assert fb.discards == 1
            assert {"type": "tab", "tab": "t1", "discarded": True,
                    "url": one.url, "title": one.title} in sink.msgs

            # The target is really gone from the browser, and the live one is
            # really still there. Polled: closeTarget answers before the
            # browser has finished taking the page down.
            ids = set()
            end = time.monotonic() + 5
            while time.monotonic() < end:
                targets = await fb.browser.cdp.send("Target.getTargets")
                ids = {t["targetId"] for t in targets["targetInfos"]
                       if t.get("type") == "page"}
                if was not in ids:
                    break
                await asyncio.sleep(0.1)
            assert was not in ids
            assert two.target_id in ids

            # With room again, showing the discarded tab loads its page on a
            # new target and streams it like any other.
            config["memory_mb"] = 100000
            seen = len(sink.frames)
            await one.show()
            assert one.target_id != was
            assert one.live and not one.discarded
            header, _ = await wait_frame(sink, "cast", after=seen,
                                         match=lambda h: h["tab"] == "t1")
            assert header["cssW"] == 320
            assert not two.live
        finally:
            await fb.shutdown()

    asyncio.run(main())
    C._find_cache = None


# ---------------------------------------------------------------------------
# What a real page asks for
# ---------------------------------------------------------------------------
# A popup, a modal dialog, a file input, a download and a password box are the
# five things a page can do that no amount of pixels answers, and every one of
# them is a round trip through the pane. They are tested against a real browser
# because the protocol's own behaviour is the thing in question: which event
# fires, what it carries, and what the page does once it is answered.

ATTACHMENT = b"the bytes of a downloaded file\n"
FILE_PAGE = "data:text/html,<body style='margin:0'><input id=f type=file>"
TITLE_PAGE = "data:text/html,<title>first</title><body style='margin:0'>hello"


class LiveHandler(BaseHTTPRequestHandler):
    """Four pages: an opener, its popup, an attachment and a locked door."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 — quiet under pytest
        pass

    def _send(self, status, body, headers=()):
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        html = [("Content-Type", "text/html; charset=utf-8")]
        if self.path == "/popup":
            self._send(200, b"<title>the popup</title><p>popped", html)
        elif self.path == "/attach":
            self._send(200, ATTACHMENT, [
                ("Content-Type", "application/octet-stream"),
                ("Content-Disposition", 'attachment; filename="notes.txt"')])
        elif self.path == "/secret":
            want = "Basic " + base64.b64encode(b"ada:hunter2").decode()
            if self.headers.get("Authorization") == want:
                self._send(200, b"<title>in</title><p id=ok>let in", html)
            else:
                self._send(401, b"<p>no", html + [
                    ("WWW-Authenticate", 'Basic realm="wiki"')])
        else:
            self._send(200, b"<title>opener</title><p>the opener", html)


@pytest.fixture(scope="module")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), LiveHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=3)


async def wait_msg(sink, kind, after=0, timeout=10, match=None):
    """The first `kind` message past `after`, or an assertion."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for msg in sink.msgs[after:]:
            if msg.get("type") == kind and (match is None or match(msg)):
                return msg
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {kind} message in {timeout}s; "
                         f"got {[m.get('type') for m in sink.msgs[after:]]}")


async def value(tab, expr, **kw):
    res = await tab.session.send("Runtime.evaluate",
                                 {"expression": expr, "returnByValue": True, **kw})
    return (res.get("result") or {}).get("value")


def test_popup_becomes_new_tab(tmp_path, monkeypatch, site):
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", f"http://127.0.0.1:{site}/",
                            css_w=400, css_h=300, dpr=1, zoom=1)
        await tab.show()
        await wait_frame(sink, "cast")

        # A gesture, because a popup without one is blocked — which is the
        # whole reason this goes through the protocol's own userGesture flag
        # rather than a plain evaluate.
        await tab.session.send("Runtime.evaluate", {
            "expression": f"window.open('http://127.0.0.1:{site}/popup', '_blank')",
            "userGesture": True})

        msg = await wait_msg(sink, "newtab")
        assert msg["opener"] == "t1" and msg["tab"].startswith("p")
        assert msg["url"].endswith("/popup") or msg["url"] == "about:blank"
        popup = fb.tab("p1", msg["tab"])
        assert popup is not None and popup.target_id == msg["targetId"]
        assert not popup.live

        # It is a tab like any other: the pane shows it and pixels come out.
        seen = len(sink.frames)
        await popup.show()
        await wait_frame(sink, "cast", after=seen,
                         match=lambda h: h["tab"] == msg["tab"])
        assert not tab.live
        assert await value(popup, "document.title") == "the popup"

    live(body, tmp_path)


def test_alert_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", TITLE_PAGE, css_w=400, css_h=300,
                            dpr=1, zoom=1)
        await tab.show()
        await wait_frame(sink, "cast")

        # Through a timer, because an alert stops the renderer: an evaluate
        # that raised it would not return until it was answered.
        await tab.session.send("Runtime.evaluate", {
            "expression": "setTimeout(function () { alert('boo'); }, 0)"})
        msg = await wait_msg(sink, "dialog")
        assert msg["tab"] == "t1" and msg["kind"] == "alert"
        assert msg["message"] == "boo" and msg["default"] == ""

        assert await tab.answer_dialog(True) is True
        # The page runs again, which is the only proof the dialog really went.
        assert await value(tab, "1 + 1") == 2

    live(body, tmp_path)


def test_file_chooser_sets_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    picked = tmp_path / "cv.txt"
    picked.write_text("a file on the computer, not on the phone")

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", FILE_PAGE, css_w=400, css_h=300,
                            dpr=1, zoom=1)
        await tab.show()
        await wait_frame(sink, "cast")

        await tab.session.send("Runtime.evaluate", {
            "expression": "document.getElementById('f').click()",
            "userGesture": True})
        msg = await wait_msg(sink, "filechooser")
        assert msg == {"type": "filechooser", "tab": "t1", "multiple": False}

        assert await tab.set_files([str(picked)]) is True
        assert await value(tab, "document.getElementById('f').files.length") == 1
        assert await value(tab, "document.getElementById('f').files[0].name") == \
            "cv.txt"

    live(body, tmp_path)


def test_download_lands_in_downloads_dir(tmp_path, monkeypatch, site):
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", f"http://127.0.0.1:{site}/",
                            css_w=400, css_h=300, dpr=1, zoom=1)
        await tab.show()
        await wait_frame(sink, "cast")

        await tab.nav(f"http://127.0.0.1:{site}/attach")
        msg = await wait_msg(sink, "download", timeout=15,
                             match=lambda m: m["state"] == "completed")
        assert msg["tab"] == "t1" and msg["name"] == "notes.txt"
        # Chrome files it under the guid; the name the site suggested is what
        # the user is told about and what is on the disk.
        path = Path(msg["path"])
        assert path.parent == C.downloads_dir()
        assert path.name == "notes.txt"
        assert path.read_bytes() == ATTACHMENT
        assert not (C.downloads_dir() / msg["guid"]).exists()

    live(body, tmp_path)


def test_basic_auth_prompt_and_success(tmp_path, monkeypatch, site):
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        # Opened straight at the protected page and shown at once, which is
        # what the pane does with a bookmark: the first navigation is suspended
        # on the challenge, so there is no page to stream until it is answered
        # and `show` must not sit on the socket waiting for one.
        tab = await fb.open("p1", "t1", f"http://127.0.0.1:{site}/secret",
                            css_w=400, css_h=300, dpr=1, zoom=1)
        started = time.monotonic()
        await tab.show()
        assert time.monotonic() - started < 5
        assert tab.live

        msg = await wait_msg(sink, "auth", timeout=15)
        assert msg["tab"] == "t1" and msg["realm"] == "wiki"
        assert msg["scheme"] == "basic"
        assert msg["host"] == f"http://127.0.0.1:{site}"

        assert await tab.answer_auth("ada", "hunter2") is True
        end = time.monotonic() + 10
        text = ""
        while time.monotonic() < end:
            text = str(await value(tab, "document.body.innerText") or "")
            if "let in" in text:
                break
            await asyncio.sleep(0.1)
        assert "let in" in text, text

        # And the stream that had nothing to show starts on its own once the
        # page behind the challenge is there.
        await wait_frame(sink, "cast")

    live(body, tmp_path)


def test_script_set_title_reaches_the_tab_message(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", TITLE_PAGE, css_w=400, css_h=300,
                            dpr=1, zoom=1)
        await tab.show()
        await wait_msg(sink, "tab", match=lambda m: m.get("title") == "first")

        seen = len(sink.msgs)
        await tab.session.send("Runtime.evaluate", {
            "expression": "document.title = 'set by a script'"})
        msg = await wait_msg(sink, "tab", after=seen,
                             match=lambda m: m.get("title") == "set by a script")
        assert msg["tab"] == "t1"
        assert tab.title == "set by a script"

    live(body, tmp_path)


def test_crash_reports_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    async def body(fb):
        sink = Sink()
        fb.attach_pane("p1", sink)
        tab = await fb.open("p1", "t1", HI, css_w=400, css_h=300, dpr=1, zoom=1)
        await tab.show()
        await wait_frame(sink, "cast")
        was = tab.target_id

        # Page.crash never answers: the renderer it was sent to is gone.
        crashing = asyncio.ensure_future(tab.session.send("Page.crash", timeout=3))
        msg = await wait_msg(sink, "error",
                             match=lambda m: m.get("code") == "crashed")
        assert msg["tab"] == "t1"
        crashing.cancel()
        await asyncio.gather(crashing, return_exceptions=True)
        assert fb.tab("p1", "t1") is None

        # The pane answers a crash by opening the tab again, naming the target
        # it remembers. That target is still listed as a page — a corpse with a
        # sad face on it — so opening has to close it rather than adopt it.
        again = await fb.open("p1", "t1", HI, target_id=was, css_w=400,
                              css_h=300, dpr=1, zoom=1)
        assert again.target_id != was
        ids = set()
        end = time.monotonic() + 5
        while time.monotonic() < end:
            targets = await fb.browser.cdp.send("Target.getTargets")
            ids = {t["targetId"] for t in targets["targetInfos"]
                   if t.get("type") == "page"}
            if was not in ids:
                break
            await asyncio.sleep(0.1)
        assert was not in ids and again.target_id in ids
        await again.show()
        await wait_frame(sink, "cast", after=len(sink.frames) - 1,
                         match=lambda h: h["tab"] == "t1")

    live(body, tmp_path)
