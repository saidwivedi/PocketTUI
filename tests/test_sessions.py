"""Session management: the group-representative rule, kill/rename, the throttle.

Everything here runs against a scripted tmux (monkeypatched A.tmux, the pattern
test_transcribe.py set), so it proves the server's own logic — which member of
a group is offered, what argv reaches tmux and in what order — on any machine.
The end-to-end proof against a real tmux server lives in test_ws_attach.py.
"""

import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402

TOKEN = "ABCDEFGHIJ"


class FakeTmux:
    """A scripted tmux server: holds session rows, answers exactly the commands
    app.py issues, and records every argv so tests can assert on order."""

    def __init__(self, sessions=()):
        # Each: name/created plus optional attached/windows/group/alias/view.
        # A session carrying "group" is a grouped member of it. Session ids
        # are handed out in listed order, the way tmux numbers creations.
        self.sessions = [dict(s) for s in sessions]
        for n, s in enumerate(self.sessions):
            s.setdefault("sid", n)
        self.next_sid = len(self.sessions)
        self.calls: list[tuple[str, ...]] = []

    def find(self, name):
        for s in self.sessions:
            if s["name"] == name:
                return s
        return None

    def __call__(self, *args):
        self.calls.append(args)
        cmd = args[0]
        if cmd == "list-sessions":
            lines = []
            for s in self.sessions:
                lines.append("\t".join([
                    s["name"],
                    str(s.get("created", 0)),
                    str(s.get("attached", 0)),
                    str(s.get("windows", 1)),
                    "1" if s.get("group") else "0",
                    s.get("group", ""),
                    f"${s['sid']}",
                    s.get("alias", ""),
                    s.get("notify", ""),
                    "1" if s.get("view") else "",
                ]))
            return 0, "".join(line + "\n" for line in lines)
        if cmd == "has-session":
            return (0, "") if self.find(args[-1].lstrip("=")) else (1, "")
        if cmd == "kill-session":
            s = self.find(args[-1].lstrip("="))
            if s is None:
                return 1, ""
            self.sessions.remove(s)
            return 0, ""
        if cmd == "rename-session":
            s = self.find(args[2].lstrip("="))
            if s is None:
                return 1, ""
            s["name"] = args[3]
            return 0, ""
        if cmd == "new-session":
            self.sessions.append({"name": args[args.index("-s") + 1],
                                  "sid": self.next_sid})
            self.next_sid += 1
            return 0, ""
        # set-option, list-panes, …: succeed quietly with no output.
        return 0, ""


@pytest.fixture
def client():
    return TestClient(A.app)


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    """Every test gets its own limiter state — no backoff or window bleed."""
    monkeypatch.setattr(A, "LIMITER", A.AuthLimiter())
    monkeypatch.setattr(A, "RATE", A.RateLimiter())


def fake(monkeypatch, sessions=()):
    tmux = FakeTmux(sessions)
    monkeypatch.setattr(A, "tmux", tmux)
    return tmux


# A group whose base has been renamed: the group is still called "work" but no
# member carries that name any more. The old name!=group hide rule would have
# hidden every member; the oldest-member rule keeps the renamed base listed.
RENAMED_BASE = (
    {"name": "work2", "created": 100, "group": "work"},
    {"name": "phone-work", "created": 200, "group": "work"},
    {"name": "solo", "created": 300},
)


# ---------------------------------------------------------------------------
# The representative rule
# ---------------------------------------------------------------------------

def test_representative_is_the_oldest_group_member(monkeypatch):
    fake(monkeypatch, RENAMED_BASE)
    reps = {r["name"]: r["representative"] for r in A.session_rows()}
    assert reps == {"work2": True, "phone-work": False, "solo": True}


def test_renamed_base_stays_listed_and_views_stay_hidden(monkeypatch):
    fake(monkeypatch, RENAMED_BASE)
    assert [s["name"] for s in A.list_sessions()] == ["solo", "work2"]


def test_creation_order_beats_a_same_second_view(monkeypatch):
    # session_created has one-second resolution, and a view is often minted in
    # the same second as its base — so "oldest" is decided by session id
    # (creation order), never by the timestamp alone. "a-work" sorting before
    # "work" must not matter either.
    fake(monkeypatch, (
        {"name": "work", "created": 100, "group": "work"},
        {"name": "a-work", "created": 100, "group": "work"},
    ))
    reps = {r["name"]: r["representative"] for r in A.session_rows()}
    assert reps == {"work": True, "a-work": False}


def test_list_survives_a_tmuxless_machine(monkeypatch):
    monkeypatch.setattr(A, "tmux", lambda *a: (1, ""))
    assert A.list_sessions() == []


