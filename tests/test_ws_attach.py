"""The WebSocket attach bridge and the reconnect replay.

Two layers. The bridge tests run against a stubbed tmux with /bin/cat standing
in for the attach child, which is enough to prove the protocol: auth closes,
the replay text frame's shape and ordering, binary round-trips, resize, and one
connection retiring another. The integration tests at the bottom run the real
thing against an isolated tmux server (`-L pockettui-test -f /dev/null`) via
the TMUX_BIN seam, so nothing they create can touch the user's own sessions.
"""

import fcntl
import json
import shutil
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402

TOKEN = "ABCDEFGHIJ"


@pytest.fixture
def client():
    # Context-managed, so every websocket in a test shares one portal and
    # therefore one event loop — the shape production has. Separate portals
    # would put two connections on two loops, where retire()'s asyncio.Event
    # could never wake the other handler.
    with TestClient(A.app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    monkeypatch.setattr(A, "LIMITER", A.AuthLimiter())
    monkeypatch.setattr(A, "RATE", A.RateLimiter())


@pytest.fixture
def bridge(monkeypatch):
    """A fully stubbed attach path: every session exists, the attach child is
    /bin/cat (echoes bytes straight back), and there is no history to replay
    unless a test stubs some in."""
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(A, "session_exists", lambda name: True)
    monkeypatch.setattr(A, "view_name",
                        lambda target, dev: f"{dev or 'ptui'}-{target}")
    monkeypatch.setattr(A, "attach_argv", lambda target, view: ["/bin/cat"])
    monkeypatch.setattr(A, "resolve_target", lambda session, dev: session)
    monkeypatch.setattr(A, "capture_history",
                        lambda name, lines=A.REPLAY_LINES: "")
    monkeypatch.setattr(A, "enable_mouse", lambda view: None)
    monkeypatch.setattr(A, "tmux", lambda *a: (0, ""))


def hello(ws, token=TOKEN, dev="phone", cols=80, rows=24):
    """The client's mandatory first frame: size + credentials in one."""
    ws.send_text(json.dumps({"type": "resize", "cols": cols, "rows": rows,
                             "token": token, "dev": dev}))


def collect_bytes(ws, needle, tries=20):
    """Binary frames accumulated until `needle` shows up (echo + PTY timing
    can split it across frames)."""
    acc = b""
    for _ in range(tries):
        msg = ws.receive()
        assert msg.get("text") is None, f"unexpected text frame: {msg}"
        acc += msg.get("bytes") or b""
        if needle in acc:
            return acc
    raise AssertionError(f"{needle!r} never arrived; got {acc!r}")


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# Auth and existence
# ---------------------------------------------------------------------------

def test_bad_token_closes_4401(client, bridge):
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws, token="WRONGWRONG")
        msg = ws.receive()
        assert msg["type"] == "websocket.close"
        assert msg["code"] == 4401


def test_silent_client_closes_4401(client, bridge):
    # Never sending the first frame means never authenticating: after the 5 s
    # handshake timeout the server must refuse, not attach at the default size.
    # (The timeout is what makes this test take ~5 s.)
    with client.websocket_connect("/ws/attach/work") as ws:
        msg = ws.receive()
        assert msg["type"] == "websocket.close"
        assert msg["code"] == 4401


def test_missing_session_closes_4404(client, bridge, monkeypatch):
    monkeypatch.setattr(A, "session_exists", lambda name: False)
    with client.websocket_connect("/ws/attach/gone") as ws:
        hello(ws)
        msg = ws.receive()
        assert msg["type"] == "websocket.close"
        assert msg["code"] == 4404


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def test_replay_text_frame_arrives_before_any_binary(client, bridge, monkeypatch):
    captured = []

    def capture(name, lines=A.REPLAY_LINES):
        captured.append(name)
        return "one\ntwo"

    monkeypatch.setattr(A, "capture_history", capture)
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws)
        msg = ws.receive()
        # The first frame of the connection is the replay, as *text* — it must
        # be on the wire before the PTY can produce a single byte.
        assert msg.get("bytes") is None
        frame = json.loads(msg["text"])
        assert frame == {"type": "replay", "data": "one\r\ntwo"}
        # resolve_target picked the session (stubbed identity here).
        assert captured == ["work"]
        # And the bridge still works after it.
        ws.send_bytes(b"ping\n")
        collect_bytes(ws, b"ping")


