"""The one test that starts a real browser, when this machine has one.

It is the only proof that the pieces fit: the pipe really is on fds 3 and 4, the
flags really do produce a browser that does not announce itself as automated,
and `close()` really does leave nothing behind — including the systemd scope,
which would otherwise keep its name and collide with the next launch.

Skipped, not failed, where there is no Chromium: most machines running the
suite are not the machine running the browser.
"""

import asyncio
import os
import subprocess
import sys
import time
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
        assert still["fmt"] == "webp" and data[:4] == b"RIFF"
        assert (still["w"], still["h"]) == (400, 300)

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
