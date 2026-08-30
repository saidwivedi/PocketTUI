"""The WebSocket attach bridge and the reconnect replay.

Two layers. The bridge tests run against a stubbed tmux with /bin/cat standing
in for the attach child, which is enough to prove the protocol: auth closes,
the replay text frame's shape and ordering, binary round-trips, resize, one
connection retiring another, and the linger window a dropped socket leaves its
PTY in. The integration tests at the bottom run the real thing against an
isolated tmux server (`-L pockettui-test -f /dev/null`) via the TMUX_BIN seam,
so nothing they create can touch the user's own sessions — including the one
property this all exists for: a phone reconnecting must not resize the window
the laptop is looking at.
"""

import asyncio
import fcntl
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
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


def report(ws, visible):
    """The client's visibility report.

    Sent on every open, on every visibilitychange, and before every intentional
    close. Its hidden→visible edge is what hands the shared window to this
    device, so most of what follows is written in these frames.
    """
    ws.send_text(json.dumps({"type": "visibility", "visible": bool(visible)}))


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


def collect_binary(ws, needle, timeout=15.0):
    """Binary frames until `needle` shows up, ignoring the control channel.

    The blocking receive runs on a thread, so a frame that never comes fails
    the test instead of hanging the suite.
    """
    acc = bytearray()
    found = threading.Event()

    def run():
        while not found.is_set():
            data = ws.receive().get("bytes")
            if data:
                acc.extend(data)
                if needle in acc:
                    found.set()

    threading.Thread(target=run, daemon=True).start()
    assert found.wait(timeout), f"{needle!r} never arrived; got {bytes(acc)!r}"
    return bytes(acc)


def reaped(pid):
    """True once `pid` is gone for good — exited *and* waited for."""
    try:
        return os.waitpid(pid, os.WNOHANG)[0] != 0
    except OSError:
        return True


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


def test_visibility_frame_flips_the_gate_and_dies_with_the_socket(client, bridge):
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        # Hidden by default: attaching alone never suppresses a push.
        assert A.visible_devs() == set()
        ws.send_text(json.dumps({"type": "visibility", "visible": True}))
        assert wait_for(lambda: A.visible_devs() == {"phone"})
        ws.send_text(json.dumps({"type": "visibility", "visible": False}))
        assert wait_for(lambda: A.visible_devs() == set())
        ws.send_text(json.dumps({"type": "visibility", "visible": True}))
        assert wait_for(lambda: A.visible_devs() == {"phone"})
        # The frame is consumed as control, never typed: /bin/cat would echo
        # it straight back if it reached the PTY.
        ws.send_bytes(b"marker\n")
        acc = collect_bytes(ws, b"marker")
        assert b"visibility" not in acc
        # And every received frame refreshes the staleness stamp.
        att = A.ATTACHED["phone-work"]
        before = att.last_seen
        ws.send_bytes(b"tick\n")
        assert wait_for(lambda: att.last_seen > before)
    # The socket is gone; so is the suppression.
    assert wait_for(lambda: A.visible_devs() == set())


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
# Linger and adoption
# ---------------------------------------------------------------------------
# A socket that simply went away leaves its PTY up for LINGER_S, so the phone's
# reconnect can take the same PTY back instead of running a second `tmux
# attach` — an attach is activity, and activity is what hands the shared window
# to whoever attached (see the real-tmux tests below).

@pytest.fixture
def short_linger(monkeypatch):
    """Linger long enough to observe, short enough to wait out in a test."""
    monkeypatch.setattr(A, "LINGER_S", 0.4)


def test_a_lost_socket_leaves_the_pty_lingering_for_the_reconnect(client, bridge):
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        att = A.ATTACHED["phone-work"]
        ws.send_bytes(b"before\n")
        collect_bytes(ws, b"before")

    # The socket is gone; the PTY is not, and the slot still holds it.
    assert wait_for(lambda: not A.ATTACHED["phone-work"].live)
    assert A.ATTACHED["phone-work"] is att
    os.kill(att.pid, 0)   # raises if the child was reaped

    with client.websocket_connect("/ws/attach/work") as ws2:
        hello(ws2)
        # The adoption announces itself in place of the replay frame: tmux
        # never re-initialises this client, so the terminal — and the modes
        # tmux believes it set on it — must survive the reconnect unreset.
        msg = ws2.receive()
        assert msg.get("bytes") is None
        assert json.loads(msg["text"]) == {"type": "adopted"}
        assert wait_for(lambda: A.ATTACHED["phone-work"].live)
        # Adopted, not respawned: same object, same PTY, same tmux client.
        assert A.ATTACHED["phone-work"] is att
        assert A.ATTACHED["phone-work"].pid == att.pid
        # And the adopted bridge carries input straight away.
        ws2.send_bytes(b"after\n")
        collect_bytes(ws2, b"after")


