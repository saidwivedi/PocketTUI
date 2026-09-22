"""The browser floor: discovery, config, flags and the protocol client.

No Chromium is involved anywhere in this file. The protocol client is fed bytes
by hand, because the properties worth pinning are exactly the ones a live
browser would hide: a message arriving in three pieces, a reply to a call that
already gave up, an event that must reach one tab's handler and not another's,
and what happens to everything in flight when the pipe dies.

The fake browsers are shell scripts that print a version string, which is all
`find_chromium` ever asks a binary.
"""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromium as C  # noqa: E402


def run(coro):
    """The suite has no async plugin; every coroutine gets its own loop."""
    return asyncio.run(coro)


async def settle():
    """Give a just-started task its turn at the loop."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def sent_ids(sent):
    return [json.loads(raw[:-1].decode())["id"] for raw in sent]


def frame(msg):
    return json.dumps(msg).encode() + b"\0"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

def test_pipe_parser_splits_on_nul_across_partial_reads():
    got = []
    cdp = C.CDP(lambda data: None)
    cdp.on("Page.loadEventFired", lambda params: got.append(("load", params)))
    cdp.on("Page.frameNavigated", lambda params: got.append(("nav", params)))

    wire = (frame({"method": "Page.loadEventFired", "params": {"timestamp": 1}})
            + frame({"method": "Page.frameNavigated", "params": {"url": "a"}})
            + frame({"method": "Page.loadEventFired", "params": {"timestamp": 2}}))
    # Odd chunks on purpose: a NUL lands mid-chunk, a message spans two chunks,
    # and the last chunk carries the tail of one message plus a whole other one.
    for cut in (7, 23, 5, 40, len(wire)):
        chunk, wire = wire[:cut], wire[cut:]
        cdp.feed(chunk)
    assert wire == b""

    assert got == [("load", {"timestamp": 1}),
                   ("nav", {"url": "a"}),
                   ("load", {"timestamp": 2})]


def test_parser_survives_a_malformed_message():
    got = []
    cdp = C.CDP(lambda data: None)
    cdp.on("Page.loadEventFired", lambda params: got.append(params))
    cdp.feed(b"{not json at all}\0" + frame({"method": "Page.loadEventFired",
                                             "params": {"timestamp": 3}}))
    assert got == [{"timestamp": 3}]


def test_replies_route_by_id_and_events_by_session():
    async def main():
        sent = []
        cdp = C.CDP(sent.append)

        attach = asyncio.ensure_future(cdp.attach("TARGET-1"))
        await settle()
        cdp.feed(frame({"id": sent_ids(sent)[0], "result": {"sessionId": "S1"}}))
        sess = await attach
        assert sess.id == "S1" and sess.target_id == "TARGET-1"

        call = asyncio.ensure_future(sess.send("Page.navigate",
                                               {"url": "https://example.net/"}))
        await settle()
        out = json.loads(sent[-1][:-1].decode())
        assert out["sessionId"] == "S1" and out["method"] == "Page.navigate"
        cdp.feed(frame({"id": out["id"], "sessionId": "S1",
                        "result": {"frameId": "F1"}}))
        assert await call == {"frameId": "F1"}

        mine, other, browser = [], [], []
        sess.on("Page.loadEventFired", mine.append)
        cdp.on("Page.loadEventFired", other.append, session="S2")
        cdp.on("Target.targetCreated", browser.append)

        cdp.feed(frame({"method": "Page.loadEventFired", "sessionId": "S1",
                        "params": {"timestamp": 9}}))
        cdp.feed(frame({"method": "Target.targetCreated",
                        "params": {"targetInfo": {"targetId": "T2"}}}))
        assert mine == [{"timestamp": 9}]
        assert other == []
        assert browser == [{"targetInfo": {"targetId": "T2"}}]

    run(main())


def test_error_reply_raises_cdperror():
    async def main():
        sent = []
        cdp = C.CDP(sent.append)
        call = asyncio.ensure_future(cdp.send("Page.navigate", {"url": "nope"}))
        await settle()
        cdp.feed(frame({"id": sent_ids(sent)[0],
                        "error": {"code": -32000, "message": "Cannot navigate"}}))
        with pytest.raises(C.CDPError) as e:
            await call
        assert e.value.code == -32000
        assert e.value.message == "Cannot navigate"
        assert e.value.method == "Page.navigate"

    run(main())


def test_eof_fails_pending_sends_with_cdpclosed():
    async def main():
        cdp = C.CDP(lambda data: None)
        first = asyncio.ensure_future(cdp.send("Browser.getVersion"))
        second = asyncio.ensure_future(cdp.send("Target.getTargets"))
        await settle()
        cdp.eof()
        for call in (first, second):
            with pytest.raises(C.CDPClosed):
                await call
        # And nothing new can be started on a dead pipe.
        with pytest.raises(C.CDPClosed):
            await cdp.send("Browser.getVersion")

    run(main())


def test_detached_target_kills_the_session_and_its_pending_send():
    async def main():
        sent = []
        cdp = C.CDP(sent.append)
        attach = asyncio.ensure_future(cdp.attach("TARGET-1"))
        await settle()
        cdp.feed(frame({"id": sent_ids(sent)[0], "result": {"sessionId": "S1"}}))
        sess = await attach

        call = asyncio.ensure_future(sess.send("Page.captureScreenshot"))
        await settle()
        cdp.feed(frame({"method": "Target.detachedFromTarget",
                        "params": {"sessionId": "S1", "targetId": "TARGET-1"}}))
        with pytest.raises(C.CDPClosed):
            await call
        assert not sess.alive
        with pytest.raises(C.CDPClosed):
            await sess.send("Page.reload")

    run(main())


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def fake_chrome(path: Path, version: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "Google Chrome {version}"\n')
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(C, "_find_cache", None)
    return tmp_path


def test_find_chromium_order(home, monkeypatch, tmp_path):
    configured = fake_chrome(tmp_path / "opt" / "my-chrome", "140.0.1.2")
    on_path = fake_chrome(tmp_path / "usr" / "google-chrome", "130.0.2.3")
    downloaded = fake_chrome(
        home / ".pockettui" / "chromium" / "125.0.6422.60" / "chrome-linux64" / "chrome",
        "125.0.6422.60")
    monkeypatch.setattr(C.shutil, "which",
                        lambda name: on_path if name == "google-chrome" else None)

    got = C.find_chromium({"binary": configured}, force=True)
    assert (got.path, got.version, got.major, got.source) == \
        (configured, "140.0.1.2", 140, "config")

    got = C.find_chromium({}, force=True)
    assert (got.path, got.source) == (on_path, "path")

    monkeypatch.setattr(C.shutil, "which", lambda name: None)
    got = C.find_chromium({}, force=True)
    assert (got.path, got.version, got.source) == (downloaded, "125.0.6422.60",
                                                   "downloaded")
    assert C.find_chromium({"binary": "/nowhere/chrome"}, force=True).path == downloaded


def test_find_chromium_rejects_a_version_below_the_floor(home, monkeypatch, tmp_path):
    old = fake_chrome(tmp_path / "usr" / "chromium", "118.0.5993.88")
    monkeypatch.setattr(C.shutil, "which",
                        lambda name: old if name == "chromium" else None)
    assert C.find_chromium({}, force=True) is None

    new = fake_chrome(
        home / ".pockettui" / "chromium" / "121.0.0.1" / "chrome-linux64" / "chrome",
        "121.0.0.1")
    got = C.find_chromium({}, force=True)
    assert (got.path, got.source) == (new, "downloaded")


def test_find_chromium_prefers_the_current_pin(home, monkeypatch, tmp_path):
    root = home / ".pockettui" / "chromium"
    newest = fake_chrome(root / "131.0.0.1" / "chrome-linux64" / "chrome", "131.0.0.1")
    pinned = fake_chrome(root / "127.0.0.1" / "chrome-linux64" / "chrome", "127.0.0.1")
    monkeypatch.setattr(C.shutil, "which", lambda name: None)
    assert C.find_chromium({}, force=True).path == newest
    (root / "current").write_text(pinned)
    assert C.find_chromium({}, force=True).path == pinned


def test_find_chromium_caches_and_force_bypasses(home, monkeypatch, tmp_path):
    found = fake_chrome(tmp_path / "usr" / "google-chrome", "133.0.0.1")
    monkeypatch.setattr(C.shutil, "which",
                        lambda name: found if name == "google-chrome" else None)
    assert C.find_chromium({}, force=True).path == found

    monkeypatch.setattr(C.shutil, "which", lambda name: None)
    assert C.find_chromium({}).path == found          # still the cached answer
    assert C.find_chromium({}, force=True) is None    # force asks again


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_defaults_floor_and_env_override(home, monkeypatch):
    monkeypatch.delenv("POCKETTUI_BROWSER_MEM_MB", raising=False)
    (home / ".pockettui").mkdir(parents=True, exist_ok=True)
    (home / ".pockettui" / "browser.json").write_text("{ this is not json")

    monkeypatch.setattr(C, "_total_ram_mb", lambda: 1024)
    cfg = C.load_browser_config()
    assert cfg["memory_mb"] == 512          # a quarter of 1 GB is under the floor
    assert cfg["freeze_after_s"] == 300
    assert cfg["idle_exit_s"] == 600
    assert cfg["dpr_max"] == 2
    assert cfg["jpeg_quality"] == 60
    assert cfg["binary"] is None

    monkeypatch.setattr(C, "_total_ram_mb", lambda: 64 * 1024)
    assert C.load_browser_config()["memory_mb"] == 2048   # and never above the cap
    monkeypatch.setattr(C, "_total_ram_mb", lambda: 6 * 1024)
    assert C.load_browser_config()["memory_mb"] == 1536   # a quarter, in between
    monkeypatch.setattr(C, "_total_ram_mb", lambda: 0)
    assert C.load_browser_config()["memory_mb"] == 512    # RAM unreadable

    monkeypatch.setenv("POCKETTUI_BROWSER_MEM_MB", "1500")
    assert C.load_browser_config()["memory_mb"] == 1500
    monkeypatch.setenv("POCKETTUI_BROWSER_MEM_MB", "lots")
    assert C.load_browser_config()["memory_mb"] == 512


def test_config_falls_back_per_key(home, monkeypatch):
    monkeypatch.delenv("POCKETTUI_BROWSER_MEM_MB", raising=False)
    (home / ".pockettui").mkdir(parents=True, exist_ok=True)
    (home / ".pockettui" / "browser.json").write_text(json.dumps({
        "memory_mb": 900,
        "jpeg_quality": 999,        # out of range
        "idle_exit_s": "soon",      # not a number
        "dpr_max": 3,
        "binary": "/opt/chrome/chrome",
    }))
    cfg = C.load_browser_config()
    assert cfg["memory_mb"] == 900
    assert cfg["dpr_max"] == 3
    assert cfg["binary"] == "/opt/chrome/chrome"
    assert cfg["jpeg_quality"] == 60
    assert cfg["idle_exit_s"] == 600


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

def test_launch_flags_never_include_no_sandbox_and_strip_headless_ua():
    found = C.Found(path="/usr/bin/google-chrome", version="153.0.8010.52",
                    major=153, source="path")
    ua = C.user_agent_for(found.version)
    flags = C.launch_flags(found, C.BROWSER_CONFIG_DEFAULTS, "/tmp/profile",
                           1280, 800, ua)

    joined = " ".join(flags)
    assert "no-sandbox" not in joined
    assert "enable-automation" not in joined
    assert "--disable-blink-features=AutomationControlled" in flags
    assert "--remote-debugging-pipe" in flags
    assert "--headless=new" in flags
    assert "--user-data-dir=/tmp/profile" in flags
    # An unbranded build would otherwise run Chromium's field trial testing
    # config: experiments no shipped Chrome applies.
    assert "--disable-field-trial-config" in flags
    assert "--window-size=1280,800" in flags
    assert not any(f.startswith("http") or f == "about:blank" for f in flags)

    assert f"--user-agent={ua}" in flags
    assert "Headless" not in ua and "Chrome/153.0.8010.52" in ua
    # What the browser itself reports, cleaned up the same way.
    real = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "HeadlessChrome/153.0.0.0 Safari/537.36")
    assert "Headless" not in C.dehead(real)
    assert "Chrome/153.0.0.0" in C.dehead(real)


# ---------------------------------------------------------------------------
# The spawn's two pipes
# ---------------------------------------------------------------------------

# The echo runs in a fresh interpreter because the fd layout is the whole
# point: only a process that has just started has fds 3 upwards free to place
# the two pipes on the awkward numbers, and pytest's own are long since taken.
ECHO = """
import os, select, subprocess, sys
sys.path.insert(0, sys.argv[1])
import chromium as C

