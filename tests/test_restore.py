"""Session restore: the record the watcher keeps of what is open under which
tmux server, the offers a lost server leaves behind, the routes that restore
or dismiss them, and the restored pane that prints its saved output.

Runs against a scripted tmux (FakeTmux from test_sessions, grown the commands
restore issues) with RESTORE_DIR in a temp dir (conftest), so it proves the
server's own logic on any machine.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as A  # noqa: E402
from test_sessions import FakeTmux  # noqa: E402

TOKEN = "ABCDEFGHIJ"


class RestoreTmux(FakeTmux):
    """FakeTmux plus a server identity, panes with a folder and a command,
    and scrollback for capture-pane. `server` None is no server at all."""

    def __init__(self, sessions=(), server="100:1700000000"):
        super().__init__(sessions)
        self.server = server

    def __call__(self, *args):
        cmd = args[0]
        if self.server is None:
            self.calls.append(args)
            return 1, ""
        if cmd == "display-message":
            self.calls.append(args)
            pid, start = self.server.split(":")
            return 0, f"{pid}\t{start}\n"
        if cmd == "list-panes" and "-a" in args:
            self.calls.append(args)
            return 0, "".join(
                f"1\t1\t{s['name']}\t{s.get('activity', 0)}\t0\t0"
                f"\t{s.get('cmd', 'zsh')}"
                f"\t%{s['sid']}\t{s.get('cwd', '/tmp')}\t{s.get('pid', '')}\n"
                for s in self.sessions)
        if cmd == "list-panes":
            self.calls.append(args)
            s = self.find(args[args.index("-t") + 1].lstrip("="))
            if s is None:
                return 1, ""
            return 0, f"1\t%{s['sid']}\t{'1' if s.get('alt') else '0'}\n"
        if cmd == "show-option" and args[1:] == ("-gv", "default-shell"):
            self.calls.append(args)
            return 0, "/usr/bin/zsh\n"
        if cmd == "capture-pane":
            self.calls.append(args)
            pane = args[args.index("-t") + 1]
            for s in self.sessions:
                if f"%{s['sid']}" == pane:
                    if s.get("alt") and "-S" not in args:
                        return 0, s.get("screen", "")   # the visible TUI
                    return 0, s.get("hist", "")
            return 1, ""
        return super().__call__(*args)


@pytest.fixture
def client():
    return TestClient(A.app)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(A, "LIMITER", A.AuthLimiter())
    monkeypatch.setattr(A, "RATE", A.RateLimiter())
    monkeypatch.setattr(A, "WATCHER", {})
    monkeypatch.setattr(A, "install_title_hook", lambda: None)


def fake(monkeypatch, sessions=(), server="100:1700000000"):
    t = RestoreTmux(sessions, server)
    monkeypatch.setattr(A, "tmux", t)
    return t


def tick(now=1000.0):
    A.restore_track(A.session_rows(), A.watch_panes(), now)


def live():
    return json.loads((A.RESTORE_DIR / "live.json").read_text())


def pending():
    return json.loads((A.RESTORE_DIR / "pending.json").read_text())


def names(rows):
    return [r["name"] for r in rows]


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

def test_the_live_set_is_recorded_without_views_or_the_update_session(monkeypatch):
    fake(monkeypatch, [
        {"name": "work", "alias": "Work", "notify": "quiet", "order": 2,
         "cwd": "/srv/work", "cmd": "claude", "group": "work"},
        {"name": "phone-work", "group": "work", "view": True},
        {"name": A.UPDATE_SESSION},
    ])
    tick()
    rec = live()
    assert rec["server"] == "100:1700000000"
    assert rec["sessions"] == [{
        "name": "work", "sid": 0, "alias": "Work", "notify": "quiet",
        "order": 2, "cwd": "/srv/work", "command": "claude", "resume": "",
        "last_cmd": "", "saved": 0}]


CLAUDE_ID = "c9f76818-0000-4000-8000-00000000abcd"
CODEX_ID = "019a1b2c-3d4e-7f00-8a9b-0c1d2e3f4a5b"


def claude_home(tmp_path, pid, sid=CLAUDE_ID):
    d = tmp_path / ".claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "sessionId": sid, "cwd": "/srv/work"}))
    return tmp_path


def codex_files(pid):
    return {610: ["/dev/pts/3", "/home/u/.codex/log/codex-tui.log",
                  f"/home/u/.codex/sessions/2026/11/07/"
                  f"rollout-2026-11-07T10-11-12-{CODEX_ID}.jsonl"]}.get(pid, [])


# zsh 100 -> claude 110; zsh 200 -> node 210 (npm's wrapper) -> codex 610;
# zsh 300 -> python3 310.
TREE = {100: [(110, "claude")], 200: [(210, "MainThread")], 210: [(610, "codex")],
        300: [(310, "python3")]}


def test_resume_is_the_exact_conversation_under_the_pane(tmp_path):
    home = claude_home(tmp_path, 110)
    assert A.restore_resume_for("100", TREE, home, codex_files) == \
        f"claude --resume {CLAUDE_ID}"
    assert A.restore_resume_for("200", TREE, home, codex_files) == \
        f"codex resume {CODEX_ID}"
    assert A.restore_resume_for("300", TREE, home, codex_files) == ""
    assert A.restore_resume_for("", TREE, home, codex_files) == ""


def test_codex_with_several_rollouts_open_takes_the_one_written_last(tmp_path):
    old, new = (tmp_path / f"rollout-2026-11-07T10-00-0{i}-{CODEX_ID[:-1]}{i}.jsonl"
                for i in (1, 2))
    old.write_text("")
    new.write_text("")
    os.utime(old, (2000, 2000))
    os.utime(new, (1000, 1000))
    files = lambda pid: [str(new), str(old)]
    assert A.restore_session_id("codex", 610, tmp_path, files) == CODEX_ID[:-1] + "1"


def test_resume_falls_back_to_the_latest_when_no_id_is_found(tmp_path):
    assert A.restore_resume_for("100", TREE, tmp_path, lambda pid: []) == \
        "claude --continue"
    assert A.restore_resume_for("200", TREE, tmp_path, lambda pid: []) == \
        "codex resume --last"
    # An id that is not a uuid is never typed at a prompt.
    home = claude_home(tmp_path, 110, sid="x; rm -rf ~")
    assert A.restore_resume_for("100", TREE, home, lambda pid: []) == \
        "claude --continue"


REAL_PROC_INFO = A.restore_proc_info


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch):
    """The fake pids in TREE are never looked up on this machine."""
    monkeypatch.setattr(A, "restore_proc_info", lambda pid: ("", None))
    monkeypatch.setattr(A, "restore_cmdline", lambda pid: "")


CODEX_NODE = "node /home/u/.nvm/versions/node/v24/bin/codex"
CODEX_BIN = "/home/u/.nvm/lib/node_modules/@openai/codex/bin/codex"


def procs(table):
    return lambda pid: table.get(pid, ("", None))


def test_codex_started_on_a_conversation_is_read_from_its_arguments(tmp_path):
    # The wrapper carries `resume <id>` as our restore types it.
    info = procs({210: (f"{CODEX_NODE} resume {CODEX_ID}", None),
                  610: (CODEX_BIN, None)})
    assert A.restore_resume_for("200", TREE, tmp_path, lambda pid: [],
                                "/srv/work", info) == f"codex resume {CODEX_ID}"
    info = procs({610: (f"{CODEX_BIN} --session-id={CODEX_ID} -m o3", None)})
    assert A.restore_session_id("codex", 610, tmp_path, lambda pid: [],
                                proc_info=info) == CODEX_ID
    # Not a uuid: no id from the arguments, and no rollout to fall back to.
    info = procs({210: (f"{CODEX_NODE} resume --last", None),
                  610: (f"{CODEX_BIN} resume x;rm", None)})
    assert A.restore_resume_for("200", TREE, tmp_path, lambda pid: [],
                                "/srv/work", info) == "codex resume --last"


def rollout(home, local_ts, sid, cwd, meta_utc):
    day = home / ".codex" / "sessions" / local_ts[:10].replace("-", "/")
    day.mkdir(parents=True, exist_ok=True)
    f = day / f"rollout-{local_ts}-{sid}.jsonl"
    f.write_text(json.dumps({"timestamp": meta_utc, "type": "session_meta",
                             "payload": {"id": sid, "timestamp": meta_utc,
                                         "cwd": cwd, "originator": "codex-tui",
                                         "base_instructions": "x" * 30000}})
                 + "\n" + json.dumps({"type": "event"}) + "\n")
    return f


def epoch(utc):
    import calendar
    return calendar.timegm(time.strptime(utc, "%Y-%m-%dT%H:%M:%S"))


def test_codex_tui_is_matched_to_the_rollout_it_began(monkeypatch, tmp_path):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        first = CODEX_ID[:-1] + "1"
        second = CODEX_ID[:-1] + "2"
        other = CODEX_ID[:-1] + "3"
        rollout(tmp_path, "2026-11-07T08-10-13", first, "/srv/work",
                "2026-11-07T08:10:13.612Z")
        rollout(tmp_path, "2026-11-07T08-15-14", second, "/srv/work/",
                "2026-11-07T08:15:14.100Z")
        # Another folder, a second after the start: never this pane's.
        rollout(tmp_path, "2026-11-07T08-15-13", other, "/srv/else",
                "2026-11-07T08:15:13.000Z")
        start = epoch("2026-11-07T08:15:12")
        info = procs({610: (CODEX_BIN, start), 210: (CODEX_NODE, start)})
        assert A.restore_resume_for("200", TREE, tmp_path, lambda pid: [],
                                    "/srv/work", info) == f"codex resume {second}"
        assert A.restore_codex_meta_id(tmp_path, "/srv/work",
                                       epoch("2026-11-07T08:10:12")) == first
        # A conversation begun two minutes after the start is not this one.
        assert A.restore_codex_meta_id(tmp_path, "/srv/work",
                                       epoch("2026-11-07T08:13:14")) == ""
        assert A.restore_codex_meta_id(tmp_path, "/srv/else",
                                       epoch("2026-11-07T08:10:12")) == ""
        # No match: the open rollout, then the folder's latest.
        info = procs({610: (CODEX_BIN, epoch("2026-11-07T09:00:00"))})
        assert A.restore_resume_for("200", TREE, tmp_path, codex_files,
                                    "/srv/work", info) == f"codex resume {CODEX_ID}"
        assert A.restore_resume_for("200", TREE, tmp_path, lambda pid: [],
                                    "/srv/work", info) == "codex resume --last"
        # Without a folder or a start time no rollout is matched.
        info = procs({610: (CODEX_BIN, start)})
        assert A.restore_resume_for("200", TREE, tmp_path, lambda pid: [],
                                    "", info) == "codex resume --last"
        info = procs({610: (CODEX_BIN, None)})
        assert A.restore_resume_for("200", TREE, tmp_path, lambda pid: [],
                                    "/srv/work", info) == "codex resume --last"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_the_process_start_parses_etimes_and_etime(monkeypatch):
    assert A._restore_etime("05:07") == 307
    assert A._restore_etime("01:05:07") == 3907
    assert A._restore_etime("2-01:05:07") == 2 * 86400 + 3907
    assert A._restore_etime("") is None
    answers = {"etimes": "", "etime": "1-00:00:10", "args": "codex"}
    monkeypatch.setattr(A, "_restore_ps", lambda pid, f: answers[f])
    args, started = REAL_PROC_INFO(999999999)
    assert args == "codex"
    assert abs(time.time() - 86410 - started) < 2


def test_the_process_tree_parses_ps(monkeypatch):
    out = "  1     0 systemd\n 100     1 /bin/zsh\n 110   100 claude\nbad line\n"
    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, out, ""))
    assert A.restore_process_tree() == {
        0: [(1, "systemd")], 1: [(100, "zsh")], 100: [(110, "claude")]}


def test_the_record_carries_the_resume_command(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(claude_home(tmp_path, 110)))
    t = fake(monkeypatch, [{"name": "cc", "cmd": "claude", "pid": "100"},
                           {"name": "sh", "sid": 1, "pid": "300"}])
    trees = []
    monkeypatch.setattr(A, "restore_process_tree",
                        lambda: trees.append(1) or TREE)
    tick(1000)
    assert {r["name"]: r["resume"] for r in live()["sessions"]} == {
        "cc": f"claude --resume {CLAUDE_ID}", "sh": ""}
    # One ps for the tick, and none on a tick with nothing due or changed.
    assert trees == [1]
    tick(1002)
    assert trees == [1]
    # A program change is looked up again at once.
    t.sessions[1]["cmd"] = "python3"
    tick(1004)
    assert trees == [1, 1]


def test_a_new_server_moves_every_live_row_to_the_offers(monkeypatch):
    t = fake(monkeypatch, [{"name": "a", "hist": "alpha\n"},
                           {"name": "b", "hist": "beta\n"}])
    tick(1000)
    assert A.restore_hist("a").read_text() == "alpha"
    # The machine rebooted: a new server, holding a session of its own.
    t.sessions = [{"name": "c", "sid": 7}]
    t.server = "200:1700000100"
    tick(2000)
    offers = pending()["sessions"]
    assert names(offers) == ["a", "b"]
    assert all(r["lost"] == 2000 for r in offers)
    # The scrollback went with the offer, out of the way of a new "a".
    assert not A.restore_hist("a").exists()
    assert A.restore_hist("a", "lost").read_text() == "alpha"
    rec = live()
    assert rec["server"] == "200:1700000100"
    assert names(rec["sessions"]) == ["c"]
    assert A.restore_count() == 2


def test_no_server_counts_as_lost_only_on_the_second_tick(monkeypatch):
    t = fake(monkeypatch, [{"name": "a"}])
    tick(1000)
    t.server = None
    t.sessions = []
    tick(1002)
    assert A.restore_count() == 0
    assert names(live()["sessions"]) == ["a"]
    tick(1004)
    assert A.restore_count() == 1
    assert live() == {"server": None, "sessions": []}


def test_a_failed_listing_is_not_a_loss_or_an_end(monkeypatch):
    fake(monkeypatch, [{"name": "a"}])
    tick(1000)
    # The identity answers but the listing came back empty: a tmux hiccup.
    A.restore_track([], [], 1002)
    assert names(live()["sessions"]) == ["a"]
    assert A.restore_count() == 0


def test_the_newer_loss_replaces_an_older_offer_of_the_same_name(monkeypatch):
    t = fake(monkeypatch, [{"name": "a", "hist": "old\n", "cwd": "/old"}])
    tick(1000)
    t.server = "200:2"
    t.sessions = [{"name": "a", "sid": 5, "hist": "new\n", "cwd": "/new"}]
    tick(2000)
    t.server = "300:3"
    t.sessions = [{"name": "z", "sid": 9}]
    tick(3000)
    offers = pending()["sessions"]
    assert [(r["name"], r["cwd"], r["lost"]) for r in offers] == [("a", "/new", 3000)]
    assert A.restore_hist("a", "lost").read_text() == "new"


def test_a_session_gone_from_the_same_server_ended_on_purpose(monkeypatch):
    t = fake(monkeypatch, [{"name": "a", "hist": "x\n"}, {"name": "b"}])
    tick(1000)
    assert A.restore_hist("a").exists()
    t.sessions = [s for s in t.sessions if s["name"] != "a"]
    tick(1002)
    assert names(live()["sessions"]) == ["b"]
    assert not A.restore_hist("a").exists()
    assert A.restore_count() == 0


def test_the_kill_route_forgets_the_session_at_once(monkeypatch, client):
    fake(monkeypatch, [{"name": "a", "hist": "x\n"}, {"name": "b"}])
    tick(1000)
    r = client.post("/api/session/kill", json={"session": "a"})
    assert r.status_code == 200
    assert names(live()["sessions"]) == ["b"]
    assert not A.restore_hist("a").exists()


def test_the_rename_route_carries_the_row_and_its_scrollback(monkeypatch, client):
    fake(monkeypatch, [{"name": "a", "hist": "x\n"}])
    tick(1000)
    r = client.post("/api/session/rename", json={"session": "a", "name": "b"})
    assert r.status_code == 200
    assert names(live()["sessions"]) == ["b"]
    assert A.restore_hist("b").read_text() == "x"
    assert not A.restore_hist("a").exists()


def test_the_record_is_written_only_when_it_changes(monkeypatch):
    fake(monkeypatch, [{"name": "a"}])
    tick(1000)
    p = A.RESTORE_DIR / "live.json"
    os.utime(p, (1, 1))
    tick(1002)
    assert p.stat().st_mtime == 1


# ---------------------------------------------------------------------------
# Scrollback saves
# ---------------------------------------------------------------------------

def captures(t):
    return sum(1 for c in t.calls if c[0] == "capture-pane")


def test_scrollback_is_saved_on_first_sight(monkeypatch):
    t = fake(monkeypatch, [{"name": "a", "hist": "one\n", "activity": 500}])
    tick(1000)
    assert captures(t) == 1
    assert live()["sessions"][0]["saved"] == 1000
    assert A.restore_hist("a").read_text() == "one"
    # The capture keeps the screen: no -E -1.
    cap = [c for c in t.calls if c[0] == "capture-pane"][-1]
    assert "-E" not in cap and "-e" in cap and "-J" in cap


def test_no_save_on_ticks_where_the_activity_stamp_did_not_move(monkeypatch):
    t = fake(monkeypatch, [{"name": "a", "hist": "one\n", "activity": 500}])
    tick(1000)
    for now in range(1002, 1300, 2):
        tick(now)
    assert captures(t) == 1


def test_a_burst_is_saved_on_the_first_quiet_tick_after_it(monkeypatch):
    t = fake(monkeypatch, [{"name": "a", "hist": "one\n", "activity": 500}])
    tick(1000)
    t.sessions[0].update(activity=1001, hist="one\ngit log\n")
    tick(1002)                                  # still moving: no capture
    assert captures(t) == 1
    t.sessions[0].update(activity=1003, hist="one\ngit log\npage\n")
    tick(1004)                                  # moved again
    assert captures(t) == 1
    tick(1006)                                  # held still for a tick
    assert captures(t) == 2
    assert A.restore_hist("a").read_text() == "one\ngit log\npage"
    assert live()["sessions"][0]["saved"] == 1006
    tick(1008)                                  # nothing new since the save
    tick(1010)
    assert captures(t) == 2
    # The watcher's state plays no part in the schedule.
    A.WATCHER["a"] = A.WatchState(state="active")
    tick(1012)
    assert captures(t) == 2


def test_output_in_the_second_of_a_save_is_saved_again(monkeypatch):
    t = fake(monkeypatch, [{"name": "a", "hist": "one\n", "activity": 1000}])
    tick(1000.2)                                # first sight, same second
    t.sessions[0]["hist"] = "one\ntwo\n"         # later in second 1000
    tick(1002.2)                                # stamp unchanged, not covered
    assert captures(t) == 2
    assert A.restore_hist("a").read_text() == "one\ntwo"
    tick(1004.2)                                # covered now
    assert captures(t) == 2


def test_a_stamp_that_keeps_moving_is_saved_every_minute(monkeypatch):
    t = fake(monkeypatch, [{"name": "a", "hist": "x\n", "activity": 500}])
    tick(1000)
    for now in range(1002, 1060, 2):
        t.sessions[0]["activity"] = now - 1
        tick(now)
    assert captures(t) == 1
    t.sessions[0]["activity"] = 1059
    tick(1060)                                  # moving for a minute
    assert captures(t) == 2
    t.sessions[0]["activity"] = 1061
    tick(1062)                                  # still moving, saved 2 s ago
    assert captures(t) == 2
    tick(1064)                                  # the stream ended
    assert captures(t) == 3


def test_an_empty_capture_never_overwrites_saved_scrollback(monkeypatch):
    t = fake(monkeypatch, [{"name": "a", "hist": "kept\n"}])
    tick(1000)
    t.sessions[0]["alt"] = True                 # a blank alternate screen
    t.sessions[0]["screen"] = "\n\n"
    assert A.restore_save("a", 2000) is False
    t.sessions[0]["alt"] = False
    t.sessions[0]["hist"] = "\n\n"
    assert A.restore_save("a", 2000) is False
    assert A.restore_hist("a").read_text() == "kept"
    assert live()["sessions"][0]["saved"] == 1000


def test_a_tui_on_the_alternate_screen_saves_its_visible_frame(monkeypatch):
    t = fake(monkeypatch, [{"name": "a", "alt": True, "hist": "shell\n",
                            "screen": "> fix the bug\n\x1b[1mDone.\x1b[0m\n"}])
    tick(1000)
    assert A.restore_hist("a").read_text() == "> fix the bug\n\x1b[1mDone.\x1b[0m"
    assert live()["sessions"][0]["saved"] == 1000
    # The visible screen only: no -S/-E, and not -a (the hidden primary grid).
    cap = [c for c in t.calls if c[0] == "capture-pane"][-1]
    assert cap == ("capture-pane", "-p", "-e", "-J", "-t", "%0")
    # The reconnect replay still answers "" for the alternate screen, and
    # never captures at all.
    before = captures(t)
    assert A.capture_history("a") == ""
    assert captures(t) == before


def test_the_shutdown_save_skips_a_tmux_that_is_not_answering(monkeypatch):
    t = fake(monkeypatch, [{"name": "a", "hist": "x\n"}])
    tick(1000)
    t.server = None
    before = captures(t)
    A.restore_save_all()
    assert captures(t) == before
    t.server = "100:1700000000"
    t.sessions[0]["hist"] = "y\n"
    A.restore_save_all()
    assert A.restore_hist("a").read_text() == "y"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def lose(monkeypatch, sessions):
    """Record `sessions` under one server, then switch to another holding
    only "other", and return the fake."""
    t = fake(monkeypatch, sessions)
    tick(1000)
    t.server = "200:2"
    t.sessions = [{"name": "other", "sid": 99}]
    tick(2000)
    t.calls.clear()
    return t


def test_get_lists_the_offers_with_lines_and_folder(monkeypatch, client, tmp_path):
    here = tmp_path / "proj"
    here.mkdir()
    lose(monkeypatch, [
        {"name": "a", "alias": "Alpha", "cwd": str(here), "cmd": "claude",
         "hist": "1\n2\n3\n"},
        {"name": "b", "cwd": str(tmp_path / "gone")},
    ])
    r = client.get("/api/restore")
    assert r.status_code == 200
    assert r.json() == {"sessions": [
        {"name": "a", "alias": "Alpha", "cwd": str(here), "cwd_exists": True,
         "command": "claude", "resume": "claude --continue",
         "resumes": "Claude Code", "last_cmd": "", "lost": 2000, "lines": 3},
        {"name": "b", "alias": "", "cwd": str(tmp_path / "gone"),
         "cwd_exists": False, "command": "zsh", "resume": "", "resumes": "",
         "last_cmd": "", "lost": 2000, "lines": 0},
    ]}


def test_sessions_payload_carries_the_offer_count(monkeypatch, client, tmp_path):
    lose(monkeypatch, [{"name": "a"}, {"name": "b"}])
    assert client.get("/api/sessions").json()["restorable"] == 2


def test_post_restores_with_the_old_folder_options_and_scrollback(
        monkeypatch, client, tmp_path):
    here = tmp_path / "proj"
    here.mkdir()
    t = lose(monkeypatch, [
        {"name": "a", "alias": "Alpha", "notify": "on", "order": 4,
         "cwd": str(here), "cmd": "claude", "hist": "saved\n"},
        {"name": "b", "cwd": str(tmp_path / "gone"), "cmd": "python3"},
    ])
    r = client.post("/api/restore", json={"names": ["a", "b"]})
    assert r.status_code == 200
    home = os.path.expanduser("~")
    assert r.json() == {"restored": [{"name": "a", "cwd": str(here)},
                                     {"name": "b", "cwd": home}],
                        "errors": []}
    made = [c for c in t.calls if c[0] == "new-session"]
    # a had output saved: its pane prints it, then becomes tmux's
    # default-shell as a login shell. b had none and starts as before.
    printed = A.restore_hist("a", "print")
    assert made == [
        ("new-session", "-d", "-e", f"ZDOTDIR={home}", "-s", "a", "-c", str(here),
         "--", "/bin/sh", "-c", 'cat -- "$1"; rm -f -- "$1"; exec "$2" -l',
         "sh", str(printed), "/usr/bin/zsh"),
        ("new-session", "-d", "-e", f"ZDOTDIR={home}", "-s", "b", "-c", home)]
    # The saved output closed by the dim separator, for the pane to print.
    text = printed.read_text()
    assert text.startswith("saved\n\x1b[0m\x1b[2m── restored ")
    assert text.endswith(", earlier output above ──\x1b[0m\n")
    assert not A.restore_hist("b", "print").exists()
    opts = [c for c in t.calls if c[0] == "set-option"]
    assert opts == [("set-option", "-t", "a", "@alias", "Alpha"),
                    ("set-option", "-t", "a", "@notify", "on"),
                    ("set-option", "-t", "a", "@ptui_order", "4")]
    # Typed, then run; b's python3 resumes nothing.
    typed = [c for c in t.calls if c[0] == "send-keys"]
    assert typed == [("send-keys", "-t", "=a:", "-l", "--", "claude --continue"),
                     ("send-keys", "-t", "=a:", "Enter")]
    assert A.restore_count() == 0
    assert pending() == {"sessions": []}
    assert not A.restore_hist("a", "lost").exists()


def test_post_restores_without_the_zdotdir_pin_on_tmux_before_3_2(
        monkeypatch, client, tmp_path):
    # tmux 3.0a has no new-session -e and rejects the whole command over it.
    monkeypatch.setattr(A, "_TMUX_VERSION", {tuple(A.TMUX_BIN): (3, 0)})
    t = lose(monkeypatch, [{"name": "b", "cwd": str(tmp_path / "gone"),
                            "cmd": "python3"}])
    assert client.post("/api/restore", json={"names": ["b"]}).status_code == 200
    home = os.path.expanduser("~")
    assert [c for c in t.calls if c[0] == "new-session"] == [
        ("new-session", "-d", "-s", "b", "-c", home)]


def test_post_runs_the_recorded_exact_command(monkeypatch, client, tmp_path):
    monkeypatch.setenv("HOME", str(claude_home(tmp_path, 110)))
    monkeypatch.setattr(A, "restore_process_tree", lambda: TREE)
    t = lose(monkeypatch, [{"name": "cc", "cmd": "claude", "pid": "100"}])
    offer = client.get("/api/restore").json()["sessions"][0]
    assert (offer["resume"], offer["resumes"]) == (
        f"claude --resume {CLAUDE_ID}", "Claude Code")
    client.post("/api/restore", json={"names": ["cc"]})
    typed = [c for c in t.calls if c[0] == "send-keys"]
    assert typed == [
        ("send-keys", "-t", "=cc:", "-l", "--", f"claude --resume {CLAUDE_ID}"),
        ("send-keys", "-t", "=cc:", "Enter")]


def test_post_resumes_codex_and_runs_nothing_in_a_plain_shell(
        monkeypatch, client, tmp_path):
    t = lose(monkeypatch, [{"name": "cx", "cmd": "codex"},
                           {"name": "sh", "sid": 1, "cmd": "zsh"}])
    offers = client.get("/api/restore").json()["sessions"]
    assert [(o["resume"], o["resumes"]) for o in offers] == [
        ("codex resume --last", "Codex"), ("", "")]
    r = client.post("/api/restore", json={"names": ["cx", "sh"]})
    assert [x["name"] for x in r.json()["restored"]] == ["cx", "sh"]
    typed = [c for c in t.calls if c[0] == "send-keys"]
    assert typed == [("send-keys", "-t", "=cx:", "-l", "--", "codex resume --last"),
                     ("send-keys", "-t", "=cx:", "Enter")]


def test_post_types_the_last_command_line_without_running_it(
        monkeypatch, client, tmp_path):
    # zsh 400 -> git 410 (`git log`, its pager below it); zsh 500 at the prompt.
    tree = {400: [(410, "git")], 410: [(420, "less")]}
    lines = {410: "git log --oneline 'a b'"}
    monkeypatch.setattr(A, "restore_process_tree", lambda: tree)
    monkeypatch.setattr(A, "restore_cmdline", lambda pid: lines.get(pid, ""))
    t = lose(monkeypatch, [{"name": "lg", "cmd": "git", "pid": "400"},
                           {"name": "sh", "sid": 1, "cmd": "zsh", "pid": "500"}])
    offers = client.get("/api/restore").json()["sessions"]
    assert [(o["resume"], o["last_cmd"]) for o in offers] == [
        ("", "git log --oneline 'a b'"), ("", "")]
    client.post("/api/restore", json={"names": ["lg", "sh"]})
    # Typed at the prompt, no Enter: running it again is the user's call.
    typed = [c for c in t.calls if c[0] == "send-keys"]
    assert typed == [("send-keys", "-t", "=lg:", "-l", "--",
                      "git log --oneline 'a b'")]


def test_the_last_command_line_is_the_program_in_front_of_the_shell():
    tree = {100: [(110, "git")], 200: [(210, "sleep"), (220, "vim")],
            300: [(310, "sleep"), (320, "make")], 400: [(410, "bash")]}
    lines = {110: "git log", 220: "vim notes.md", 410: "bash",
             320: "make\nrm -rf x"}.get
    assert A.restore_last_cmd("100", tree, "git", lines) == "git log"
    # Named as tmux names the pane's program, past a background job.
    assert A.restore_last_cmd("200", tree, "vim", lines) == "vim notes.md"
    # Two children and none named so: nothing rather than a guess.
    assert A.restore_last_cmd("300", tree, "python3", lines) == ""
    # A control character would run the line when typed.
    assert A.restore_last_cmd("300", tree, "make", lines) == ""
    # A shell in front records nothing.
    for shell in ("zsh", "bash", "fish", "sh"):
        assert A.restore_last_cmd("400", tree, shell, lines) == ""
    assert A.restore_last_cmd("", tree, "git", lines) == ""


def test_the_record_carries_the_last_command_line(monkeypatch):
    monkeypatch.setattr(A, "restore_process_tree", lambda: {400: [(410, "git")]})
    monkeypatch.setattr(A, "restore_cmdline", lambda pid: "git log")
    t = fake(monkeypatch, [{"name": "lg", "cmd": "git", "pid": "400"}])
    tick(1000)
    assert live()["sessions"][0]["last_cmd"] == "git log"
    # Back at the prompt: the program changed, and a shell records nothing.
    t.sessions[0]["cmd"] = "zsh"
    tick(1002)
    assert live()["sessions"][0]["last_cmd"] == ""


def test_post_refuses_a_name_in_use_and_keeps_the_offer(monkeypatch, client, tmp_path):
    t = lose(monkeypatch, [{"name": "other", "cmd": "claude"}])
    r = client.post("/api/restore", json={"names": ["other", "nope"]})
    assert r.json() == {"restored": [], "errors": [
        {"name": "other", "error": "name in use"},
        {"name": "nope", "error": "nothing to restore"}]}
    assert not [c for c in t.calls if c[0] in ("new-session", "send-keys")]
    assert A.restore_count() == 1


def test_post_rejects_a_body_without_names(monkeypatch, client):
    fake(monkeypatch)
    assert client.post("/api/restore", json={"names": "a"}).status_code == 400


def test_dismiss_drops_named_offers_or_all_of_them(monkeypatch, client, tmp_path):
    lose(monkeypatch, [{"name": "a", "hist": "x\n"}, {"name": "b"},
                       {"name": "c"}])
    r = client.post("/api/restore/dismiss", json={"names": ["a"]})
    assert r.json() == {"dismissed": ["a"]}
    assert not A.restore_hist("a", "lost").exists()
    assert names(client.get("/api/restore").json()["sessions"]) == ["b", "c"]
    r = client.post("/api/restore/dismiss", json={"all": True})
    assert sorted(r.json()["dismissed"]) == ["b", "c"]
    assert client.get("/api/sessions").json()["restorable"] == 0


def test_the_capability_is_reported():
    assert A.server_capabilities()["restore"] is True


# ---------------------------------------------------------------------------
# The attaches after a restore
# ---------------------------------------------------------------------------

@pytest.fixture
def bridge(monkeypatch):
    """test_ws_attach's stubbed attach path: /bin/cat as the attach child."""
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(A, "session_exists", lambda name: True)
    monkeypatch.setattr(A, "view_name",
                        lambda target, dev: f"{dev or 'ptui'}-{target}")
    monkeypatch.setattr(A, "attach_argv", lambda target, view: ["/bin/cat"])
    monkeypatch.setattr(A, "resolve_target", lambda session, dev: session)
    monkeypatch.setattr(A, "capture_history",
                        lambda name, lines=A.REPLAY_LINES, screen=False: "live1\nlive2")
    monkeypatch.setattr(A, "enable_mouse", lambda view: None)
    monkeypatch.setattr(A, "tmux", lambda *a: (0, ""))


