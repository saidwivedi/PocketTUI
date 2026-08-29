"""The pane watcher, prompt detection, and the notification plumbing.

Three layers. detect_prompt is a pure table — text in, shape out. The watcher
state machine is driven through watch_update with hand-built session/pane rows
and the clocks passed as plain arguments, so every episode rule (IDLE_S,
MIN_BUSY_S, re-arm, the 30 s gap, the bell edge, @notify) is proved without
sleeping or patching time. The transports and endpoints run against seams —
a fake pywebpush module, a captured urllib, tmp state files, a scripted tmux —
so the whole file passes with or without pywebpush installed, which is itself
one of the guarantees under test.
"""

import asyncio
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    return TestClient(A.app)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Every test gets its own limiter and watcher state."""
    monkeypatch.setattr(A, "LIMITER", A.AuthLimiter())
    monkeypatch.setattr(A, "RATE", A.RateLimiter())
    monkeypatch.setattr(A, "WATCHER", {})


@pytest.fixture
def push_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "PUSH_SUBS_PATH", tmp_path / "subs.json")
    monkeypatch.setattr(A, "VAPID_PATH", tmp_path / "vapid.json")
    return tmp_path


def fake_module(**kw):
    """A stand-in pywebpush: anything truthy makes push_available() True."""
    return SimpleNamespace(**kw)


SUB = {"endpoint": "https://push.example/reg/1",
       "keys": {"p256dh": "P", "auth": "A"}}


# ---------------------------------------------------------------------------
# detect_prompt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lines,cursor,expect", [
    # y/n variants, wherever they sit in the tail.
    (["Overwrite file? [y/n]"], "",
     ("prompt", ["y", "n"], "Overwrite file? [y/n]")),
    (["Apply patch (y/n)"], "",
     ("prompt", ["y", "n"], "Apply patch (y/n)")),
    (["Delete branch? [Y/n]"], "",
     ("prompt", ["y", "n"], "Delete branch? [Y/n]")),
    (["Answer yes/no to continue"], "",
     ("prompt", ["y", "n"], "Answer yes/no to continue")),
    # Question phrasing without an explicit y/n.
    (["Do you want to apply these changes"], "",
     ("prompt", ["y", "n"], "Do you want to apply these changes")),
    (["Would you like me to fix it"], "",
     ("prompt", ["y", "n"], "Would you like me to fix it")),
    (["Are you sure about that"], "",
     ("prompt", ["y", "n"], "Are you sure about that")),
    # Numbered menu with a ❯ marker — the marked line is the one quoted.
    (["Choose an option:", "  1. Yes", "❯ 2. No", "  3. Always"], "",
     ("menu", ["1", "2", "3"], "❯ 2. No")),
    # Without a marker, the first menu line stands in.
    (["pick one", "1) apply", "2) skip"], "",
     ("menu", ["1", "2"], "1) apply")),
    # Options are capped at four.
    (["1. a", "2. b", "3. c", "4. d", "5. e"], "",
     ("menu", ["1", "2", "3", "4"], "1. a")),
    # A lone numbered line is prose, not a menu.
    (["1. first do this", "then run make"], "", ("quiet", [], "")),
    # The claude-style input box, spotted on the cursor's own line.
    (["╭───────╮", "│ > ", "╰───────╯"], "│ > ",
     ("waiting", [], "│ >")),
    # A trailing question with nothing tappable is still "waiting".
    (["please tell me", "What is your name? "], "What is your name? ",
     ("waiting", [], "What is your name?")),
    # htop-ish screen: meters and an F-key bar match nothing.
    (["  1  [|||||       25%]   Tasks: 88",
      "  Mem[|||||||||  3.1G/8G]",
      "F1Help  F2Setup  F10Quit"], "F1Help  F2Setup  F10Quit",
     ("quiet", [], "")),
    # Plain scrolling output.
    (["building foo.c", "linking foo", "done."], "", ("quiet", [], "")),
    # Empty pane.
    ([], "", ("quiet", [], "")),
])
def test_detect_prompt_table(lines, cursor, expect):
    assert A.detect_prompt(lines, cursor) == expect


def test_detect_prompt_reads_only_the_last_five_nonempty_lines():
    lines = ["Do you want the old thing?", "", *[f"line {i}" for i in range(6)]]
    assert A.detect_prompt(lines, "") == ("quiet", [], "")


def test_detect_prompt_weights_the_cursor_line_first():
    # Both a stale y/n and a live input box are in the tail; the cursor's own
    # line says what is actually being asked *now* — but the y/n scan still
    # wins overall, because options beat an empty-handed "waiting".
    lines = ["old [y/n] text", "│ > "]
    kind, options, line = A.detect_prompt(lines, "│ > ")
    assert (kind, options) == ("prompt", ["y", "n"])


# ---------------------------------------------------------------------------
# The watcher state machine
# ---------------------------------------------------------------------------

def row(name, notify=False, alias="", representative=True):
    return {"name": name, "created": 0, "attached": 0, "windows": 1,
            "grouped": False, "group": "", "sid": 0, "alias": alias,
            "notify": notify, "representative": representative}


def pane(name, activity, cmd="zsh", bell=False, alt=False, active=True):
    return {"session": name, "activity": activity, "bell": bell, "alt": alt,
            "cmd": cmd, "window_active": active}


def classify_yn(name):
    return "prompt", ["y", "n"], "Continue? [y/n]"


def classify_quiet(name):
    return "quiet", [], ""


def classify_boom(name):
    raise AssertionError("classification must only run on a qualifying "
                         "busy-to-idle transition")


def kinds(events):
    return [(e["kind"], e["payload"].get("kind") or e["payload"].get("type"))
            for e in events]


def test_prompt_fires_once_per_episode_with_chips_and_push():
    rows = [row("work", notify=True, alias="My Run")]
    t = 1000
    # First sight mid-burst: baseline, never a notification.
    assert A.watch_update(rows, [pane("work", t, cmd="node")], t + 1, 0.0,
                          classify_boom) == []
    # Still working 20 s later.
    assert A.watch_update(rows, [pane("work", t + 20, cmd="node")], t + 20,
                          20.0, classify_boom) == []
    # Quiet for IDLE_S: the transition classifies once and fires both channels.
    events = A.watch_update(rows, [pane("work", t + 20, cmd="node")], t + 30,
                            30.0, classify_yn)
    ws = [e for e in events if e["kind"] == "ws"]
    push = [e for e in events if e["kind"] == "push"]
    assert ws == [{"kind": "ws", "session": "work", "payload":
                   {"type": "prompt", "options": ["y", "n"],
                    "line": "Continue? [y/n]"}}]
    assert len(push) == 1
    assert push[0]["payload"] == {
        "title": "My Run", "body": "Continue? [y/n]", "tag": "ptui-work",
        "session": "work", "kind": "waiting"}
    assert A.WATCHER["work"].state == "waiting"
    # Later idle ticks are silent — one notification per idle episode —
    # and the waiting badge holds.
    assert A.watch_update(rows, [pane("work", t + 20, cmd="node")], t + 60,
                          60.0, classify_boom) == []
    assert A.WATCHER["work"].state == "waiting"


def test_activity_resume_clears_the_chips_and_rearms():
    rows = [row("work", notify=True)]
    t = 1000
    A.watch_update(rows, [pane("work", t, cmd="node")], t + 1, 0.0, classify_boom)
    A.watch_update(rows, [pane("work", t + 20, cmd="node")], t + 20, 20.0,
                   classify_boom)
    A.watch_update(rows, [pane("work", t + 20, cmd="node")], t + 30, 30.0,
                   classify_yn)
    # The user answered; output resumes → the clear frame goes out.
    events = A.watch_update(rows, [pane("work", t + 40, cmd="node")], t + 40,
                            40.0, classify_boom)
    assert events == [{"kind": "ws", "session": "work", "payload":
                       {"type": "prompt", "options": [], "line": ""}}]
    assert A.WATCHER["work"].state == "active"
    # A second full episode fires again (the gap has passed by then).
    A.watch_update(rows, [pane("work", t + 60, cmd="node")], t + 60, 60.0,
                   classify_boom)
    events = A.watch_update(rows, [pane("work", t + 60, cmd="node")], t + 70,
                            70.0, classify_yn)
    assert ("push", "waiting") in kinds(events)


def test_min_busy_gate_blocks_short_shell_episodes():
    # `ls`: a two-second flicker of output, shell prompt back. No
    # classification, no notification.
    rows = [row("work", notify=True)]
    t = 1000
    A.watch_update(rows, [pane("work", t - 100)], t, 0.0, classify_boom)
    A.watch_update(rows, [pane("work", t + 2)], t + 3, 3.0, classify_boom)
    events = A.watch_update(rows, [pane("work", t + 2)], t + 10, 10.0,
                            classify_boom)
    assert events == []
    assert A.WATCHER["work"].state == "idle"
    assert A.WATCHER["work"].busy_started == 0.0


def test_sleep_then_echo_done_fires_one_finished():
    # The acceptance demo: `sleep 30; echo done`. The typed command is a
    # sub-MIN_BUSY burst, but sleep still holds the pane, so the episode
    # spans the quiet run and the final burst clears the gate.
    rows = [row("work", notify=True)]
    classify = classify_quiet
    A.watch_update(rows, [pane("work", 1000, cmd="zsh")], 1001, 1.0, classify_boom)
    A.watch_update(rows, [pane("work", 1000, cmd="sleep")], 1003, 3.0, classify_boom)
    # Idle transition during the sleep: gated, silent, episode kept open.
    assert A.watch_update(rows, [pane("work", 1000, cmd="sleep")], 1008, 8.0,
                          classify_boom) == []
    assert A.WATCHER["work"].busy_started != 0.0
    # done prints, the shell is back.
    A.watch_update(rows, [pane("work", 1030, cmd="zsh")], 1031, 31.0, classify_boom)
    events = A.watch_update(rows, [pane("work", 1030, cmd="zsh")], 1038, 38.0,
                            classify)
    push = [e for e in events if e["kind"] == "push"]
    assert len(push) == 1
    assert push[0]["payload"]["kind"] == "finished"
    assert push[0]["payload"]["body"] == "sleep finished"
    # And only once.
    assert A.watch_update(rows, [pane("work", 1030, cmd="zsh")], 1050, 50.0,
                          classify_boom) == []


def test_resident_agent_with_no_shape_notifies_quiet():
    rows = [row("work", notify=True)]
    t = 1000
    A.watch_update(rows, [pane("work", t, cmd="claude")], t + 1, 0.0, classify_boom)
    A.watch_update(rows, [pane("work", t + 20, cmd="claude")], t + 20, 20.0,
                   classify_boom)
    events = A.watch_update(rows, [pane("work", t + 20, cmd="claude")], t + 30,
                            30.0, classify_quiet)
    assert kinds(events) == [("push", "quiet")]
    assert events[0]["payload"]["body"] == "went quiet — may need input"


def test_notify_off_computes_state_but_dispatches_nothing():
    rows = [row("work", notify=False)]
    t = 1000
    A.watch_update(rows, [pane("work", t, cmd="node")], t + 1, 0.0, classify_boom)
    A.watch_update(rows, [pane("work", t + 20, cmd="node")], t + 20, 20.0,
                   classify_boom)
    events = A.watch_update(rows, [pane("work", t + 20, cmd="node")], t + 30,
                            30.0, classify_yn)
    # The badge and the in-app chips still happen; the push does not.
    assert [e["kind"] for e in events] == ["ws"]
    assert A.WATCHER["work"].state == "waiting"


def test_min_gap_swallows_a_second_notification_inside_30s():
    rows = [row("work", notify=True)]
    t = 1000
    A.watch_update(rows, [pane("work", t, cmd="node")], t + 1, 0.0, classify_boom)
    A.watch_update(rows, [pane("work", t + 15, cmd="node")], t + 15, 15.0,
                   classify_boom)
    events = A.watch_update(rows, [pane("work", t + 15, cmd="node")], t + 25,
                            25.0, classify_yn)
    assert ("push", "waiting") in kinds(events)
    # Answered, a real second episode (12 s of work), second question — the
    # transition lands 25 s after the first push.
    A.watch_update(rows, [pane("work", t + 30, cmd="node")], t + 30, 30.0,
                   classify_boom)
    A.watch_update(rows, [pane("work", t + 42, cmd="node")], t + 42, 42.0,
                   classify_boom)
    events = A.watch_update(rows, [pane("work", t + 42, cmd="node")], t + 50,
                            50.0, classify_yn)
    # Chips still go out; the push is inside the gap and dropped.
    assert kinds(events) == [("ws", "prompt")]


def test_bell_edge_fires_immediately_even_while_busy():
    rows = [row("work", notify=True)]
    t = 1000
    A.watch_update(rows, [pane("work", t)], t + 1, 0.0, classify_boom)
    events = A.watch_update(rows, [pane("work", t + 2, bell=True)], t + 2, 2.0,
                            classify_boom)
    assert kinds(events) == [("push", "bell")]
    # Held high, it does not fire again.
    assert A.watch_update(rows, [pane("work", t + 4, bell=True)], t + 4, 4.0,
                          classify_boom) == []


def test_non_representatives_and_vanished_sessions_are_dropped():
    rows = [row("work", notify=True),
            row("phone-work", notify=True, representative=False)]
    panes = [pane("work", 1000, cmd="node"), pane("phone-work", 1000, cmd="node")]
    A.watch_update(rows, panes, 1001, 0.0, classify_boom)
    assert set(A.WATCHER) == {"work"}
    # The session dies; its state goes with it.
    assert A.watch_update([], [], 1002, 1.0, classify_boom) == []
    assert A.WATCHER == {}


def test_watch_panes_parses_and_filters_active_panes(monkeypatch):
    out = ("1\t1\twork\t1000\t0\t0\tnode\n"
           "0\t0\twork\t900\t0\t0\tvim\n"      # inactive pane: dropped
           "1\t0\twork\t800\t1\t1\tpython\n")  # active pane, background window
    calls = []

    def tmux(*args):
        calls.append(args)
        return 0, out

    monkeypatch.setattr(A, "tmux", tmux)
    panes = A.watch_panes()
    assert calls[0][0:2] == ("list-panes", "-a")
    assert [(p["session"], p["activity"], p["cmd"], p["window_active"],
             p["bell"], p["alt"]) for p in panes] == [
        ("work", 1000, "node", True, False, False),
        ("work", 800, "python", False, True, True),
    ]


def test_watcher_aggregates_activity_and_bell_across_windows():
    # Activity is the max over the session's windows; the bell can ring in a
    # background window; the command is the active window's.
    rows = [row("work", notify=True)]
    panes = [pane("work", 900, cmd="node", active=True),
             pane("work", 1000, cmd="make", active=False, bell=False)]
    A.watch_update(rows, panes, 1001, 0.0, classify_boom)
    w = A.WATCHER["work"]
    assert w.last_activity == 1000
    assert w.cmd == "node"


# ---------------------------------------------------------------------------
# The watcher's loop plumbing
# ---------------------------------------------------------------------------

def test_watcher_runs_under_the_lifespan(monkeypatch):
    ticks = []
    monkeypatch.setattr(A, "POLL_S", 0.01)
    monkeypatch.setattr(A, "watch_tick_sync", lambda: ticks.append(1) or [])
    with TestClient(A.app):
        deadline = time.monotonic() + 3.0
        while not ticks and time.monotonic() < deadline:
            time.sleep(0.01)
    assert ticks, "the lifespan never ran a watcher tick"


def test_notify_session_views_targets_only_that_sessions_views(monkeypatch):
    a = A.Attachment(0, 0, asyncio.Queue(), "work")
    b = A.Attachment(0, 0, asyncio.Queue(), "other")
    monkeypatch.setattr(A, "ATTACHED", {"phone-work": a, "phone-other": b})
    A.notify_session_views("work", "PAYLOAD")
    assert a.out.get_nowait() == "PAYLOAD"
    assert b.out.empty()


def test_watch_tick_sync_dispatches_push_and_returns_ws(monkeypatch):
    sent = []
    monkeypatch.setattr(A, "session_rows", lambda: [])
    monkeypatch.setattr(A, "watch_panes", lambda: [])
    monkeypatch.setattr(A, "watch_update", lambda *a, **k: [
        {"kind": "push", "session": "s", "payload": {"kind": "waiting"}},
        {"kind": "ws", "session": "s", "payload": {"type": "prompt"}},
    ])
    monkeypatch.setattr(A, "dispatch_notification", lambda p: sent.append(p))
    assert A.watch_tick_sync() == [
        {"kind": "ws", "session": "s", "payload": {"type": "prompt"}}]
    assert sent == [{"kind": "waiting"}]


# ---------------------------------------------------------------------------
# Push endpoints
# ---------------------------------------------------------------------------

def test_status_degrades_when_pywebpush_is_absent(client, push_paths, monkeypatch):
    monkeypatch.setattr(A, "_webpush_module", lambda: None)
    r = client.get("/api/push/status")
    assert r.status_code == 200
    assert r.json() == {"push": False, "vapid_key": "", "subscribed": 0,
                        "ntfy": False}


def test_status_reports_key_subs_and_ntfy(client, push_paths, monkeypatch):
    monkeypatch.setattr(A, "_webpush_module", fake_module)
    monkeypatch.setattr(A, "vapid_keys",
                        lambda: {"private_key": "PRIV", "public_key": "PUB"})
    monkeypatch.setenv("POCKETTUI_NTFY_URL", "https://ntfy.example/t")
    A.save_push_subs([{"endpoint": "https://push.example/x"}])
    assert client.get("/api/push/status").json() == {
        "push": True, "vapid_key": "PUB", "subscribed": 1, "ntfy": True}


def test_subscribe_unsubscribe_round_trip(client, push_paths, monkeypatch):
    monkeypatch.setattr(A, "_webpush_module", fake_module)
    r = client.post("/api/push/subscribe",
                    json={"subscription": SUB, "dev": "phone"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    stored = A.load_push_subs()
    assert [s["endpoint"] for s in stored] == [SUB["endpoint"]]
    assert stored[0]["dev"] == "phone"
    # Same endpoint again replaces, never duplicates.
    client.post("/api/push/subscribe", json={"subscription": SUB})
    assert len(A.load_push_subs()) == 1
    r = client.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert A.load_push_subs() == []
    # Unsubscribing again is still a success.
    assert client.post("/api/push/unsubscribe",
                       json={"endpoint": SUB["endpoint"]}).status_code == 200


def test_subscribe_answers_503_without_pywebpush(client, push_paths, monkeypatch):
    monkeypatch.setattr(A, "_webpush_module", lambda: None)
    r = client.post("/api/push/subscribe", json={"subscription": SUB})
    assert r.status_code == 503
    assert r.json() == {"error": "push_unavailable"}


@pytest.mark.parametrize("sub", [
    None,
    {},
    {"endpoint": "http://not-https.example", "keys": {"p256dh": "P", "auth": "A"}},
    {"endpoint": "https://push.example/x", "keys": {"p256dh": "P"}},
    {"endpoint": "https://push.example/x"},
])
def test_subscribe_rejects_malformed_subscriptions(client, push_paths,
                                                   monkeypatch, sub):
    monkeypatch.setattr(A, "_webpush_module", fake_module)
    r = client.post("/api/push/subscribe", json={"subscription": sub})
    assert r.status_code == 400
    assert A.load_push_subs() == []


def test_subscribe_caps_the_store_evicting_the_oldest(client, push_paths,
                                                      monkeypatch):
    monkeypatch.setattr(A, "_webpush_module", fake_module)
    monkeypatch.setattr(A, "PUSH_SUBS_MAX", 3)
    monkeypatch.setattr(A, "RATE_PUSH", 100)
    for n in range(5):
        sub = {"endpoint": f"https://push.example/{n}",
               "keys": {"p256dh": "P", "auth": "A"}}
        client.post("/api/push/subscribe", json={"subscription": sub})
    assert [s["endpoint"] for s in A.load_push_subs()] == [
        "https://push.example/2", "https://push.example/3",
        "https://push.example/4"]


def test_subscribe_throttles_at_the_push_bucket(client, push_paths, monkeypatch):
    monkeypatch.setattr(A, "_webpush_module", fake_module)
    for _ in range(A.RATE_PUSH):
        assert client.post("/api/push/subscribe",
                           json={"subscription": SUB}).status_code == 200
    r = client.post("/api/push/subscribe", json={"subscription": SUB})
    assert r.status_code == 429
    assert r.json()["error"] == "rate_limited"


def test_vapid_keys_mint_once_and_persist(push_paths):
    pytest.importorskip("cryptography")
    first = A.vapid_keys()
    assert first["private_key"] and first["public_key"]
    # An uncompressed P-256 point is 65 bytes -> 87 urlsafe-b64 chars.
    assert len(first["public_key"]) == 87
    assert (A.VAPID_PATH.stat().st_mode & 0o777) == 0o600
    assert A.vapid_keys() == first


# ---------------------------------------------------------------------------
# /api/notify and the /api/sessions payload
# ---------------------------------------------------------------------------

# The renamed-base group from test_sessions.py: "work2" represents the group,
# "phone-work" is a device view, "solo" stands alone with @notify already on.
def scripted_tmux(monkeypatch):
    lines = ("work2\t100\t0\t1\t1\twork\t$0\t\t\n"
             "phone-work\t200\t0\t1\t1\twork\t$1\t\t\n"
             "solo\t300\t0\t1\t0\t\t$2\tMy Solo\ton\n")
    calls = []

    def tmux(*args):
        calls.append(args)
        if args[0] == "list-sessions":
            return 0, lines
        return 0, ""

    monkeypatch.setattr(A, "tmux", tmux)
    return calls


def test_session_rows_carry_the_notify_flag(monkeypatch):
    scripted_tmux(monkeypatch)
    flags = {r["name"]: r["notify"] for r in A.session_rows()}
    assert flags == {"work2": False, "phone-work": False, "solo": True}


def test_notify_on_sets_the_option_on_the_representative(client, monkeypatch):
    calls = scripted_tmux(monkeypatch)
    r = client.post("/api/notify", json={"session": "work2", "on": True})
    assert r.status_code == 200
    assert r.json() == {"session": "work2", "notify": True}
    assert ("set-option", "-t", "work2", "@notify", "on") in calls


def test_notify_off_unsets_the_option(client, monkeypatch):
    calls = scripted_tmux(monkeypatch)
    r = client.post("/api/notify", json={"session": "solo", "on": False})
    assert r.status_code == 200
    assert r.json() == {"session": "solo", "notify": False}
    assert ("set-option", "-u", "-t", "solo", "@notify") in calls


@pytest.mark.parametrize("target", ["phone-work", "missing"])
def test_notify_refuses_non_representatives_and_ghosts(client, monkeypatch,
                                                       target):
    calls = scripted_tmux(monkeypatch)
    r = client.post("/api/notify", json={"session": target, "on": True})
    assert r.status_code == 404
    assert not any(c[0] == "set-option" for c in calls)


def test_sessions_payload_carries_notify_state_and_activity(client, monkeypatch):
    scripted_tmux(monkeypatch)
    A.WATCHER["solo"] = A.WatchState(state="waiting", last_activity=1234)
    by_name = {s["name"]: s
               for s in client.get("/api/sessions").json()["sessions"]}
    assert by_name["solo"]["notify"] is True
    assert by_name["solo"]["state"] == "waiting"
    assert by_name["solo"]["last_activity"] == 1234
    # A session the watcher has not seen reads idle, not missing fields.
    assert by_name["work2"]["notify"] is False
    assert by_name["work2"]["state"] == "idle"
    assert by_name["work2"]["last_activity"] == 0


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

PAYLOAD = {"title": "My Run", "body": "Continue? [y/n]", "tag": "ptui-work",
           "session": "work", "kind": "waiting"}


class FakePushError(Exception):
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.response = SimpleNamespace(status_code=status)


def test_webpush_called_per_sub_and_410_prunes(push_paths, monkeypatch):
    calls = []

    def webpush(sub, data, **kw):
        calls.append((sub, data, kw))
        if sub["endpoint"].endswith("/dead"):
            raise FakePushError(410)

    monkeypatch.setattr(A, "_webpush_module", lambda: fake_module(webpush=webpush))
    monkeypatch.setattr(A, "vapid_keys",
                        lambda: {"private_key": "PRIV", "public_key": "PUB"})
    live = {"endpoint": "https://push.example/live",
            "subscription": {"endpoint": "https://push.example/live"}}
    dead = {"endpoint": "https://other.example/dead",
            "subscription": {"endpoint": "https://other.example/dead"}}
    A.save_push_subs([live, dead])

    A.send_webpush_all(PAYLOAD)

    assert len(calls) == 2
    sub, data, kw = calls[0]
    assert json.loads(data) == PAYLOAD
    assert kw["vapid_private_key"] == "PRIV"
    assert kw["ttl"] == 3600
    # aud is the endpoint's own origin, per subscription. Apple rejects a
    # mailto: sub on a non-public domain (403 BadJwtToken) and any exp
    # 24 h or more out, so the claims pin an https sub and a 12 h exp.
    claims = kw["vapid_claims"]
    assert claims["sub"] == "https://pockettui.com"
    assert claims["aud"] == "https://push.example"
    assert 0 < claims["exp"] - int(time.time()) < 24 * 3600
    assert calls[1][2]["vapid_claims"]["aud"] == "https://other.example"
    # The 410 pruned only the dead one.
    assert [s["endpoint"] for s in A.load_push_subs()] == [live["endpoint"]]


def test_webpush_transient_failure_keeps_the_subscription(push_paths, monkeypatch):
    def webpush(sub, data, **kw):
        raise FakePushError(500)

    monkeypatch.setattr(A, "_webpush_module", lambda: fake_module(webpush=webpush))
    monkeypatch.setattr(A, "vapid_keys",
                        lambda: {"private_key": "PRIV", "public_key": "PUB"})
    entry = {"endpoint": "https://push.example/x",
             "subscription": {"endpoint": "https://push.example/x"}}
    A.save_push_subs([entry])
    A.send_webpush_all(PAYLOAD)  # must not raise
    assert A.load_push_subs() == [entry]


def att(dev="phone", session="work", visible=True, age=0.0):
    """An attachment as the visibility gate sees it: `age` seconds since the
    socket last showed traffic."""
    a = A.Attachment(0, 0, asyncio.Queue(), session, dev)
    a.visible = visible
    a.last_seen = time.monotonic() - age
    return a


def test_webpush_held_for_the_visible_devices_sub_only(push_paths, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_webpush_module", lambda: fake_module(
        webpush=lambda sub, data, **kw: calls.append(sub)))
    monkeypatch.setattr(A, "vapid_keys",
                        lambda: {"private_key": "PRIV", "public_key": "PUB"})
    phone = {"endpoint": "https://push.example/phone",
             "subscription": {"endpoint": "https://push.example/phone"},
             "dev": "phone"}
    tablet = {"endpoint": "https://push.example/tablet",
              "subscription": {"endpoint": "https://push.example/tablet"},
              "dev": "tablet"}
    A.save_push_subs([phone, tablet])
    monkeypatch.setattr(A, "ATTACHED", {"phone-work": att("phone")})

    A.send_webpush_all(PAYLOAD)

    # The visible phone's send is held; the tablet, not in the app, gets it.
    assert [s["endpoint"] for s in calls] == [tablet["endpoint"]]
    # Held is not pruned: the phone's subscription survives untouched.
    assert [s["endpoint"] for s in A.load_push_subs()] == [
        phone["endpoint"], tablet["endpoint"]]


@pytest.mark.parametrize("shape", ["no-client", "hidden", "stale"])
def test_webpush_sends_unless_a_live_visible_client_holds_it(
        push_paths, monkeypatch, shape):
    attached = {
        # The visible client disconnected — its state died with the socket.
        "no-client": {},
        # Attached but backgrounded: attachment alone never suppresses.
        "hidden": {"phone-work": att("phone", visible=False)},
        # visible=true from a socket silent past the bound is not believed.
        "stale": {"phone-work": att("phone", age=A.VISIBLE_STALE_S + 5)},
    }[shape]
    calls = []
    monkeypatch.setattr(A, "_webpush_module", lambda: fake_module(
        webpush=lambda sub, data, **kw: calls.append(sub)))
    monkeypatch.setattr(A, "vapid_keys",
                        lambda: {"private_key": "PRIV", "public_key": "PUB"})
    A.save_push_subs([{"endpoint": "https://push.example/phone",
                       "subscription": {"endpoint": "https://push.example/phone"},
                       "dev": "phone"}])
    monkeypatch.setattr(A, "ATTACHED", attached)
    A.send_webpush_all(PAYLOAD)
    assert len(calls) == 1


def test_ntfy_posts_title_headers_and_body(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        seen["body"] = req.data
        seen["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("POCKETTUI_NTFY_URL", "https://ntfy.example/pockettui")
    monkeypatch.setenv("POCKETTUI_APP_URL", "https://pockettui.example/app/")
    A.send_ntfy(PAYLOAD)
    assert seen["url"] == "https://ntfy.example/pockettui"
    assert seen["body"] == "My Run: Continue? [y/n]".encode()
    assert seen["timeout"] == 5
    assert seen["headers"]["Title"] == "My Run"
    assert seen["headers"]["Tags"] == "bell"
    assert seen["headers"]["Click"] == "https://pockettui.example/app/#session=work"


def test_ntfy_does_nothing_without_the_env(monkeypatch):
    def urlopen(*a, **k):
        raise AssertionError("no POCKETTUI_NTFY_URL, no request")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.delenv("POCKETTUI_NTFY_URL", raising=False)
    A.send_ntfy(PAYLOAD)


def test_ntfy_held_while_any_client_is_visible(monkeypatch):
    # ntfy is one topic with no device identity, so the hold is global.
    def urlopen(*a, **k):
        raise AssertionError("a visible client must hold the ntfy send")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("POCKETTUI_NTFY_URL", "https://ntfy.example/t")
    monkeypatch.setattr(A, "ATTACHED", {"phone-work": att("phone")})
    A.send_ntfy(PAYLOAD)


def test_ntfy_failure_is_swallowed(monkeypatch):
    def urlopen(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("POCKETTUI_NTFY_URL", "https://ntfy.example/t")
    A.send_ntfy(PAYLOAD)  # must not raise


def test_dispatch_survives_a_broken_transport(monkeypatch):
    def boom(payload):
        raise RuntimeError("transport bug")

    sent = []
    monkeypatch.setattr(A, "send_webpush_all", boom)
    monkeypatch.setattr(A, "send_ntfy", lambda p: sent.append(p))
    A.dispatch_notification(PAYLOAD)  # webpush exploding must not stop ntfy
    assert sent == [PAYLOAD]


# ---------------------------------------------------------------------------
# Boot without pywebpush
# ---------------------------------------------------------------------------

def test_app_imports_and_degrades_without_pywebpush():
    """The real guarantee, not a monkeypatched one: with the import machinery
    refusing pywebpush outright, app.py still imports and answers push:false."""
    code = f"""