target = int(sys.argv[2])
r_in, w_in = os.pipe()
r_out, w_out = os.pipe()
if target:
    others = [r_in, w_in, r_out]
    for i, fd in enumerate(others):
        if fd == target:
            others[i] = os.dup(fd)
            os.close(fd)
    r_in, w_in, r_out = others
    if w_out != target:
        os.dup2(w_out, target)
        os.close(w_out)
        w_out = target

proc = subprocess.Popen(
    ["/bin/sh", "-c", "exec cat <&3 >&4"],
    preexec_fn=C._pipe_preexec(r_in, w_out), pass_fds=(3, 4),
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
os.close(r_in)
os.close(w_out)
os.write(w_in, b"ping\\n")
got = os.read(r_out, 64) if select.select([r_out], [], [], 10)[0] else b"<nothing>"
os.close(w_in)
os.close(r_out)
proc.wait(timeout=10)
sys.stdout.write(got.decode("utf-8", "replace").strip())
"""


def echo_through_child(write_end_on=0):
    """Run `cat <&3 >&4` the way `launch` runs Chrome, and echo one line.

    Chrome is not needed to test the part that broke: which fd the child finds
    each end of each pipe on.
    """
    out = subprocess.run(
        [sys.executable, "-c", ECHO, str(Path(__file__).resolve().parent.parent),
         str(write_end_on)],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_the_child_gets_the_read_end_on_fd_3_and_the_write_end_on_fd_4():
    assert echo_through_child() == "ping"


@pytest.mark.parametrize("awkward", [3, 4, 5, 10, 11])
def test_the_pipe_ends_survive_landing_on_an_awkward_fd(awkward):
    # The ends can be allocated anywhere, including on the fds the child needs
    # and on any number the move itself might use as scratch. Copying one end
    # over the other leaves the child echoing into a pipe it is also reading,
    # which is a browser that starts and then reports its connection gone.
    assert echo_through_child(write_end_on=awkward) == "ping"


def test_scope_prefix_caps_memory_and_names_the_unit():
    cmd, unit = C._scope_prefix(800)
    assert cmd[:4] == ["systemd-run", "--user", "--scope", "--quiet"]
    assert f"--unit={unit}" in cmd
    assert unit.startswith(f"pockettui-browser-{os.getpid()}-")
    assert "MemoryHigh=800M" in cmd and "MemoryMax=960M" in cmd
    assert cmd[-1] == "--"


def test_launch_env_hides_session_bus_and_flags_basic_password_store():
    # Chrome asks the session bus to move itself into a scope of its own a
    # second after it starts, which empties the one it was launched in; with no
    # bus address in its environment there is nobody to ask.
    env = C.launch_env({"PATH": "/bin", "XDG_RUNTIME_DIR": "/run/user/1000",
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"})
    assert "DBUS_SESSION_BUS_ADDRESS" not in env
    # systemd-run reaches the user manager through this instead, so the scope
    # is still made.
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert env["PATH"] == "/bin"
    assert "DBUS_SESSION_BUS_ADDRESS" not in C.launch_env()

    found = C.Found(path="/usr/bin/google-chrome", version="153.0.8010.52",
                    major=153, source="path")
    flags = C.launch_flags(found, C.BROWSER_CONFIG_DEFAULTS, "/tmp/profile",
                           1280, 800, C.user_agent_for(found.version))
    # No keyring on a headless box: without this the profile's cookies are
    # either unencryptable or lost between runs, and every login goes with them.
    assert "--password-store=basic" in flags


# ---------------------------------------------------------------------------
# Frames on the wire
# ---------------------------------------------------------------------------

def test_frame_header_pack_unpack():
    body = bytes(range(256)) * 4
    header = {"tab": "t1", "seq": 7, "kind": "cast", "w": 800, "h": 600,
              "cssW": 400, "cssH": 300, "zoom": 1.5, "fmt": "jpeg"}
    wire = C.pack_frame(header, body)

    # The client reads the header length first, so it has to be exactly where
    # it says it is: four little-endian bytes, then that many bytes of JSON.
    n = int.from_bytes(wire[:4], "little")
    assert json.loads(wire[4:4 + n].decode()) == header
    assert wire[4 + n:] == body

    got_header, got_body = C.unpack_frame(wire)
    assert got_header == header
    assert got_body == body

    for broken in (b"", b"\x01\x00\x00", wire[:len(wire) - 1 - len(body)]):
        with pytest.raises(ValueError):
            C.unpack_frame(broken)


def test_writer_drops_stale_cast_frames_but_never_stills():
    q = C.SendQueue()
    for seq in range(5):
        q.put_frame({"tab": "t1", "seq": seq, "kind": "cast"}, b"cast")
    q.put_frame({"tab": "t2", "seq": 9, "kind": "cast"}, b"other pane's tab")
    q.put_frame({"tab": "t1", "seq": 10, "kind": "still"}, b"settled")
    q.put_json({"type": "tab", "tab": "t1", "url": "https://example.net/"})
    q.put_frame({"tab": "t1", "seq": 11, "kind": "popup"}, b"menu")

    out = [item if isinstance(item, dict) else C.unpack_frame(item)[0]
           for item in q.drain()]
    kinds = [(o.get("kind") or o.get("type"), o.get("tab"), o.get("seq"))
             for o in out]
    # Only the last two of one tab's cast backlog survive, in order; the other
    # tab's is its own; and nothing that is not a cast frame is ever dropped.
    assert kinds == [("cast", "t1", 3), ("cast", "t1", 4), ("cast", "t2", 9),
                     ("still", "t1", 10), ("tab", "t1", None),
                     ("popup", "t1", 11)]
    assert q.drain() == []


def test_the_queue_hands_items_out_in_order_and_closes():
    async def main():
        q = C.SendQueue()
        q.put_json({"type": "ready"})
        q.put_frame({"tab": "t1", "seq": 1, "kind": "still"}, b"pixels")
        assert await q.get() == {"type": "ready"}
        assert C.unpack_frame(await q.get())[1] == b"pixels"
        pending = asyncio.ensure_future(q.get())
        await settle()
        assert not pending.done()
        q.close()
        assert await pending is None

    run(main())


def a_jpeg(w, h):
    return (b"\xff\xd8" + b"\xff\xe0\x00\x04ab" + b"\xff\xc0" +
            (11).to_bytes(2, "big") + b"\x08" + h.to_bytes(2, "big") +
            w.to_bytes(2, "big") + b"\x01\x11\x00" + b"\xff\xda\x00\x02")


def a_png(w, h):
    return (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" +
            w.to_bytes(4, "big") + h.to_bytes(4, "big"))


def a_webp(w, h, flavour):
    head = b"RIFF" + (0).to_bytes(4, "little") + b"WEBP"
    if flavour == "VP8X":
        return (head + b"VP8X" + (10).to_bytes(4, "little") + b"\x00\x00\x00\x00" +
                (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little"))
    if flavour == "VP8L":
        bits = (w - 1) | ((h - 1) << 14)
        return (head + b"VP8L" + (5).to_bytes(4, "little") + b"\x2f" +
                bits.to_bytes(4, "little"))
    return (head + b"VP8 " + (20).to_bytes(4, "little") + b"\x00\x00\x00" +
            b"\x9d\x01\x2a" + w.to_bytes(2, "little") + h.to_bytes(2, "little"))


def test_image_size_reads_the_bytes():
    # The frame header carries the true size of the image in it, because the
    # browser does not say: a screencast frame's metadata describes the page,
    # and Chrome may have scaled the frame down to the maximum it was given.
    assert C.image_size(a_jpeg(1280, 800)) == (1280, 800)
    assert C.image_size(a_png(640, 400)) == (640, 400)
    for flavour in ("VP8X", "VP8L", "VP8 "):
        assert C.image_size(a_webp(390, 844, flavour)) == (390, 844), flavour
    # Anything unreadable falls back to the size that was asked for.
    assert C.image_size(b"nothing like an image") is None
    assert C.image_size(b"") is None
    assert C.image_size(b"\xff\xd8\xff") is None



# ---------------------------------------------------------------------------
# One tab's stream
# ---------------------------------------------------------------------------

class FakeSession:
    """A Session's shape, recording every call and answering from a table.

    The point of testing the stream against this rather than a browser is that
    the order of the protocol calls *is* the behaviour: metrics before the
    screencast, a frozen tab woken before it is streamed again, and a
    screenshot that is thrown away when a newer frame overtook it.
    """

    def __init__(self, replies: "dict | None" = None) -> None:
        self.calls: list = []
        self.replies = replies or {}
        self.handlers: dict = {}
        self.alive = True
        self.target_id = "TARGET-1"
        self.cdp = None

    async def send(self, method, params=None, timeout=15):
        self.calls.append((method, params or {}))
        reply = self.replies.get(method)
        if callable(reply):
            return await reply(params or {})
        return reply if reply is not None else {}

    def on(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def off(self, event, callback):
        self.handlers.get(event, []).remove(callback)

    def fire(self, event, params):
        for cb in list(self.handlers.get(event, ())):
            cb(params)

    def methods(self):
        return [m for m, _ in self.calls]

    def params(self, method):
        return [p for m, p in self.calls if m == method]


class FakeSink:
    def __init__(self) -> None:
        self.frames: list = []
        self.msgs: list = []

    def put_json(self, msg):
        self.msgs.append(msg)

    def put_frame(self, header, body):
        self.frames.append((header, body))


def a_tab(session, sink, **kw):
    cfg = dict(C.BROWSER_CONFIG_DEFAULTS, memory_mb=512, freeze_after_s=0.01,
               jpeg_quality=55, dpr_max=2)
    cfg.update(kw.pop("config", {}))
    tab = C.PaneTab("p1", "t1", session, "TARGET-1", sink, cfg,
                    **{"css_w": 800, "css_h": 600, **kw})
    tab._wire()          # what start() does once the domains are enabled
    return tab


def cast_frame(payload=b"\x01\x02\x03", sid=1):
    return {"sessionId": sid, "data": base64.b64encode(payload).decode(),
            "metadata": {"deviceWidth": 800, "deviceHeight": 600}}


def test_stream_state_machine_show_hide_freeze():
    async def main():
        sess, sink = FakeSession(), FakeSink()
        tab = a_tab(sess, sink)

        await tab.show()
        seen = sess.methods()
        assert seen.index("Emulation.setDeviceMetricsOverride") < \
            seen.index("Page.startScreencast")
        cast = sess.params("Page.startScreencast")[0]
        assert cast["format"] == "jpeg" and cast["quality"] == 55
        assert (cast["maxWidth"], cast["maxHeight"]) == (800, 600)
        assert cast["everyNthFrame"] == 1
        assert tab.live and not tab.frozen

        sess.calls.clear()
        await tab.hide()
        assert "Page.stopScreencast" in sess.methods()
        assert not tab.live and not tab.frozen
        # The freeze is on a timer, so a tab the user flicks away from and back
        # to is never frozen at all.
        await asyncio.sleep(0.1)
        assert ("Page.setWebLifecycleState", {"state": "frozen"}) in sess.calls
        assert tab.frozen

        sess.calls.clear()
        await tab.show()
        seen = sess.methods()
        assert ("Page.setWebLifecycleState", {"state": "active"}) in sess.calls
        assert seen.index("Page.setWebLifecycleState") < \
            seen.index("Page.startScreencast")
        assert tab.live and not tab.frozen

        # And a screencast frame goes out as a `cast` frame, acknowledged back
        # to the browser so the next one is produced.
        sess.fire("Page.screencastFrame", cast_frame(b"\xff\xd8jpeg"))
        await settle()
        header, body = sink.frames[-1]
        assert header["kind"] == "cast" and header["fmt"] == "jpeg"
        assert (header["w"], header["h"]) == (800, 600)
        assert header["seq"] == 1 and body == b"\xff\xd8jpeg"
        tab.ack(header["seq"])
        await settle()
        assert ("Page.screencastFrameAck", {"sessionId": 1}) in sess.calls

        tab._teardown()

    run(main())


def test_settle_frame_dropped_when_newer_cast_frame_arrived():
    async def main():
        gate = asyncio.Event()
        taken = []

        async def screenshot(params):
            taken.append(params)
            await gate.wait()
            return {"data": base64.b64encode(b"RIFFwebp").decode()}

        sess = FakeSession({"Page.captureScreenshot": screenshot})
        sink = FakeSink()
        tab = a_tab(sess, sink)
        await tab.show()

        # Two screencast frames while the picture is being taken: the page is
        # painting on its own, and its frames are newer than the picture. It
        # is dropped and they go instead.
        tab.note_input("mouseMoved")
        await asyncio.sleep(C.SETTLE_DELAY_S + 0.05)
        assert taken and taken[0]["fromSurface"] is True
        assert taken[0]["format"] == "webp"
        sess.fire("Page.screencastFrame", cast_frame(b"newer"))
        sess.fire("Page.screencastFrame", cast_frame(b"newer still"))
        gate.set()
        await asyncio.sleep(0.05)
        assert [h["kind"] for h, _ in sink.frames] == ["cast", "cast"]
        assert [b for _h, b in sink.frames] == [b"newer", b"newer still"]

        # One frame is the capture's own echo — a screenshot forces a surface
        # commit and the screencast answers with a frame of the same picture —
        # so the settled frame supersedes it and goes out alone. Without it
        # the last state of a drag would never arrive at all: the screencast
        # skips frames it made while the previous one was unacknowledged and
        # never goes back for them.
        sink.frames.clear()
        gate.clear()
        tab.note_input("mouseReleased")
        await asyncio.sleep(C.SETTLE_DELAY_S + 0.05)
        sess.fire("Page.screencastFrame", cast_frame(b"echo of the capture"))
        gate.set()
        await asyncio.sleep(0.05)
        assert [h["kind"] for h, _ in sink.frames] == ["still"]
        assert sink.frames[-1][0]["fmt"] == "webp"

        tab._teardown()

    run(main())


def test_zoom_metrics_override_math():
    sess, sink = FakeSession(), FakeSink()
    tab = a_tab(sess, sink, css_w=1000, css_h=700, dpr=2, zoom=1.5)

    # The viewport shrinks by the zoom and the scale grows by it, so the same
    # number of device pixels comes back with everything on it half again as
    # big — and the page never learns it is zoomed.
    assert tab._metrics() == {"width": 667, "height": 467,
                              "deviceScaleFactor": 3, "mobile": False}
    assert tab._surface() == (2001, 1401)

    # dpr_max is a ceiling on the phone's own ratio, not on the zoom.
    tab.dpr = 3
    assert tab._metrics()["deviceScaleFactor"] == 3

    tab.zoom = 1.0
    assert tab._metrics() == {"width": 1000, "height": 700,
                              "deviceScaleFactor": 2, "mobile": False}
    assert tab._surface() == (2000, 1400)

    # A client that cannot keep up is streamed at 1x whatever it asked for.
    tab._dpr_forced = True
    assert tab._metrics()["deviceScaleFactor"] == 1


def test_resize_and_zoom_restart_a_live_stream():
    async def main():
        sess, sink = FakeSession(), FakeSink()
        tab = a_tab(sess, sink)
        await tab.resize(400, 300, 1)
        # Hidden: the override is re-sent and nothing is streamed.
        assert "Page.startScreencast" not in sess.methods()
        assert sess.params("Emulation.setDeviceMetricsOverride")[-1]["width"] == 400

        await tab.show()
        sess.calls.clear()
        await tab.set_zoom(2.0)
        assert sess.methods().count("Page.startScreencast") == 1
        assert "Page.stopScreencast" in sess.methods()
        assert sess.params("Page.startScreencast")[-1]["maxWidth"] == 400
        assert sess.params("Emulation.setDeviceMetricsOverride")[-1] == {
            "width": 200, "height": 150, "deviceScaleFactor": 2.0, "mobile": False}
        tab._teardown()

    run(main())


def test_slow_acks_drop_to_one_pixel_per_pixel():
    async def main():
        sess, sink = FakeSession(), FakeSink()
        tab = a_tab(sess, sink, dpr=2)
        await tab.show()
        assert sess.params("Page.startScreencast")[0]["maxWidth"] == 1600

        for _ in range(C.SLOW_ACK_STREAK):
            sess.fire("Page.screencastFrame", cast_frame())
            await settle()
            seq = sink.frames[-1][0]["seq"]
            # Pretend the frame left a second ago: the link is the limit here,
            # not the browser.
            sid, _sent, timer = tab._unacked[seq]
            tab._unacked[seq] = (sid, time.monotonic() - 1.0, timer)
            tab.ack(seq)
        await asyncio.sleep(0.05)
        assert tab._dpr_forced
        assert sess.params("Page.startScreencast")[-1]["maxWidth"] == 800
        tab._teardown()

    run(main())


def test_a_popup_is_painted_from_screenshots_until_the_next_click():
    async def main():
        shots = []

        async def screenshot(params):
            shots.append(params)
            return {"data": base64.b64encode(b"menu").decode()}

        sess = FakeSession({"Page.captureScreenshot": screenshot})
        sink = FakeSink()
        tab = a_tab(sess, sink)
        await tab.show()

        # The page says a <select> is open. Screencast frames do not contain a
        # native popup at all, so the tab is painted from screenshots instead.
        sess.fire("Runtime.bindingCalled",
                  {"name": "ptuiPopup", "payload": json.dumps({"open": True})})
        await asyncio.sleep(1.0 / C.POPUP_FPS + 0.05)
        assert [h["kind"] for h, _ in sink.frames].count("popup") >= 1
        assert shots[0]["optimizeForSpeed"] is True

        # The click that picks an option ends it.
        tab.note_input("mousePressed")
        await asyncio.sleep(1.0 / C.POPUP_FPS + 0.05)
        seen = len(sink.frames)
        await asyncio.sleep(1.0 / C.POPUP_FPS + 0.05)
        assert not tab._popup_open
        assert "popup" not in [h["kind"] for h, _ in sink.frames[seen:]]

        # And the page's selection arrives on the other binding.
        sess.fire("Runtime.bindingCalled", {"name": "ptuiSel", "payload": "picked"})
        assert tab.selection == "picked"
        tab._teardown()

    run(main())


def test_the_page_helper_is_one_iife_with_only_its_three_bindings():
    src = C.FULL_PAGE_HELPER
    assert src.strip().startswith("(()") and src.strip().endswith(")();")
    assert src.count("ptuiPopup(") == 1 and src.count("ptuiSel(") == 1
    assert src.count("ptuiTitle(") == 1
    assert "\nvar " not in src and "\nwindow." not in src
    assert len(src.splitlines()) < 80


def test_tab_state_pushes_only_for_the_main_frame():
    async def main():
        sess = FakeSession({"Page.getNavigationHistory": {
            "currentIndex": 1,
            "entries": [{"id": 1, "url": "https://example.net/one", "title": "one"},
                        {"id": 2, "url": "https://example.net/two", "title": "two"}]}})
        sink = FakeSink()
        tab = a_tab(sess, sink)
        tab.frame_id = "MAIN"

        sess.fire("Page.frameNavigated",
                  {"frame": {"id": "SUB", "parentId": "MAIN", "url": "https://ads"}})
        sess.fire("Page.frameStartedLoading", {"frameId": "SUB"})
        await asyncio.sleep(0.1)
        assert sink.msgs == []

        sess.fire("Page.frameNavigated",
                  {"frame": {"id": "MAIN", "url": "https://example.net/two"}})
        await asyncio.sleep(0.1)
        assert sink.msgs[-1] == {"type": "tab", "tab": "t1",
                                "url": "https://example.net/two", "title": "two",
                                "loading": False, "canBack": True, "canFwd": False,
                                "targetId": "TARGET-1"}
        tab._teardown()

    run(main())


def test_a_fresh_input_under_the_capture_does_not_strand_held_frames():
    async def main():
        gate = asyncio.Event()

        async def screenshot(params):
            await gate.wait()
            return {"data": base64.b64encode(b"RIFFwebp").decode()}

        sess = FakeSession({"Page.captureScreenshot": screenshot})
        sink = FakeSink()
        tab = a_tab(sess, sink)
        await tab.show()

        tab.note_input("mouseMoved")
        await asyncio.sleep(C.SETTLE_DELAY_S + 0.05)
        sess.fire("Page.screencastFrame", cast_frame(b"held"))
        assert tab._capturing
        # A second input re-arms the timer, which cancels the capture in flight.
        # The frame it was holding has to go out and the flag has to come down,
        # or every frame after this one is held and none of them is ever sent.
        tab.note_input("mouseMoved")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not tab._capturing
        assert [(h["kind"], b) for h, b in sink.frames] == [("cast", b"held")]

        gate.set()
        await asyncio.sleep(C.SETTLE_DELAY_S + 0.1)
        assert [h["kind"] for h, _ in sink.frames] == ["cast", "still"]
        tab._teardown()

    run(main())


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
# The mapping rather than the dispatch: what a client message becomes is a fact
# about the protocol, and the two things that are easy to get wrong in it —
# whether a key carries text and what a Mac's Command key means on a Linux box —
# are worth pinning without a browser or a socket anywhere near them.

def test_key_mapping_text_vs_raw_and_meta_remap():
    # A printable character arrives with text on it, and that is what makes it
    # a keyDown: the page types the letter.
    down = C.key_event_params({"kind": "down", "key": "a", "code": "KeyA",
                               "keyCode": 65, "text": "a"}, "linux")
    assert down["type"] == "keyDown"
    assert down["text"] == "a" and down["unmodifiedText"] == "a"
    assert down["windowsVirtualKeyCode"] == 65
    assert down["nativeVirtualKeyCode"] == 65

    # A chord carries none, and a keyDown with no text would type nothing while
    # hiding the key from the page's own handlers. rawKeyDown is the one that
    # reaches them.
    chord = C.key_event_params({"kind": "down", "key": "c", "code": "KeyC",
                                "keyCode": 67, "text": "", "mods": 2}, "linux")
    assert chord["type"] == "rawKeyDown" and "text" not in chord
    assert chord["modifiers"] == C.MOD_CTRL

    assert C.key_event_params({"kind": "up", "key": "a", "text": "a"},
                              "linux")["type"] == "keyUp"

    # The keys a client may report a keyCode of 0 for, and which Chrome's own
    # editing commands are driven off.
    want = {"ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39,
            "Enter": 13, "Tab": 9, "Backspace": 8, "Delete": 46, "Escape": 27,
            "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34}
    for key, code in want.items():
        got = C.key_event_params({"kind": "down", "key": key, "keyCode": 0},
                                 "linux")
        assert got["windowsVirtualKeyCode"] == code, key
        assert got["nativeVirtualKeyCode"] == code, key
    # Enter's character is a carriage return, which is what a real browser puts
    # in the event — a textarea fed "\n" gets a break with no key behind it.
    enter = C.key_event_params({"kind": "down", "key": "Enter"}, "linux")
    assert enter["type"] == "keyDown" and enter["text"] == "\r"

    # Cmd from a Mac client is Ctrl on a Linux host: the page runs there, and
    # its chords are that browser's. Left as Meta it would arrive as Super,
    # which no page binds anything to.
    mac_cmd = {"kind": "down", "key": "c", "code": "KeyC", "mods": C.MOD_META}
    assert C.key_event_params(mac_cmd, "linux")["modifiers"] == C.MOD_CTRL
    assert C.key_event_params(mac_cmd, "darwin")["modifiers"] == C.MOD_META
    # Shift and Alt ride along untouched either way.
    both = {"kind": "down", "key": "C", "mods": C.MOD_META | C.MOD_SHIFT}
    assert C.key_event_params(both, "linux")["modifiers"] == \
        C.MOD_CTRL | C.MOD_SHIFT
    assert C.key_event_params(both, "darwin")["modifiers"] == \
        C.MOD_META | C.MOD_SHIFT

    # The numeric keypad, which a page tells apart by location alone.
    pad = C.key_event_params({"kind": "down", "key": "1", "text": "1",
                              "location": 3}, "linux")
    assert pad["isKeypad"] is True and pad["location"] == 3
    assert C.key_event_params({"kind": "down", "key": "1", "text": "1"},
                              "linux")["isKeypad"] is False
    assert C.key_event_params({"kind": "down", "key": "a", "text": "a",
                              "repeat": True}, "linux")["autoRepeat"] is True


def test_wheel_and_mouse_coordinates_divide_by_zoom():
    # The canvas is the page's surface at the pane's size; the viewport behind
    # it was made `zoom` times smaller, so the page's own coordinate for a point
    # on the canvas is that point divided by the zoom. A click that skipped the
    # division lands somewhere else on a zoomed page.
    at = C.mouse_event_params({"kind": "down", "x": 200, "y": 100,
                               "button": "left", "buttons": 1, "clicks": 2},
                              2.0, "linux")
    assert at["type"] == "mousePressed"
    assert (at["x"], at["y"]) == (100.0, 50.0)
    assert at["button"] == "left" and at["buttons"] == 1
    assert at["clickCount"] == 2

    one = C.mouse_event_params({"kind": "move", "x": 200, "y": 100}, 1.0, "linux")
    assert (one["x"], one["y"]) == (200.0, 100.0)
    assert one["type"] == "mouseMoved" and one["button"] == "none"

    # And so does a wheel notch: a delta that was not divided would scroll a
    # zoomed page further the more it was zoomed in.
    wheel = C.mouse_event_params({"kind": "wheel", "x": 40, "y": 60,
                                 "dx": 0, "dy": 240}, 2.0, "linux")
    assert wheel["type"] == "mouseWheel"
    assert (wheel["x"], wheel["y"]) == (20.0, 30.0)
    assert (wheel["deltaX"], wheel["deltaY"]) == (0.0, 120.0)
    # Nothing but a wheel carries a delta at all.
    assert "deltaY" not in one

    # Cmd+click is Ctrl+click on a Linux host, or a Mac user's "open in a new
    # tab" would open in the tab they were reading.
    cmd = {"kind": "down", "x": 0, "y": 0, "button": "left",
           "mods": C.MOD_META}
    assert C.mouse_event_params(cmd, 1.0, "linux")["modifiers"] == C.MOD_CTRL
    assert C.mouse_event_params(cmd, 1.0, "darwin")["modifiers"] == C.MOD_META

    # A button this end does not know is no button, and a kind it does not know
    # is a refusal rather than a guess.
    assert C.mouse_event_params({"kind": "move", "button": "thumb"},
                                1.0, "linux")["button"] == "none"
    with pytest.raises(ValueError):
        C.mouse_event_params({"kind": "fling"}, 1.0, "linux")

    # A zoom of zero (a client mid-gesture) is a division that must not happen.
    assert C.mouse_event_params({"kind": "move", "x": 8, "y": 8}, 0,
                                "linux")["x"] == 8.0


def test_cursor_probe_js_has_no_percent():
    # FULL_PAGE_HELPER is %-formatted, and a probe that grew a percent sign
    # would either break that formatting or be quietly rewritten by it. They
    # are separate strings today; this is what keeps them that way.
    assert "%" not in C.FAVICON_JS          # already formatted, see the source
    probes = {"LINK_PROBE_JS": C.LINK_PROBE_JS, "HIT_PROBE_JS": C.HIT_PROBE_JS,
              "HIT_CURSOR_JS": C.HIT_CURSOR_JS}
    for name, src in probes.items():
        assert "%" not in src, name
        assert src.startswith("(function (x, y) {"), name
        assert src.rstrip().endswith("})"), name

    # Each one has to be a single expression, because that is how it is called:
    # probe_call wraps it in parentheses and applies it to the point.
    call = C.probe_call(C.HIT_CURSOR_JS, 12.5, 7)
    assert call.endswith("(12.5,7)")
    if shutil.which("node") is None:
        pytest.skip("no node to parse the probes with")
    for name, src in probes.items():
        wrapper = f"var v = {C.probe_call(src, 1, 2)};\n"
        proc = subprocess.run(["node", "--check", "-"], input=wrapper,
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{name}: {proc.stderr}"

    # The two strings that are injected whole rather than called on a point.
    for name, src in (("FAVICON_JS", f"var v = {C.FAVICON_JS};\n"),
                      ("FULL_PAGE_HELPER", C.FULL_PAGE_HELPER)):
        proc = subprocess.run(["node", "--check", "-"], input=src,
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{name}: {proc.stderr}"


# ---------------------------------------------------------------------------
# Memory: what the browser costs, and what the watchdog does about it
# ---------------------------------------------------------------------------

class FakeProc:
    """A /proc of one's own: pid -> {ppid, pss, rss, cgroup}.

    Every number ProcessTree reads comes through a reader, which is what makes
    a process tree that never existed testable — including the cases the real
    machine will not reproduce on demand: a renderer re-parented out of the
    tree, a pid that exits between being listed and being read.
    """

    def __init__(self, table, ps="", platform="linux") -> None:
        self.table = table
        self.ps = ps
        self.platform = platform

    def pids(self):
        return list(self.table)

    def _get(self, pid, key, default=None):
        return (self.table.get(pid) or {}).get(key, default)

    def ppid(self, pid):
        return self._get(pid, "ppid")

    def pss_kb(self, pid):
        return self._get(pid, "pss")

    def rss_kb(self, pid):
        return self._get(pid, "rss")

    def cgroup(self, pid):
        return self._get(pid, "cgroup", "")

    def ps_text(self):
        return self.ps


SCOPE = "pockettui-browser-4242-1"
IN_SCOPE = ("0::/user.slice/user-1000.slice/user@1000.service/"
            f"app.slice/{SCOPE}.scope\n")
ELSEWHERE = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/other.scope\n"


def test_pss_sum_from_fake_proc_tree():
    # systemd-run at the root, Chrome under it, two renderers under that, and
    # one renderer whose parent went away: it hangs off init, out of the tree
    # altogether, and is only found because its cgroup is our scope.
    reader = FakeProc({
        100: {"ppid": 1, "pss": 2 * 1024, "rss": 9 * 1024, "cgroup": IN_SCOPE},
        101: {"ppid": 100, "pss": 120 * 1024, "rss": 400 * 1024, "cgroup": IN_SCOPE},
        102: {"ppid": 101, "pss": 60 * 1024, "rss": 300 * 1024, "cgroup": IN_SCOPE},
        103: {"ppid": 101, "pss": 40 * 1024, "rss": 280 * 1024, "cgroup": IN_SCOPE},
        104: {"ppid": 1, "pss": 30 * 1024, "rss": 250 * 1024, "cgroup": IN_SCOPE},
        900: {"ppid": 1, "pss": 700 * 1024, "rss": 900 * 1024, "cgroup": ELSEWHERE},
    }, ps="")

    m = C.ProcessTree.measure(100, SCOPE, reader)
    assert m.procs == 5                       # the tree, plus the stray
    assert m.mb == pytest.approx(252.0)       # 2 + 120 + 60 + 40 + 30
    assert m.approx is False

    # Without the unit there is nothing to find the re-parented renderer by,
    # and the unrelated process is never counted either way.
    plain = C.ProcessTree.measure(100, None, reader)
    assert plain.procs == 4 and plain.mb == pytest.approx(222.0)
    assert 900 not in C.ProcessTree.pids(100, SCOPE, reader)


def test_rss_baseline_subtraction_macos():
    # `ps -axo pid,ppid,rss` as macOS prints it, header and all. Every process
    # maps the same framework, so the smallest RSS in the tree is taken as that
    # shared baseline and counted once rather than n times.
    reader = FakeProc({}, platform="darwin", ps=(
        "  PID  PPID    RSS\n"
        "    1     0  12000\n"
        "  100     1 300000\n"
        "  101   100 250000\n"
        "  102   100 200000\n"
        "  900     1 800000\n"))
    m = C.ProcessTree.measure(100, None, reader)
    assert m.procs == 3
    assert m.approx is True
    # 750000 - 2 * 200000 = 350000 KB
    assert m.mb == pytest.approx(350000 / 1024)

    assert C.ProcessTree.measure(4242, None, reader) == \
        C.Measurement(mb=0.0, approx=True, procs=0)


def test_measure_survives_vanished_pid():
    # 102 has no smaps_rollup but still has a status file: RSS, marked approx.
    # 103 has neither — it exited while this pass was reading /proc — and is
    # skipped rather than counted as zero or raised over.
    reader = FakeProc({
        100: {"ppid": 1, "pss": 10 * 1024, "rss": 20 * 1024},
        101: {"ppid": 100, "pss": 50 * 1024, "rss": 90 * 1024},
        102: {"ppid": 100, "pss": None, "rss": 70 * 1024},
        103: {"ppid": 100, "pss": None, "rss": None},
    })
    m = C.ProcessTree.measure(100, None, reader)
    assert m.procs == 3
    assert m.mb == pytest.approx(130.0)
    assert m.approx is True

    # And the real reader, on a pid this machine does not have: no exception,
    # and nothing measured.
    real = C.ProcessTree.measure(0x7FFFFFFF)
    assert real.procs == 0 and real.mb == 0.0


class FakeCDP:
    """The browser connection, recording calls and minting target ids."""

    def __init__(self) -> None:
        self.calls: list = []
        self.sessions: list = []
        self._n = 0

    async def send(self, method, params=None, timeout=15):
        self.calls.append((method, params or {}))
        if method == "Target.createTarget":
            self._n += 1
            return {"targetId": f"TARGET-{self._n}"}
        return {}

    async def attach(self, target_id, timeout=15):
        sess = FakeSession()
        sess.target_id = target_id
        sess.cdp = self
        self.sessions.append(sess)
        return sess

    def on(self, event, callback, session=None):
        pass

    def off(self, event, callback, session=None):
        pass

    def methods(self):
        return [m for m, _ in self.calls]

    def params(self, method):
        return [p for m, p in self.calls if m == method]


class FakeBrowser:
    def __init__(self, pid=4242, unit=SCOPE) -> None:
        self.cdp = FakeCDP()
        self.pid = pid
        self.unit = unit
        self.user_agent = "Mozilla/5.0 Chrome/140.0.0.0"
        self.config: dict = {}
        self.closed = asyncio.get_running_loop().create_future()
        self.kills = 0

    async def close(self):
        if not self.closed.done():
            self.closed.set_result(0)

    def _signal_group(self, sig):
        self.kills += 1


class FakeTree:
    """A measurement on demand, in place of this machine's /proc."""

    def __init__(self, mb=100.0) -> None:
        self.mb = mb
        self.calls: list = []

    def measure(self, root_pid, unit=None, reader=None):
        self.calls.append((root_pid, unit))
        return C.Measurement(mb=self.mb, approx=False, procs=9)


def a_browser(monkeypatch, **cfg):
    """A FullBrowser whose Chromium is two fakes, and the browsers it made.

    Every test that uses it takes the `home` fixture: a launch creates the
    downloads directory and sweeps the staged uploads, and neither belongs in
    the home directory of whoever is running the suite.
    """
    found = C.Found(path="/fake/chrome", version="140.0.0.1", major=140,
                    source="path")
    monkeypatch.setattr(C, "find_chromium",
                        lambda config=None, force=False: found)
    made: list = []

    async def launch(f, config=None, width=C.DEFAULT_WIDTH, height=C.DEFAULT_HEIGHT):
        browser = FakeBrowser()
        made.append(browser)
        return browser

    monkeypatch.setattr(C, "launch", launch)
    config = dict(C.BROWSER_CONFIG_DEFAULTS, memory_mb=1000, freeze_after_s=60,
                  idle_exit_s=60, jpeg_quality=55)
    config.update(cfg)
    fb = C.FullBrowser(config)
    return fb, made


async def three_tabs(fb):
    """t1, t2, t3 of one pane, shown in that order: t3 live, t1 hidden longest."""
    sink = FakeSink()
    fb.attach_pane("p1", sink)
    tabs = []
    for name in ("t1", "t2", "t3"):
        tab = await fb.open("p1", name, f"https://example.net/{name}")
        await tab.show()
        await asyncio.sleep(0.01)   # so the shown-at order is not a tie
        tabs.append(tab)
    return sink, tabs


def test_soft_cap_discards_oldest_hidden_first_never_live(home):
    async def main():
        with pytest.MonkeyPatch.context() as mp:
            fb, made = a_browser(mp)
            sink, (one, two, three) = await three_tabs(fb)
            assert three.live and not one.live and not two.live
            cdp = made[0].cdp

            fb.watchdog.tree = FakeTree(900.0)      # soft is 800 of 1000
            cdp.calls.clear()
            sink.msgs.clear()
            await fb.watchdog.tick()

            # The oldest hidden tab, not the live one and not the newer hidden
            # one, and its target is really given back to the browser.
            assert ("Target.closeTarget", {"targetId": one.target_id}) in cdp.calls
            assert one.discarded and not two.discarded and not three.discarded
            assert three.live
            assert {"type": "tab", "tab": "t1", "discarded": True,
                    "url": one.url, "title": one.title} in sink.msgs
            assert fb.snapshot()["discards"] == 1
            assert fb.snapshot()["memMb"] == 900
            assert fb.snapshot()["softMb"] == 800 and fb.snapshot()["procs"] == 9
            assert fb.tab("p1", "t1") is one        # still the pane's tab

            # One tab per tick, oldest first, and never the one on screen: the
            # third tick has nothing left it is allowed to take.
            cdp.calls.clear()
            await fb.watchdog.tick()
            assert two.discarded and three.live
            cdp.calls.clear()
            await fb.watchdog.tick()
            assert "Target.closeTarget" not in cdp.methods()
            assert three.live and not three.discarded
            assert fb.snapshot()["discards"] == 2

            # The browser's own news about the targets that were closed, which
            # arrives after they are already records: a discarded tab has no
            # target, so neither event finds it and neither takes the record
            # away or tells the pane its tab is gone.
            sink.msgs.clear()
            fb._on_target_destroyed({"targetId": one.target_id})
            fb._on_target_crashed({"targetId": two.target_id})
            assert fb.tab("p1", "t1") is one and fb.tab("p1", "t2") is two
            assert sink.msgs == []

            await fb.shutdown()

    run(main())


def test_hard_cap_relaunches_and_reopens_records(home):
    async def main():
        with pytest.MonkeyPatch.context() as mp:
            fb, made = a_browser(mp)
            left = FakeSink()
            right = FakeSink()
            fb.attach_pane("p1", left)
            fb.attach_pane("p2", right)
            one = await fb.open("p1", "t1", "https://example.net/one")
            two = await fb.open("p1", "t2", "https://example.net/two")
            far = await fb.open("p2", "t1", "https://example.net/far")
            await one.show()
            await far.show()
            first_targets = (one.target_id, far.target_id)

            fb.watchdog.tree = FakeTree(1400.0)     # the cap is 1000
            left.msgs.clear()
            right.msgs.clear()
            await fb.watchdog.tick()                # one strike, and a discard
            assert fb.restarts == 0 and two.discarded
            await fb.watchdog.tick()                # two in a row: restart

            assert fb.restarts == 1
            assert len(made) == 2 and made[0].closed.done()
            assert fb.browser is made[1]

            # Each pane hears once, and the tab it was showing is already back
            # on a new target of the new browser.
            for sink in (left, right):
                restarted = [m for m in sink.msgs if m.get("code") == "restarted"]
                assert len(restarted) == 1
                assert restarted[0] == {"type": "error", "tab": None,
                                        "code": "restarted",
                                        "message": "Browser restarted: memory cap 1000 MB"}
            assert one.live and far.live
            assert not one.discarded and not far.discarded
            assert (one.target_id, far.target_id) != first_targets
            assert one.session.cdp is made[1].cdp

            # The tab nobody was looking at is a record: still the pane's tab,
            # still with its URL, and the pane was told it is discarded.
            assert fb.tab("p1", "t2") is two and two.discarded
            assert two.url == "https://example.net/two"
            assert {"type": "tab", "tab": "t2", "discarded": True,
                    "url": two.url, "title": two.title} in left.msgs
            assert fb.snapshot()["restarts"] == 1

            await fb.shutdown()

    run(main())


def test_show_of_discarded_tab_recreates_target(home):
    async def main():
        with pytest.MonkeyPatch.context() as mp:
            fb, made = a_browser(mp)
            sink = FakeSink()
            fb.attach_pane("p1", sink)
            one = await fb.open("p1", "t1", "https://example.net/one")
            await one.show()
            two = await fb.open("p1", "t2", "https://example.net/two")
            await two.show()                        # t1 is hidden now
            one.url = "https://example.net/one#where-i-was"
            was = one.target_id

            fb.watchdog.tree = FakeTree(900.0)
            await fb.watchdog.tick()
            assert one.discarded
            sink.msgs.clear()
            cdp = made[0].cdp
            cdp.calls.clear()

            await one.show()
            assert not one.discarded and one.live
            assert one.target_id != was
            assert ("Target.createTarget",
                    {"url": "about:blank", "newWindow": True}) in cdp.calls
            # The page it was on, loaded again, and a plain `tab` message: the
            # pane learns it has a live tab back the same way it always does.
            assert ("Page.navigate",
                    {"url": "https://example.net/one#where-i-was"}) in one.session.calls
            await asyncio.sleep(0.1)
            states = [m for m in sink.msgs
                      if m.get("type") == "tab" and m.get("tab") == "t1"]
            assert states and all("discarded" not in m for m in states)
            assert states[-1]["targetId"] == one.target_id
            assert two.live is False

            await fb.shutdown()

    run(main())


def test_idle_exit_after_last_tab(home):
    async def main():
        with pytest.MonkeyPatch.context() as mp:
            fb, made = a_browser(mp, idle_exit_s=0.05)
            sink = FakeSink()
            fb.attach_pane("p1", sink)
            tab = await fb.open("p1", "t1", "https://example.net/one")
            await tab.show()
            assert fb.browser is made[0]

            await fb.close_tab("p1", "t1")
            # The pane is still attached, and that is not a reason to keep a
            # browser: a pane with no full tab in it needs none.
            await asyncio.sleep(0.2)
            assert fb.browser is None
            assert made[0].closed.done()
            assert fb.watchdog._task is None
            assert fb.snapshot()["running"] is False
            assert fb.snapshot()["memMb"] is None

            # And the next open starts one again.
            again = await fb.open("p1", "t2", "https://example.net/two")
            assert fb.browser is made[1] and not again.dead
            await fb.shutdown()

    run(main())


def test_watchdog_ticks_on_its_own_and_stops_with_the_browser(home):
    async def main():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(C, "WATCHDOG_TICK_S", 0.02)
            fb, made = a_browser(mp)
            sink = FakeSink()
            fb.attach_pane("p1", sink)
            tab = await fb.open("p1", "t1", "https://example.net/one")
            await tab.show()
            tree = FakeTree(100.0)
            fb.watchdog.tree = tree
            await asyncio.sleep(0.1)
            assert tree.calls and tree.calls[0] == (made[0].pid, SCOPE)
            assert fb.snapshot()["memMb"] == 100

            await fb.shutdown()
            ticks = len(tree.calls)
            # Nothing outlives the browser: uvicorn's shutdown waits on tasks.
            assert fb.watchdog._task is None
            await asyncio.sleep(0.1)
            assert len(tree.calls) == ticks
            assert not [t for t in asyncio.all_tasks()
                        if t is not asyncio.current_task() and not t.done()]

    run(main())


# ---------------------------------------------------------------------------
# What the page asks for: popups, dialogs, credentials, files, downloads
# ---------------------------------------------------------------------------
# All of it without a browser, because each one is a rule about *which* call
# goes out and when — interception that is only enabled after a 401, a dialog
# that is dismissed rather than left holding the page, a download renamed off
# the guid Chrome filed it under.


def test_popup_target_becomes_pane_tab_and_over_cap_navigates_opener(home):
    async def main():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(C, "MAX_TARGETS", 2)
            fb, made = a_browser(mp)
            sink = FakeSink()
            fb.attach_pane("p1", sink)
            opener = await fb.open("p1", "t1", "https://example.net/one")
            await opener.show()
            sink.msgs.clear()

            # A target with one of ours named as its opener is a window.open or
            # a target=_blank: a tab in any browser, and a window nobody can
            # see in this one unless the pane is told.
            fb._on_target_created({"targetInfo": {
                "targetId": "POP-1", "type": "page", "openerId": opener.target_id,
                "url": "https://example.net/popup"}})
            await asyncio.sleep(0.15)

            told = [m for m in sink.msgs if m["type"] == "newtab"]
            assert told == [{"type": "newtab", "tab": "p1",
                             "url": "https://example.net/popup",
                             "opener": "t1", "targetId": "POP-1"}]
            popup = fb.tab("p1", "p1")
            assert popup is not None and popup.target_id == "POP-1"
            # Configured like any other tab, and never navigated: the target is
            # already going where the page sent it.
            assert "Page.enable" in popup.session.methods()
            assert "Page.navigate" not in popup.session.methods()
            assert [p["name"] for p in popup.session.params("Runtime.addBinding")] == \
                ["ptuiPopup", "ptuiSel", "ptuiTitle"]
            assert not popup.live       # the pane decides what to show

            # A target of ours with no opener is not a popup, and neither is a
            # popup of a target we do not own.
            before = dict(fb._tabs)
            fb._on_target_created({"targetInfo": {"targetId": "OTHER",
                                                  "type": "page", "url": "x"}})
            fb._on_target_created({"targetInfo": {"targetId": "OTHER2",
                                                  "type": "page",
                                                  "openerId": "NOT-OURS"}})
            await asyncio.sleep(0.05)
            assert fb._tabs == before

            # At the cap there is no tab to put it in, so the page it was
            # opening is shown in the tab that asked for it.
            cdp = made[0].cdp
            cdp.calls.clear()
            sink.msgs.clear()
            fb._on_target_created({"targetInfo": {
                "targetId": "POP-2", "type": "page", "openerId": opener.target_id,
                "url": "https://example.net/second"}})
            await asyncio.sleep(0.15)
            assert [m for m in sink.msgs if m["type"] == "newtab"] == []
            assert ("Target.closeTarget", {"targetId": "POP-2"}) in cdp.calls
            assert opener.url == "https://example.net/second"
            assert ("Page.navigate", {"url": "https://example.net/second"}) in \
                opener.session.calls

            # And the browser's news that a tab's target is gone takes the
            # record with it.
            fb._on_target_destroyed({"targetId": "POP-1"})
            assert fb.tab("p1", "p1") is None
            assert {"type": "gone", "tab": "p1"} in sink.msgs

            await fb.shutdown()

    run(main())


def test_dialog_roundtrip_and_timeout_dismiss():
    async def main():
        with pytest.MonkeyPatch.context() as mp:
            sess, sink = FakeSession(), FakeSink()
            tab = a_tab(sess, sink)

            sess.fire("Page.javascriptDialogOpening",
                      {"type": "prompt", "message": "your name?",
                       "defaultPrompt": "ada", "url": "https://example.net/"})
            assert sink.msgs[-1] == {
                "type": "dialog", "tab": "t1", "kind": "prompt",
                "message": "your name?", "default": "ada",
                "url": "https://example.net/"}
            # One at a time: a second while the first is up is not stacked.
            sess.fire("Page.javascriptDialogOpening",
                      {"type": "alert", "message": "also this"})
            assert len([m for m in sink.msgs if m["type"] == "dialog"]) == 1

            assert await tab.answer_dialog(True, "grace") is True
            assert ("Page.handleJavaScriptDialog",
                    {"accept": True, "promptText": "grace"}) in sess.calls

            # An empty answer to a prompt is an answer: left out, the page gets
            # the default it suggested rather than the nothing that was typed.
            sess.calls.clear()
            sess.fire("Page.javascriptDialogOpening",
                      {"type": "prompt", "message": "?", "defaultPrompt": "ada"})
            assert await tab.answer_dialog(True, "") is True
            assert ("Page.handleJavaScriptDialog",
                    {"accept": True, "promptText": ""}) in sess.calls
            # Nothing left to answer, and no call made for the second try.
            sess.calls.clear()
            assert await tab.answer_dialog(True, "again") is False
            assert sess.calls == []

            # A phone in a pocket is what the deadline is for: the page cannot
            # be left stopped forever. An alert is dismissed by its only
            # button, which is OK; everything else by Cancel.
            mp.setattr(C, "DIALOG_TIMEOUT_S", 0.01)
            for kind, accept in (("alert", True), ("confirm", False),
                                 ("beforeunload", False)):
                sess.calls.clear()
                sess.fire("Page.javascriptDialogOpening",
                          {"type": kind, "message": "?"})
                await asyncio.sleep(0.1)
                assert ("Page.handleJavaScriptDialog", {"accept": accept}) in \
                    sess.calls, kind
                assert tab._dialog is None

            # And one the page itself closed leaves nothing behind to dismiss.
            sess.fire("Page.javascriptDialogOpening", {"type": "alert", "m": ""})
            sess.fire("Page.javascriptDialogClosed", {"result": False})
            assert tab._dialog is None
            tab._teardown()

    run(main())


def test_auth_enables_fetch_lazily_and_disables_after_success():
    async def main():
        with pytest.MonkeyPatch.context() as mp:
            sess, sink = FakeSession(), FakeSink()
            tab = a_tab(sess, sink)
            tab.frame_id = "MAIN"

            # Interception pauses every request the page makes, so nothing but
            # a challenge on the document itself is allowed to turn it on.
            sess.fire("Network.responseReceived", {
                "type": "Document", "frameId": "MAIN",
                "response": {"status": 200, "headers": {}}})
            sess.fire("Network.responseReceived", {
                "type": "XHR", "frameId": "MAIN",
                "response": {"status": 401,
                             "headers": {"WWW-Authenticate": "Basic realm=x"}}})
            sess.fire("Network.responseReceived", {
                "type": "Document", "frameId": "MAIN",
                "response": {"status": 401,
                             "headers": {"WWW-Authenticate": "Bearer x"}}})
            await asyncio.sleep(0.05)
            assert "Fetch.enable" not in sess.methods()

            sess.fire("Network.responseReceived", {
                "type": "Document", "frameId": "MAIN",
                "response": {"status": 401,
                             "headers": {"Www-Authenticate": 'Digest realm="wiki"'}}})
            await asyncio.sleep(0.05)
            assert sess.params("Fetch.enable")[0] == {
                "handleAuthRequests": True,
                "patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
            assert "Page.reload" in sess.methods()

            # Every paused request is let straight through; the pause is only
            # there for the challenge that rides on one of them.
            sess.fire("Fetch.requestPaused", {"requestId": "R1"})
            await asyncio.sleep(0.05)
            assert ("Fetch.continueRequest", {"requestId": "R1"}) in sess.calls

            sess.fire("Fetch.authRequired", {
                "requestId": "R2",
                "authChallenge": {"origin": "https://example.net", "realm": "wiki",
                                  "scheme": "digest", "source": "Server"}})
            assert sink.msgs[-1] == {"type": "auth", "tab": "t1",
                                     "host": "https://example.net",
                                     "realm": "wiki", "scheme": "digest"}
            # A second challenge under the first is refused rather than queued.
            sess.fire("Fetch.authRequired", {"requestId": "R3",
                                             "authChallenge": {"realm": "other"}})
            await asyncio.sleep(0.05)
            assert len([m for m in sink.msgs if m["type"] == "auth"]) == 1
            assert ("Fetch.continueWithAuth",
                    {"requestId": "R3",
                     "authChallengeResponse": {"response": "CancelAuth"}}) in sess.calls

            mp.setattr(C, "FETCH_LINGER_S", 0.01)
            assert await tab.answer_auth("ada", "hunter2") is True
            assert ("Fetch.continueWithAuth", {
                "requestId": "R2",
                "authChallengeResponse": {"response": "ProvideCredentials",
                                          "username": "ada",
                                          "password": "hunter2"}}) in sess.calls

            # Chrome has the credentials for the realm now, so the pause on
            # every request is given back.
            await asyncio.sleep(0.1)
            assert "Fetch.disable" in sess.methods()
            assert tab._fetch_on is False

            # A cancel gives the pause back on the same clock: the page is not
            # getting in, and every request of its is paused for nothing.
            sess.calls.clear()
            sess.fire("Network.responseReceived", {
                "type": "Document", "frameId": "MAIN",
                "response": {"status": 401,
                             "headers": {"www-authenticate": "Basic realm=z"}}})
            await asyncio.sleep(0.05)
            sess.fire("Fetch.authRequired", {"requestId": "R4",
                                             "authChallenge": {"realm": "z"}})
            assert await tab.cancel_auth() is True
            assert ("Fetch.continueWithAuth",
                    {"requestId": "R4",
                     "authChallengeResponse": {"response": "CancelAuth"}}) in sess.calls
            await asyncio.sleep(0.1)
            assert tab._fetch_on is False

            # And a 401 from somewhere else later turns it on again.
            sess.calls.clear()
            sess.fire("Network.responseReceived", {
                "type": "Document", "frameId": "MAIN",
                "response": {"status": 401,
                             "headers": {"www-authenticate": "Basic realm=z"}}})
            await asyncio.sleep(0.05)
            assert "Fetch.enable" in sess.methods()
            tab._teardown()

    run(main())


def test_title_binding_updates_tab():
    async def main():
        sess = FakeSession({"Page.getNavigationHistory": {
            "currentIndex": 0,
            "entries": [{"id": 1, "url": "https://example.net/app", "title": ""}]}})
        sink = FakeSink()
        tab = a_tab(sess, sink)

        # targetInfoChanged does not fire for a title a script sets, and an app
        # that sets every title it will ever have that way would otherwise keep
        # the first one forever.
        sess.fire("Runtime.bindingCalled",
                  {"name": "ptuiTitle", "payload": "Inbox (3)"})
        assert tab.title == "Inbox (3)"
        await asyncio.sleep(0.1)
        msg = [m for m in sink.msgs if m["type"] == "tab"][-1]
        assert msg["title"] == "Inbox (3)"
        assert "favicon" not in msg          # nothing read one yet

        # The same title again is not news.
        before = len(sink.msgs)
        sess.fire("Runtime.bindingCalled",
                  {"name": "ptuiTitle", "payload": "Inbox (3)"})
        await asyncio.sleep(0.1)
        assert len(sink.msgs) == before

        # The icon is read by the page once it has loaded, and rides the push
        # that follows.
        icon = "data:image/x-icon;base64,QUJD"
        sess.replies["Runtime.evaluate"] = {"result": {"value": icon}}
        sess.fire("Page.loadEventFired", {})
        await asyncio.sleep(0.15)
        assert tab.favicon == icon
        assert sink.msgs[-1]["favicon"] == icon
        evaluated = sess.params("Runtime.evaluate")[0]
        assert evaluated["awaitPromise"] is True
        assert evaluated["expression"] == C.FAVICON_JS

        # A page with no icon leaves the one that was there; a different host
        # takes it away, because another site's icon is worse than none.
        sess.replies["Runtime.evaluate"] = {"result": {"value": ""}}
        sess.fire("Page.loadEventFired", {})
        await asyncio.sleep(0.1)
        assert tab.favicon == icon
        tab._on_navigated({"frame": {"id": "MAIN", "url": "https://other.example.net/"}})
        assert tab.favicon == ""
        tab._teardown()

    run(main())


def test_download_events_throttled_and_renamed(home):
    async def main():
        with pytest.MonkeyPatch.context() as mp:
            # A staged upload from yesterday, which the launch sweeps away, and
            # one from just now, which it leaves.
            staged = C.uploads_dir()
            (staged / "old").mkdir(parents=True)
            (staged / "old" / "cv.pdf").write_bytes(b"old")
            os.utime(staged / "old", (0, 0))
            (staged / "new").mkdir(parents=True)

            fb, made = a_browser(mp)
            sink = FakeSink()
            fb.attach_pane("p1", sink)
            tab = await fb.open("p1", "t1", "https://example.net/")
            await tab.show()
            tab.frame_id = "F-MAIN"

            assert not (staged / "old").exists() and (staged / "new").is_dir()
            folder = C.downloads_dir()
            assert oct(os.stat(folder).st_mode)[-3:] == "700"
            assert made[0].cdp.params("Browser.setDownloadBehavior")[0] == {
                "behavior": "allow", "downloadPath": str(folder),
                "eventsEnabled": True}

            sink.msgs.clear()
            fb._on_download_begin({"frameId": "F-MAIN", "guid": "GUID1",
                                   "url": "https://example.net/q?id=1",
                                   "suggestedFilename": "report.pdf"})
            assert sink.msgs[-1] == {
                "type": "download", "tab": "t1", "guid": "GUID1",
                "name": "report.pdf", "url": "https://example.net/q?id=1",
                "state": "inProgress", "received": 0, "total": 0, "path": ""}

            # A progress event per chunk read is not a message per chunk read.
            for got in (10, 20, 30, 40):
                fb._on_download_progress({"guid": "GUID1", "state": "inProgress",
                                          "receivedBytes": got, "totalBytes": 100})
            assert len([m for m in sink.msgs if m["type"] == "download"]) == 1

            # Chrome files the download under its guid, so the name the site
            # suggested has to be put back — beside the one already there.
            (folder / "report.pdf").write_bytes(b"an older one")
            (folder / "GUID1").write_bytes(b"%PDF-1.4")
            fb._on_download_progress({"guid": "GUID1", "state": "completed",
                                      "receivedBytes": 100, "totalBytes": 100})
            await asyncio.sleep(0.2)
            done = [m for m in sink.msgs if m["type"] == "download"][-1]
            assert done["state"] == "completed" and done["received"] == 100
            assert done["path"] == str(folder / "report (2).pdf")
            assert (folder / "report (2).pdf").read_bytes() == b"%PDF-1.4"
            assert (folder / "report.pdf").read_bytes() == b"an older one"
            assert not (folder / "GUID1").exists()

            # A download from an iframe names a frame that is nobody's main
            # one, so it goes to the tab the user was last touching. A name
            # with a path in it is not a name.
            sink.msgs.clear()
            tab.note_input("mousePressed")
            fb._on_download_begin({"frameId": "F-IFRAME", "guid": "GUID2",
                                   "url": "https://example.net/x",
                                   "suggestedFilename": "../../etc/passwd"})
            began = [m for m in sink.msgs if m["type"] == "download"][-1]
            assert began["tab"] == "t1" and began["name"] == "passwd"

            # A cancelled one is said once and forgotten.
            fb._on_download_progress({"guid": "GUID2", "state": "canceled",
                                      "receivedBytes": 4, "totalBytes": 0})
            assert [m for m in sink.msgs if m["type"] == "download"][-1]["state"] \
                == "canceled"
            assert fb._downloads == {}

            await fb.shutdown()

    run(main())


def test_a_stream_the_page_was_not_ready_for_starts_when_it_loads():
    async def main():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(C, "CAST_START_S", 0.05)

            async def never(params):
                # What `Session.send` raises for a call the browser does not
                # answer inside its timeout.
                await asyncio.sleep(0.01)
                raise asyncio.TimeoutError

            sess = FakeSession({"Page.startScreencast": never})
            sink = FakeSink()
            tab = a_tab(sess, sink)
            tab.frame_id = "MAIN"

            # A first navigation that has not committed — a page behind an auth
            # challenge is the case this exists for — has no surface to cast,
            # and the call never answers. Holding the pane's socket here would
            # stop the user answering the challenge that is holding the page.
            started = time.monotonic()
            await tab.show()
            assert time.monotonic() - started < 1
            assert tab.live and tab._cast_pending

            sess.replies["Page.startScreencast"] = {}
            sess.calls.clear()
            tab._on_stopped({"frameId": "MAIN"})
            await asyncio.sleep(0.05)
            assert "Page.startScreencast" in sess.methods()
            assert not tab._cast_pending
            tab._teardown()

    run(main())