def test_linger_expiry_reaps_the_pty_and_the_next_connect_is_fresh(
        client, bridge, short_linger):
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        att = A.ATTACHED["phone-work"]

    assert wait_for(lambda: "phone-work" not in A.ATTACHED, timeout=5)
    # Reaped, not merely forgotten — a zombie would still be waitable.
    assert wait_for(lambda: reaped(att.pid), timeout=5)

    with client.websocket_connect("/ws/attach/work") as ws2:
        hello(ws2)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        assert A.ATTACHED["phone-work"].pid != att.pid


def test_a_superseding_connection_still_retires_rather_than_adopts(client, bridge):
    # The other takeover: the old socket is still live, so it must be told to
    # go, and the new connection gets its own PTY.
    with client.websocket_connect("/ws/attach/work") as first:
        hello(first)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        original = A.ATTACHED["phone-work"]
        with client.websocket_connect("/ws/attach/work") as second:
            hello(second)
            assert wait_for(lambda: A.ATTACHED.get("phone-work") not in
                            (None, original))
            assert A.ATTACHED["phone-work"].pid != original.pid
            assert wait_for(lambda: reaped(original.pid))
            second.send_bytes(b"mine\n")
            collect_bytes(second, b"mine")


def test_adoption_skips_an_unchanged_resize_but_applies_a_real_one(
        client, bridge, monkeypatch):
    # The client re-sends its size on every connect. Re-applying it would be a
    # resize tmux counts as activity, so an unchanged size must not reach the
    # ioctl; a rotation must.
    calls = []
    real = A.set_winsize

    def spy(fd, cols, rows):
        changed = real(fd, cols, rows)
        calls.append((cols, rows, changed))
        return changed

    monkeypatch.setattr(A, "set_winsize", spy)
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws, cols=90, rows=30)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        fd = A.ATTACHED["phone-work"].fd
    assert wait_for(lambda: not A.ATTACHED["phone-work"].live)

    with client.websocket_connect("/ws/attach/work") as ws2:
        hello(ws2, cols=90, rows=30)
        assert wait_for(lambda: A.ATTACHED["phone-work"].live)
        assert wait_for(lambda: calls and calls[-1] == (90, 30, False))
    assert wait_for(lambda: not A.ATTACHED["phone-work"].live)

    with client.websocket_connect("/ws/attach/work") as ws3:
        hello(ws3, cols=44, rows=30)
        assert wait_for(lambda: calls and calls[-1] == (44, 30, True))

        def winsize():
            rows, cols = struct.unpack(
                "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]
            return cols, rows

        assert wait_for(lambda: winsize() == (44, 30)), winsize()


def test_the_server_going_down_takes_a_lingering_pty_with_it(bridge):
    # A lingering PTY has no handler of its own to notice the shutdown, so the
    # lifespan reaps it — otherwise its tmux client would outlive the server.
    with TestClient(A.app) as c:
        with c.websocket_connect("/ws/attach/work") as ws:
            hello(ws)
            assert wait_for(lambda: "phone-work" in A.ATTACHED)
            att = A.ATTACHED["phone-work"]
        assert wait_for(lambda: not A.ATTACHED["phone-work"].live)
    assert "phone-work" not in A.ATTACHED
    assert wait_for(lambda: reaped(att.pid))


def test_a_lingering_attachment_is_neither_present_nor_notified():
    # The push gate and the chip channel both key on a live socket. A PTY
    # lingering while the phone is locked must count for neither, or the
    # notification it exists to allow would be suppressed.
    att = A.Attachment(0, -1, asyncio.Queue(), session="work", dev="phone")
    att.visible = True
    A.ATTACHED["unit-view"] = att
    try:
        assert A.visible_devs() == {"phone"}
        A.notify_session_views("work", "chips")
        assert att.out.qsize() == 1

        att.live = False
        assert A.visible_devs() == set()
        A.notify_session_views("work", "chips")
        assert att.out.qsize() == 1
    finally:
        del A.ATTACHED["unit-view"]


def test_a_dropped_socket_takes_its_visibility_with_it(client, bridge):
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        ws.send_text(json.dumps({"type": "visibility", "visible": True}))
        assert wait_for(lambda: A.visible_devs() == {"phone"})
    assert wait_for(lambda: not A.ATTACHED["phone-work"].live)
    assert A.visible_devs() == set()


def test_a_claim_never_signals_an_attachment_that_has_lost_its_socket():
    # claim_size signals a PID, so the one thing it must never do is fire at an
    # attachment that is on its way out: the child is about to be reaped and
    # the number handed to whatever the kernel allocates next. Every gate is
    # checked here rather than in an integration test, because the window this
    # guards is a few microseconds wide and cannot be raced deliberately.
    sent = []
    real_kill = os.kill

    def spy(pid, sig):
        sent.append((pid, sig))

    att = A.Attachment(os.getpid(), -1, asyncio.Queue(),
                       session="work", dev="phone")
    A.ATTACHED["guard-view"] = att
    A.os.kill = spy
    try:
        assert A.claim_size("guard-view", att) is True
        assert sent == [(os.getpid(), signal.SIGWINCH)]

        # A retired attachment: superseded by a newer connection, its PTY
        # about to go. retire() is what clears `live`, and either gate alone
        # is enough to stop the signal.
        sent.clear()
        att.last_claim = 0.0
        att.retire()
        assert att.live is False
        assert A.claim_size("guard-view", att) is False

        # Live again but no longer the view's attachment — a newer connection
        # took the slot and this object is a leftover.
        att.live = True
        att.retired = asyncio.Event()
        att.last_claim = 0.0
        A.ATTACHED["guard-view"] = A.Attachment(
            0, -1, asyncio.Queue(), session="work", dev="phone")
        assert A.claim_size("guard-view", att) is False

        # And gone from the dict entirely.
        del A.ATTACHED["guard-view"]
        assert A.claim_size("guard-view", att) is False
        assert sent == []
    finally:
        A.os.kill = real_kill
        A.ATTACHED.pop("guard-view", None)


def test_a_claim_that_fired_is_throttled_but_a_no_op_is_not():
    # The throttle is deliberately *after* the fact: iOS fires visibilitychange
    # twice on one unlock, and the second must not signal again. A call that
    # was refused stamps nothing, so the next real edge is not swallowed.
    sent = []
    real_kill = os.kill
    att = A.Attachment(os.getpid(), -1, asyncio.Queue(),
                       session="work", dev="phone")
    A.ATTACHED["throttle-view"] = att
    A.os.kill = lambda pid, sig: sent.append(pid)
    try:
        assert A.claim_size("throttle-view", att) is True
        assert A.claim_size("throttle-view", att) is False   # the double-fire
        assert len(sent) == 1

        stamp = att.last_claim
        att.live = False
        assert A.claim_size("throttle-view", att) is False
        # Refused by the guard, not the throttle: nothing was stamped, so the
        # attachment's own clock did not move.
        assert att.last_claim == stamp
    finally:
        A.os.kill = real_kill
        del A.ATTACHED["throttle-view"]


def test_teardown_clears_live_even_when_the_pty_does_not_linger(client, bridge):
    # `live` is what says "a socket is speaking for this device": the push gate
    # reads it (visible_devs), the chip channel reads it, and a claim refuses
    # to signal without it. The non-lingering endings used to leave it True on
    # a reaped attachment.
    with client.websocket_connect("/ws/attach/work") as first:
        hello(first)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        original = A.ATTACHED["phone-work"]
        report(first, True)
        assert wait_for(lambda: A.visible_devs() == {"phone"})
        with client.websocket_connect("/ws/attach/work") as second:
            hello(second)
            # Superseded: retire() clears `live` up front, so nothing counts
            # the retired attachment for the rest of its teardown.
            assert wait_for(lambda: original.live is False)
            assert wait_for(lambda: reaped(original.pid))
            assert A.claim_size("phone-work", original) is False
            assert A.visible_devs() == set()


def test_the_adoption_contract_sends_adopted_in_place_of_the_replay(
        client, bridge, monkeypatch):
    # Both halves of the contract the client's term.reset() hangs off. A fresh
    # attach is re-initialised by tmux, so it gets the scrollback replay; an
    # adopted one must not, even though the same history is sitting there to
    # send — painting it costs a reset, and the reset takes the DECSET modes
    # tmux still believes this terminal has.
    monkeypatch.setattr(A, "capture_history",
                        lambda name, lines=A.REPLAY_LINES: "kept")
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws)
        frame = ws.receive()
        assert frame.get("bytes") is None
        assert json.loads(frame["text"]) == {"type": "replay", "data": "kept"}
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        # No "adopted" frame anywhere behind it: collect_bytes fails on a text
        # frame, so the echo proves the rest of the stream is binary.
        ws.send_bytes(b"fresh\n")
        collect_bytes(ws, b"fresh")
    assert wait_for(lambda: not A.ATTACHED["phone-work"].live)

    with client.websocket_connect("/ws/attach/work") as ws2:
        hello(ws2)
        frame = ws2.receive()
        assert frame.get("bytes") is None
        assert json.loads(frame["text"]) == {"type": "adopted"}
        ws2.send_bytes(b"adopted\n")
        collect_bytes(ws2, b"adopted")   # and no replay behind it either


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------
# The out queue is per attachment, and the PTY fills it far faster than a phone
# on a stalled link drains it. Reads stop at the high-water mark and start again
# at the low one, which is what keeps a `yes` on a dead link from growing that
# queue until the machine swaps. Nothing may be dropped or reordered by it.