# ---------------------------------------------------------------------------
# Create: name validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,fragment", [
    ("", "empty"),
    ("a" * (A.SESSION_NAME_MAX + 1), "too long"),
    ("bad.name", "'.' or ':'"),
    ("bad:name", "'.' or ':'"),
    ("bad\x01name", "control characters"),
    ("solo", "already exists"),
])
def test_create_rejects_bad_names(client, monkeypatch, name, fragment):
    tmux = fake(monkeypatch, ({"name": "solo", "created": 1},))
    r = client.post("/api/session", json={"name": name})
    assert r.status_code == 400
    assert fragment in r.json()["error"]
    assert not any(c[0] == "new-session" for c in tmux.calls)


def test_create_makes_a_detached_session(client, monkeypatch):
    tmux = fake(monkeypatch)
    r = client.post("/api/session", json={"name": "fresh"})
    assert r.status_code == 200
    assert r.json() == {"session": "fresh"}
    made = next(c for c in tmux.calls if c[0] == "new-session")
    assert "-d" in made and "fresh" in made


# ---------------------------------------------------------------------------
# The start-up sweep
# ---------------------------------------------------------------------------

def test_sweep_kills_only_this_apps_unattached_views(monkeypatch):
    tmux = fake(monkeypatch, (
        {"name": "base", "created": 100, "group": "base"},
        # Marked by mark_view: ours, nobody on it, goes.
        {"name": "phone-base", "created": 200, "group": "base", "view": True},
        # Minted before the marker existed, but named the way view_name names
        # them — the migration rule catches both spellings.
        {"name": "mba-base", "created": 210, "group": "base"},
        {"name": "ptui-base", "created": 220, "group": "base"},
        # Someone is looking at this one.
        {"name": "ipad-base", "created": 230, "group": "base",
         "view": True, "attached": 1},
        # A grouped clone the user made by hand: no marker, and a name that is
        # none of ours — even one that ends in "-base", when the prefix is not
        # a name any device could have sent (see DEV_RE).
        {"name": "mirror", "created": 240, "group": "base"},
        {"name": "My_clone-base", "created": 250, "group": "base"},
        {"name": "solo", "created": 300},
    ))
    assert A.sweep_views() == ["phone-base", "mba-base", "ptui-base"]
    assert [x["name"] for x in tmux.sessions] == [
        "base", "ipad-base", "mirror", "My_clone-base", "solo"]


def test_sweep_spares_the_representative_even_when_it_looks_like_a_view(
        monkeypatch):
    # The oldest member is the user's session whatever it is called, and
    # "work-work" ends with "-work" — the name rule must never outrank it.
    tmux = fake(monkeypatch, (
        {"name": "work-work", "created": 100, "group": "work"},
        {"name": "phone-work", "created": 200, "group": "work"},
    ))
    assert A.sweep_views() == ["phone-work"]
    assert [x["name"] for x in tmux.sessions] == ["work-work"]


# ---------------------------------------------------------------------------
# Kill
# ---------------------------------------------------------------------------

def test_kill_kills_every_group_member_base_last(client, monkeypatch):
    tmux = fake(monkeypatch, (
        {"name": "work", "created": 100, "group": "work"},
        {"name": "phone-work", "created": 200, "group": "work"},
        {"name": "tab-work", "created": 300, "group": "work"},
        {"name": "solo", "created": 400},
    ))
    r = client.post("/api/session/kill", json={"session": "work"})
    assert r.status_code == 200
    assert r.json() == {"killed": "work"}
    kills = [c for c in tmux.calls if c[0] == "kill-session"]
    assert kills == [
        ("kill-session", "-t", "=phone-work"),
        ("kill-session", "-t", "=tab-work"),
        ("kill-session", "-t", "=work"),
    ]
    assert [s["name"] for s in tmux.sessions] == ["solo"]


def test_kill_of_an_ungrouped_session_kills_just_it(client, monkeypatch):
    tmux = fake(monkeypatch, ({"name": "solo", "created": 1},))
    r = client.post("/api/session/kill", json={"session": "solo"})
    assert r.status_code == 200
    assert [c for c in tmux.calls if c[0] == "kill-session"] == [
        ("kill-session", "-t", "=solo")]


@pytest.mark.parametrize("target", ["phone-work", "missing"])
def test_kill_refuses_non_representatives_and_ghosts(client, monkeypatch, target):
    tmux = fake(monkeypatch, RENAMED_BASE)
    r = client.post("/api/session/kill", json={"session": target})
    assert r.status_code == 404
    assert r.json() == {"error": "no such session"}
    assert not any(c[0] == "kill-session" for c in tmux.calls)


def test_kill_answers_500_when_tmux_refuses(client, monkeypatch):
    tmux = fake(monkeypatch, ({"name": "solo", "created": 1},))

    def stubborn(*args):
        if args[0] == "kill-session":
            return 1, ""
        return tmux(*args)

    monkeypatch.setattr(A, "tmux", stubborn)
    r = client.post("/api/session/kill", json={"session": "solo"})
    assert r.status_code == 500


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------