def test_no_replay_frame_when_history_is_empty(client, bridge):
    # bridge stubs capture_history to "" — a fresh session, or the alt-screen
    # skip, which answers "" identically. No text frame may arrive at all; the
    # first frame the client sees is attach output.
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws)
        ws.send_bytes(b"ping\n")
        collect_bytes(ws, b"ping")  # asserts no text frame slipped in


def test_capture_history_skips_the_alternate_screen(monkeypatch):
    def tmux(*args):
        if args[0] == "list-panes":
            return 0, "1\t%1\t1\n"
        raise AssertionError("capture-pane must not run for an alt-screen pane")

    monkeypatch.setattr(A, "tmux", tmux)
    assert A.capture_history("s") == ""


def test_capture_history_captures_history_only(monkeypatch):
    calls = []

    def tmux(*args):
        calls.append(args)
        if args[0] == "list-panes":
            # Two panes; only the active one may be captured.
            return 0, "0\t%1\t0\n1\t%2\t0\n"
        return 0, "one\ntwo\n"

    monkeypatch.setattr(A, "tmux", tmux)
    assert A.capture_history("s", lines=10) == "one\ntwo"
    cap = calls[1]
    assert cap[0] == "capture-pane"
    assert cap[cap.index("-t") + 1] == "%2"
    # -E -1 is the "history only" half: the attach repaint restores the visible
    # screen, so the capture must stop one line above it.
    assert cap[cap.index("-S") + 1] == "-10"
    assert cap[cap.index("-E") + 1] == "-1"
    for flag in ("-p", "-e", "-J"):
        assert flag in cap


def test_capture_history_drops_oldest_lines_past_the_byte_cap(monkeypatch):
    lines = [f"line-{i:04d}" + "x" * 90 for i in range(30)]

    def tmux(*args):
        if args[0] == "list-panes":
            return 0, "1\t%1\t0\n"
        return 0, "\n".join(lines) + "\n"

    monkeypatch.setattr(A, "tmux", tmux)
    monkeypatch.setattr(A, "REPLAY_MAX_BYTES", 1000)
    text = A.capture_history("s")
    kept = text.split("\n")
    assert len(text.encode()) <= 1000
    # Newest survive, oldest go.
    assert kept[-1] == lines[-1]
    assert lines[0] not in kept


# ---------------------------------------------------------------------------
# The bridge itself
# ---------------------------------------------------------------------------

def test_binary_round_trip_through_the_pty(client, bridge):
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws)
        ws.send_bytes(b"hello-bridge\n")
        collect_bytes(ws, b"hello-bridge")