# One block is 8 bytes and holds no newline: the PTY rewrites \n on the way out,
# and these have to come back byte for byte.
FLOOD_BLOCKS = 40000
FLOOD_EXPECTED = b"".join(b"%07d." % i for i in range(FLOOD_BLOCKS))
FLOOD_TAIL = b"%07d." % (FLOOD_BLOCKS - 1)


@pytest.fixture
def flooding_pty(monkeypatch):
    """An attach child that floods and then behaves like /bin/cat, with marks
    small enough to hit in a test."""
    monkeypatch.setattr(A, "HIGH_WATER", 64 << 10)
    monkeypatch.setattr(A, "LOW_WATER", 16 << 10)
    src = ("import sys\n"
           "out = sys.stdout.buffer\n"
           f"out.write(b''.join(b'%07d.' % i for i in range({FLOOD_BLOCKS})))\n"
           "out.flush()\n"
           "for line in sys.stdin.buffer:\n"
           "    out.write(line)\n"
           "    out.flush()\n")
    monkeypatch.setattr(A, "attach_argv",
                        lambda target, view: [sys.executable, "-c", src])


@pytest.fixture
def stalled_send(monkeypatch):
    """Hold every binary frame at the socket, the way a phone whose link has
    gone away holds it. Returns the release."""
    real = A.WebSocket.send_bytes
    gate = threading.Event()

    async def send_bytes(self, data):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        await real(self, data)

    monkeypatch.setattr(A.WebSocket, "send_bytes", send_bytes)
    return gate