def test_rename_kills_views_before_renaming(client, monkeypatch):
    tmux = fake(monkeypatch, (
        {"name": "work", "created": 100, "group": "work"},
        {"name": "phone-work", "created": 200, "group": "work"},
    ))
    r = client.post("/api/session/rename",
                    json={"session": "work", "name": "runs"})
    assert r.status_code == 200
    assert r.json() == {"session": "runs"}
    ops = [c for c in tmux.calls if c[0] in ("kill-session", "rename-session")]
    assert ops == [
        ("kill-session", "-t", "=phone-work"),
        ("rename-session", "-t", "=work", "runs"),
    ]
    assert [s["name"] for s in tmux.sessions] == ["runs"]


def test_rename_validates_before_touching_any_view(client, monkeypatch):
    tmux = fake(monkeypatch, (
        {"name": "work", "created": 100, "group": "work"},
        {"name": "phone-work", "created": 200, "group": "work"},
        {"name": "taken", "created": 300},
    ))
    for bad in ("", "has.dot", "has:colon", "taken"):
        r = client.post("/api/session/rename",
                        json={"session": "work", "name": bad})
        assert r.status_code == 400, bad
    # A refused rename must not have cost the phone its view.
    assert not any(c[0] == "kill-session" for c in tmux.calls)


@pytest.mark.parametrize("target", ["phone-work", "missing"])
def test_rename_refuses_non_representatives_and_ghosts(client, monkeypatch, target):
    fake(monkeypatch, RENAMED_BASE)
    r = client.post("/api/session/rename",
                    json={"session": target, "name": "fresh"})
    assert r.status_code == 404


def test_alias_follows_the_representative_rule(client, monkeypatch):
    tmux = fake(monkeypatch, RENAMED_BASE)
    # The renamed base (name != group) is still a valid alias target …
    r = client.post("/api/alias", json={"session": "work2", "alias": "Runs"})
    assert r.status_code == 200
    assert ("set-option", "-t", "work2", "@alias", "Runs") in tmux.calls
    # … and the view never is.
    r = client.post("/api/alias", json={"session": "phone-work", "alias": "x"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Auth and throttle
# ---------------------------------------------------------------------------

def test_session_endpoints_refuse_without_a_token(client, monkeypatch):
    fake(monkeypatch, RENAMED_BASE)
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    for route in ("/api/session", "/api/session/kill", "/api/session/rename"):
        r = client.post(route, json={"session": "work2", "name": "x"})
        assert r.status_code == 401, route


def test_dbg_logs_every_line_and_answers_with_no_body(client, monkeypatch, capsys):
    """The phone's debug tail reaches the journal and nothing comes back."""
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    r = client.post(
        "/api/dbg",
        json={"dev": "phone", "lines": ["fit rows=20 cols=52", "x" * 400]},
        headers={A.TOKEN_HEADER: TOKEN},
    )
    assert r.status_code == 204
    assert not r.content
    out = capsys.readouterr().out
    assert "dbg[phone] fit rows=20 cols=52\n" in out
    # Over-long lines are truncated rather than dropped.
    assert "dbg[phone] " + "x" * A.DBG_LINE_CHARS + "\n" in out


def test_dbg_refuses_without_a_token(client, monkeypatch):
    monkeypatch.setattr(A, "AUTH_TOKEN", TOKEN)
    r = client.post("/api/dbg", json={"dev": "phone", "lines": ["fit rows=20"]})
    assert r.status_code == 401


def test_session_mutation_throttles_at_the_limit(client, monkeypatch):
    fake(monkeypatch, ())
    # Kills of a missing session: they 404, but they passed auth, so each one
    # spends the bucket — exactly what "counts requests that passed auth" means.
    for _ in range(A.RATE_SESSION_MUTATE):
        assert client.post("/api/session/kill",
                           json={"session": "x"}).status_code == 404
    r = client.post("/api/session/kill", json={"session": "x"})
    assert r.status_code == 429
    data = r.json()
    assert data["error"] == "rate_limited"
    assert data["retry_after"] == int(r.headers["Retry-After"]) >= 1
    # The other bucket is untouched: /api/file still answers (404 for a bad
    # path, not 429).
    assert client.get("/api/file", params={"path": "/nope"}).status_code == 404


def test_rate_limiter_window_rolls_over(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    rl = A.RateLimiter()
    for _ in range(3):
        assert rl.allow("session_mutate", "1.2.3.4", 3) == 0.0
    # Refused, and told how long the window has left.
    now[0] += 10.0
    assert rl.allow("session_mutate", "1.2.3.4", 3) == pytest.approx(50.0)
    # Other buckets and other IPs are unaffected.
    assert rl.allow("file", "1.2.3.4", 3) == 0.0
    assert rl.allow("session_mutate", "5.6.7.8", 3) == 0.0
    # The window expires and the same caller is welcome again.
    now[0] += 51.0
    assert rl.allow("session_mutate", "1.2.3.4", 3) == 0.0