def test_resize_control_frame_reaches_the_pty(client, bridge):
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        fd = A.ATTACHED["phone-work"].fd
        ws.send_text(json.dumps({"type": "resize", "cols": 101, "rows": 41}))

        def winsize():
            rows, cols = struct.unpack(
                "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]
            return cols, rows

        assert wait_for(lambda: winsize() == (101, 41)), winsize()


def test_second_connect_to_the_same_view_retires_the_first(client, bridge):
    with client.websocket_connect("/ws/attach/work") as first:
        hello(first)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        original = A.ATTACHED["phone-work"]
        with client.websocket_connect("/ws/attach/work") as second:
            hello(second)
            # The first connection is closed by the server, not the client;
            # PTY output may still be in flight ahead of the close.
            for _ in range(20):
                msg = first.receive()
                if msg["type"] == "websocket.close":
                    break
            else:
                raise AssertionError("first connection never closed")
            assert wait_for(
                lambda: A.ATTACHED.get("phone-work") not in (None, original))
            # And the second one owns a working bridge.
            second.send_bytes(b"still-alive\n")
            collect_bytes(second, b"still-alive")


def test_notify_rides_the_out_queue_as_text(client, bridge):
    # The control channel P3 builds on: a str handed to Attachment.notify comes
    # out of the same socket as a text frame, interleaved with the PTY bytes.
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        payload = json.dumps({"type": "prompt", "options": ["y", "n"]})
        A.ATTACHED["phone-work"].notify(payload)
        msg = ws.receive()
        assert msg.get("text") == payload


# ---------------------------------------------------------------------------
# Integration: the real tmux, on an isolated server
# ---------------------------------------------------------------------------

TMUX_TEST_BIN = ["tmux", "-L", "pockettui-test", "-f", "/dev/null"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
class TestRealTmux:
    """End-to-end against a private tmux server. The TMUX_BIN seam points every
    helper and endpoint at `-L pockettui-test`, so the suite can create, rename
    and kill sessions freely; kill-server in teardown leaves nothing behind."""

    @pytest.fixture(autouse=True)
    def isolated_server(self, monkeypatch):
        monkeypatch.setattr(A, "TMUX_BIN", list(TMUX_TEST_BIN))
        monkeypatch.setattr(A, "AUTH_TOKEN", None)
        yield
        subprocess.run([*TMUX_TEST_BIN, "kill-server"],
                       capture_output=True, timeout=10)

    def new_session(self, name):
        rc, _ = A.tmux("new-session", "-d", "-s", name, "-x", "80", "-y", "24")
        assert rc == 0, f"could not create {name}"

    def test_list_and_representative_rule(self):
        self.new_session("base")
        assert "base" in [s["name"] for s in A.list_sessions()]
        # A grouped device view appears in the rows but never in the list.
        rc, _ = A.tmux("new-session", "-d", "-s", "phone-base", "-t", "base")
        assert rc == 0
        reps = {r["name"]: r["representative"] for r in A.session_rows()}
        assert reps == {"base": True, "phone-base": False}
        assert [s["name"] for s in A.list_sessions()] == ["base"]

    def test_create_endpoint_end_to_end(self, client):
        # api_new_session pins ZDOTDIR via `new-session -e`, which tmux only
        # grew in 3.2 — on an older tmux the endpoint cannot work at all, and
        # that is a property of the installed tmux, not of this change.
        rc, _ = A.tmux("new-session", "-d", "-e", "P=1", "-s", "e-probe")
        if rc != 0:
            pytest.skip("this tmux predates new-session -e (3.2), "
                        "which /api/session depends on")
        A.tmux("kill-session", "-t", "=e-probe")
        r = client.post("/api/session", json={"name": "made"})
        assert r.status_code == 200
        assert A.session_exists("made")

    def test_rename_and_kill_end_to_end(self, client):
        self.new_session("base")
        rc, _ = A.tmux("new-session", "-d", "-s", "phone-base", "-t", "base")
        assert rc == 0
        rc, _ = A.tmux("set-option", "-t", "base", "@alias", "My Run")
        assert rc == 0

        r = client.post("/api/session/rename",
                        json={"session": "base", "name": "renamed"})
        assert r.status_code == 200
        assert r.json() == {"session": "renamed"}
        # The view died, the base survived under its new name — and it is
        # still the one the list shows (the renamed-base scenario, for real).
        assert not A.session_exists("phone-base")
        assert A.session_exists("renamed")
        rows = A.list_sessions()
        assert [s["name"] for s in rows] == ["renamed"]
        # The alias is a session option and rides through the rename.
        assert rows[0]["alias"] == "My Run"

        # A fresh view of the renamed session, then kill takes the whole group.
        rc, _ = A.tmux("new-session", "-d", "-s", "phone-renamed", "-t", "renamed")
        assert rc == 0
        r = client.post("/api/session/kill", json={"session": "renamed"})
        assert r.status_code == 200
        assert not A.session_exists("renamed")
        assert not A.session_exists("phone-renamed")

    def test_capture_history_returns_scrollback_not_screen(self):
        self.new_session("hist")
        rc, _ = A.tmux("send-keys", "-t", "hist", "seq 1 200", "Enter")
        assert rc == 0
        text = ""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            text = A.capture_history("hist")
            if "150" in text:
                break
            time.sleep(0.1)
        stripped = [line.strip() for line in text.splitlines()]
        # Lines that scrolled off the 24-row screen are in the replay …
        assert "100" in stripped
        assert "150" in stripped
        # … and the lines still on the visible screen are not: the attach
        # repaint provides those, so replaying them would paint them twice.
        assert "200" not in stripped

    def test_capture_history_is_empty_for_a_fresh_session(self):
        self.new_session("blank")
        assert A.capture_history("blank") == ""
