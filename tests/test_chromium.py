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
import json
import os
import subprocess
import sys
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