def test_a_stalled_socket_cannot_grow_the_out_queue_without_bound(
        client, bridge, flooding_pty, stalled_send):
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        att = A.ATTACHED["phone-work"]
        # The child floods; the socket takes nothing. Reads stop one read past
        # the mark, and with the reader off the fd the queue stops growing.
        assert wait_for(lambda: att.paused, timeout=10)
        peak = att.pending
        assert peak <= A.HIGH_WATER + 65536, peak
        time.sleep(0.3)
        assert att.paused and att.pending == peak

        # Let the socket drain: reads resume under the low mark, and every byte
        # the child wrote is still there, in order, exactly once.
        stalled_send.set()
        assert collect_binary(ws, FLOOD_TAIL, timeout=30) == FLOOD_EXPECTED
        assert wait_for(lambda: not att.paused and att.pending == 0)


def test_a_paused_reader_comes_back_for_the_connection_that_adopts(
        client, bridge, flooding_pty, stalled_send):
    # A socket that dies while its reader is paused leaves the PTY lingering
    # with nothing on the fd. linger_pty drops those bytes, but draining them is
    # still what puts the reader back — without it the reconnect would adopt a
    # PTY nobody is reading.
    with client.websocket_connect("/ws/attach/work") as ws:
        hello(ws)
        assert wait_for(lambda: "phone-work" in A.ATTACHED)
        att = A.ATTACHED["phone-work"]
        assert wait_for(lambda: att.paused, timeout=10)

    stalled_send.set()
    assert wait_for(lambda: not att.live)
    assert wait_for(lambda: not att.paused and att.pending <= A.LOW_WATER,
                    timeout=10)

    with client.websocket_connect("/ws/attach/work") as ws2:
        hello(ws2)
        assert json.loads(ws2.receive()["text"]) == {"type": "adopted"}
        assert A.ATTACHED["phone-work"] is att
        # Reading works on the adopted PTY: this echo only comes back if the
        # fd still has its callback.
        ws2.send_bytes(b"after\n")
        collect_binary(ws2, b"after")


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
        self._ptys = []
        self._stop = threading.Event()
        yield
        self._stop.set()
        for pid, fd in self._ptys:
            try:
                os.killpg(os.getpgid(pid), signal.SIGHUP)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
        subprocess.run([*TMUX_TEST_BIN, "kill-server"],
                       capture_output=True, timeout=10)

    def new_session(self, name, cols=80, rows=24):
        rc, _ = A.tmux("new-session", "-d", "-s", name,
                       "-x", str(cols), "-y", str(rows))
        assert rc == 0, f"could not create {name}"

    # -- the laptop half of the pair ---------------------------------------

    def laptop(self, session, cols=200, rows=50):
        """An ordinary tmux client of `session` on its own PTY.

        Not a WebSocket: this is the desktop terminal the phone shares the
        window with, and the client whose size the phone must stop stealing.
        Its screen is read and dropped by a thread for the life of the test —
        a tmux client whose tty nobody reads blocks on its own repaint and then
        stops answering keys, which would look exactly like the bug.
        """
        pid, fd = A.spawn_pty(
            [*TMUX_TEST_BIN, "attach", "-t", f"={session}"], cols, rows)
        os.set_blocking(fd, False)
        self._ptys.append((pid, fd))

        def drain():
            while not self._stop.is_set():
                try:
                    if not os.read(fd, 1 << 16):
                        return
                except BlockingIOError:
                    time.sleep(0.01)
                except OSError:
                    return

        threading.Thread(target=drain, daemon=True).start()
        assert wait_for(lambda: self.clients(session) != [])
        return fd

    def width(self, session="base"):
        rc, out = A.tmux("display-message", "-p", "-t", session,
                         "#{window_width}")
        return out.strip() if rc == 0 else ""

    def clients(self, session):
        rc, out = A.tmux("list-clients", "-t", session, "-F", "#{client_tty}")
        return out.split() if rc == 0 else []

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

    # -- the pair: a phone and a laptop on one window -----------------------

    def test_activity_still_decides_the_shared_window_size(self, client):
        # The baseline the linger exists to protect. tmux's `window-size
        # latest` is the behaviour the user wants: whoever was active last owns
        # the size, so the phone attaching claims it and the laptop's next
        # keystroke claims it straight back — with the phone still attached.
        self.new_session("base", cols=200, rows=50)
        lap = self.laptop("base")
        assert wait_for(lambda: self.width() == "200"), self.width()
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            assert wait_for(lambda: self.width() == "40"), self.width()
            os.write(lap, b"echo laptop-typing\n")
            assert wait_for(lambda: self.width() == "200"), self.width()
            # And it stays there while the phone sits idle.
            time.sleep(0.5)
            assert self.width() == "200"

    def test_reconnect_churn_never_resizes_the_shared_window(self, client):
        # The bug: the phone's socket dies seconds after the app backgrounds
        # and comes back on a 0.5–5 s backoff. Every reconnect used to be a
        # fresh `tmux attach`, which is activity, which snapped the laptop's
        # window to phone size until the next keystroke snapped it back. That
        # churn is the flicker; adoption removes it.
        self.new_session("base", cols=200, rows=50)
        lap = self.laptop("base")
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            # The first attach is a real one and claims the window, as it
            # should; the laptop takes it back. Both are waited for, or the
            # churn below would start from an unsettled size.
            assert wait_for(lambda: self.width() == "40"), self.width()
            os.write(lap, b"echo laptop-typing\n")
            assert wait_for(lambda: self.width() == "200"), self.width()
        phone_tty = self.clients("phone-base")
        assert len(phone_tty) == 1

        widths = []
        stop = threading.Event()

        def sample():
            while not stop.is_set():
                widths.append(self.width())
                time.sleep(0.05)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        try:
            for n in range(5):
                with client.websocket_connect("/ws/attach/base") as ws:
                    hello(ws, cols=40, rows=20)
                    assert wait_for(lambda: A.ATTACHED["phone-base"].live)
                    # The laptop keeps working through the churn.
                    os.write(lap, f"echo round-{n}\n".encode())
                    time.sleep(0.2)
                assert wait_for(lambda: not A.ATTACHED["phone-base"].live)
                # Same tmux client throughout: tmux saw neither a detach nor
                # an attach, which is why nothing resized.
                assert self.clients("phone-base") == phone_tty
        finally:
            stop.set()
            sampler.join(timeout=2)
        assert set(widths) == {"200"}, sorted(set(widths))

    def test_an_adopted_reconnect_gets_the_output_it_missed(self, client):
        self.new_session("base", cols=200, rows=50)
        lap = self.laptop("base")
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            assert wait_for(lambda: self.clients("phone-base") != [])
        assert wait_for(lambda: not A.ATTACHED["phone-base"].live)

        # Output produced while the phone had no socket at all. The PTY is
        # still being drained, so tmux is not blocked on it.
        os.write(lap, b"echo GAPMARK\n")
        assert wait_for(lambda: b"GAPMARK" in subprocess.run(
            [*TMUX_TEST_BIN, "capture-pane", "-p", "-t", "base"],
            capture_output=True).stdout)

        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            # The adopted PTY never re-attached, so tmux has no reason to
            # repaint by itself — redraw_view is what puts the screen the
            # phone missed on the wire, as PTY bytes rather than as the
            # scrollback replay that precedes them.
            collect_binary(ws, b"GAPMARK")
            # Input works immediately after the adoption.
            ws.send_bytes(b"echo ADOPTED-INPUT\n")
            collect_binary(ws, b"ADOPTED-INPUT")

    def test_linger_expiry_detaches_and_the_next_connect_re_attaches(
            self, client, monkeypatch):
        monkeypatch.setattr(A, "LINGER_S", 0.5)
        self.new_session("base", cols=200, rows=50)
        lap = self.laptop("base")
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            assert wait_for(lambda: A.ATTACHED.get("phone-base") is not None)
            pid = A.ATTACHED["phone-base"].pid
            assert wait_for(lambda: self.width() == "40"), self.width()
            os.write(lap, b"echo laptop-typing\n")
            assert wait_for(lambda: self.width() == "200"), self.width()

        # Nobody came back: the PTY goes, and with it the tmux client.
        assert wait_for(lambda: "phone-base" not in A.ATTACHED, timeout=5)
        assert wait_for(lambda: self.clients("phone-base") == [])
        assert wait_for(lambda: reaped(pid), timeout=5)   # no zombie either
        assert self.width() == "200"

        # Picking the phone back up later is a real attach again — and a real
        # attach claiming the window size is the behaviour to keep.
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            assert wait_for(lambda: self.width() == "40"), self.width()

    def test_killing_the_session_takes_a_lingering_pty_with_it(self, client):
        self.new_session("base", cols=200, rows=50)
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            assert wait_for(lambda: self.clients("phone-base") != [])
            pid = A.ATTACHED["phone-base"].pid
        assert wait_for(lambda: not A.ATTACHED["phone-base"].live)

        r = client.post("/api/session/kill", json={"session": "base"})
        assert r.status_code == 200
        # The child loses its session, the drain sees the PTY hang up, and the
        # linger ends there rather than at its timeout.
        assert wait_for(lambda: "phone-base" not in A.ATTACHED, timeout=5)
        assert wait_for(lambda: reaped(pid), timeout=5)

    def test_attaching_heals_the_0_8_118_size_pins(self, client):
        self.new_session("base", cols=200, rows=50)
        rc, _ = A.tmux("new-session", "-d", "-s", "phone-base", "-t", "base")
        assert rc == 0
        # Exactly what build 0.8.118 wrote into the user's tmux server.
        A.tmux("set-option", "-w", "-t", "phone-base", "window-size", "smallest")
        A.tmux("set-option", "-w", "-t", "phone-base", "aggressive-resize", "on")
        A.tmux("set-hook", "-t", "phone-base", "window-linked",
               "set-option -w window-size smallest")
        A.tmux("set-hook", "-a", "-t", "phone-base", "window-linked",
               "set-option -w aggressive-resize on")
        assert A.tmux("show-hooks", "-t", "phone-base")[1].count(
            "window-linked") == 2

        def pins():
            rc, out = A.tmux("list-windows", "-t", "base", "-F",
                             "#{window-size} #{aggressive-resize}")
            return out.strip()

        assert pins() == "smallest 1"
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            # Back to the inherited default: activity decides the size again.
            assert wait_for(lambda: pins() == "latest 0"), pins()
            assert wait_for(
                lambda: A.tmux("show-hooks", "-t", "phone-base")[1].strip() == "")

    # -- the visibility claim ----------------------------------------------
    #
    # Adoption is what stopped a reconnecting phone from stealing the laptop's
    # window, and it works — but it took the pickup with it: a phone that came
    # back from a pocket adopted silently and sat at laptop size. The claim
    # puts the pickup back without putting the churn back, and the difference
    # between the two is entirely in the visibility reports below.

    def settled(self, session, want):
        """Wait for the shared window to reach `want` and stay there."""
        assert wait_for(lambda: self.width(session) == want), \
            self.width(session)
        time.sleep(0.3)
        assert self.width(session) == want

    def sampler(self, session="base"):
        """Every shared-window width seen until the caller stops it."""
        seen = []
        stop = threading.Event()

        def run():
            while not stop.is_set():
                seen.append(self.width(session))
                time.sleep(0.05)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return seen, stop, t

    def test_a_phone_that_reported_hidden_claims_the_window_on_pickup(
            self, client):
        # The pickup, end to end. The phone locks (hidden), its socket dies,
        # the laptop keeps the window at 200; the phone comes back inside the
        # linger window and the visible report on the far side is what hands
        # the window to it. Nothing else in the reconnect may do it — the
        # adoption is deliberately invisible to tmux.
        self.new_session("base", cols=200, rows=50)
        lap = self.laptop("base")
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            report(ws, True)
            assert wait_for(lambda: self.width() == "40"), self.width()
            os.write(lap, b"echo laptop-typing\n")
            self.settled("base", "200")
            # The lock, reported while the socket is still up.
            report(ws, False)
            assert wait_for(lambda: A.ATTACHED["phone-base"].visible is False)
        assert wait_for(lambda: not A.ATTACHED["phone-base"].live)
        self.settled("base", "200")

        with client.websocket_connect("/ws/attach/base") as ws2:
            hello(ws2, cols=40, rows=20)
            assert wait_for(lambda: A.ATTACHED["phone-base"].live)
            # Adopting is silent: same tmux client, no attach, no resize. The
            # window is still the laptop's at this point.
            self.settled("base", "200")
            report(ws2, True)
            assert wait_for(lambda: self.width() == "40"), self.width()
            # And the laptop takes it straight back the moment it is used —
            # the claim is a claim, not a pin.
            os.write(lap, b"echo laptop-again\n")
            assert wait_for(lambda: self.width() == "200"), self.width()

    def test_a_foregrounded_phone_churning_never_claims_the_window(self, client):
        # The other side of the same coin, and the regression that matters
        # most: a phone sitting in the user's hand loses its socket anyway
        # (flaky wifi, a walk out of range) and reconnects on its backoff. It
        # never reported hidden, so every reconnect's visible=true is
        # true→true — no edge, no claim — and the laptop's window never moves.
        self.new_session("base", cols=200, rows=50)
        lap = self.laptop("base")
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            report(ws, True)
            assert wait_for(lambda: self.width() == "40"), self.width()
            os.write(lap, b"echo laptop-typing\n")
            self.settled("base", "200")

        seen, stop, t = self.sampler()
        try:
            for n in range(5):
                assert wait_for(lambda: not A.ATTACHED["phone-base"].live)
                with client.websocket_connect("/ws/attach/base") as ws:
                    hello(ws, cols=40, rows=20)
                    # Exactly what the client sends in onopen, every time.
                    report(ws, True)
                    assert wait_for(lambda: A.ATTACHED["phone-base"].live)
                    os.write(lap, f"echo round-{n}\n".encode())
                    time.sleep(0.2)
        finally:
            stop.set()
            t.join(timeout=2)
        assert set(seen) == {"200"}, sorted(set(seen))

    def test_a_fresh_attachments_first_visible_report_claims_nothing(
            self, client):
        # A fresh attachment starts hidden, so the visible=true every client
        # sends in onopen is an edge on paper. It must not claim: the attach it
        # rode in on already did, and the report arrives a moment later — long
        # enough for the laptop to have taken the window back, which this
        # would then steal a second time for a phone that did nothing. The
        # exemption is for the *first* report of a fresh attachment only; the
        # pickup case above is an adopted one and still claims.
        self.new_session("base", cols=200, rows=50)
        lap = self.laptop("base")
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            # The attach itself claims, as it always has.
            assert wait_for(lambda: self.width() == "40"), self.width()
            os.write(lap, b"echo laptop-typing\n")
            self.settled("base", "200")
            # …and only now does the connect-time report land.
            report(ws, True)
            assert wait_for(lambda: A.ATTACHED["phone-base"].visible is True)
            self.settled("base", "200")

    def test_an_intentional_close_and_reopen_claims_the_window(self, client):
        # Closing the terminal (back to the session list, or a rail switch on a
        # wide layout) drops the socket the same way a lock does, and the PTY
        # lingers the same way. Without the client reporting hidden on its way
        # out, reopening straight afterwards adopts a still-visible attachment
        # and comes up at the laptop's size — the reproducible half of
        # "sometimes it doesn't resize". The report before the close is what
        # makes the reopen an edge.
        self.new_session("base", cols=200, rows=50)
        lap = self.laptop("base")
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            report(ws, True)
            assert wait_for(lambda: self.width() == "40"), self.width()
            os.write(lap, b"echo laptop-typing\n")
            self.settled("base", "200")
            report(ws, False)          # closeTerminal(), before sock.close()
            assert wait_for(lambda: A.ATTACHED["phone-base"].visible is False)

        # Reopened immediately, well inside the linger.
        with client.websocket_connect("/ws/attach/base") as ws2:
            hello(ws2, cols=40, rows=20)
            report(ws2, True)
            assert wait_for(lambda: self.width() == "40"), self.width()

    def test_a_live_socket_claims_on_the_visible_edge_with_no_input(self, client):
        # No reconnect at all: the app was backgrounded and foregrounded fast
        # enough that the socket survived. The edge still has to claim, and it
        # has to do it without anything that reads as user input — no key is
        # written to the PTY here.
        self.new_session("base", cols=200, rows=50)
        lap = self.laptop("base")
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            report(ws, True)
            assert wait_for(lambda: self.width() == "40"), self.width()
            os.write(lap, b"echo laptop-typing\n")
            self.settled("base", "200")

            report(ws, False)
            assert wait_for(lambda: A.ATTACHED["phone-base"].visible is False)
            self.settled("base", "200")
            report(ws, True)
            assert wait_for(lambda: self.width() == "40"), self.width()

    def test_claiming_a_window_this_client_already_owns_resizes_nothing(
            self, client, monkeypatch):
        # The common case: the phone is the last active client already, so the
        # claim must be a no-op. The old primitive could not be — it worked by
        # writing a size tmux did not have and taking it back, so every claim
        # cost a real resize and a repaint at a size no client ever asked for.
        # SIGWINCH cannot: the size on the fd is never touched, and tmux
        # recalculates to the numbers it already had.
        self.new_session("base", cols=200, rows=50)
        lap = self.laptop("base")
        fired = []
        signalled = []
        real = A.claim_size
        real_kill = os.kill

        def spy(view, me):
            # Nothing else in this test signals, so a bare wrapper on os.kill
            # is enough to prove the claim really reached the tmux client
            # rather than being skipped as unnecessary.
            monkeypatch.setattr(
                A.os, "kill",
                lambda pid, sig: (signalled.append((pid, sig)),
                                  real_kill(pid, sig))[1])
            try:
                got = real(view, me)
            finally:
                monkeypatch.setattr(A.os, "kill", real_kill)
            fired.append(got)
            return got

        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            report(ws, True)
            assert wait_for(lambda: self.width() == "40"), self.width()
            fd = A.ATTACHED["phone-base"].fd
            time.sleep(0.3)

            def fd_size():
                rows, cols = struct.unpack(
                    "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]
                return cols, rows

            # Sampled in-process and unthrottled, so a size written and taken
            # back within a millisecond is still caught — the fake intermediate
            # the jiggle depended on would show up here as (40, 19).
            fd_seen = set()
            stop = threading.Event()

            def watch_fd():
                while not stop.is_set():
                    fd_seen.add(fd_size())

            t = threading.Thread(target=watch_fd, daemon=True)
            t.start()
            seen, stop_w, tw = self.sampler()
            try:
                A.claim_size = spy
                report(ws, False)
                assert wait_for(
                    lambda: A.ATTACHED["phone-base"].visible is False)
                report(ws, True)
                assert wait_for(lambda: fired == [True], timeout=5), fired
                time.sleep(0.5)
            finally:
                A.claim_size = real
                stop.set()
                stop_w.set()
                t.join(timeout=2)
                tw.join(timeout=2)

            # The signal really went out — and moved nothing, on either side.
            assert fired == [True]
            assert signalled == [(A.ATTACHED["phone-base"].pid,
                                  signal.SIGWINCH)]
            assert fd_seen == {(40, 20)}, fd_seen
            assert set(seen) == {"40"}, sorted(set(seen))
            # And the no-op left a working client behind: the laptop can still
            # take the window, and the phone can still take it back.
            os.write(lap, b"echo laptop-typing\n")
            assert wait_for(lambda: self.width() == "200"), self.width()

    def test_a_hidden_report_that_never_reached_the_server_still_claims(
            self, client):
        # The latch. iOS can kill the socket before visibilitychange fires, so
        # the hidden never goes out and the server still believes this device
        # is on screen — the reconnect's visible=true would be true→true and
        # claim nothing. The client remembers the miss and replays the pair,
        # false then true, on the next open; the false is what makes the true
        # an edge again.
        self.new_session("base", cols=200, rows=50)
        lap = self.laptop("base")
        with client.websocket_connect("/ws/attach/base") as ws:
            hello(ws, cols=40, rows=20)
            report(ws, True)
            assert wait_for(lambda: self.width() == "40"), self.width()
            os.write(lap, b"echo laptop-typing\n")
            self.settled("base", "200")
        # Dropped still believing itself visible — no hidden was ever sent.
        assert wait_for(lambda: not A.ATTACHED["phone-base"].live)
        assert A.ATTACHED["phone-base"].visible is True

        with client.websocket_connect("/ws/attach/base") as ws2:
            hello(ws2, cols=40, rows=20)
            assert wait_for(lambda: A.ATTACHED["phone-base"].live)
            report(ws2, False)
            assert wait_for(lambda: A.ATTACHED["phone-base"].visible is False)
            report(ws2, True)
            assert wait_for(lambda: self.width() == "40"), self.width()