def hello(ws):
    ws.send_text(json.dumps({"type": "resize", "cols": 80, "rows": 24,
                             "token": TOKEN, "dev": "phone"}))


def test_an_attach_replays_the_live_history_only(bridge):
    # The saved output is in the pane's own history once printed there, so
    # the attach sends no copy of its own, even with an old .restored file
    # left behind by an earlier version.
    p = A.restore_hist("work", "restored")
    p.parent.mkdir(parents=True)
    p.write_text("before\n")
    with TestClient(A.app) as c:
        with c.websocket_connect("/ws/attach/work?fresh=1") as ws:
            hello(ws)
            frame = json.loads(ws.receive()["text"])
            assert frame == {"type": "replay", "data": "live1\r\nlive2"}


# ---------------------------------------------------------------------------
# The card, fed the route's own answer
# ---------------------------------------------------------------------------

# Just enough DOM for renderRestoreCard: elements that keep their children,
# attributes and text, and a textContent that reads them back.
FAKE_DOM = r"""
class Node {
  constructor(tag) { this.tag = tag; this.childNodes = []; this.attrs = {};
                     this.className = ""; this.hidden = false; this.checked = false;
                     this.text = ""; this.dataset = {}; }
  appendChild(c) { this.childNodes.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = v;
                       if (k.startsWith("data-")) this.dataset[k.slice(5)] = v; }
  addEventListener() {}
  set innerHTML(v) { this.childNodes = []; }
  set textContent(v) { this.text = v; this.childNodes = []; }
  get textContent() { return this.text + this.childNodes.map((c) => c.textContent).join(""); }
}
const document = {
  createElement: (t) => new Node(t),
  createTextNode: (t) => { const n = new Node("#text"); n.text = t; return n; },
};
const nodes = {};
function $(id) { return nodes[id] || (nodes[id] = new Node("div")); }
"""