import sys
from importlib.abc import MetaPathFinder

class Block(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "pywebpush":
            raise ImportError("blocked for the test")
        return None

sys.meta_path.insert(0, Block())
sys.path.insert(0, {str(REPO)!r})
import app
assert app.push_available() is False
assert app._webpush_module() is None
print("degraded-ok")
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "degraded-ok" in out.stdout


# ---------------------------------------------------------------------------
# Integration: the real tmux, on an isolated server
# ---------------------------------------------------------------------------

TMUX_TEST_BIN = ["tmux", "-L", "pockettui-test", "-f", "/dev/null"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_notify_option_round_trips_through_session_rows(client, monkeypatch):
    monkeypatch.setattr(A, "TMUX_BIN", list(TMUX_TEST_BIN))
    try:
        rc, _ = A.tmux("new-session", "-d", "-s", "nwork")
        assert rc == 0
        r = client.post("/api/notify", json={"session": "nwork", "on": True})
        assert r.status_code == 200
        row = A.find_row(A.session_rows(), "nwork")
        assert row is not None and row["notify"] is True
        r = client.post("/api/notify", json={"session": "nwork", "on": False})
        assert r.status_code == 200
        row = A.find_row(A.session_rows(), "nwork")
        assert row is not None and row["notify"] is False
        # And the watcher's pane poll parses real output for the session.
        panes = [p for p in A.watch_panes() if p["session"] == "nwork"]
        assert len(panes) == 1
        assert panes[0]["activity"] > 0
        assert panes[0]["window_active"] is True
    finally:
        subprocess.run([*TMUX_TEST_BIN, "kill-server"], capture_output=True,
                       timeout=10)
