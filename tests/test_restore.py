"""Session restore: the record the watcher keeps of what is open under which
tmux server, the offers a lost server leaves behind, the routes that restore
or dismiss them, and the first attach that replays their saved output.

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
                f"1\t1\t{s['name']}\t0\t0\t0\t{s.get('cmd', 'zsh')}"
                f"\t%{s['sid']}\t{s.get('cwd', '/tmp')}\n"
                for s in self.sessions)
        if cmd == "list-panes":
            self.calls.append(args)
            s = self.find(args[args.index("-t") + 1].lstrip("="))
            if s is None:
                return 1, ""
            return 0, f"1\t%{s['sid']}\t{'1' if s.get('alt') else '0'}\n"
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
        "order": 2, "cwd": "/srv/work", "command": "claude", "saved": 0}]


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


def test_scrollback_is_saved_on_first_sight_busy_to_idle_and_every_minute_busy(
        monkeypatch):
    t = fake(monkeypatch, [{"name": "a", "hist": "one\n"}])
    tick(1000)                                  # first sight
    assert captures(t) == 1
    assert live()["sessions"][0]["saved"] == 1000
    A.WATCHER["a"] = A.WatchState(state="active")
    tick(1002)                                  # busy, saved 2 s ago
    tick(1030)
    assert captures(t) == 1
    tick(1061)                                  # busy for a minute
    assert captures(t) == 2
    t.sessions[0]["hist"] = "one\ntwo\n"
    A.WATCHER["a"].state = "ready"
    tick(1063)                                  # the busy->idle edge
    assert captures(t) == 3
    assert A.restore_hist("a").read_text() == "one\ntwo"
    tick(1200)                                  # idle: nothing more
    assert captures(t) == 3
    # The capture keeps the screen: no -E -1.
    cap = [c for c in t.calls if c[0] == "capture-pane"][-1]
    assert "-E" not in cap and "-e" in cap and "-J" in cap


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
         "command": "claude", "lost": 2000, "lines": 3},
        {"name": "b", "alias": "", "cwd": str(tmp_path / "gone"),
         "cwd_exists": False, "command": "zsh", "lost": 2000, "lines": 0},
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
    assert made == [
        ("new-session", "-d", "-e", f"ZDOTDIR={home}", "-s", "a", "-c", str(here)),
        ("new-session", "-d", "-e", f"ZDOTDIR={home}", "-s", "b", "-c", home)]
    opts = [c for c in t.calls if c[0] == "set-option"]
    assert opts == [("set-option", "-t", "a", "@alias", "Alpha"),
                    ("set-option", "-t", "a", "@notify", "on"),
                    ("set-option", "-t", "a", "@ptui_order", "4")]
    typed = [c for c in t.calls if c[0] == "send-keys"]
    assert typed == [("send-keys", "-t", "=a:", "-l", "--", "claude --continue")]
    assert A.restore_count() == 0
    assert pending() == {"sessions": []}
    assert A.restore_hist("a", "restored").read_text() == "saved"
    assert not A.restore_hist("a", "lost").exists()


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
# The first attach
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


def test_the_first_attach_replays_the_saved_output_then_the_live_history(bridge):
    p = A.restore_hist("work", "restored")
    p.parent.mkdir(parents=True)
    p.write_text("before\n\x1b[31mred\x1b[0m\n")
    os.utime(p, (1762430520, 1762430520))
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(1762430520))
    with TestClient(A.app) as c:
        with c.websocket_connect("/ws/attach/work") as ws:
            hello(ws)
            msg = ws.receive()
            assert msg.get("bytes") is None
            frame = json.loads(msg["text"])
            assert frame == {"type": "replay", "data": (
                "before\r\n\x1b[31mred\x1b[0m\r\n\x1b[0m\x1b[2m── restored "
                + when + ", earlier output above ──\x1b[0m\r\nlive1\r\nlive2")}
            ws.send_bytes(b"ping\n")
            acc = b""
            while b"ping" not in acc:
                m = ws.receive()
                assert m.get("text") is None
                acc += m.get("bytes") or b""
        assert not p.exists()
        # The second attach is an ordinary one.
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
         "text": "Alphaaproj·claude·3 lines of output"},
        {"ticked": True, "name": "b",
         "text": "bgone·zsh·folder missingname in use"},
    ]