def test_the_card_renders_the_routes_answer(monkeypatch, client, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    here = tmp_path / "proj"
    here.mkdir()
    lose(monkeypatch, [
        {"name": "a", "alias": "Alpha", "cwd": str(here), "cmd": "claude",
         "hist": "1\n2\n3\n"},
        {"name": "b", "cwd": str(tmp_path / "gone")},
    ])
    payload = client.get("/api/restore").json()
    payload["sessions"].append({
        "name": "c", "alias": "", "cwd": str(here), "cwd_exists": True,
        "command": "python3", "resume": "", "resumes": "",
        "last_cmd": "python3 train.py --config configs/long_run.yaml",
        "lost": 2000, "lines": 0})

    spec = importlib.util.spec_from_file_location(
        "build_mobile", Path(__file__).resolve().parent.parent / "build_mobile.py")
    bm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bm)
    doc = bm.assemble()

    def chunk(head):
        at = doc.index("\n" + head) + 1
        return doc[at:doc.index("\n}\n", at) + 3]

    js = "\n".join([FAKE_DOM, chunk("function el("), chunk("function restoreWhen("),
                    chunk("function restoreByline("), chunk("function renderRestoreCard("),
                    "let restoreErrors = { b: \"name in use\" };",
                    "renderRestoreCard(%s.sessions);" % json.dumps(payload),
                    """
const rows = $("restore-rows").childNodes.map((row) => ({
  ticked: row.childNodes[0].checked,
  name: row.childNodes[0].dataset.name,
  text: row.childNodes[1].textContent,
}));
console.log(JSON.stringify({ hidden: $("restore-card").hidden,
                             note: $("restore-note").text, rows }));
"""])
    f = tmp_path / "card.js"
    f.write_text(js, encoding="utf-8")
    out = json.loads(subprocess.run([node, str(f)], check=True,
                                    capture_output=True).stdout.decode())
    assert out["hidden"] is False
    assert out["note"].startswith("The computer restarted on ")
    assert out["note"].endswith(". These sessions were open.")
    assert out["rows"] == [
        {"ticked": True, "name": "a",
         "text": "Alphaaproj·resumes Claude Code·3 lines of output"},
        {"ticked": True, "name": "b",
         "text": "bgone·zsh·folder missingname in use"},
        {"ticked": True, "name": "c",
         "text": "cproj·types python3 train.py --config configs/long_…"},
    ]
