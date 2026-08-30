#!/usr/bin/env python3
"""PocketTUI — phone-facing tmux terminal.

Serves a single-page mobile web app that lists the computer's tmux sessions
and attaches to them over a WebSocket-bridged PTY. Reached both directly at
http://<host>:5560/ and behind `tailscale serve` at https://<host>/pockettui/,
so every URL the frontend uses is relative — the proxy strips the /pockettui
prefix and this server always sees paths rooted at /.

Attach uses a *grouped* session (tmux new-session -t <target>): the phone gets
an independent view of the same windows, so attaching from the phone never
resizes or detaches the client already attached on the laptop. Each device gets
its own view, named <device>-<target>, so two phones can watch one session at
once; the views hide themselves from the session list by being grouped clones.
"""

import argparse
import array
import asyncio
import base64
import concurrent.futures
import dataclasses
import errno
import fcntl
import getpass
import hashlib
import hmac
import json
import math
import os
import pty
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import time
import urllib.parse
import urllib.request
import wave
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
import uvicorn

import resolver

HERE = Path(__file__).resolve().parent
HTML_PATH = HERE / "mobile_app.html"
VENDOR_DIR = HERE / "vendor"
TOKEN_PATH = HERE / ".token"
VOICE_DIR = HERE / "voice"
# Stamped into the tarball by deploy_cloudflare.sh. Absent from a git checkout
# and from installs made before versioning existed, which reads as "unknown".
VERSION_PATH = HERE / "VERSION"

# Cache-busting stamp, injected into the HTML/sw at serve time. Bumping on every
# server start is what makes iOS drop the old PWA shell after a redeploy.
CACHE_VERSION = time.strftime("%Y%m%d-%H%M%S")

# A device name from the client, which becomes part of a tmux session name
# (<device>-<target>). Anything else is treated as absent rather than rejected,
# so a client that sends nothing still gets the single-view behaviour below.
DEV_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,15}$")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the pane watcher for the server's lifetime.

    The watcher (see "Pane watcher & notifications" below) is the one piece of
    this server that acts without a request to answer, so it lives as a task
    the lifespan owns: started once the app is up, cancelled — and awaited, so
    a mid-tick capture finishes cleanly — on the way down. The way down also
    takes any PTY still lingering without a socket (see linger_pty), which has
    no handler of its own to notice the shutdown.
    """
    task = asyncio.create_task(watcher_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await reap_lingering()


app = FastAPI(title="PocketTUI", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Pairing token
# ---------------------------------------------------------------------------
# This server bridges a full shell to anything that can reach it, and it binds
# beyond loopback by default, so every data route is gated on a shared secret.
# The token is short enough to retype on a phone (10 base32 chars, ~50 bits),
# which only stays safe because guessing is throttled — see AuthLimiter.

TOKEN_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"  # RFC4648 base32
TOKEN_LEN = 10
TOKEN_RE = re.compile(f"^[{TOKEN_ALPHABET}]{{{TOKEN_LEN}}}$")

# The header carries it rather than a cookie: allow_origins=["*"] plus a cookie
# the browser attaches automatically would let any page in the world drive this
# shell. A header has to be set deliberately by our own frontend.
TOKEN_HEADER = "X-PocketTUI-Token"

# The shell itself must load unauthenticated — it is what prompts for the token.
# Only the routes that read or touch the machine are gated.
GATED_PREFIXES = ("/api/",)

# The one gated path that answers without the header: a signed download link,
# which the browser fetches as a plain navigation and a navigation cannot carry
# one. Its query string is the credential instead — see api_fs_signed_download.
SIGNED_PATHS = ("/api/fs/signed_download",)


def normalize_token(raw: str) -> str:
    """Canonical form of a typed token, or "" if it is not one.

    The user sees XXXXX-XXXXX but may retype it without the dash, in lower case,
    or with the whitespace a phone keyboard adds; all of those are the same
    token. Anything that is not 10 base32 characters after that is not a token
    at all, which is what lets a malformed .token file read as simply absent.
    """
    text = re.sub(r"[\s-]", "", str(raw or "")).upper()
    # 0 and 1 are excluded from the alphabet as look-alikes, so a typed 0 or 1
    # can only be a misread O or I — map rather than reject.
    text = text.replace("0", "O").replace("1", "I")
    return text if TOKEN_RE.match(text) else ""


def format_token(token: str) -> str:
    """Group the canonical form as XXXXX-XXXXX — far easier to read off a screen."""
    return f"{token[:5]}-{token[5:]}"


def generate_token() -> str:
    """A fresh token. `secrets`, not `random`: this is the only thing guarding a shell."""
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LEN))


def read_token() -> str:
    """The token on disk, or "" if there is none that is usable.

    A file we cannot read, or one holding something that is not a token, is
    treated as absent rather than as an error: the caller's job is to refuse to
    start either way, and the message it prints is the same.
    """
    try:
        return normalize_token(TOKEN_PATH.read_text(encoding="utf-8"))
    except OSError:
        return ""


def write_token(token: str) -> None:
    """Write the canonical token 0600.

    The mode is set before the secret is written, so it is never briefly
    world-readable on a shared machine.
    """
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    os.chmod(TOKEN_PATH, 0o600)


# Set by main() before uvicorn starts. None means --no-auth: every gate below
# short-circuits open, which main() only permits on a loopback bind.
AUTH_TOKEN: str | None = None


def token_ok(candidate: str) -> bool:
    """Whether `candidate` is the pairing token.

    compare_digest rather than ==, so the number of leading characters a guess
    got right cannot be read off the response time.
    """
    if AUTH_TOKEN is None:
        return True
    return hmac.compare_digest(normalize_token(candidate), AUTH_TOKEN)


# ---------------------------------------------------------------------------
# Guess throttling
# ---------------------------------------------------------------------------

class AuthLimiter:
    """Exponential backoff on failed token attempts, per source IP and overall.

    ~50 bits of token is only out of reach while an attacker gets a bounded
    number of tries per second; unthrottled, a LAN peer could walk the space.
    After FREE_TRIES failures an IP is locked out for a doubling delay, so a
    sustained attack costs exponentially more wall-clock time than it gains.

    The global counter exists because the per-IP one is trivially sidestepped
    from a botnet or a spoofable v6 range: it applies the same backoff to the
    server as a whole once failures pile up regardless of where they came from.
    Its allowance is looser so that one attacker cannot cheaply lock out the
    legitimate phone — the per-IP limit is the sharp one.
    """

    FREE_TRIES = 5
    GLOBAL_FREE_TRIES = 50
    BASE_DELAY = 1.0
    MAX_DELAY = 300.0
    # A quiet IP is forgotten, so a phone that mistyped its token last week is
    # not still serving a penalty — and the table cannot grow without bound.
    FORGET_AFTER = 3600.0

    def __init__(self) -> None:
        self.failures: dict[str, tuple[int, float]] = {}
        self.global_failures = 0
        self.global_blocked_until = 0.0

    def _delay(self, count: int, free: int) -> float:
        over = count - free
        if over <= 0:
            return 0.0
        return min(self.BASE_DELAY * (2 ** (over - 1)), self.MAX_DELAY)

    def _prune(self, now: float) -> None:
        for key, (_, last) in list(self.failures.items()):
            if now - last > self.FORGET_AFTER:
                del self.failures[key]

    def blocked_for(self, ip: str) -> float:
        """Seconds this source must wait, or 0 if it may try now."""
        now = time.monotonic()
        self._prune(now)
        wait = max(0.0, self.global_blocked_until - now)
        entry = self.failures.get(ip)
        if entry is not None:
            count, last = entry
            wait = max(wait, last + self._delay(count, self.FREE_TRIES) - now)
        return max(0.0, wait)

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        count = self.failures.get(ip, (0, 0.0))[0] + 1
        self.failures[ip] = (count, now)
        self.global_failures += 1
        self.global_blocked_until = now + self._delay(
            self.global_failures, self.GLOBAL_FREE_TRIES)

    def record_success(self, ip: str) -> None:
        """Clear this source's penalty — the holder of the token is not an attacker.

        The global counter is also relieved, otherwise a long-running server
        would drift into permanent backoff on accumulated typos alone.
        """
        self.failures.pop(ip, None)
        self.global_failures = 0
        self.global_blocked_until = 0.0


LIMITER = AuthLimiter()


class RateLimiter:
    """Fixed-window request throttle, per (bucket, source IP).

    AuthLimiter guards the token; this guards everything an *authenticated*
    caller can do too fast — a stuck client retry loop creating sessions, or a
    page of image links hammering /api/file. Same construction as its
    neighbour: a dict, time.monotonic, and pruning on use, no external deps.

    A bucket names one class of endpoint, so heavy legitimate traffic on one
    (file serving) can never starve another (session mutation) of its own
    allowance. Windows are fixed rather than sliding: the count starts with a
    bucket's first request and the whole bucket forgets WINDOW seconds later,
    which is coarse but cheap and plenty for limits this size.
    """

    WINDOW = 60.0

    def __init__(self) -> None:
        # (bucket, ip) -> (count, window start)
        self.counts: dict[tuple[str, str], tuple[int, float]] = {}

    def allow(self, bucket: str, ip: str, limit: int) -> float:
        """Seconds this caller must wait, or 0.0 if the request may run now."""
        now = time.monotonic()
        for key, (_, started) in list(self.counts.items()):
            if now - started >= self.WINDOW:
                del self.counts[key]
        key = (bucket, ip)
        count, started = self.counts.get(key, (0, now))
        if count >= limit:
            return max(0.0, started + self.WINDOW - now)
        self.counts[key] = (count + 1, started)
        return 0.0


RATE = RateLimiter()

# Per-minute allowances. Session mutation (create/kill/rename) is something a
# human does a few times an hour; the file route serves every image link on a
# busy screen, so it gets room to breathe.
RATE_SESSION_MUTATE = 20
RATE_FILE = 120


def throttled(bucket: str, limit: int, request: Request) -> Response | None:
    """The 429 this request has earned, or None if it may proceed.

    Called at the top of a handler, so only requests that already passed the
    auth middleware are ever counted. The IP comes from the connection itself,
    for the reason peer_ip() gives.
    """
    wait = RATE.allow(bucket, peer_ip(request.scope.get("client")), limit)
    if wait <= 0:
        return None
    retry = max(1, math.ceil(wait))
    return no_store(JSONResponse(
        {"error": "rate_limited", "retry_after": retry},
        status_code=429, headers={"Retry-After": str(retry)}))


def peer_ip(scope_client) -> str:
    """The address the connection actually came from.

    Deliberately not X-Forwarded-For: that header is set by whoever is calling,
    so trusting it would let an attacker mint a fresh rate-limit bucket per
    guess simply by varying it.
    """
    return scope_client[0] if scope_client else "unknown"


def check_auth(candidate: str, ip: str, where: str) -> tuple[bool, str]:
    """Throttled token check. Returns (ok, reason) — reason is empty when ok.

    Single place for the sequence every entry point needs: refuse outright while
    a source is backing off, verify, then either forgive or penalise it.
    """
    if AUTH_TOKEN is None:
        return True, ""
    wait = LIMITER.blocked_for(ip)
    if wait > 0:
        log(f"auth blocked {where} ip={ip} retry-after={wait:.0f}s")
        return False, "too many attempts"
    if token_ok(candidate):
        LIMITER.record_success(ip)
        return True, ""
    LIMITER.record_failure(ip)
    log(f"auth fail {where} ip={ip}")
    return False, "bad token"


@app.middleware("http")
async def require_token(request: Request, call_next):
    """Gate the API routes on the pairing token.

    Registered before CORSMiddleware so it sits *inside* it: a 401 returned from
    out here would carry no Access-Control-Allow-Origin, and the browser would
    hide it from the frontend as a network error — leaving the phone unable to
    tell a wrong token from an unreachable host.

    Preflights pass through untouched. They carry no custom headers by
    definition, so 401-ing them would stop the browser ever sending the real
    request that does carry the token.
    """
    if AUTH_TOKEN is None or request.method == "OPTIONS":
        return await call_next(request)
    if not request.url.path.startswith(GATED_PREFIXES):
        return await call_next(request)
    if request.url.path in SIGNED_PATHS:
        return await call_next(request)

    ok, reason = check_auth(
        request.headers.get(TOKEN_HEADER, ""),
        peer_ip(request.scope.get("client")),
        f"http {request.url.path}",
    )
    if not ok:
        return no_store(JSONResponse({"error": reason}, status_code=401))
    return await call_next(request)


# The public static shell (apps.saidwivedi.in/pockettui/) calls this server
# cross-origin, so /api/sessions has to answer with CORS headers. The WebSocket
# is unaffected — WS handshakes are not subject to CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    # Chrome's private-network preflight is rejected outright without this —
    # the public shell reaching this tailnet host is exactly that case.
    allow_private_network=True,
)


@app.middleware("http")
async def private_network(request: Request, call_next):
    """Let a public page reach this private-network host.

    Chrome's private-network-access check wants this on the preflight; setting
    it on every response is harmless where it isn't read.
    """
    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------

# The tmux invocation every helper builds on. A list rather than a string so
# tests can point the whole server at an isolated tmux server
# (["tmux", "-L", "pockettui-test", "-f", "/dev/null"]) without touching the
# user's real sessions.
TMUX_BIN: list[str] = ["tmux"]


def tmux(*args: str) -> tuple[int, str]:
    """Run a tmux command, returning (returncode, stdout). Never raises."""
    try:
        p = subprocess.run(
            [*TMUX_BIN, *args], capture_output=True, text=True, timeout=5
        )
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def session_exists(name: str) -> bool:
    return tmux("has-session", "-t", f"={name}")[0] == 0


def session_rows() -> list[dict]:
    """Every tmux session, parsed, each carrying a group-representative flag.

    A session group holds one session the user made plus this app's per-device
    views of it, and only one member should ever stand for the group in the
    list or be a valid target for alias/kill/rename. The representative is the
    *oldest* member — not the one whose name matches the group, because the
    group keeps its original name forever while `rename-session` changes the
    session's: a renamed base would otherwise vanish from its own list. The
    views are always younger (they are created by attaching to something
    already listed), so oldest-member picks the user's session under any name.
    Ungrouped sessions represent themselves.
    """
    rc, out = tmux(
        "list-sessions",
        "-F",
        "#{session_name}\t#{session_created}\t#{session_attached}\t#{session_windows}"
        "\t#{session_grouped}\t#{session_group}\t#{session_id}\t#{@alias}\t#{@notify}",
    )
    if rc != 0:
        return []

    rows = []
    for line in out.splitlines():
        # The alias and notify fields are last and empty when unset, so split
        # to a fixed width rather than requiring every field to be present.
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name, created, attached, windows, grouped, group, sid = parts[:7]
        alias = parts[7] if len(parts) > 7 else ""
        notify = parts[8] if len(parts) > 8 else ""
        rows.append({
            "name": name,
            "created": int(created or 0),
            "attached": int(attached or 0),
            "windows": int(windows or 0),
            "grouped": grouped == "1",
            "group": group,
            "sid": _session_sid(sid),
            "alias": alias,
            "notify": _notify_mode(notify),
        })

    # Oldest member per group, ordered by session id: tmux hands ids out in
    # creation order at full resolution, where session_created only has whole
    # seconds — and a view *is* minted within the same second as its base often
    # enough that a timestamp would tie exactly when it matters. The trailing
    # fields only order rows an unparseable id has left tied.
    oldest: dict[str, tuple[int, int, str]] = {}
    for row in rows:
        if not row["grouped"]:
            continue
        candidate = (row["sid"], row["created"], row["name"])
        if row["group"] not in oldest or candidate < oldest[row["group"]]:
            oldest[row["group"]] = candidate
    for row in rows:
        row["representative"] = (not row["grouped"]
                                 or oldest[row["group"]][2] == row["name"])
    return rows


def _session_sid(raw: str) -> int:
    """A #{session_id} ("$3") as its creation-ordered number; huge if unreadable,
    so a malformed id merely loses the representative race instead of crashing
    the whole list."""
    try:
        return int(raw.lstrip("$"))
    except ValueError:
        return 1 << 62


def _notify_mode(raw: str) -> str:
    """The raw @notify option as one of "off"/"on"/"quiet". Legacy installs
    only ever wrote "on", and any other hand-set non-empty value was an opt-in
    too, so everything but the two known-quiet spellings reads as the louder
    mode rather than silently downgrading someone's notifications."""
    if raw in ("", "off"):
        return "off"
    return "quiet" if raw == "quiet" else "on"


def find_row(rows: list[dict], name: str) -> dict | None:
    """The row for `name` out of a session_rows() result, or None."""
    for row in rows:
        if row["name"] == name:
            return row
    return None


def other_group_members(rows: list[dict], row: dict) -> list[str]:
    """The names of every other session in `row`'s group — this app's device
    views of it, or the user's own hand-made clones. Empty for an ungrouped
    session."""
    if not row["grouped"]:
        return []
    return [r["name"] for r in rows
            if r["grouped"] and r["group"] == row["group"]
            and r["name"] != row["name"]]


def list_sessions() -> list[dict]:
    """All non-phone tmux sessions with their active pane's command, newest first."""
    sessions = []
    for row in session_rows():
        # Hide the grouped non-representatives — this app's own views are born
        # grouped onto their target, which is race-free in a way a marker set
        # after spawn is not. A user's own hand-made clone is hidden too,
        # reasonably: it mirrors a session already on the list.
        if not row["representative"]:
            continue
        cmd, title = active_pane(row["name"])
        # The watcher's view of this session rides along regardless of the
        # @notify opt-in: the badge is in-app signal, and the phone's WS dies
        # seconds after backgrounding, so the list is where "still waiting on
        # you" survives. A session the watcher has not seen yet reads idle.
        w = WATCHER.get(row["name"])
        sessions.append({
            "name": row["name"],
            "created": row["created"],
            "attached": row["attached"],
            "windows": row["windows"],
            "command": cmd,
            "title": title,
            "alias": row["alias"],
            "notify": row["notify"],
            "state": w.state if w else "idle",
            "last_activity": w.last_activity if w else 0,
        })

    sessions.sort(key=lambda s: s["created"], reverse=True)
    return sessions


def pane_cwd(name: str) -> str:
    """The working directory of the session's active pane, or "".

    list-panes rather than display-message, for the reason given in active_pane:
    display-message resolves its target against the calling client's session,
    and this server has no tty and so no session of its own.
    """
    rc, out = tmux(
        "list-panes", "-t", f"={name}", "-f", "#{pane_active}",
        "-F", "#{pane_current_path}",
    )
    if rc != 0 or not out.strip():
        return ""
    path = out.strip().splitlines()[0].strip()
    return path if os.path.isdir(path) else ""


def capture_pane(name: str, lines: int = 60) -> list[str]:
    """The visible text of the session's active pane, newest last, or [].

    The transcription prompt and the register detection both want to know what
    the user is looking at. `-p` prints to stdout instead of a buffer; the
    pane_active filter picks the same pane every other helper here reads, and
    for the same reason list-panes is used to find it rather than
    display-message (see active_pane).
    """
    rc, out = tmux("list-panes", "-t", f"={name}", "-f", "#{pane_active}",
                   "-F", "#{pane_id}")
    if rc != 0 or not out.strip():
        return []
    pane = out.strip().splitlines()[0].strip()
    rc, out = tmux("capture-pane", "-p", "-t", pane, "-S", f"-{max(0, lines)}")
    if rc != 0:
        return []
    return [line.rstrip() for line in out.splitlines()][-lines:]


# How much scrollback a reconnect replays. 500 lines is a few screens of
# context — enough to read back through what happened while the phone was away,
# small enough to send in one frame on a cellular link.
REPLAY_LINES = 500

# Hard ceiling on one replay frame. 500 lines only exceed this when something
# printed pathologically long lines; the oldest are dropped rather than letting
# one reconnect stall the socket behind megabytes of history.
REPLAY_MAX_BYTES = 2 * 1024 * 1024


def capture_history(name: str, lines: int = REPLAY_LINES) -> str:
    """The tail of the active pane's *history* — the text above the visible
    screen — or "" when there is nothing worth replaying.

    Feeds the reconnect replay: the attach repaint restores the visible screen
    itself, so `-E -1` stops one line short of it and nothing is painted twice.
    `-e` keeps the SGR colors; `-J` joins tmux's soft-wrapped lines back
    together so xterm rewraps them at the phone's own width.

    A pane sitting in the alternate screen (htop, full-screen TUIs) answers ""
    outright: its history belongs to the primary screen the user is not looking
    at, and the attach repaint restores the TUI exactly as before.
    """
    # The active pane is filtered here rather than with `list-panes -f`, the
    # way session_group avoids `list-sessions -f`: tmux 3.0a has no -f on the
    # list commands at all, and passing it fails the whole command.
    rc, out = tmux("list-panes", "-t", f"={name}",
                   "-F", "#{pane_active}\t#{pane_id}\t#{alternate_on}")
    if rc != 0:
        return ""
    pane = ""
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == "1":
            pane = parts[1].strip()
            if len(parts) > 2 and parts[2].strip() == "1":
                return ""
            break
    if not pane:
        return ""
    rc, out = tmux("capture-pane", "-p", "-e", "-J", "-t", pane,
                   "-S", f"-{max(0, lines)}", "-E", "-1")
    if rc != 0:
        return ""
    text = out.rstrip("\n")
    if len(text.encode("utf-8")) > REPLAY_MAX_BYTES:
        kept = text.split("\n")
        size = sum(len(line.encode("utf-8")) + 1 for line in kept)
        drop = 0
        while drop < len(kept) and size > REPLAY_MAX_BYTES:
            size -= len(kept[drop].encode("utf-8")) + 1
            drop += 1
        text = "\n".join(kept[drop:])
    return text


def resolve_target(session: str, dev: str) -> str:
    """The session name whose active pane reflects what this device sees.

    The device's own grouped view is the pane the user is actually looking at,
    so it is tried first; the base session is the fallback for a device that is
    watching without a view of its own.
    """
    if not session:
        return ""
    for candidate in (view_name(session, dev) if dev else "", session):
        if candidate and session_exists(candidate):
            return candidate
    return ""


def tmux_names() -> list[str]:
    """Session and window names, which are vocabulary the user says out loud."""
    names: list[str] = []
    for args in (("list-sessions", "-F", "#{session_name}"),
                 ("list-windows", "-a", "-F", "#{window_name}")):
        rc, out = tmux(*args)
        if rc == 0:
            names.extend(line.strip() for line in out.splitlines() if line.strip())
    return names


def active_pane(name: str) -> tuple[str, str]:
    """(pane_current_command, pane_title) of the session's active pane.

    list-panes with a pane_active filter, rather than display-message: the
    latter resolves its target against the calling client's session, which this
    server (running without a tty) does not have.
    """
    rc, out = tmux(
        "list-panes", "-t", f"={name}", "-f", "#{pane_active}",
        "-F", "#{pane_current_command}\t#{pane_title}",
    )
    if rc != 0 or not out.strip():
        return "", ""
    parts = out.strip().splitlines()[0].split("\t")
    return parts[0], (parts[1] if len(parts) > 1 else "")


def session_group(name: str) -> str:
    """The name of the group `name` belongs to, or "" if it is ungrouped.

    Filtered here rather than with `list-sessions -f`, which older tmux (3.0a)
    does not have — passing it there fails the whole command.
    """
    rc, out = tmux("list-sessions",
                   "-F", "#{session_name}\t#{session_grouped}\t#{session_group}")
    if rc != 0:
        return ""
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0] == name:
            return parts[2] if parts[1] == "1" else ""
    return ""


def view_name(target: str, dev: str) -> str:
    """The name of this device's own grouped view of `target`.

    Without a device name (an old cached shell) this keeps the original single
    view per target. With one, each device gets its own, which is the whole
    point: two devices hold different views and so never detach each other.
    """
    if not dev:
        return "ptui-" + target
    base = f"{dev}-{target}"
    # The name is user-facing on both ends, so it can collide with a real
    # session the user made. Reuse it only when it is already a view of this
    # target; otherwise step aside rather than attaching to the wrong session.
    name = base
    for n in range(2, 10):
        if not session_exists(name) or session_group(name) == target:
            return name
        name = f"{base}-{n}"
    return name


def attach_argv(target: str, view: str) -> list[str]:
    """Command that gives the phone its own view of `target`.

    A grouped session shares the target's window objects but keeps its own
    current window, so the phone can sit on a different window than the laptop.
    Size is not private that way: it belongs to the shared window, and tmux's
    `window-size latest` gives that window the size of whichever client was
    active last — a keystroke, or an attach. Reuse the view across reconnects
    (-d kicks off any stale client of it) so the phone's window selection
    survives a dropout.
    """
    if session_exists(view):
        return [*TMUX_BIN, "attach", "-d", "-t", f"={view}"]
    return [*TMUX_BIN, "new-session", "-s", view, "-t", f"={target}"]


def enable_mouse(view: str) -> None:
    """Turn on mouse reporting for the phone's own session only.

    Drag-to-scroll on the phone works by synthesising SGR wheel events, which
    tmux only acts on with `mouse on`. The option is set on this device's view
    alone (a grouped session carries its own options), so the laptop's client of
    the same windows keeps whatever the user configured.
    """
    # No "=" exact-match prefix here: set-option rejects it outright
    # ("no such session"), unlike the session-target commands above.
    tmux("set-option", "-t", view, "mouse", "on")


# Build 0.8.118 tried to stop two clients resizing each other by pinning the
# shared windows to `window-size smallest` + `aggressive-resize on`, and by
# hooking `window-linked` on the view so windows opened later got pinned too.
# That made the smallest client win permanently — the laptop stuck at phone
# size — so the pins are gone. They outlive the code, though: they were written
# into the user's running tmux server, where they survive every upgrade. This
# undoes them, which is why it runs on attach rather than living in a release
# note nobody can execute.

def heal_size_pins(view: str) -> None:
    """Undo build 0.8.118's size pinning on the windows this view shares.

    Only the exact pinned values are unset, so a window-size the user chose for
    themselves — `manual`, say — survives. The hooks go whatever the windows
    say: `set-hook -u` drops the whole array (both entries the pin installed),
    and leaving them behind would re-pin the next window the group opens.
    """
    rc, out = tmux("list-windows", "-t", view, "-F",
                   "#{window_id}\t#{window-size}\t#{aggressive-resize}")
    fix: list[str] = []
    for line in (out.splitlines() if rc == 0 else []):
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        wid, size, aggressive = parts
        if size == "smallest":
            fix += [";", "set-option", "-w", "-t", wid, "-u", "window-size"]
        if aggressive in ("1", "on"):
            fix += [";", "set-option", "-w", "-t", wid, "-u",
                    "aggressive-resize"]
    rc, out = tmux("show-hooks", "-t", view)
    if rc == 0 and "window-linked" in out:
        fix += [";", "set-hook", "-u", "-t", view, "window-linked"]
    if fix:
        # One invocation for the lot: tmux takes ";"-separated commands, and
        # unsetting an option that is not set is a no-op, so this is safe to
        # repeat.
        tmux(*fix[1:])
        log(f"healed 0.8.118 size pins on view={view}")


def prepare_view(view: str) -> None:
    """Post-attach setup for this device's view, off the event loop.

    The attach child is what spawns the session, so this waits for the fork
    rather than racing it. Only a real attach comes through here — a reconnect
    that adopts a lingering PTY (see linger_pty) changes nothing tmux can see.
    """
    for _ in range(20):
        if session_exists(view):
            enable_mouse(view)
            heal_size_pins(view)
            return
        time.sleep(0.05)


def redraw_view(view: str) -> None:
    """Force a full repaint of whatever client is on `view`.

    An adopted PTY never detached, so tmux has no reason to repaint — but the
    reconnecting client missed whatever landed while its socket was down and
    would sit on a stale screen. refresh-client redraws that one client and,
    unlike an attach, does not count as activity, so the shared window keeps
    its size.
    """
    rc, out = tmux("list-clients", "-t", view, "-F", "#{client_tty}")
    for tty in (out.split() if rc == 0 else []):
        tmux("refresh-client", "-t", tty)


# ---------------------------------------------------------------------------
# Static routes
# ---------------------------------------------------------------------------

def no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index() -> Response:
    html = HTML_PATH.read_text(encoding="utf-8").replace("__CACHE_VERSION__", CACHE_VERSION)
    # Served from the backend itself, so the frontend stays same-origin. The
    # sentinel says so explicitly: a public static build substitutes an empty
    # string here and must ask the user for a backend instead of guessing that
    # its own origin serves the API.
    html = html.replace("__BACKEND_URL__", "same-origin")
    return no_store(Response(html, media_type="text/html; charset=utf-8"))


@app.get("/manifest.json")
def manifest() -> Response:
    data = {
        "name": "PocketTUI",
        "short_name": "PocketTUI",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#FAF8F3",
        "theme_color": "#FAF8F3",
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png",
             "purpose": "any maskable"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "any maskable"},
        ],
    }
    return no_store(JSONResponse(data))


@app.get("/sw.js")
def service_worker() -> Response:
    js = (HERE / "sw.js").read_text(encoding="utf-8").replace(
        "__CACHE_VERSION__", CACHE_VERSION)
    return no_store(Response(js, media_type="application/javascript"))


def _icon_path(name: str) -> Path | None:
    """An icon from the flat install layout, or the repo's assets/ dir.

    An install has its icons next to app.py; a checkout keeps them in assets/
    and only the build copies them out. Serving both means running from a
    checkout does not need a build first.
    """
    for path in (HERE / name, HERE / "assets" / name):
        if path.exists():
            return path
    return None


@app.get("/icon.svg")
def icon_svg() -> Response:
    # Not part of the installed runtime (nothing references it), so answer 404
    # rather than raising when the file is absent.
    path = _icon_path("icon.svg")
    if path is None:
        return Response(status_code=404)
    return FileResponse(path, media_type="image/svg+xml")


@app.get("/icon-{size}.png")
def icon_png(size: str) -> Response:
    path = _icon_path(f"icon-{size}.png")
    if path is None:
        return Response(status_code=404)
    return FileResponse(path, media_type="image/png")


@app.get("/vendor/{name}")
def vendor(name: str) -> Response:
    # Basename-only lookup keeps path traversal out of the vendor dir.
    path = VENDOR_DIR / Path(name).name
    if not path.exists():
        return Response(status_code=404)
    kind = "text/css" if path.suffix == ".css" else "application/javascript"
    return FileResponse(path, media_type=kind)


@app.get("/api/version")
def api_version() -> Response:
    """Which build this install is running, or "" when it cannot say.

    A checkout has no VERSION file and neither does an install predating the
    stamp, so an empty string is a normal answer rather than an error — the
    caller compares it against $BASE_URL/version.txt and treats a blank as
    "unknown", exactly as install.sh does.
    """
    try:
        version = VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        version = ""
    return no_store(JSONResponse({"version": version}))


@app.get("/api/sessions")
def api_sessions() -> Response:
    return no_store(JSONResponse({"sessions": list_sessions()}))


@app.get("/api/session_cwd")
def api_session_cwd(session: str = "", dev: str = "") -> Response:
    """The working directory of the pane this device is looking at, or "".

    Asked at the moment the file explorer opens, rather than carried on every
    /api/sessions row: the list refreshes constantly and would pay one more
    tmux call per session for an answer only this tap uses. resolve_target
    prefers the device's own grouped view, so the cwd is the pane the user is
    actually looking at rather than the base session's.
    """
    target = resolve_target(session, dev if DEV_RE.match(dev) else "")
    return no_store(JSONResponse({"cwd": pane_cwd(target) if target else ""}))


# A display name only: long enough to be useful in the list row, short enough that
# it cannot push the real session name out of the row.
ALIAS_MAX = 60

# A real session name shares the same row, so it gets the same budget.
SESSION_NAME_MAX = ALIAS_MAX


def clean_alias(value: str) -> str:
    """Strip what would break the tmux option or the list row: controls and tabs."""
    text = "".join(c for c in str(value) if c.isprintable())
    return text.strip()[:ALIAS_MAX]


def validate_session_name(value: str) -> tuple[str, str]:
    """Check a proposed session name. Returns (name, error) — one is always empty.

    Unlike clean_alias, this rejects rather than trims: the name is what the user
    will type at `tmux attach`, so silently altering it would hand them a session
    they cannot find again.
    """
    name = str(value).strip()
    if not name:
        return "", "Session name cannot be empty."
    if len(name) > SESSION_NAME_MAX:
        return "", f"Session name is too long (max {SESSION_NAME_MAX} characters)."
    if not all(c.isprintable() for c in name):
        return "", "Session name cannot contain control characters."
    # tmux reads both as target syntax (session.window, session:window), so a name
    # containing either is unaddressable afterwards.
    if "." in name or ":" in name:
        return "", "Session name cannot contain '.' or ':'."
    if session_exists(name):
        return "", f"A session named '{name}' already exists."
    return name, ""


@app.post("/api/alias")
def api_alias(body: dict = Body(...)) -> Response:
    """Set (or clear) a session's display name, without renaming the session.

    Stored as the session's own `@alias` option, so it lives and dies with the
    session and every device sees the same name — the user's tooling still finds
    the session under its real name.
    """
    name = str(body.get("session", ""))
    # A grouped non-representative is one of this app's views (or the user's
    # own mirror of a listed session), never something the list offers to name.
    row = find_row(session_rows(), name)
    if row is None or not row["representative"]:
        return JSONResponse({"error": "no such session"}, status_code=404)

    alias = clean_alias(body.get("alias", ""))
    # No "=" exact-match prefix: set-option rejects it, as noted in enable_mouse.
    if alias:
        rc, _ = tmux("set-option", "-t", name, "@alias", alias)
    else:
        rc, _ = tmux("set-option", "-u", "-t", name, "@alias")
    if rc != 0:
        return JSONResponse({"error": "could not set alias"}, status_code=500)
    return no_store(JSONResponse({"session": name, "alias": alias}))


@app.post("/api/session/kill")
def api_session_kill(request: Request, body: dict = Body(...)) -> Response:
    """Kill a session — and with it every device view grouped onto it.

    Killing only the base would leave the grouped views holding the same
    windows alive, so the whole group goes: views first, the base last, each by
    exact name. Only the group's representative is a valid target, for the same
    reason it is the only member the list shows.
    """
    refusal = throttled("session_mutate", RATE_SESSION_MUTATE, request)
    if refusal is not None:
        return refusal

    name = str(body.get("session", ""))
    rows = session_rows()
    row = find_row(rows, name)
    if row is None or not row["representative"]:
        return JSONResponse({"error": "no such session"}, status_code=404)

    for member in [*other_group_members(rows, row), name]:
        rc, _ = tmux("kill-session", "-t", f"={member}")
        if rc != 0:
            return JSONResponse(
                {"error": f"tmux could not kill '{member}'."}, status_code=500)
    return no_store(JSONResponse({"killed": name}))


@app.post("/api/session/rename")
def api_session_rename(request: Request, body: dict = Body(...)) -> Response:
    """Give a session a new real tmux name.

    Distinct from /api/alias by design: the alias is the display name this app
    shows, the rename is the name the user's own tooling addresses. `@alias` is
    a session option and survives the rename untouched.

    The device views grouped onto the session are killed first rather than
    carried across — they are throwaway caches named after the old name, and
    every phone reconnecting mints a fresh view against the new one.
    """
    refusal = throttled("session_mutate", RATE_SESSION_MUTATE, request)
    if refusal is not None:
        return refusal

    old = str(body.get("session", ""))
    rows = session_rows()
    row = find_row(rows, old)
    if row is None or not row["representative"]:
        return JSONResponse({"error": "no such session"}, status_code=404)

    new, error = validate_session_name(body.get("name", ""))
    if error:
        return JSONResponse({"error": error}, status_code=400)

    for member in other_group_members(rows, row):
        rc, _ = tmux("kill-session", "-t", f"={member}")
        if rc != 0:
            return JSONResponse(
                {"error": f"tmux could not kill '{member}'."}, status_code=500)
    rc, _ = tmux("rename-session", "-t", f"={old}", new)
    if rc != 0:
        return JSONResponse(
            {"error": f"tmux could not rename '{old}'."}, status_code=500)
    return no_store(JSONResponse({"session": new}))


# ---------------------------------------------------------------------------
# Voice transcription
# ---------------------------------------------------------------------------
# The phone records audio and posts the blob here; a local acoustic model turns
# it into text — Parakeet-TDT through sherpa-onnx where it is installed, else
# whisper.cpp — and the resolver repairs the technical tokens the model had no
# way to know. Nothing leaves the machine.

# A phone holding the mic key open still has to produce a request this server
# will look at. 20 MB of AAC is far longer than anyone dictates in one breath.
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_AUDIO_SECONDS = 90

# whisper on CPU runs roughly real-time on base.en, so a 90 s cap needs well
# under 30 s; past that something is wrong and the phone is already waiting.
WHISPER_TIMEOUT_S = 30
FFMPEG_TIMEOUT_S = 15

# The transcript is short and the audio pass dominates, so the snap can have far
# more room than a resolve on every keystroke ever could.
TRANSCRIBE_BUDGET_S = 2.0

# Roughly 200 tokens: whisper truncates the prompt beyond its context anyway,
# and a longer one starts to bias the decode towards vocabulary over what was
# actually said.
MAX_PROMPT_CHARS = 900

# And a cap on the *count*, because the character budget stopped being the
# binding constraint once history joined the sources. Ranking by rarity fills
# the prompt with long separator-carrying paths (mean ~12 characters), so 900
# chars now buys 65 words where it used to buy a handful — and whisper imitates
# a prompt that dense instead of merely taking its vocabulary. It starts
# emitting the separators it sees, turning spoken "dash dash force with lease"
# into `-dash-force-with-lease` and "jellyfish piper-tts" into
# `jellifish_piper-tts`, and at the extreme it runs whole phrases together
# ("run pytest on tests" as "runPitastonTest"). Swept over the 42-clip
# benchmark, exact-match peaks at 20 words (21/42, matching the old prompt) and
# falls away on both sides — 19 at 12 words, 18 at 24, 16 at 40.
MAX_PROMPT_WORDS = 20

# How far past the visible pane the prompt's own capture reaches. The words the
# user is about to say are often on the command they ran two screens ago, and a
# capture-pane is cheap; 200 lines bounds what a session with a million-line
# scrollback can cost the request.
PROMPT_SCROLLBACK_LINES = 200

# Live-debugging aid for the empty-transcript phone bug: logs the pipeline's
# numbers per request and snapshots the last upload to fixed paths for offline
# inspection. Opt-in diagnostic — off by default; set POCKETTUI_VOICE_DEBUG=1
# to enable while chasing an issue.
VOICE_DEBUG = os.environ.get("POCKETTUI_VOICE_DEBUG", "0") == "1"
VOICE_DEBUG_ORIG_PATH = HERE / ".last_voice.orig"
VOICE_DEBUG_WAV_PATH = HERE / ".last_voice.wav"


def whisper_paths() -> tuple[Path | None, Path | None]:
    """(binary, model) for the local whisper install, or (None, None) if absent.

    Both halves are needed, so a half-finished setup_voice.sh run reads the same
    as no install at all rather than failing later inside the subprocess. None
    rather than an empty Path because Path("") is Path("."), which is a real
    (and truthy) path — an easy way to answer "installed" by accident.
    """
    missing = (None, None)
    env_bin = os.environ.get("POCKETTUI_WHISPER_BIN", "")
    binary = Path(env_bin) if env_bin else VOICE_DIR / "whisper-cli"
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        return missing

    env_model = os.environ.get("POCKETTUI_WHISPER_MODEL", "")
    if env_model:
        model = Path(env_model)
        return (binary, model) if model.is_file() else missing

    # base.en by preference, but any ggml in the directory beats none: swapping
    # the model is meant to be a matter of dropping a different file in.
    models = sorted(VOICE_DIR.glob("ggml-*.bin"))
    if not models:
        return missing
    preferred = [m for m in models if "base.en" in m.name]
    return binary, (preferred or models)[0]


# ---------------------------------------------------------------------------
# Parakeet-TDT (sherpa-onnx)
# ---------------------------------------------------------------------------
# The primary engine. Parakeet decodes an utterance in ~0.23 s where whisper
# base.en takes seconds, and it writes ordinary English better, so whisper stays
# only as the fallback for an install that has not fetched the ONNX model (or
# cannot import sherpa-onnx). The two engines are picked between per request,
# never blended: whichever runs, the resolver downstream sees the same shape.
#
# The model directory ships as one of sherpa-onnx's release tarballs; the v2
# (English) build is what setup_voice.sh fetches and what the probe prefers by
# name. Any directory holding the four expected files works, which is what makes
# swapping in a different Parakeet build a matter of dropping it into
# voice/parakeet/.
PARAKEET_DIR = VOICE_DIR / "parakeet"
PARAKEET_FILES = ("encoder.int8.onnx", "decoder.int8.onnx",
                  "joiner.int8.onnx", "tokens.txt")

# Beam search rather than greedy: both score the same on the 42-clip benchmark
# (16/42 exact after the resolver), but only modified_beam_search can take
# hotwords, and hotwords are the next step for this engine. Choosing it now
# means that step is a config change rather than a re-verification.
PARAKEET_DECODING = "modified_beam_search"

# Same ceiling whisper's threads have: enough to use the machine, bounded so a
# many-core server does not hand one utterance every core it owns.
PARAKEET_THREADS = min(4, os.cpu_count() or 4)

# The deadline whisper's subprocess timeout is for this engine, and it has to
# clear two hurdles: stay under the phone's 30 s fetch abort, so the server is
# the half that decides a decode has failed, and leave room for a request that
# spent part of its budget queued behind another decode (a typical one is
# ~0.3 s, so the queue is not what spends this).
PARAKEET_TIMEOUT_S = 20


def parakeet_model_dir() -> Path | None:
    """The Parakeet model directory, or None if this install has no usable one.

    Mirrors whisper_paths() down to the shape of the preference: an override
    wins, otherwise the directory is discovered by glob under voice/parakeet/,
    and a directory missing any of the four files it needs reads as "not
    installed" rather than failing later inside sherpa-onnx.

    The preference is by name, the way base.en is preferred among the ggml
    models. This product transcribes English — the whisper half only ever
    downloads `.en` models — and Parakeet's v2 is the English build where v3 is
    multilingual and measurably worse on English. So a v2 present anywhere in
    the directory wins, and only if there is none does the newest of whatever
    else is there get used. Someone who actually wants v3 names it in
    POCKETTUI_PARAKEET_MODEL, which is the same escape hatch
    POCKETTUI_WHISPER_MODEL is for the ggml side.
    """
    env_dir = os.environ.get("POCKETTUI_PARAKEET_MODEL", "")
    if env_dir:
        candidate = Path(env_dir)
        return candidate if _parakeet_dir_complete(candidate) else None

    # Descending, so that among equally-preferred builds the newest version
    # wins; the glob's shape is what makes any sherpa-onnx Parakeet tarball a
    # drop-in rather than something this function has to know the name of.
    candidates = [c for c in sorted(PARAKEET_DIR.glob("sherpa-onnx-*parakeet*"),
                                    reverse=True)
                  if _parakeet_dir_complete(c)]
    preferred = [c for c in candidates if "-v2-" in c.name]
    usable = preferred or candidates
    return usable[0] if usable else None


def _parakeet_dir_complete(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in PARAKEET_FILES)


def parakeet_available() -> bool:
    """Can this install decode with Parakeet — the module and a model both?

    The import is the expensive half of the answer (sherpa-onnx pulls in its
    native library), so it is attempted only once a model directory has been
    found; an install with no model never pays for it.

    A decode that ran past its deadline retires the engine (see run_parakeet),
    and this is where that shows: an install whose Parakeet is wedged reads as
    not installed, so voice_engine() falls back to whisper for a request that
    named no engine and answers not_setup for one that insisted on this one.
    """
    if _parakeet_dead or parakeet_model_dir() is None:
        return False
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:  # noqa: BLE001 — a broken wheel reads as "not installed"
        return False
    return True


def voice_engine(requested: str = "") -> str:
    """Which engine this request will use: "parakeet", "whisper", or "".

    "" means neither is installed, which is the only case that answers
    not_setup. POCKETTUI_VOICE_ENGINE forces a choice, and a forced engine that
    is not actually installed reports "" rather than silently falling through to
    the other one — a machine pinned to an engine should say so plainly instead
    of quietly running the model its operator ruled out.

    `requested` is the per-request choice a client may ask for, and it obeys the
    same rule: asking for an engine this install does not have answers "" rather
    than the other one, because the client is the half that owns the fallback
    and cannot choose one it is not told about. The env wins over it, so pinning
    a machine to an engine still overrides every phone talking to it. A value
    that names neither engine reads as no request at all.
    """
    forced = os.environ.get("POCKETTUI_VOICE_ENGINE", "").strip().lower()
    choice = forced if forced in ("parakeet", "whisper") else requested.strip().lower()
    if choice == "parakeet":
        return "parakeet" if parakeet_available() else ""
    if choice == "whisper":
        return "whisper" if whisper_paths()[0] is not None else ""
    if parakeet_available():
        return "parakeet"
    return "whisper" if whisper_paths()[0] is not None else ""


def _bpe_vocab_text(tokens: Path) -> str:
    """tokens.txt rendered as the "piece<TAB>score" vocabulary sherpa-onnx reads.

    The score is a constant -1.0: sherpa-onnx uses the vocab to *segment*
    hotwords, not to weight them (hotwords_score does the weighting), so the
    per-piece log-probability the format carries is never consulted.
    """
    lines = []
    for line in tokens.read_text(encoding="utf-8").splitlines():
        # "<piece> <id>" — split from the right, because a piece may itself
        # contain spaces and only the trailing id is guaranteed to be one field.
        piece = line.rsplit(" ", 1)[0]
        if piece:
            lines.append(f"{piece}\t-1.0")
    return "\n".join(lines) + "\n"


def parakeet_bpe_vocab(model_dir: Path) -> Path:
    """Path to the bpe.vocab sherpa-onnx needs for hotwords, writing it if absent.

    The release tarballs ship tokens.txt but no bpe.vocab, and sherpa-onnx needs
    the vocab to turn a hotword's spelling into the pieces the model decodes in.
    The file it wants is derivable from tokens.txt alone, so it is synthesized
    once rather than made a second download.

    Written beside the model where that directory is writable, and into a cache
    directory otherwise — a model tree mounted read-only, or shared between
    installs, must still be able to run this engine rather than failing at
    build time over a file it can regenerate in a second.
    """
    tokens = model_dir / "tokens.txt"
    vocab = model_dir / "bpe.vocab"
    if vocab.is_file():
        return vocab
    if not os.access(model_dir, os.W_OK):
        cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        vocab = cache / "pockettui" / f"{model_dir.name}.bpe.vocab"
        vocab.parent.mkdir(parents=True, exist_ok=True)
        if vocab.is_file():
            return vocab
    # Written whole via a temporary name, so an interrupted write cannot leave a
    # truncated vocab that would be trusted on the next request.
    tmp = vocab.with_name(vocab.name + ".part")
    tmp.write_text(_bpe_vocab_text(tokens), encoding="utf-8")
    tmp.replace(vocab)
    return vocab


# The recognizer costs ~1.8 s to build and nothing to keep, and this server is
# long-lived, so it is built on the first transcription and held for the life of
# the process. Keyed by model directory so an override taking effect mid-process
# (as the tests do) rebuilds rather than serving the previous model.
_parakeet_recognizer: tuple[str, object] | None = None

# Every touch of that resident recognizer goes through this one worker, which is
# the serialization: sherpa-onnx's OfflineRecognizer is a native object with no
# lock of its own, and the route runs in FastAPI's threadpool, so two phones
# recording at once would otherwise decode through the same object from two
# threads. A single worker makes the second request queue behind the first
# instead — ~0.3 s for a typical decode, which no one notices — and costs the
# uncontended case one submit round-trip.
_parakeet_pool: concurrent.futures.ThreadPoolExecutor | None = None

# Set when a decode outlives PARAKEET_TIMEOUT_S. There is no killing the native
# thread that is still inside onnxruntime, so the worker it holds is gone for
# the life of the process and every later decode would queue behind it forever.
# The flag retires the engine instead: parakeet_available() reads it, and the
# process has to be restarted to get Parakeet back.
_parakeet_dead = False


def parakeet_pool() -> concurrent.futures.ThreadPoolExecutor:
    """The single worker every recognizer call runs on, started on first use."""
    global _parakeet_pool
    if _parakeet_pool is None:
        _parakeet_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="parakeet")
    return _parakeet_pool


def parakeet_recognizer(model_dir: Path):
    """The resident OfflineRecognizer for `model_dir`, built on first use."""
    global _parakeet_recognizer
    key = str(model_dir)
    if _parakeet_recognizer is not None and _parakeet_recognizer[0] == key:
        return _parakeet_recognizer[1]

    import sherpa_onnx

    # modeling_unit and bpe_vocab travel together or not at all: sherpa-onnx
    # 1.13.6 segfaults on modeling_unit="bpe" with an empty bpe_vocab, taking
    # the whole server down rather than raising. Building the pair here — the
    # only place either is passed — is what makes that combination unreachable.
    vocab = parakeet_bpe_vocab(model_dir)
    recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(model_dir / "encoder.int8.onnx"),
        decoder=str(model_dir / "decoder.int8.onnx"),
        joiner=str(model_dir / "joiner.int8.onnx"),
        tokens=str(model_dir / "tokens.txt"),
        num_threads=PARAKEET_THREADS,
        model_type="nemo_transducer",
        decoding_method=PARAKEET_DECODING,
        modeling_unit="bpe",
        bpe_vocab=str(vocab),
    )
    _parakeet_recognizer = (key, recognizer)
    return recognizer


# How many hotwords one request may carry. Measured, not guessed: 500 words
# cost nothing over none in decode time (the context graph is built once per
# stream and walked alongside the beam), so the cap is set by how much
# vocabulary is worth having rather than by what the decoder can afford.
MAX_HOTWORDS = 500

# The boost applied to every hotword, and the ceiling no configuration may
# cross. Above roughly 1.5 the context graph outweighs the acoustic score and
# the decoder collapses into repeating hotwords back at the user — a failure
# that produces confident nonsense rather than an error, so the ceiling is
# structural: HOTWORD_SCORE_MAX clamps whatever is configured, which puts the
# cliff out of reach of a typo in the environment rather than merely
# undocumented.
HOTWORD_SCORE_MAX = 1.5
# 0.5 is the top of the band that leaves the 42-clip benchmark where it was
# (23/42 strict, 25/42 blind at every score from 0.15 to 0.5; 0.6 and above
# lose a clip). Neutral is the honest result and the expected one: the
# benchmark is dictated by someone whose vocabulary the model already handles,
# so what these words are for — a name only this user says — is the thing it
# cannot measure. The default is therefore chosen as the most bias available
# for free: the highest score that costs nothing on the clips that can be
# checked, with the first observed regression a clear step above it.
HOTWORD_SCORE_DEFAULT = 0.5


def hotword_score() -> float:
    """The per-hotword boost this request will use, clamped below the cliff.

    POCKETTUI_HOTWORD_SCORE is for tuning against a recording of one's own
    voice; anything unparseable reads as "unset" rather than as an error,
    because a malformed number in the environment must not cost a user their
    dictation.
    """
    raw = os.environ.get("POCKETTUI_HOTWORD_SCORE", "").strip()
    try:
        score = float(raw) if raw else HOTWORD_SCORE_DEFAULT
    except ValueError:
        score = HOTWORD_SCORE_DEFAULT
    return max(0.0, min(HOTWORD_SCORE_MAX, score))


def _hotword_pieces(word: str) -> list[str]:
    """`word` split into the pieces sherpa-onnx can actually bias towards.

    Two filters, both of them things the encoder does rather than opinions
    about what makes a good hotword:

    "/" is a separator, not a character. sherpa-onnx rewrites every "/" in the
    hotwords string to a newline before parsing, so a path handed over whole
    does not become one hotword — it becomes several, silently. Splitting here
    makes that explicit and keeps the count honest: a path contributes its
    segments, and the caller's budget is spent on segments it can see.

    Everything else has to survive the bpe encoder, whose vocabulary is the
    ASCII the model was trained on. A piece carrying anything else encodes to
    an out-of-vocabulary token, which sherpa-onnx logs and drops — so it is
    dropped here instead, where it costs a budget slot rather than a log line.
    """
    pieces = []
    for piece in word.split("/"):
        piece = piece.strip().strip(".,;:'\"()[]{}")
        # Two characters is the floor a bpe piece is worth boosting at: below
        # it the hotword matches inside half the words in the language and
        # biases towards noise.
        if len(piece) < 2 or len(piece) > 40:
            continue
        if not piece.isascii() or not any(c.isalpha() for c in piece):
            continue
        # Whitespace would make the line a multi-word phrase rather than the
        # single word it is meant to be, and ":" leads sherpa's per-line score
        # syntax. Neither can survive in a piece that reached here by splitting
        # a shell word, so this is a guard rather than a filter.
        if any(c.isspace() or c == ":" for c in piece):
            continue
        pieces.append(piece)
    return pieces


def _hotword_username() -> str:
    """The login name, or "" when there is none to read.

    The one word in the list that comes from the machine rather than from
    anything the request carried, so it is read here rather than passed in.
    Never raises: an account whose name the system will not give up costs the
    vocabulary one word, not the decode.
    """
    try:
        return str(getpass.getuser()).strip()
    except Exception:  # noqa: BLE001
        return ""


def parakeet_hotwords(history: list[str] | None = None,
                      learned: list[str] | None = None) -> str:
    """The vocabulary of this request as the hotwords string sherpa-onnx reads.

    One phrase per line, each carrying its own ":score" — the per-stream form,
    which is what lets the boost be configuration rather than something baked
    into the recognizer at build time. The score is written on every line or
    none: sherpa-onnx fills a missing score from the recognizer's default only
    for the *other* list it is merging with, so a partially-scored list gets a
    boost nobody chose.

    Learned words come first because they are the strongest evidence this
    server has — the user corrected this exact word by hand — and because the
    cap is a real one at this vocabulary's size. The login username comes next,
    always and from the system rather than from any source passed in: home and
    project paths are dictated constantly, and it is the one word whose absence
    is felt on every one of them. Reserving it here is what makes it a
    guarantee — Parakeet only ever had it when the scrollback happened to, and
    a decode with a cold history is exactly when the word is hardest to hear.
    Ahead of history is far enough: nothing the user merely typed can crowd it
    out of the cap. History and ssh hosts follow in the order they arrive, which
    history_vocabulary() has already sorted by how often and how recently the
    user typed them.

    History and ssh words are filtered twice more on the way in, because both
    of the ways that source wastes the cap are mechanical. Bare English is
    dropped for the same reason the prompt drops it — Parakeet already writes
    "and" and "here" correctly, so a slot spent there is a slot a name needed.
    And a numeric-suffix family collapses to its first member: one `echo LINE_1
    … LINE_60` in the scrollback would otherwise buy 60 of the 500 slots for a
    word the user typed once. Learned words bypass both, exactly as they bypass
    the prompt's shape test: shape is a guess at whether the model needs a word,
    and a learned word carries the answer.

    Empty in, near-empty out: a machine with no history and nothing learned is
    left with the username alone, and one whose login has no name at all gets no
    hotwords argument rather than an empty one.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    families: set[str] = set()
    # The username is a source of its own, between the two, and trusted like the
    # learned words: it is a name with no pronunciation to be right about, and
    # nothing about its shape would tell the filters that.
    for source, trusted in ((learned or [], True), ([_hotword_username()], True),
                            (history or [], False)):
        for word in source:
            for piece in _hotword_pieces(word):
                key = piece.lower()
                if key in seen:
                    continue
                if not trusted:
                    if _is_common_english(piece):
                        continue
                    family = _hotword_family(piece)
                    if family in families:
                        continue
                    families.add(family)
                seen.add(key)
                ordered.append(piece)
                if len(ordered) >= MAX_HOTWORDS:
                    return _hotwords_text(ordered)
    return _hotwords_text(ordered)


def _hotword_family(piece: str) -> str:
    """The key that makes `LINE_1` and `LINE_60` one word rather than sixty.

    Stripping the trailing digit run is the whole rule: `LINE_1` and `LINE_60`
    share the stem "line_", `global_step500` and `global_step2000` share
    "global_step". It is deliberately blunt, so `sherpa-v2` and `sherpa-v3`
    collapse too — telling a version suffix from a counter needs machinery this
    does not have, and losing the second version of a name costs one slot while
    keeping a generated family costs dozens.

    A stem shorter than three characters is not a family, it is a coincidence:
    `v1` and `v2` are separate words, not two members of "v".
    """
    stem = piece.rstrip("0123456789")
    if len(stem) == len(piece) or len(stem) < 3:
        return piece.lower()
    return stem.lower()


def _hotwords_text(words: list[str]) -> str:
    score = hotword_score()
    return "\n".join(f"{word} :{score}" for word in words)


def _parakeet_word_confidences(tokens: list[str],
                               ys_log_probs: list[float]) -> dict[str, float]:
    """How sure the decoder was of each word, keyed by the word lowercased.

    Parakeet scores BPE pieces, not words: `tokens` is the piece list and
    `ys_log_probs` its per-piece log-probs, one for one. A leading space marks
    the piece that starts a word, so the pieces between two of those markers —
    plus any punctuation, which attaches to the piece before it — are one word.

    A word's probability is the *minimum* of its pieces', not their product or
    their mean: one doubted piece is what makes a word suspect, and averaging
    would let the confident pieces of a long word hide it. A word said twice
    keeps the lower of the two, for the same reason.

    Keys are lowercased and stripped the way the resolver compares its tokens,
    so a caller can look up the word it is holding without normalising twice.
    """
    if len(tokens) != len(ys_log_probs) or not tokens:
        return {}
    confidences: dict[str, float] = {}
    word, worst = "", 0.0
    for index, (piece, log_prob) in enumerate(zip(tokens, ys_log_probs)):
        if index == 0 or piece.startswith(" "):
            if word:
                _keep_lower_confidence(confidences, word, worst)
            word, worst = piece.strip(), log_prob
        else:
            word += piece
            worst = min(worst, log_prob)
    if word:
        _keep_lower_confidence(confidences, word, worst)
    return confidences


def _keep_lower_confidence(confidences: dict[str, float], word: str,
                           log_prob: float) -> None:
    key = word.lower().strip(",.!?;:")
    if not key:
        return
    probability = math.exp(log_prob)
    confidences[key] = min(confidences.get(key, probability), probability)


def _doubted_summary(confidence: dict[str, float] | None, keep: int = 3) -> str:
    """The few words the decoder was least sure of, for the request log.

    The whole dict would drown the line, and the confident words are not what
    anyone reads a log for: the least-sure words are the ones that explain a
    transcript that came out wrong.
    """
    if not confidence:
        return "-"
    worst = sorted(confidence.items(), key=lambda item: item[1])[:keep]
    return ",".join(f"{word}:{prob:.2f}" for word, prob in worst)


def run_parakeet(model_dir: Path, wav: Path,
                 hotwords: str | None = None) -> tuple[str, dict[str, float] | None]:
    """The transcript of `wav` and its per-word confidences, or ("", None).

    `hotwords` is per-request vocabulary biasing — the channel that does for
    Parakeet what --prompt does for whisper. See parakeet_hotwords() for the
    string's shape; the recognizer is built with the bpe vocabulary that
    consumes it.

    The confidences are an improvement on the transcript, never a precondition
    for one: a result that carries no per-piece log-probs, or one whose pieces
    and log-probs do not line up, yields None rather than failing the decode.

    Raises subprocess.TimeoutExpired past PARAKEET_TIMEOUT_S, which is whisper's
    shape for the same failure and reaches the phone as transcribe_timeout.
    """
    with wave.open(str(wav)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            return "", None
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    # int16 PCM to the float32 in [-1, 1) sherpa-onnx wants. array rather than
    # numpy: this is the only array work in the server and numpy is not a
    # dependency it otherwise needs.
    pcm = array.array("h")
    pcm.frombytes(frames)
    samples = [s / 32768.0 for s in pcm]
    if not samples:
        return "", None

    # The build too, not only the decode: the first request after a restart
    # builds the recognizer, and two of them arriving together must not race to
    # build it twice. The result is read here as well, on the same worker: the
    # native result object belongs to the recognizer this thread owns, and
    # touching its fields from the caller's thread would put a second thread
    # back on the object the single worker exists to keep to itself.
    def decode() -> tuple[str, dict[str, float] | None]:
        recognizer = parakeet_recognizer(model_dir)
        stream = recognizer.create_stream(hotwords=hotwords) if hotwords \
            else recognizer.create_stream()
        stream.accept_waveform(rate, samples)
        recognizer.decode_stream(stream)
        result = stream.result
        try:
            confidence = _parakeet_word_confidences(list(result.tokens),
                                                    list(result.ys_log_probs))
        except Exception:  # noqa: BLE001 — a transcript without them is fine
            confidence = None
        return " ".join(result.text.split()), confidence or None

    global _parakeet_pool, _parakeet_dead
    future = parakeet_pool().submit(decode)
    try:
        return future.result(timeout=PARAKEET_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        # The worker is still inside the native decode and cannot be
        # interrupted, so the executor is abandoned rather than shut down —
        # shutdown() would block on the very thread that is stuck. Dropping the
        # reference leaves it to leak quietly for the life of the process,
        # which is the shorter of the two now that the engine is retired.
        _parakeet_dead = True
        _parakeet_pool = None
        raise subprocess.TimeoutExpired("parakeet decode", PARAKEET_TIMEOUT_S)


def _is_common_english(word: str) -> bool:
    """Is this a word whisper writes correctly with no help from the prompt?

    The common-word list holds base forms, so an inflected one ("results",
    "running", "machines") has to be reduced before it is looked up — otherwise
    every plural in the scrollback buys space in a budget meant for names.
    """
    lowered = word.lower()
    if lowered in resolver.COMMON_WORDS:
        return True
    for suffix in ("ing", "ers", "ed", "es", "er", "s"):
        if lowered.endswith(suffix) and len(lowered) - len(suffix) >= 3:
            stem = lowered[:-len(suffix)]
            # Both spelling changes English makes when it inflects: a dropped
            # "e" ("moving" → "move") and a doubled consonant ("running" → run).
            candidates = [stem, stem + "e"]
            if len(stem) >= 2 and stem[-1] == stem[-2]:
                candidates.append(stem[:-1])
            if any(c in resolver.COMMON_WORDS for c in candidates):
                return True
    return False


def _prompt_rarity(word: str) -> float:
    """How much this word needs to be in the prompt, 0.0 meaning not at all.

    whisper already writes ordinary English correctly, so a bare dictionary word
    in the vocabulary buys nothing and costs the budget a real identifier needs.
    What earns space is everything the model cannot reach on its own: names with
    separators or internal capitals, names carrying digits, and unusual bare
    words like `camerahmr` or `sdwivedi` that are not English at all.
    """
    bare = word.strip(".,;:")
    if not bare:
        return 0.0
    if _is_common_english(bare):
        return 0.0

    score = 0.5  # not a common word: already worth something
    if any(c in bare for c in "_./-"):
        score += 1.0
    if bare[1:] != bare[1:].lower():  # camelCase, HTTPServer
        score += 0.8
    if any(c.isdigit() for c in bare) and any(c.isalpha() for c in bare):
        score += 0.5
    # A plain lowercase run of letters is dictionary-shaped even when it is not
    # in the list, so it ranks below anything carrying a separator, a capital or
    # a digit. Length is the only signal left for telling `pancake` from
    # `camerahmr`, and it is a weak one, so it ramps rather than steps: a short
    # bare word is probably English, a long one is probably a coined name.
    if bare.isalpha() and bare.islower():
        score += min(0.4, max(-0.3, (len(bare) - 7) * 0.1))
    return score


# Learned words are the strongest evidence in the prompt — the user corrected
# whisper by hand, twice, on this exact word — but they are also the source
# least able to say whether today's utterance will contain them. Capped so a
# growing store cannot squeeze the screen and the cwd out of the 20 slots.
MAX_PROMPT_LEARNED = 5


def transcribe_prompt(screen: list[str], cwd: str,
                      history: list[str] | None = None,
                      scrollback: list[str] | None = None,
                      learned: list[str] | None = None) -> str:
    """A whisper --prompt naming the words this user is about to say.

    whisper conditions on the prompt as if it were text it had just decoded, so
    a natural sentence followed by the vocabulary biases it towards `pytest`
    over "pie test" without teaching it a format it then tries to imitate.

    The budget is spent on rarity rather than on source order alone. Every
    candidate is scored by how badly whisper needs it — `sdwivedi` and
    `test_camerahmr.py` are words the model cannot reach unaided, while `data`
    and `report` are words it already writes correctly — and the cap then cuts
    from the bottom of that ranking instead of from the end of the last source.

    `scrollback` is a wider capture of the same pane, reaching back past the
    visible screen. It is a separate parameter rather than a longer `screen`
    because screen_tokens() keeps only its last MAX_SCREEN_LINES lines — the
    exact window the register detection is entitled to see — so a longer list
    handed to it would be truncated back to the visible screen and the older
    lines lost.

    `learned` are words this user has corrected by hand in past transcripts,
    most recently reinforced first. They carry the highest source weight there
    is and are capped at MAX_PROMPT_LEARNED — see that constant.
    """
    scored: list[tuple[float, float, int, str]] = []
    seen: set[str] = set()
    order = 0
    lead = "Terminal session. Commands and files: "
    room = MAX_PROMPT_CHARS - len(lead)
    kept: list[str] = []
    used = 0

    def take(words, source: float, ranked: bool = False,
             floor: float = 0.0) -> None:
        """Score one source's words. `ranked` means the source already ordered
        them by importance, and that ordering is folded into the score.

        `floor` is a rarity this source's words are worth regardless of their
        shape. Only the learned source sets it: shape is a guess at whether
        whisper needs a word, and a learned word carries the answer — the user
        already corrected this exact word by hand, twice.
        """
        nonlocal order
        words = list(words)
        for position, word in enumerate(words):
            word = str(word).strip()
            # Single letters and pure numbers cost prompt budget and bias
            # nothing; the point is names the model would otherwise miss.
            if len(word) < 2 or word.lower() in seen or not any(c.isalpha() for c in word):
                continue
            rarity = max(_prompt_rarity(word), floor)
            if rarity <= 0.0:
                # A bare English word the model already spells correctly. It
                # would occupy budget a real identifier needs, so it is dropped
                # outright rather than merely ranked last.
                continue
            weight = source
            if ranked and words:
                # Rarity says how badly whisper needs a word; it does not say
                # whether the user will ever utter it. For a source that has
                # already ranked its words by how much they are really used,
                # that ranking must dominate shape — `echo LINE_1` … `LINE_60`
                # are sixty separator-and-digit tokens scoring the shape maximum
                # that this user ran once, and on shape alone they fill the
                # whole prompt ahead of the paths he types daily. Rarity is
                # scaled down to a tiebreaker so it orders words of comparable
                # standing instead of reordering the list wholesale.
                weight += 2.0 * (1.0 - position / len(words))
                rarity *= 0.25
            seen.add(word.lower())
            # `order` keeps the sort total and therefore deterministic: equal
            # scores stay in the order the sources produced them.
            scored.append((rarity + weight, weight, -order, word))
            order += 1

    username = ""
    try:
        # Home-directory paths are dictated constantly, and the username is
        # otherwise only in the vocab when it happens to be on screen.
        username = str(getpass.getuser()).strip()
    except Exception:
        username = ""
    # The username is always included, ahead of the ranking: home-directory paths
    # are dictated constantly and it is the one word whose absence is felt on
    # every single one of them. Its slot and characters are reserved here, before
    # any take() call, because `seen` only records that a word was *scored* — not
    # that it will survive the cut below. Reserving after scoring and testing
    # `not in seen` silently dropped the username whenever another source had
    # also offered it (history, a cwd path segment) and the ranking then filled
    # all MAX_PROMPT_WORDS slots with higher-scoring words. Adding it to `seen`
    # now also keeps every later source from scoring it a second time.
    if username and len(username) >= 2:
        seen.add(username.lower())
        kept.append(username)
        used += len(username) + 1

    # Learned words first, and at the top weight: every other source is a guess
    # that the user might say a word, while this one is a record that whisper
    # got this exact word wrong and the user fixed it by hand twice. Capped
    # rather than trusted wholesale — see MAX_PROMPT_LEARNED. `floor` keeps a
    # plain-looking learned word (`sdwivedi` scores like any lowercase run) from
    # being dropped by the shape test, which is precisely the case where shape
    # was the thing that failed.
    take(list(learned or [])[:MAX_PROMPT_LEARNED], 4.0, floor=1.5)
    # Source weight breaks ties between equally rare words: what is on screen is
    # what the user is looking at, and is likelier to be what they are saying.
    take(resolver.screen_tokens(screen), 3.0)
    # Scrollback second, so a word visible right now is claimed by the line
    # above at the full screen weight and only the older words arrive here.
    # 2.5 rather than 3.0: what has scrolled off is still the user's own
    # session and outranks the cwd listing, but a filename from an hour ago is
    # not what they are looking at. The capture overlaps the visible screen and
    # is passed whole — trimming it to the non-overlapping head would mean
    # guessing where `screen` ended, and `seen` already does that exactly.
    if scrollback:
        take(resolver.screen_tokens(scrollback, limit=len(scrollback)), 2.5)
    if cwd:
        names, branches = resolver.cwd_vocabulary(cwd)
        take(names, 2.0)
        take(branches, 2.0)
    take(tmux_names(), 1.0)
    # Last by source weight, and deliberately so: history is the broadest source
    # and the only one that can name a path nowhere near today's session, but a
    # word in front of the user beats one from last month at equal rarity.
    # `ranked`, because history_vocabulary() has already sorted it by how often
    # and how recently each word was actually used.
    take(history or [], 0.5, ranked=True)

    scored.sort(key=lambda t: (-t[0], -t[1], -t[2]))
    for _, _, _, word in scored:
        if len(kept) >= MAX_PROMPT_WORDS:
            break
        # `continue`, not `break`: a single very long path must not end the fill
        # when shorter words behind it still fit.
        if used + len(word) + 1 > room:
            continue
        kept.append(word)
        used += len(word) + 1
    if not kept:
        return ""
    # Space-separated, not comma-joined: whisper conditions on the prompt as
    # recently-decoded text, so a comma-separated list teaches it to emit
    # comma-fragmented transcripts for spoken paths (", slash, is, last,
    # cluster,"). Space-separated keeps the vocabulary bias without the
    # format imitation.
    return lead + " ".join(kept) + "."


# Below this fraction of full scale, no 20 ms frame in the clip holds anything
# whisper should be asked about. Real speech in the benchmark peaks at frame RMS
# around 0.3 of full scale, so this sits ~30x under the quietest thing that
# matters — the gate is meant to catch an accidental tap, never a soft talker.
SILENCE_RMS = 0.01
MIN_AUDIO_SECONDS = 0.5


@dataclasses.dataclass
class SilenceCheck:
    """The gate's verdict plus the metrics it was computed from.

    Truthy/falsy exactly like the plain bool this replaces (`__bool__` mirrors
    `verdict`), so every existing `if is_silent(wav):` / `assert is_silent(...)`
    call site keeps working unchanged; callers that want the numbers (logging)
    read the fields instead of recomputing them.
    """

    verdict: bool
    duration_s: float = 0.0
    peak_rms: float = 0.0        # fraction of full scale, whole-clip peak sample
    max_frame_rms: float = 0.0   # fraction of full scale, peak 20ms-frame RMS

    def __bool__(self) -> bool:
        return self.verdict


def is_silent(wav: Path) -> SilenceCheck:
    """Whether the clip holds no speech worth spending a transcription on.

    whisper hallucinates confidently on silence — it will answer a pocket tap
    with a sentence nobody said — so the cheap check happens here rather than
    letting the model invent something the user then has to delete.

    Anything unreadable answers False: a clip this cannot parse is whisper's
    problem to have, not a reason to silently drop what the user said.
    """
    try:
        with wave.open(str(wav), "rb") as clip:
            if clip.getsampwidth() != 2:  # decode_audio always writes s16
                return SilenceCheck(False)
            rate = clip.getframerate() or 16000
            frames = clip.getnframes()
            duration_s = frames / rate
            if frames < rate * MIN_AUDIO_SECONDS:
                return SilenceCheck(True, duration_s=duration_s)
            samples = array.array("h", clip.readframes(frames))
    except (OSError, wave.Error, ValueError):
        return SilenceCheck(False)
    if sys.byteorder == "big":
        samples.byteswap()  # wave data is little-endian
    if not samples:
        return SilenceCheck(True, duration_s=duration_s)

    peak_rms = max(abs(s) for s in samples) / 32768

    # Peak frame RMS, not overall: a short word inside a long quiet recording
    # has to keep the clip, and averaging would bury it.
    step = max(1, int(rate * 0.02))
    limit = SILENCE_RMS * 32768
    max_frame_rms = 0.0
    for start in range(0, len(samples) - step + 1, step):
        window = samples[start:start + step]
        rms = math.sqrt(sum(s * s for s in window) / len(window))
        max_frame_rms = max(max_frame_rms, rms)
        if rms >= limit:
            return SilenceCheck(False, duration_s=duration_s, peak_rms=peak_rms,
                                max_frame_rms=max_frame_rms / 32768)
    return SilenceCheck(True, duration_s=duration_s, peak_rms=peak_rms,
                        max_frame_rms=max_frame_rms / 32768)


def run_whisper(binary: Path, model: Path, wav: Path, prompt: str) -> str:
    """The transcript of `wav`, or "" if whisper produced nothing usable.

    LD_LIBRARY_PATH points at the install directory because the binary ships
    with its own ggml shared objects: the RUNPATH baked in at build time names
    wherever it was compiled, which is not where it ends up.
    """
    argv = [
        str(binary), "-m", str(model), "-f", str(wav),
        "-t", str(min(8, os.cpu_count() or 4)),
        "-nt",  # no timestamps — the compose bar wants a sentence
        "-np",  # no progress prints, so stdout is only the transcript
    ]
    if prompt:
        argv += ["--prompt", prompt]

    env = dict(os.environ)
    lib = str(binary.parent)
    env["LD_LIBRARY_PATH"] = (
        lib + os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else lib
    )
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=WHISPER_TIMEOUT_S, env=env)
    if proc.returncode != 0:
        return ""
    # whisper marks non-speech as bracketed events ([BLANK_AUDIO], [MUSIC]);
    # they are not words the user said, so they must not reach the compose bar.
    text = re.sub(r"[\[(](BLANK_AUDIO|INAUDIBLE|MUSIC|SOUND|NOISE)[^\])]*[\])]",
                  " ", proc.stdout, flags=re.IGNORECASE)
    return " ".join(text.split())


# iOS MediaRecorder writes fragmented mp4 (audio/mp4; codecs=mp4a.40.2) whose
# per-fragment timestamps restart at zero; ffmpeg's mp4 demuxer then decodes
# only the first fragment instead of the whole recording. Below this size an
# ADTS extraction is assumed to have failed rather than produced a real clip,
# so the direct-decode fallback runs instead of handing whisper a fragment.
MIN_EXTRACTED_AAC_BYTES = 256


def _looks_like_aac_in_mp4(content_type: str) -> bool:
    ct = content_type.lower()
    return "mp4" in ct or "aac" in ct


def _extract_aac(src: Path, aac: Path) -> bool:
    """Pull the raw AAC stream out of a (possibly fragmented) mp4 container.

    `-c copy -f adts` bypasses the mp4 demuxer's timestamp bookkeeping
    entirely, which is what the fragmented-recording bug lives in. ffmpeg
    prints many harmless "non monotonically increasing dts" warnings doing
    this — expected noise, not a sign of failure; only the exit code and the
    output size say whether it worked.
    """
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-y", "-i", str(src), "-c", "copy", "-f", "adts", str(aac)],
        capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S)
    return (proc.returncode == 0 and aac.exists()
            and aac.stat().st_size >= MIN_EXTRACTED_AAC_BYTES)


def _decode_to_wav(src: Path, wav: Path) -> bool:
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-t", str(MAX_AUDIO_SECONDS), "-i", str(src),
         "-ar", "16000", "-ac", "1", "-f", "wav", "-y", str(wav)],
        capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S)
    return proc.returncode == 0 and wav.exists() and wav.stat().st_size > 0


def decode_audio(raw: bytes, wav: Path, content_type: str = "") -> str:
    """Turn whatever the phone recorded into the 16 kHz mono WAV whisper wants.

    Returns "" on success or an error key. For AAC-in-mp4 uploads (iOS
    MediaRecorder), the container is often fragmented in a way that makes
    ffmpeg's mp4 demuxer decode only the first fragment; extracting the raw
    ADTS stream first (`-c copy -f adts`) bypasses that broken container
    bookkeeping, then the ADTS is decoded to WAV as usual. Any other input
    (webm/opus, wav, or a failed/undersized extraction) falls back to the
    existing direct decode, so the client's Content-Type is a hint, never a
    trust boundary — ffmpeg still reads the actual container from the bytes.
    """
    src = wav.with_suffix(".in")
    src.write_bytes(raw)

    if _looks_like_aac_in_mp4(content_type):
        aac = wav.with_suffix(".aac")
        if _extract_aac(src, aac) and _decode_to_wav(aac, wav):
            return ""

    if _decode_to_wav(src, wav):
        return ""
    return "undecodable_audio"


@app.get("/api/voice_status")
def api_voice_status() -> Response:
    """Which engines this install has, and which one a request would get.

    Two separate questions, because the client uses them for different things:
    `engines` says what it may offer the user to pick between, while `active` is
    what it gets by asking for nothing — "" when neither is installed, and the
    env-forced engine when the operator has pinned one, so a machine that ignores
    the picker says so here rather than in a surprising transcript.
    """
    return no_store(JSONResponse({
        "engines": {"parakeet": parakeet_available(),
                    "whisper": bool(whisper_paths()[0])},
        "active": voice_engine(),
    }))


@app.post("/api/transcribe")
async def api_transcribe(request: Request) -> Response:
    """Transcribe recorded audio into text the terminal would accept.

    The body is the raw recording rather than a multipart form: the phone has
    exactly one file to send, and reading it directly keeps python-multipart out
    of the dependency list. Every failure answers a shape the client can fall
    back on — it drops to the phone's own dictation rather than losing what the
    user just said.
    """
    raw = await request.body()
    session = str(request.query_params.get("session", ""))
    dev = str(request.query_params.get("dev", ""))
    engine = str(request.query_params.get("engine", ""))
    content_type = request.headers.get("content-type", "")
    # Reading the body needs the event loop, but ffmpeg and whisper must not
    # hold it for the seconds they take — every other session on this server
    # would stall behind them.
    return await run_in_threadpool(
        transcribe, raw, session, dev if DEV_RE.match(dev) else "",
        content_type=content_type, engine=engine)


def transcribe(raw: bytes, session: str, dev: str, content_type: str = "",
               engine: str = "") -> Response:
    """The transcription pipeline, off the event loop.

    Split from the route so the subprocess work runs in the threadpool the way
    every other handler here does, and so the tests can drive it without HTTP.
    `content_type` is only for the debug log line below (see VOICE_DEBUG) —
    decode_audio never trusts it, since ffmpeg reads the container from the
    bytes themselves. `engine` is what the client asked for, which voice_engine()
    weighs against the env and this install's assets.
    """
    # Assets before body: an install without the voice pieces answers the same
    # way whatever it was sent, which lets the phone probe with an empty body
    # before it records rather than telling the user after the fact.
    engine = voice_engine(engine)
    if not engine:
        return JSONResponse({"error": "not_setup"}, status_code=503)
    binary, model = whisper_paths() if engine == "whisper" else (None, None)
    if not shutil.which("ffmpeg"):
        return JSONResponse({"error": "no_ffmpeg"}, status_code=503)

    if not raw:
        return JSONResponse({"error": "empty_audio"}, status_code=422)
    if len(raw) > MAX_AUDIO_BYTES:
        return JSONResponse({"error": "audio_too_large"}, status_code=413)

    if VOICE_DEBUG:
        try:
            VOICE_DEBUG_ORIG_PATH.write_bytes(raw)
        except OSError:
            pass

    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="pockettui-voice-") as tmp:
            wav = Path(tmp) / "audio.wav"
            error = decode_audio(raw, wav, content_type)
            if error:
                log(f"transcribe content-type={content_type!r} bytes={len(raw)} "
                    f"decode_error={error}")
                return JSONResponse({"error": error}, status_code=422)

            if VOICE_DEBUG:
                try:
                    shutil.copyfile(wav, VOICE_DEBUG_WAV_PATH)
                except OSError:
                    pass

            # An accidental tap costs nothing here, rather than a second of CPU
            # and a hallucinated sentence in the user's compose bar.
            check = is_silent(wav)
            if check:
                # Tests may stub is_silent down to a plain bool; only a real
                # SilenceCheck carries metrics to log.
                duration_s = getattr(check, "duration_s", 0.0)
                peak_rms = getattr(check, "peak_rms", 0.0)
                max_frame_rms = getattr(check, "max_frame_rms", 0.0)
                # ffmpeg's `-t MAX_AUDIO_SECONDS` decode cap silently drops
                # anything past it; the client needs to know its upload was
                # cut short even when what survived reads as silence.
                truncated = duration_s >= MAX_AUDIO_SECONDS - 0.05
                log(f"transcribe content-type={content_type!r} bytes={len(raw)} "
                    f"duration={duration_s:.2f}s peak={peak_rms:.4f} "
                    f"max_frame_rms={max_frame_rms:.4f} silent=yes "
                    f"truncated={truncated} ms=0 raw=''")
                payload = {"text": "", "raw": "", "ms": 0}
                if truncated:
                    payload["truncated"] = True
                return no_store(JSONResponse(payload))

            # Gathered before the transcript so the prompt can steer the decode,
            # and reused after it for the register and the vocabulary index.
            target = resolve_target(session, dev)
            screen = capture_pane(target) if target else []
            cwd = pane_cwd(target) if target else ""
            # A second, wider capture for the prompt vocabulary only. The words
            # that have scrolled off are still worth biasing the decode
            # towards; they must not reach the resolver, where the register
            # detection and the window matching are entitled to see exactly the
            # visible pane and nothing older.
            scrollback = capture_pane(target, lines=PROMPT_SCROLLBACK_LINES) if target else []

            # The paths a user dictates are frequently nowhere near this pane —
            # a cluster mount typed a hundred times last month is in neither the
            # screen nor the cwd, and the history file is the only source that
            # has ever seen those words. Parsed once and cached, so this is a
            # dict lookup on every request after the first.
            # Configured ssh hosts ride the same channel, after history: they
            # are the same kind of word (something the user says that is
            # nowhere in this pane) and belong at the same low weight.
            # Home dotfile names ride it last, for the same reason and one more
            # of their own: "open bashrc" is a word a zsh user's history has
            # never contained, so this is the only source that has it at all.
            history = (resolver.history_vocabulary() + resolver.ssh_hosts()
                       + resolver.dotfile_names())
            # Words this user has corrected by hand in past transcripts. Read
            # from the same cached-on-(mtime, size) store the resolver uses, so
            # this is a dict lookup once the file has been read.
            learned = resolver.learned_words()

            decode_started = time.monotonic()
            hotword_count = 0
            confidence = None
            if engine == "parakeet":
                # No prompt: Parakeet takes none. The same vocabulary rides
                # hotwords instead, which is this engine's equivalent channel.
                # Biasing is an improvement to the decode, never a precondition
                # for one — anything that goes wrong assembling or applying it
                # costs the user their hotwords, not their transcript.
                try:
                    hotwords = parakeet_hotwords(history, learned)
                except Exception:  # noqa: BLE001 — decode without them instead
                    hotwords = ""
                hotword_count = len(hotwords.splitlines()) if hotwords else 0
                try:
                    text, confidence = run_parakeet(parakeet_model_dir(), wav,
                                                    hotwords=hotwords or None)
                except subprocess.TimeoutExpired:
                    # A deadline is not a hotword problem, and retrying without
                    # them would queue a second 20 s wait behind the worker that
                    # is already stuck — the phone would wait 40 s for the
                    # answer the first failure already knew.
                    raise
                except Exception:  # noqa: BLE001
                    hotword_count = 0
                    text, confidence = run_parakeet(parakeet_model_dir(), wav)
            else:
                text = run_whisper(binary, model, wav,
                                   transcribe_prompt(screen, cwd, history,
                                                     scrollback, learned))
            decode_ms = int((time.monotonic() - decode_started) * 1000)
            ms = int((time.monotonic() - started) * 1000)

        truncated = check.duration_s >= MAX_AUDIO_SECONDS - 0.05
        log(f"transcribe content-type={content_type!r} bytes={len(raw)} "
            f"duration={check.duration_s:.2f}s peak={check.peak_rms:.4f} "
            f"max_frame_rms={check.max_frame_rms:.4f} silent=no "
            f"truncated={truncated} engine={engine} hotwords={hotword_count} "
            f"decode_ms={decode_ms} ms={ms} "
            f"doubted={_doubted_summary(confidence)} raw={text[:80]!r}")

        if not text:
            payload = {"text": "", "raw": "", "ms": ms}
            if truncated:
                payload["truncated"] = True
            return no_store(JSONResponse(payload))

        result = resolver.resolve(text, screen=screen, cwd=cwd,
                                  tmux_names=tmux_names(),
                                  budget=TRANSCRIBE_BUDGET_S, asr=True,
                                  extra_vocab=history, confidence=confidence)
        # Doubt is only worth flagging to the user for a word that actually
        # reached them: the resolver already rewrites plenty of what the
        # decoder got wrong, and re-litigating its low-confidence guesses
        # after they've been corrected would just teach the user to distrust
        # words that are no longer there. So this walks result["text"], the
        # final surface the phone shows, not the raw ASR output the decoder
        # produced before resolver.resolve() had a chance to fix it.
        unsure: list[str] = []
        if confidence:
            seen: set[str] = set()
            for word in result["text"].split():
                key = word.lower().strip(",.!?;:")
                if (key and key not in seen and key in confidence
                        and confidence[key] < resolver.ASR_CONF_LOW):
                    seen.add(key)
                    unsure.append(word)
        if unsure:
            log(f"transcribe unsure={','.join(unsure)}")
        payload = {"text": result["text"], "raw": text, "ms": ms}
        if truncated:
            payload["truncated"] = True
        if unsure:
            payload["unsure"] = unsure
        return no_store(JSONResponse(payload))
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "transcribe_timeout"}, status_code=500)
    except Exception:  # noqa: BLE001 — the phone gets a shape, never a traceback
        return JSONResponse({"error": "transcribe_failed"}, status_code=500)


@app.post("/api/learn")
def api_learn(body: dict = Body(...)) -> Response:
    """Record that the user edited a transcript before sending it.

    The edit is the only labelled data this feature ever gets: whisper proposed
    `heard`, the user sent `sent`, and the difference between them names the
    words it got wrong. Extraction and every gate on it live in the resolver —
    this route only carries the pair across and appends what survives.

    Always answers 200. The phone fires this and forgets it; there is no UI for
    a failure to reach, and a learning miss costs the user nothing they would
    ever notice. Nothing leaves this machine.
    """
    heard = str(body.get("heard", "") or "")
    sent = str(body.get("sent", "") or "")
    learned = 0
    if heard.strip() and sent.strip() and heard != sent:
        try:
            learned = resolver.learn_corrections(heard, sent)
        except Exception:  # noqa: BLE001 — never let a lesson break a send
            learned = 0
    return no_store(JSONResponse({"learned": learned}))


@app.get("/api/learned")
def api_learned() -> Response:
    """Every correction the store holds, so the user can see what was learned.

    Newest-use first, unpromoted entries included: a pair the user has only
    corrected once is exactly the one they most want to catch before it fires
    twice and starts rewriting transcripts on its own.
    """
    entries = sorted(resolver.load_learned(), key=lambda e: e["last_ts"], reverse=True)
    out = [{
        "wrong": e["wrong"],
        "right": e["right"],
        "count": e["count"],
        "utterances": e["utterances"],
        "last_ts": e["last_ts"],
        "promoted": e["utterances"] >= resolver.LEARNED_PROMOTE_AT,
    } for e in entries]
    return no_store(JSONResponse({"entries": out}))


@app.post("/api/learned_delete")
def api_learned_delete(body: dict = Body(...)) -> Response:
    """Remove one learned pair, or all of them with `{"all": true}`."""
    if body.get("all"):
        deleted = resolver.clear_learned()
    else:
        wrong = str(body.get("wrong", "") or "")
        right = str(body.get("right", "") or "")
        deleted = resolver.delete_correction(wrong, right) if wrong and right else 0
    return no_store(JSONResponse({"deleted": deleted}))


@app.post("/api/session")
def api_new_session(request: Request, body: dict = Body(...)) -> Response:
    """Create a detached tmux session the phone can then attach to.

    Detached because this server has no tty to attach from; the phone picks the
    session up over the usual WebSocket route once it appears in the list.
    """
    refusal = throttled("session_mutate", RATE_SESSION_MUTATE, request)
    if refusal is not None:
        return refusal

    name, error = validate_session_name(body.get("name", ""))
    if error:
        return JSONResponse({"error": error}, status_code=400)

    start_dir = str(body.get("dir", "")).strip()
    start_dir = os.path.expanduser(start_dir) if start_dir else os.path.expanduser("~")
    if not os.path.isdir(start_dir):
        return JSONResponse(
            {"error": f"No such directory: {start_dir}"}, status_code=400)

    # Terminal multiplexers that wrap the shell (cmux) export a ZDOTDIR pointing
    # at a per-attach relay dir, and it sticks in the tmux server environment
    # long after that dir is gone. New sessions inherit it, zsh looks for .zshrc
    # somewhere that does not exist, and the pane comes up with none of the
    # user's config. Pin it to $HOME — where zsh looks when ZDOTDIR is unset.
    # Setting it empty does NOT work: zsh then resolves .zshrc against "", and
    # new-session has no -u to drop a variable outright.
    rc, _ = tmux("new-session", "-d", "-e", f"ZDOTDIR={os.path.expanduser('~')}",
                 "-s", name, "-c", start_dir)
    if rc != 0:
        return JSONResponse({"error": f"tmux could not create '{name}'."},
                            status_code=500)
    return no_store(JSONResponse({"session": name}))


# Paths printed by a remote session may name storage under a mount point that
# differs from where the same storage is mounted here — rewrite the prefix so
# a path tapped in the terminal resolves to the local mount. Configured via
# POCKETTUI_PATH_REWRITES, a comma-separated list of src:dst pairs; empty (and
# no rewriting) when unset.
def _parse_path_rewrites(raw: str) -> list[tuple[str, str]]:
    rewrites = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        parts = entry.split(":")
        if len(parts) != 2:
            continue
        src, dst = parts[0].strip(), parts[1].strip()
        if not src or not dst:
            continue
        rewrites.append((src, dst))
    return rewrites


PATH_REWRITES = _parse_path_rewrites(os.environ.get("POCKETTUI_PATH_REWRITES", ""))


MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}


@app.get("/api/file")
def api_file(request: Request, path: str = "") -> Response:
    """Serve an image or video by absolute path, for tap-to-view in the terminal.

    No restriction on where the file lives beyond the extension allowlist: this
    server already bridges a full shell to the same clients, so reading a file
    off disk crosses no boundary they could not cross by typing `cat`.

    Every rejection answers 404 alike, so a probe learns nothing about which
    check failed — or about what exists. Range requests (which iOS video needs)
    are handled by starlette's FileResponse itself.
    """
    refusal = throttled("file", RATE_FILE, request)
    if refusal is not None:
        return refusal

    # Tools print paths with a leading ~ as often as expanded ones.
    if path.startswith("~"):
        path = str(Path.home()) + path[1:]
    p = Path(path)
    if not p.is_absolute():
        return Response(status_code=404)
    if not p.exists():
        for src, dst in PATH_REWRITES:
            if path.startswith(src):
                alt = Path(dst + path[len(src):])
                if alt.exists():
                    p = alt
                    break
    p = p.resolve()
    kind = MEDIA_TYPES.get(p.suffix.lower())
    if kind is None or not p.is_file():
        return Response(status_code=404)
    return no_store(FileResponse(p, media_type=kind))


# ---------------------------------------------------------------------------
# File explorer
# ---------------------------------------------------------------------------
# Browse, read and edit files from the phone. Same design stance as /api/file:
# this server already bridges a full shell to the same clients, so a filesystem
# route crosses no boundary they could not cross by typing `cat` — the checks
# below exist for honest errors, not access control.

# The editor is for files a phone can sensibly hold and render; past this it is
# a job for the shell (or for /api/fs/download).
MAX_TEXT_BYTES = 2 * 1024 * 1024
# An upload is a file the user picked from the phone, not a stream: capped
# where a phone's patience runs out rather than where the disk does.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def fs_path(raw: str) -> Path | None:
    """`raw` as an absolute local path, or None when it cannot be one.

    The same treatment /api/file gives its path: a leading ~ is the user's
    home, and a path with nothing at it is retried through
    POCKETTUI_PATH_REWRITES so a remote session's mount names still resolve
    here. A path that exists nowhere comes back unrewritten — the create
    routes point at paths that do not exist yet, and they mean them literally.
    Deliberately no resolve(): a symlinked directory keeps its own name, so
    the breadcrumb the phone shows matches the path the user walked.
    """
    path = str(raw or "")
    if path.startswith("~"):
        path = str(Path.home()) + path[1:]
    if not path.startswith("/"):
        return None
    if not os.path.lexists(path):
        for src, dst in PATH_REWRITES:
            if path.startswith(src):
                alt = dst + path[len(src):]
                if os.path.lexists(alt):
                    return Path(alt)
    return Path(path)


def fs_error(code: str, status: int, **extra) -> Response:
    return no_store(JSONResponse({"error": code, **extra}, status_code=status))


def fs_hash(data: bytes) -> str:
    """The write-conflict token: what is on disk, as content rather than time.

    mtime is not the token because its granularity lies on some filesystems —
    two writes inside one timestamp tick would read as "unchanged". A missing
    file hashes to "", which is also what a client sends to mean "I am
    creating this file"; the two are the same fact.
    """
    return hashlib.sha256(data).hexdigest()


def fs_current_hash(p: Path) -> str:
    try:
        return fs_hash(p.read_bytes())
    except OSError:
        return ""


def atomic_write(p: Path, data: bytes, mode: int | None) -> None:
    """Write `data` to `p` whole-or-not-at-all.

    tempfile-then-replace in the target's own directory, so a crash mid-write
    can never leave a half-written file and a reader never sees one. An
    existing file keeps its mode; a new one gets a plain 0o644 rather than
    mkstemp's private 0o600 — this is the user's own tree, not a secret like
    the token file.
    """
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix="." + p.name + ".")
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), stat.S_IMODE(mode) if mode is not None else 0o644)
            fh.write(data)
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@app.get("/api/fs/list")
def api_fs_list(path: str = "") -> Response:
    """A directory's entries: dirs first, then everything else, alphabetical.

    Dotfiles included — on this machine they are half of what anyone opens an
    editor for. No path defaults to $HOME, which is where the session list's
    folder button starts; `home` rides along so the client can shorten paths
    under it to ~ without a second request.
    """
    p = fs_path(path or "~")
    if p is None:
        return fs_error("bad_path", 400)
    if not p.exists():
        return fs_error("not_found", 404)
    if not p.is_dir():
        return fs_error("not_a_directory", 400)
    try:
        names = os.listdir(p)
    except OSError:
        return fs_error("not_readable", 403)
    entries = []
    for name in names:
        # Classified through the symlink, so a linked directory is enterable
        # and a linked file editable. "link" is what remains — a broken link
        # or a special file — which the client can only offer to download.
        try:
            st = os.stat(p / name)
            kind = "dir" if stat.S_ISDIR(st.st_mode) else (
                "file" if stat.S_ISREG(st.st_mode) else "link")
        except OSError:
            st, kind = None, "link"
        entries.append({
            "name": name,
            "type": kind,
            "size": st.st_size if st is not None and kind == "file" else 0,
            "mtime": int(st.st_mtime) if st is not None else 0,
        })
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower(), e["name"]))
    return no_store(JSONResponse(
        {"path": str(p), "home": str(Path.home()), "entries": entries}))


@app.get("/api/fs/read")
def api_fs_read(path: str = "") -> Response:
    """A text file's content, plus the hash the editor will write back against.

    Binary (a null byte anywhere) is refused outright — there is nothing an
    editor could show. Text that merely is not clean UTF-8 is decoded with
    replacement characters instead and flagged `lossy`: the phone can still
    *read* it, but writing the decoded form back would corrupt the file, so
    the client opens it read-only.
    """
    p = fs_path(path)
    if p is None:
        return fs_error("bad_path", 400)
    if not p.is_file():
        return fs_error("not_found", 404)
    try:
        st = p.stat()
        if st.st_size > MAX_TEXT_BYTES:
            return fs_error("too_large", 413, size=st.st_size)
        data = p.read_bytes()
    except OSError:
        return fs_error("not_readable", 403)
    if b"\x00" in data:
        return fs_error("binary_file", 415)
    try:
        content, lossy = data.decode("utf-8"), False
    except UnicodeDecodeError:
        content, lossy = data.decode("utf-8", errors="replace"), True
    return no_store(JSONResponse({
        "path": str(p), "content": content, "hash": fs_hash(data),
        "size": len(data), "mtime": int(st.st_mtime), "lossy": lossy,
    }))


@app.post("/api/fs/write")
def api_fs_write(body: dict = Body(...)) -> Response:
    """Write a file atomically, guarded by the hash the editor read it at.

    `hash` "" means "I am creating this file": an existing one answers 409
    rather than being clobbered, exactly as a stale hash does — both are the
    same fact, the file on disk is not what the editor thinks it is. The 409
    carries what is there now (hash "" when the file is gone) and no content;
    the client refetches, or resends against the reported hash to overwrite.
    """
    p = fs_path(str(body.get("path", "")))
    if p is None:
        return fs_error("bad_path", 400)
    content = body.get("content")
    if not isinstance(content, str):
        return fs_error("bad_content", 400)
    base = str(body.get("hash", "") or "")

    try:
        st = os.stat(p)
    except OSError:
        st = None
    if st is not None and not stat.S_ISREG(st.st_mode):
        return fs_error("not_a_file", 400)
    current = fs_current_hash(p) if st is not None else ""
    if current != base:
        return fs_error("conflict", 409, hash=current,
                        mtime=int(st.st_mtime) if st is not None else None)
    if not p.parent.is_dir():
        return fs_error("not_found", 404)

    data = content.encode("utf-8")
    try:
        atomic_write(p, data, st.st_mode if st is not None else None)
    except OSError:
        return fs_error("not_writable", 403)
    # The new token, so the editor keeps saving without a round-trip re-read.
    return no_store(JSONResponse({"hash": fs_hash(data),
                                  "mtime": int(os.stat(p).st_mtime)}))


@app.post("/api/fs/mkdir")
def api_fs_mkdir(body: dict = Body(...)) -> Response:
    p = fs_path(str(body.get("path", "")))
    if p is None:
        return fs_error("bad_path", 400)
    try:
        os.mkdir(p)
    except FileExistsError:
        return fs_error("exists", 409)
    except FileNotFoundError:
        return fs_error("not_found", 404)
    except OSError:
        return fs_error("not_writable", 403)
    return no_store(JSONResponse({"path": str(p)}))


@app.post("/api/fs/rename")
def api_fs_rename(body: dict = Body(...)) -> Response:
    """Rename (or move) without clobbering: an existing destination is a 409.

    Check-then-rename is not airtight against a concurrent create, but the
    only other writer on this machine is the user's own shell — the check is
    for honest answers, not locking.
    """
    src = fs_path(str(body.get("src", "")))
    dst = fs_path(str(body.get("dst", "")))
    if src is None or dst is None:
        return fs_error("bad_path", 400)
    if not os.path.lexists(src):
        return fs_error("not_found", 404)
    if os.path.lexists(dst):
        return fs_error("exists", 409)
    try:
        os.rename(src, dst)
    except OSError:
        return fs_error("not_writable", 403)
    return no_store(JSONResponse({"path": str(dst)}))


@app.post("/api/fs/delete")
def api_fs_delete(body: dict = Body(...)) -> Response:
    """Delete a file; a directory only when it is already empty.

    Deliberately no recursive mode: a tree is never one tap (or one fat-thumbed
    confirm) from gone, and the shell — where rm -r asks nothing — is a tap
    away for anyone who means it.
    """
    p = fs_path(str(body.get("path", "")))
    if p is None:
        return fs_error("bad_path", 400)
    if not os.path.lexists(p):
        return fs_error("not_found", 404)
    try:
        # A symlink to a directory is a link, and deleting it must remove the
        # link — never walk into what it points at.
        if p.is_dir() and not p.is_symlink():
            os.rmdir(p)
        else:
            os.unlink(p)
    except OSError as err:
        # POSIX says ENOTEMPTY; some systems report EEXIST for the same fact.
        if err.errno in (errno.ENOTEMPTY, errno.EEXIST):
            return fs_error("not_empty", 409)
        return fs_error("not_writable", 403)
    return no_store(JSONResponse({"deleted": str(p)}))


@app.post("/api/fs/upload")
async def api_fs_upload(request: Request) -> Response:
    """Store a raw upload at ?path=. Mirrors /api/transcribe's shape: the body
    is the file itself, not a multipart form — the phone has exactly one file
    to send, and this keeps python-multipart out of the dependency list.

    No clobber unless ?overwrite=1, which the client only sends after asking.
    """
    raw = await request.body()
    path = str(request.query_params.get("path", ""))
    overwrite = request.query_params.get("overwrite", "") == "1"
    return await run_in_threadpool(fs_store_upload, raw, path, overwrite)


def fs_store_upload(raw: bytes, path: str, overwrite: bool) -> Response:
    p = fs_path(path)
    if p is None:
        return fs_error("bad_path", 400)
    if len(raw) > MAX_UPLOAD_BYTES:
        return fs_error("too_large", 413)
    if not p.parent.is_dir():
        return fs_error("not_found", 404)
    mode = None
    if os.path.lexists(p):
        if not overwrite:
            return fs_error("exists", 409)
        if not p.is_file():
            return fs_error("not_a_file", 400)
        mode = os.stat(p).st_mode
    try:
        atomic_write(p, raw, mode)
    except OSError:
        return fs_error("not_writable", 403)
    return no_store(JSONResponse({"path": str(p), "size": len(raw)}))


@app.get("/api/fs/download")
def api_fs_download(path: str = "") -> Response:
    """Any file, as an attachment.

    Distinct from /api/file, which stays a media-only inline route for the
    terminal's tap-to-view: this one is the explorer's get-it-onto-the-phone
    path, and it types nothing — the phone's own viewer decides.
    """
    p = fs_path(path)
    if p is None or not p.is_file():
        return fs_error("not_found", 404)
    return no_store(FileResponse(p, media_type="application/octet-stream",
                                 filename=p.name))


# A download the *browser* performs, rather than the page. Fetching the file
# with the token header means holding all of it in the PWA's memory before the
# save sheet ever opens, which iOS answers by killing the app; handing the
# browser a URL lets it stream to disk, and FileResponse serves it in chunks.
#
# A navigation carries no header, so the link carries its own credential: an
# HMAC over the path and an expiry, keyed on a secret minted at startup. The
# key never leaves this process and dies with it, so a restart voids every
# outstanding link — the right lifetime for something that lives a minute.
# Nothing else changes: the mint is gated like its neighbours, and the signed
# route resolves the path through the same fs_path() every /api/fs/* route
# uses, so it can reach exactly what an authenticated caller could reach.
DOWNLOAD_KEY = secrets.token_bytes(32)
DOWNLOAD_TTL = 60


def download_sig(path: str, expires: int) -> str:
    return hmac.new(DOWNLOAD_KEY, f"{expires}\n{path}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


@app.get("/api/fs/download_link")
def api_fs_download_link(path: str = "") -> Response:
    """Mint a short-lived signed URL for `path`, relative like the client's own.

    Answered relative (no leading slash) because that is what apiURL() composes
    against: the same string works same-origin, behind `tailscale serve`'s
    /pockettui/ prefix, and against a cross-origin backend.

    What is signed is the path *after* fs_path() — expanded and rewritten — so
    the signed route resolves the same file this one checked, not a spelling of
    it that could land somewhere else.
    """
    p = fs_path(path)
    if p is None or not p.is_file():
        return fs_error("not_found", 404)
    target = str(p)
    expires = int(time.time()) + DOWNLOAD_TTL
    query = urllib.parse.urlencode({"path": target, "exp": expires,
                                    "sig": download_sig(target, expires)})
    return no_store(JSONResponse({"url": f"api/fs/signed_download?{query}",
                                  "name": p.name, "expires_in": DOWNLOAD_TTL}))


@app.get("/api/fs/signed_download")
def api_fs_signed_download(request: Request, path: str = "", exp: str = "",
                           sig: str = "") -> Response:
    """Stream a file whose link this server signed. Unauthenticated by design.

    Throttled where its neighbours are not, because this is the only /api/ route
    a stranger can call at all: without the key they cannot get past the
    signature, and the bucket keeps them from trying at speed. The signature is
    checked before the expiry, so a tampered path never reads as merely stale.
    """
    refusal = throttled("download", RATE_FILE, request)
    if refusal is not None:
        return refusal
    try:
        expires = int(exp)
    except ValueError:
        return fs_error("bad_signature", 403)
    if not hmac.compare_digest(sig, download_sig(path, expires)):
        return fs_error("bad_signature", 403)
    if time.time() > expires:
        return fs_error("expired", 403)
    p = fs_path(path)
    if p is None or not p.is_file():
        return fs_error("not_found", 404)
    return no_store(FileResponse(p, media_type="application/octet-stream",
                                 filename=p.name))


# ---------------------------------------------------------------------------
# Pasted images
# ---------------------------------------------------------------------------
# An image on the clipboard reaches Claude Code as a path on the prompt line,
# which means the bytes have to land somewhere first. They land here, and the
# route answers with the absolute path the client types.

# A clipboard image is a screenshot, not a photo library; past this the paste
# was a mistake worth answering quickly.
MAX_IMAGE_BYTES = 15 * 1024 * 1024
# Under $HOME rather than beside this file: images churn on every paste, and
# staged inside the repo they would dirty `git status` continuously. A $HOME
# holding a space would break the space-free path the client pastes without
# bracketing — out of scope.
IMAGE_DIR = Path.home() / ".pockettui" / "images"
IMAGE_KEEP = 30


def sniff_image(raw: bytes) -> str:
    """The extension `raw` earns from its magic bytes, or "" for anything else.

    The client's Content-Type is never consulted — the same stance decode_audio
    takes with audio, where the header is a hint and the bytes are the fact.
    All four extensions are in MEDIA_TYPES above, so a staged image is already
    servable back through GET /api/file for tap-to-view.
    """
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ""


def prune_images(d: Path) -> None:
    """Keep the newest IMAGE_KEEP staged pastes; unlink the rest.

    Count rather than age: one rule, no clock math. Pasting more than
    IMAGE_KEEP images before Claude Code has read the first would evict an
    unread one — a wide enough window to accept that. Each unlink stands alone
    because a file deleted underneath us is not a reason to fail the upload
    that just succeeded.
    """
    try:
        staged = sorted(d.glob("paste-*"), key=lambda p: p.stat().st_mtime,
                        reverse=True)
    except OSError:
        return
    for stale in staged[IMAGE_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            pass


def store_image(raw: bytes, session: str, dev: str) -> Response:
    """Stage a pasted image, off the event loop.

    Split from the route the way transcribe is, so the write runs in the
    threadpool and the tests can drive it without HTTP. `session` and `dev` are
    the client's context for the log line only — an image lands in the same
    place whoever pasted it.

    Unlike /api/fs/upload, a missing directory is created rather than a 404:
    the client names no path here, so there is nothing for it to have got wrong.
    """
    if not raw:
        return JSONResponse({"error": "empty"}, status_code=422)
    if len(raw) > MAX_IMAGE_BYTES:
        return JSONResponse({"error": "too_large"}, status_code=413)
    ext = sniff_image(raw)
    if not ext:
        return JSONResponse({"error": "not_image"}, status_code=422)

    # No spaces or colons anywhere in the name: the client pastes this path
    # straight at a prompt, which may not be bracketing what it receives.
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    p = IMAGE_DIR / f"paste-{stamp}-{secrets.token_hex(3)}{ext}"
    try:
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write(p, raw, None)
    except OSError:
        return JSONResponse({"error": "not_writable"}, status_code=500)
    # After the write, so the file just staged counts as one of the survivors.
    prune_images(IMAGE_DIR)
    log(f"image session={session!r} dev={dev!r} bytes={len(raw)} name={p.name}")
    return no_store(JSONResponse({"path": str(p), "bytes": len(raw)}))


@app.post("/api/image")
async def api_image(request: Request) -> Response:
    """Stage an image the user pasted, and answer with its path.

    The body is the image itself rather than a multipart form, the shape
    /api/transcribe and /api/fs/upload already take. Whatever Content-Type the
    browser hung on the clipboard blob is ignored — store_image sniffs the
    bytes, so a mislabelled paste is rejected on what it actually is.
    """
    refusal = throttled("file", RATE_FILE, request)
    if refusal is not None:
        return refusal
    raw = await request.body()
    session = str(request.query_params.get("session", ""))
    dev = str(request.query_params.get("dev", ""))
    return await run_in_threadpool(
        store_image, raw, session, dev if DEV_RE.match(dev) else "")


# ---------------------------------------------------------------------------
# PTY <-> WebSocket bridge
# ---------------------------------------------------------------------------

def set_winsize(fd: int, cols: int, rows: int) -> bool:
    """Put `cols`x`rows` on the PTY. False means it was already that size.

    A size tmux already has is not written at all. Every client re-sends its
    size the moment it connects, and a resize is activity — under
    `window-size latest` an activity is what hands the shared window to this
    client. The kernel would swallow a no-op TIOCSWINSZ anyway; not making the
    call is the version of that which does not depend on the kernel.
    """
    cols = max(2, min(int(cols), 500))
    rows = max(2, min(int(rows), 300))
    try:
        cur = struct.unpack(
            "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]
    except OSError:
        cur = ()
    if cur == (rows, cols):
        return False
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    return True


# The window a device is looking at should be sized for that device, and tmux
# under `window-size latest` gives it to whichever client was active last.
# Typing counts; a fresh attach counts; refresh-client does not, and neither
# does re-writing a size tmux already has (see set_winsize).
#
# SIGWINCH to our own child is the one thing that reads as a real terminal
# resize without being one. The child execs into a tmux *client*, whose
# SIGWINCH handler sends MSG_RESIZE to the server; the server's handler calls
# server_client_update_latest() — the same function a physically resized
# terminal goes through — before recalculating the window. So the claim is
# tmux's native path: exactly one resize, no fake intermediate size on the way,
# no option to mutate, and a clean no-op when the window already has our size
# (the server recalculates to the same numbers and nothing moves). Verified
# against tmux 3.5a.
#
# How long a claim that actually fired stops the next one. Not a debounce
# before the fact — the visible edge already suppresses duplicate trues — just
# enough to absorb iOS firing visibilitychange twice on one unlock.
CLAIM_THROTTLE_S = 1.0


def claim_size(view: str, me: "Attachment") -> bool:
    """Hand the shared window to this client's size. True if it signalled.

    Called synchronously on the hidden→visible edge: picking a device up is the
    user saying they are looking at it, and before the linger (which suppresses
    the attach that used to do this implicitly) that look happened to claim the
    window. Synchronous on purpose — the guard below is only worth anything if
    nothing can await between it and the kill, or the PID could be reaped and
    recycled underneath us.
    """
    now = time.monotonic()
    if now - me.last_claim < CLAIM_THROTTLE_S:
        return False
    # This attachment must still be the view's, still have a socket, and not be
    # on its way out: signalling a reaped PID would land on whatever the kernel
    # handed the number to next.
    if ATTACHED.get(view) is not me or not me.live or me.retired.is_set():
        return False
    try:
        os.kill(me.pid, signal.SIGWINCH)
    except OSError:
        return False
    me.last_claim = now
    log(f"claim size view={view} pid={me.pid}")
    return True


# A bare $HOME/.terminfo (holding only the user's own terminal entry) makes
# ncurses resolve TERM against that directory alone, so tmux dies with
# "missing or unsuitable terminal: xterm-256color". Naming the system databases
# explicitly restores the standard entries without touching the user's home.
# The trailing entries are macOS: Homebrew's tmux links against its own ncurses,
# whose database lives under the brew prefix (/opt/homebrew on Apple Silicon,
# /usr/local on Intel) rather than in the system one. Listing a directory that
# does not exist is harmless — ncurses just skips it.
TERMINFO_DIRS = ":".join([
    str(Path.home() / ".terminfo"),
    "/etc/terminfo",
    "/lib/terminfo",
    "/usr/share/terminfo",
    "/opt/homebrew/share/terminfo",
    "/usr/local/share/terminfo",
    "/opt/homebrew/opt/ncurses/share/terminfo",
    "/usr/local/opt/ncurses/share/terminfo",
])


def spawn_pty(argv: list[str], cols: int, rows: int) -> tuple[int, int]:
    """Fork a child on a new PTY running `argv`. Returns (pid, master_fd).

    openpty + fork rather than pty.fork() so the window size is already on the
    tty before the child execs — tmux reads it at startup.
    """
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ,
                struct.pack("HHHH", max(2, rows), max(2, cols), 0, 0))
    pid = os.fork()
    if pid == 0:
        # Child: fresh session leader owning the PTY as its controlling terminal.
        try:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            for target in (0, 1, 2):
                os.dup2(slave, target)
            os.close(master)
            if slave > 2:
                os.close(slave)
            os.environ["TERM"] = "xterm-256color"
            os.environ["TERMINFO_DIRS"] = TERMINFO_DIRS
            os.environ.pop("TMUX", None)  # otherwise tmux refuses to nest
            os.execvp(argv[0], argv)
        except (OSError, ValueError):
            os._exit(1)
    os.close(slave)
    return pid, master


async def reap(pid: int, fd: int) -> None:
    """Tear down the child so its tmux client detaches, then close the PTY."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGHUP)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    # SIGHUP is asynchronous, so a single WNOHANG wait would leave a zombie for
    # the life of the process. Poll briefly, then escalate to SIGKILL.
    for _ in range(20):
        try:
            if os.waitpid(pid, os.WNOHANG)[0] != 0:
                return
        except OSError:
            return
        await asyncio.sleep(0.05)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        os.waitpid(pid, 0)
    except OSError:
        pass


# Attaching is not safe to do concurrently for one view: `tmux attach -d`
# detaches whichever client is already there, so two overlapping attaches kick
# each other, and each kicked client's PTY dies, closing its WebSocket, whose
# browser then reconnects and kicks the other one back. That ping-pong is what
# made opening a session flap several times before settling. One lock per
# view name serialises attaches, and ATTACHED tracks the one PTY a view has so
# a new connection can take it over deliberately instead of racing it.
# Keying on the view rather than the target is what lets two devices watch one
# session: they hold different views, so neither ever retires the other, while
# the same device reconnecting still lands on its own view and takes it back.
#
# An entry is in one of two states, and which one it is decides what a new
# connection does with it:
#   live  — a WebSocket handler is pumping it. A new connection retires it
#           (the retired/done handshake) and attaches its own PTY.
#   lingering — the socket is gone but the PTY is not, for LINGER_S. A new
#           connection adopts it: same PTY, same tmux client, so tmux sees
#           neither a detach nor an attach. That is the whole point — an
#           attach is activity, and `window-size latest` hands the shared
#           window to the last active client, so a phone reconnecting on its
#           backoff every few seconds would drag the laptop's window down to
#           phone size over and over. Nothing tmux can see happens now.
# Entries leave the dict only under the lock, so the linger timer expiring and
# a reconnect adopting cannot both win.
ATTACH_LOCKS: dict[str, asyncio.Lock] = {}
ATTACHED: dict[str, "Attachment"] = {}

# How long a socket-less PTY waits for its device to come back. Long enough to
# cover a phone's 0.5–5 s reconnect backoff and a screen lock the user thinks
# better of; short enough that picking the phone back up later is a real
# attach, which is what claims the window size for it again.
LINGER_S = 20.0

# How much unsent PTY output one attachment may hold before it stops reading,
# and how far that has to fall for it to start again. A phone on a stalled link
# drains far slower than `yes` or a verbose build fills, so without this the out
# queue is only bounded by the machine's memory. Nothing is dropped or
# coalesced: pausing the reader is backpressure onto this attachment's *own*
# tmux client (every attachment spawns one — see spawn_pty below), so only that
# client's output stalls while tmux goes on painting the pane for everyone else,
# and the bytes are still there, in order, when reading resumes. The gap between
# the marks is what keeps a fast client from stop-starting a read at a time.
HIGH_WATER = 4 << 20
LOW_WATER = 1 << 20

# Monotonic id per WebSocket, so interleaved connections stay tellable apart in
# the log — the flapping this guards against is only legible with these.
CONN_SEQ = 0


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Attachment:
    """The one PTY this server holds for a phone session.

    `retire()` is what a newer connection calls to take over a *live* one: it
    wakes the old connection's own handler, which then tears its PTY down and
    closes its WebSocket on its own thread of control. Reaping another
    connection's fd from the outside is not enough — closing an fd out from
    under an add_reader does not reliably fire the callback, so the old handler
    would block forever on its queue and leave the browser holding a socket
    that never closes.

    `adopt()` is the other takeover, of a *lingering* one, where there is no
    handler to wake and nothing to tear down: the new connection just starts
    pumping the PTY that is already there.

    `notify()` is how anything else in this server speaks to the attached
    client: a str lands on the same out queue the PTY bytes ride and pump_out
    sends it as a *text* frame — the server→client control channel, which the
    client reads as JSON where PTY bytes stay binary.

    `queued`/`drained` are the bookkeeping that bounds the out queue: they live
    here rather than in the handler's closures because the PTY outlives the
    socket — a paused reader may have to be re-armed by a *later* connection's
    pump_out, or by linger_pty with no connection at all.

    `visible`/`last_seen` feed the push gate (visible_devs): the client
    reports its document visibility over the control channel, and every
    received frame stamps last_seen so a dead connection's stale "visible"
    stops counting. The hidden→visible edge of that same report is also what
    hands the shared window to this device (claim_size), which is why
    `visible` survives a linger while everything else about the socket does
    not.
    """

    __slots__ = ("pid", "fd", "out", "done", "retired", "session", "dev",
                 "visible", "last_seen", "last_claim", "live", "linger_task",
                 "reader", "pending", "paused")

    def __init__(self, pid: int, fd: int, out: asyncio.Queue,
                 session: str = "", dev: str = "") -> None:
        self.pid = pid
        self.fd = fd
        self.out = out
        self.done = asyncio.Event()
        self.retired = asyncio.Event()
        # A WebSocket handler is pumping this PTY. False means lingering: the
        # PTY is up but nobody is reading it, and linger_task holds the timer
        # that reaps it if the device does not come back.
        self.live = True
        self.linger_task: asyncio.Task | None = None
        # The session this view is watching, so the pane watcher can find
        # every attached view of a session without decoding view names.
        self.session = session
        # This view's device name — the same identity push subscriptions are
        # stored under, which is what makes the push gate per-device.
        self.dev = dev
        # Hidden until the client explicitly says otherwise: a client that
        # never reports (an older frontend) must never suppress a push.
        self.visible = False
        self.last_seen = time.monotonic()
        # When this attachment last claimed the shared window (claim_size).
        self.last_claim = 0.0
        # The fd's read callback, and the bytes it has queued but nobody has
        # sent yet. `paused` means the callback is off the fd: the queue is at
        # the high-water mark and whoever drains it next puts the reader back
        # (queued/drained).
        self.reader = None
        self.pending = 0
        self.paused = False

    def retire(self) -> None:
        # `live` goes down with the socket, not with the teardown that follows
        # it: from here on this attachment has no client to speak for. A claim
        # racing the handover would otherwise signal a PID about to be reaped,
        # and the push gate would go on counting a device whose socket is
        # already spoken for (visible_devs, notify_session_views).
        self.live = False
        self.retired.set()

    def adopt(self) -> None:
        """Hand this lingering PTY to a fresh WebSocket.

        Synchronous on purpose, and called under the view's lock: cancelling
        the timer and claiming the PTY in one step is what stops the timer from
        reaping a PTY somebody just adopted. Everything scoped to a connection
        rather than to the PTY starts over — the two handshake events.

        `visible` deliberately does not: it is a property of the device, not of
        the socket, and the reconnect's own visible=true has to read as an edge
        only when the device really went away. A phone that locked reported
        hidden before iOS killed the socket, so it comes back to a False and
        claims the window; a phone churning in the foreground never reported
        hidden, so its reconnect is True→True and claims nothing. Nothing else
        reads `visible` off a lingering attachment — the push gate takes only
        live ones (visible_devs).
        """
        if self.linger_task is not None:
            self.linger_task.cancel()
            self.linger_task = None
        self.live = True
        self.done = asyncio.Event()
        self.retired = asyncio.Event()
        self.last_seen = time.monotonic()

    def notify(self, text: str) -> None:
        """Queue one JSON control message for this connection's client."""
        self.out.put_nowait(text)

    def queued(self, data: bytes) -> None:
        """Account for one chunk arriving, stopping reads at the high mark."""
        self.pending += len(data)
        if not self.paused and self.pending >= HIGH_WATER:
            self.paused = True
            asyncio.get_running_loop().remove_reader(self.fd)

    def drained(self, data: bytes) -> None:
        """Account for one chunk leaving, starting reads again at the low one.

        Called before the send it belongs to, not after: the send is the slow
        part, and a reader that only came back once the socket had swallowed
        everything would never have a queue to work ahead into.
        """
        self.pending -= len(data)
        if self.paused and self.pending <= LOW_WATER:
            self.paused = False
            try:
                asyncio.get_running_loop().add_reader(self.fd, self.reader)
            except (OSError, ValueError):
                # The PTY was reaped under us — a linger cancelled by the
                # shutdown sweep drains one last item after its fd is closed.
                # There is nothing left to read either way.
                pass


def attach_lock(name: str) -> asyncio.Lock:
    lock = ATTACH_LOCKS.get(name)
    if lock is None:
        lock = ATTACH_LOCKS[name] = asyncio.Lock()
    return lock


async def linger_pty(view: str, me: Attachment,
                     loop: asyncio.AbstractEventLoop) -> None:
    """Hold a socket-less PTY open for LINGER_S, then reap it.

    Cancelled by Attachment.adopt when the device comes back in time; the
    device that does not come back leaves through here, and its next connect is
    a real attach — which is exactly right, because picking the phone back up
    *should* claim the window size for it.

    The queue has to be drained meanwhile: tmux keeps painting the pane, and a
    PTY nobody reads fills up and blocks its writer. The bytes are dropped
    rather than buffered — a reconnect replays the pane's scrollback and then
    repaints from tmux itself (see redraw_view), so tmux's own history is the
    buffer, and a better one than a queue this process would have to cap.
    """
    deadline = time.monotonic() + LINGER_S
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                item = await asyncio.wait_for(me.out.get(), left)
            except asyncio.TimeoutError:
                break
            if item is None:
                break         # the PTY hung up — its session was killed
            if not isinstance(item, str):
                # Dropping the bytes still has to give back the backpressure
                # they took: a reader left off the fd here would be missing
                # from it for the reconnect that adopts this PTY.
                me.drained(item)
        # Leaving is a decision about the dict, so it is made under the lock:
        # a reconnect that got there first is holding it, and adopt() has
        # already flipped `live` by the time this can look.
        async with attach_lock(view):
            if me.live:
                return
            if ATTACHED.get(view) is me:
                del ATTACHED[view]
    except asyncio.CancelledError:
        # Adopted (or the server is going down and reap_lingering has it).
        raise
    try:
        loop.remove_reader(me.fd)
    except (OSError, ValueError):
        pass
    await reap(me.pid, me.fd)
    me.done.set()
    log(f"linger over view={view} pid={me.pid} reason=timeout-or-pty-gone")


async def reap_lingering() -> None:
    """Tear down every socket-less PTY, for a server on its way down.

    A lingering PTY has no handler to notice the shutdown, so without this its
    tmux client would outlive the server that spawned it.
    """
    loop = asyncio.get_running_loop()
    for view, att in list(ATTACHED.items()):
        if att.live:
            continue
        if att.linger_task is not None:
            att.linger_task.cancel()
        if ATTACHED.get(view) is att:
            del ATTACHED[view]
        try:
            loop.remove_reader(att.fd)
        except (OSError, ValueError):
            pass
        await reap(att.pid, att.fd)


@app.websocket("/ws/attach/{session_name}")
async def ws_attach(ws: WebSocket, session_name: str) -> None:
    await ws.accept()

    global CONN_SEQ
    CONN_SEQ += 1
    cid = CONN_SEQ
    log(f"conn {cid} open  session={session_name}")

    # The client sends its size as the first frame; use it for the initial
    # winsize so tmux never paints at the default 80x24 and then reflows. That
    # same frame carries the token: CORS does not apply to WebSockets, so this
    # handshake is the only thing standing between the open port and a shell.
    # Both live in one frame because a second receive() for the token would eat
    # the resize — and every check here runs before any PTY or tmux command.
    cols, rows = 80, 24
    token = ""
    dev = ""
    try:
        first = await asyncio.wait_for(ws.receive(), timeout=5)
        if first.get("type") == "websocket.disconnect":
            return
        text = first.get("text")
        if text:
            msg = json.loads(text)
            token = msg.get("token", "")
            # Names this device's view. A name that could not be a tmux session
            # reads as no name at all — the legacy single-view path still works.
            candidate = str(msg.get("dev", ""))
            dev = candidate if DEV_RE.match(candidate) else ""
            if msg.get("type") == "resize":
                cols, rows = msg.get("cols", cols), msg.get("rows", rows)
    except (asyncio.TimeoutError, ValueError, KeyError, json.JSONDecodeError):
        # A client that never sent a first frame has not authenticated either,
        # so with auth on this falls through to the 4401 below rather than
        # attaching at the default size.
        pass

    if AUTH_TOKEN is not None:
        ok, reason = check_auth(token, peer_ip(ws.scope.get("client")), "ws")
        if not ok:
            log(f"conn {cid} close session={session_name} reason={reason}")
            await ws.close(code=4401, reason="bad token")
            return

    if not session_exists(session_name):
        log(f"conn {cid} close session={session_name} reason=no-such-session")
        await ws.close(code=4404, reason=f"no tmux session {session_name!r}")
        return

    loop = asyncio.get_running_loop()

    # Everything below serialises on the *view*, not the target: that is what
    # lets two devices watch one session without retiring each other.
    view = view_name(session_name, dev)
    log(f"conn {cid} view={view}")

    # The tail of the pane's scrollback, captured before the PTY exists so the
    # target is the one this device was last looking at. The *view's* pane when
    # there is one, falling back to the base session for a first open. Empty
    # (fresh session, alt screen, capture failure) means no frame at all: the
    # client then resets on the first binary frame instead, exactly as it does
    # against an old server. Whether it goes on the wire at all is decided
    # below, once we know whether this connection attached or adopted; either
    # way it is still ahead of any PTY byte, because nothing drains `out` onto
    # the socket until pump_out starts.
    target = resolve_target(session_name, dev)
    history = await asyncio.to_thread(capture_history, target) if target else ""

    # Claim the view's PTY — adopt a lingering one, or retire a live one and
    # spawn ours — as one atomic step, so two connections can never be pumping
    # one view at once and nothing can reap a PTY this connection just took.
    async with attach_lock(view):
        prev = ATTACHED.get(view)
        adopted = prev is not None and not prev.live
        if adopted:
            me = prev
            me.adopt()
            log(f"conn {cid} adopting lingering pty={me.pid}")
        else:
            if prev is not None:
                del ATTACHED[view]
                log(f"conn {cid} retiring previous attachment pid={prev.pid}")
                prev.retire()
                # Let the old handler finish its own teardown before this
                # attach runs, so `tmux attach -d` never has a live client to
                # kick.
                try:
                    await asyncio.wait_for(prev.done.wait(), timeout=3)
                except asyncio.TimeoutError:
                    log(f"conn {cid} previous attachment slow to exit; "
                        "continuing")
            pid, fd = spawn_pty(attach_argv(session_name, view), cols, rows)
            os.set_blocking(fd, False)
            # PTY reads land in this queue via add_reader; None marks the PTY
            # closing. str items are JSON control messages (Attachment.notify).
            out: asyncio.Queue = asyncio.Queue()
            me = Attachment(pid, fd, out, session_name, dev)
            ATTACHED[view] = me
        pid, fd, out = me.pid, me.fd, me.out

    if adopted:
        # No replay: the client must keep the terminal it already has. tmux saw
        # neither a detach nor an attach, so it will not re-initialise this
        # client — the modes it set once (mouse tracking, bracketed paste,
        # application cursor keys) live only in the client's terminal, and the
        # reset that painting a replay frame costs would take them with it,
        # leaving scroll dead until the next fresh attach. This frame says so;
        # redraw_view below puts the screen back over whatever is still there.
        await ws.send_text(json.dumps({"type": "adopted"}))
        # The reader is already on this fd from the connection that spawned it.
        # Only the size and the repaint are this connection's to do: the client
        # re-sends its size on every connect, and a size tmux already has must
        # not be written (see set_winsize) — but a real change, a rotation say,
        # is a resize the user asked for.
        set_winsize(fd, cols, rows)
        asyncio.create_task(asyncio.to_thread(redraw_view, view))
    else:
        # A real attach: tmux re-initialises the client, so the terminal is
        # about to be rewritten anyway and history goes out first for the
        # repaint to land on top of.
        if history:
            await ws.send_text(json.dumps(
                {"type": "replay", "data": history.replace("\n", "\r\n")}))
        # Off-thread: waits for the session the attach child is spawning.
        asyncio.create_task(asyncio.to_thread(prepare_view, view))

    # Set by pump_out when the PTY hangs up, which is the one ending that must
    # never linger — there would be nothing left to adopt.
    pty_gone = False

    # Whether the next visibility frame is this connection's first, which is
    # the one the client sends unprompted on open (see the claim rule below).
    first_visibility = True

    def on_readable() -> None:
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            # EIO on a PTY master means the child hung up — normal termination.
            data = b""
        if not data:
            loop.remove_reader(fd)
            out.put_nowait(None)
        else:
            out.put_nowait(data)
            me.queued(data)

    # An adopted PTY kept its reader through the linger — re-adding one would
    # only replace the identical callback. It is still stored, because a
    # reader paused under the previous connection is re-armed from here.
    me.reader = on_readable
    if not adopted:
        loop.add_reader(fd, on_readable)

    async def pump_out() -> None:
        nonlocal pty_gone
        while True:
            data = await out.get()
            if data is None:
                pty_gone = True
                break
            # Binary frames are raw PTY bytes; text frames are the JSON control
            # channel (notify). The client tells them apart by frame type.
            if isinstance(data, str):
                await ws.send_text(data)
            else:
                me.drained(data)
                await ws.send_bytes(data)

    async def pump_in() -> None:
        nonlocal first_visibility
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            me.last_seen = time.monotonic()
            if (data := msg.get("bytes")) is not None:
                os.write(fd, data)
                watch_saw_input(session_name)
                continue
            text = msg.get("text")
            if text is None:
                continue
            # Text frames are either a control message (resize, visibility)
            # or raw keystrokes.
            if text.startswith("{"):
                try:
                    ctl = json.loads(text)
                except json.JSONDecodeError:
                    ctl = None
                if ctl and ctl.get("type") == "resize":
                    set_winsize(fd, ctl.get("cols", 80), ctl.get("rows", 24))
                    continue
                if ctl and ctl.get("type") == "visibility":
                    was = me.visible
                    me.visible = bool(ctl.get("visible"))
                    initial, first_visibility = first_visibility, False
                    # Looking at this device again is what claims the shared
                    # window for it — the edge only, so the report every
                    # connect carries leaves a foregrounded phone alone.
                    #
                    # A fresh attachment starts hidden, so its connect-time
                    # visible=true is an edge against nothing: the attach it
                    # rode in on already claimed the window, and claiming again
                    # would take it back off a laptop that had reclaimed it in
                    # between. An adopted one is the opposite — a stored False
                    # is a device that really did go away (the phone reported
                    # hidden before the lock killed its socket), and its return
                    # is exactly the pickup this exists for.
                    connect_time = initial and not adopted
                    if me.visible and not was and not connect_time:
                        # Inline, not a task: the guard inside is only sound
                        # with nothing awaiting between it and the signal.
                        claim_size(view, me)
                    continue
            os.write(fd, text.encode("utf-8"))
            watch_saw_input(session_name)

    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in()),
             asyncio.create_task(me.retired.wait())]
    try:
        # Whichever comes first ends this connection: the PTY exiting, the client
        # going away, or a newer connection retiring us.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect, OSError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        # Whether the PTY outlives this socket is decided here, in one
        # synchronous step: no await, so no other connection can see this
        # attachment half-way between live and lingering. A socket that simply
        # went away (the phone backgrounding) leaves the PTY up for the
        # reconnect to adopt; being superseded or losing the PTY itself ends it
        # now. No lock around any of it: a retiring connection holds that lock
        # while waiting on me.done, so taking it would deadlock. Identity is
        # enough — only we ever remove ourselves.
        lingering = (not me.retired.is_set() and not pty_gone
                     and ATTACHED.get(view) is me)
        # No socket either way, so nothing may still speak for this device: a
        # claim can no longer signal the PID (claim_size), and the push gate
        # stops counting it (visible_devs). The lingering branch needs it too —
        # it is what tells linger_pty and a reconnect that this PTY is free.
        me.live = False
        if lingering:
            # `visible` is left exactly as the device last reported it, so the
            # reconnect can tell a phone that locked from one that never left
            # the foreground (see Attachment.adopt). Nothing believes it
            # meanwhile: the push gate reads live sockets only (visible_devs).
            me.linger_task = asyncio.create_task(linger_pty(view, me, loop))
        elif ATTACHED.get(view) is me:
            del ATTACHED[view]
        if me.retired.is_set():
            reason = "superseded"
        elif lingering:
            reason = f"socket-gone; pty lingers {LINGER_S:.0f}s"
        else:
            reason = "client-or-pty-gone"
        log(f"conn {cid} close session={session_name} reason={reason}")
        if not lingering:
            try:
                loop.remove_reader(fd)
            except (OSError, ValueError):
                pass
            await reap(pid, fd)
            # Unblocks the newer connection, which is waiting for our PTY to be
            # gone.
            me.done.set()
        try:
            await ws.close()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Pane watcher & notifications
# ---------------------------------------------------------------------------
# The phone's WebSocket dies within seconds of the app backgrounding, so
# "your agent is waiting on you" can only be detected here, server-side. One
# asyncio task polls tmux and runs a small state machine per session; when a
# watched session goes quiet after real work, the visible pane is read once
# and classified, and the verdict goes out as a Web Push / ntfy notification
# (per-session opt-in via the @notify session option, mirroring @alias) and as
# prompt chips over the WS control channel to whoever is still attached.

# Tick period. Two tmux subprocesses per tick regardless of session count —
# list-sessions (via session_rows) and one list-panes -a — so the cost is
# constant; capture-pane runs only on busy→idle transitions, never per tick.
POLL_S = 2.0

# A session is BUSY while its last output is younger than this. 6 s rides out
# a compiler pausing between files without reading it as "done".
IDLE_S = float(os.environ.get("POCKETTUI_NOTIFY_IDLE_S", "6.0"))

# Only a busy episode at least this long earns a classification when the pane
# is back at a shell: typing a quick command and reading its output is not an
# event anyone needs pushed. A program still holding the pane is read however
# short the episode was — see watch_update.
MIN_BUSY_S = 10.0

# "Done" is only worth a push when the turn ran long enough that you walked
# away from it, so an idle composer needs far more work behind it than a
# question does. This is also what keeps an agent's own startup silent:
# `claude` painting its banner is a two-second episode ending at an empty
# composer, which is a shape but not news.
READY_MIN_BUSY_S = float(
    os.environ.get("POCKETTUI_NOTIFY_READY_BUSY_S", "30.0"))

# Per-session floor between notifications, so a flapping session cannot turn
# a phone into a metronome.
NOTIFY_GAP_S = 30.0

# What counts as "nothing running": the pane has fallen back to a shell.
SHELL_COMMANDS = {"bash", "zsh", "sh", "fish", "dash", "ash", "ksh", "tcsh",
                  "csh"}


@dataclasses.dataclass
class WatchState:
    """One session's slice of the watcher's memory."""

    last_activity: int = 0        # newest #{window_activity} seen (epoch s)
    busy_started: float = 0.0     # epoch when the open episode began; 0 = none
    episode_cmd: str = ""         # last non-shell pane command seen in it
    cmd: str = ""                 # active pane's command, last tick
    bell: bool = False            # last #{window_bell_flag}, for edge detection
    fired: bool = False           # this idle episode was already read
    fired_sig: str = ""           # signature of the last idle push actually sent
    notified_at: float | None = None   # monotonic stamp of the last dispatch
    state: str = "idle"           # "active" | "waiting" | "idle" (the badge)
    prompt: dict | None = None    # the chips frame currently showing, if any


# Keyed by representative session name. Written by the watcher's worker
# thread, read by /api/sessions — single-field reads of a GIL-protected dict,
# so a race costs at most one tick of staleness.
WATCHER: dict[str, WatchState] = {}


def watch_saw_input(session: str) -> None:
    """Forget what this session last notified about, because the user answered.

    Called when a keystroke from a phone reaches the pane through this server.
    A prompt that has been typed at is a different situation from the one that
    was pushed, so the next idle episode is news again even when it classifies
    to the same line. Input from the physical keyboard deliberately does not
    reset anything: someone sitting at the machine does not need a push, and a
    repeat episode staying silent for them is the right trade.
    """
    w = WATCHER.get(session)
    if w is not None:
        w.fired_sig = ""


PROMPT_YN_RE = re.compile(r"\[y/n\]|\(y/n\)|\byes/no\b", re.IGNORECASE)
PROMPT_ASK_RE = re.compile(
    r"do you want|would you like|proceed\?|continue\?|are you sure",
    re.IGNORECASE)
# The border tolerance is not decoration: Claude Code draws its permission
# dialog inside a rounded box, so a real menu line arrives as `│ ❯ 1. Yes` and
# a menu regex anchored at the digit would never see one.
PROMPT_MENU_RE = re.compile(r"^\s*│?\s*(❯\s*)?(\d+)[.)]\s")
# Claude Code's composer renders ❯ (often followed by a NBSP, or nothing at
# all once capture_pane rstrips an empty composer), so both glyphs count.
PROMPT_BOX_RE = re.compile(r"^\s*│?\s*[>❯](\s|$)")
# The glyphs a TUI paints its frames with. A line made only of these is
# furniture: it carries no text, whatever its width.
BOX_CHARS = "─│╭╮╰╯└┘┌┐├┤"


def strip_chrome(line: str) -> str:
    """`line` with its box borders and padding taken off.

    A boxed question captures as `│ Do you want to proceed?           │`, and
    nobody wants to read the borders on a lock screen. NBSP folds to a plain
    space first, because that is what Claude Code pads with.
    """
    return line.replace("\xa0", " ").strip().strip(BOX_CHARS).strip()


def composer_text(line: str) -> str | None:
    """What a composer line holds after its marker, or None if it is not one.

    "" means the box is empty and the program is waiting on its human; any
    other text is a half-typed message, which is a human at the keyboard —
    the one situation that must never turn into a notification.
    """
    m = PROMPT_BOX_RE.match(line)
    if m is None:
        return None
    return strip_chrome(line[m.end():])


def last_substantive(lines: list[str]) -> str:
    """The last line of the capture that actually says something, or "".

    Read over the whole capture rather than the 5-line tail, because an
    agent's composer box and its shortcut hint fill that tail on their own and
    the line worth quoting — the last thing the agent said — sits above them.
    Box chrome, the composer itself and the TUI's own footer hints are skipped.
    What survives is both the notification body and the thing that makes a
    "ready" signature move when the agent has genuinely said something new.
    """
    for line in reversed(lines):
        text = strip_chrome(line)
        if not text or PROMPT_BOX_RE.match(line):
            continue
        # `? for shortcuts`, `⏵⏵ accept edits on`: pane furniture that repaints
        # by itself and would otherwise be quoted at the user as news.
        if text.startswith("?") or text.startswith("⏵"):
            continue
        return text[:120]
    return ""


def detect_prompt(lines: list[str],
                  cursor_line: str = "") -> tuple[str, list[str], str]:
    """What the pane's tail looks like it is asking. Pure — table-tested.

    Returns (kind, options, line): kind "prompt" (a y/n question, options
    ["y","n"]), "menu" (a numbered chooser, options its digits), "waiting" (a
    trailing question with nothing tappable, options []), "ready" (an empty
    composer: the program has stopped talking and is waiting on its human,
    with `line` the last thing it said), "drafting" (a composer with a
    half-typed message in it) or "quiet" (no shape at all). `line` is the text
    that becomes the notification body. Only the last 5 non-empty lines are
    read — a prompt is at the bottom of a pane or it is history — with the
    cursor's own line weighted first, because that is where an interactive
    program parks it. "ready" is the exception: what an agent last said scrolls
    well above the composer it is sitting in, so that verdict reads the whole
    capture.

    The scan order is load-bearing. A menu is looked for before question
    phrasing because Claude Code's permission dialog is both at once ("Do you
    want to proceed?" over `❯ 1. Yes` / `2.` / `3.`), and a dialog that only
    accepts digits must not put y/n under the user's thumb. The composer comes
    after both, because a question is what the pane is asking and a composer is
    merely where the answer would be typed.
    """
    scan = [line for line in lines if line.strip()][-5:]
    composer = composer_text(cursor_line) if cursor_line.strip() else None
    ordered = [line for line in reversed(scan) if line != cursor_line]
    # A composer holding a draft is the user's own half-typed sentence, so it
    # is not evidence that anything is being asked: it stays out of the scans,
    # or someone typing "do you want me to rebase" would prompt themselves.
    if cursor_line.strip() and not composer:
        ordered.insert(0, cursor_line)

    for line in ordered:
        if PROMPT_YN_RE.search(line):
            return "prompt", ["y", "n"], line.strip()

    # A numbered menu is only a menu when at least two choices sit together —
    # a lone "1." is prose.
    run: list[tuple[str, bool, str]] = []
    menu: list[tuple[str, bool, str]] | None = None
    for line in [*scan, ""]:
        m = PROMPT_MENU_RE.match(line)
        if m:
            run.append((m.group(2), bool(m.group(1)), line))
            continue
        if len(run) >= 2 and menu is None:
            menu = run
        run = []
    if menu:
        options = [digit for digit, _, _ in menu][:4]
        # ❯ marks the pre-selected line, but "❯ 1. Yes" on its own is a useless
        # thing to read on a phone: the question the options belong to is the
        # body worth sending, and the marked line only stands in when the
        # chooser asks nothing in so many words.
        marked = next((line for _, sel, line in menu if sel), menu[0][2])
        question = next((line for line in scan
                         if not PROMPT_MENU_RE.match(line)
                         and (PROMPT_YN_RE.search(line)
                              or PROMPT_ASK_RE.search(line)
                              or strip_chrome(line).endswith("?"))), marked)
        return "menu", options, strip_chrome(question)

    for line in ordered:
        if PROMPT_ASK_RE.search(line):
            return "prompt", ["y", "n"], line.strip()

    if composer is not None:
        if composer:
            return "drafting", [], ""
        return ("ready", [],
                last_substantive(lines) or "ready for your next message")

    if cursor_line.strip() and cursor_line.rstrip().endswith("?"):
        return "waiting", [], cursor_line.strip()
    return "quiet", [], ""


def classify_session(name: str) -> tuple[str, list[str], str]:
    """Read the session's visible pane once and run detect_prompt over it.

    Called only on a busy→idle transition, so its two tmux calls (cursor
    position, then the capture) are paid per episode, not per tick.
    """
    lines = capture_pane(name, 30)
    cursor_line = ""
    rc, out = tmux("list-panes", "-t", f"={name}",
                   "-F", "#{pane_active}\t#{cursor_y}\t#{pane_height}")
    if rc == 0:
        for row in out.splitlines():
            parts = row.split("\t")
            if len(parts) >= 3 and parts[0] == "1":
                try:
                    y, height = int(parts[1]), int(parts[2])
                except ValueError:
                    break
                # The capture's last line is the visible bottom row, so the
                # cursor's line sits (height - 1 - y) lines above it.
                idx = len(lines) - (height - y)
                if 0 <= idx < len(lines):
                    cursor_line = lines[idx]
                break
    return detect_prompt(lines, cursor_line)


def watch_panes() -> list[dict]:
    """Every window's active pane across all sessions, parsed.

    One subprocess for the whole server. The pane_active filter is a format
    field tested here rather than `list-panes -f`, which tmux 3.0a does not
    have — same workaround capture_history uses.
    """
    rc, out = tmux(
        "list-panes", "-a", "-F",
        "#{pane_active}\t#{window_active}\t#{session_name}"
        "\t#{window_activity}\t#{window_bell_flag}\t#{alternate_on}"
        "\t#{pane_current_command}",
    )
    if rc != 0:
        return []
    panes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 7 or parts[0] != "1":
            continue
        try:
            activity = int(parts[3] or 0)
        except ValueError:
            activity = 0
        panes.append({
            "session": parts[2],
            "activity": activity,
            "bell": parts[4] == "1",
            "alt": parts[5] == "1",
            "cmd": parts[6],
            "window_active": parts[1] == "1",
        })
    return panes


def _watch_notify(row: dict, w: WatchState, mono: float,
                  kind: str, body: str, sig: str = "") -> list[dict]:
    """The push event this moment has earned, or [] — the @notify opt-in, the
    per-session gap and the repeat-signature guard are enforced here, in one
    place.

    `sig` identifies what this notification would say. An idle episode that
    would say exactly what the last one said is not news — an agent's TUI
    redraws its footer every second or two, so a redraw pause reads as a whole
    busy→idle episode and the same "waiting" would otherwise go out forever.
    A skipped send is not a send: it must not consume the gap either, or the
    one notification that does have something new to say lands inside the gap
    of a repeat that was never delivered. Passing no signature (the bell) opts
    out of the guard entirely — a program ringing twice means it twice.
    """
    if row["notify"] == "off":
        return []
    if sig and sig == w.fired_sig:
        log(f"watch skip session={row['name']} kind={kind} (same as last)")
        return []
    if w.notified_at is not None and mono - w.notified_at < NOTIFY_GAP_S:
        return []
    w.notified_at = mono
    if sig:
        w.fired_sig = sig
    log(f"watch notify session={row['name']} kind={kind} body={body[:60]!r}")
    payload = {
        "title": row["alias"] or row["name"],
        "body": body,
        "tag": "ptui-" + row["name"],
        "session": row["name"],
        "kind": kind,
    }
    # Quiet mode rides in the payload: sw.js hands it to showNotification's
    # `silent`, and the ntfy transport lowers its priority.
    if row["notify"] == "quiet":
        payload["silent"] = True
    return [{"kind": "push", "session": row["name"], "payload": payload}]


def watch_update(rows: list[dict], panes: list[dict], now: float, mono: float,
                 classify=None) -> list[dict]:
    """Advance every session's state machine one tick.

    `rows` is session_rows(), `panes` watch_panes(); `now` is epoch seconds
    (the clock #{window_activity} is on) and `mono` a monotonic clock for
    notification spacing — both parameters so tests can drive the machine
    without patching time. Returns the tick's events: {"kind": "ws", ...} for
    prompt-chip control frames and {"kind": "push", ...} for notifications.

    The rules, as decided: BUSY while output is younger than IDLE_S; a
    busy→idle transition reads the pane once and classifies it, either after
    an episode of at least MIN_BUSY_S or, whatever its length, while a program
    still holds the pane; one notification per idle episode, re-armed when
    activity advances, never closer than NOTIFY_GAP_S per session; an empty
    composer ("ready") additionally wants READY_MIN_BUSY_S of work behind it,
    and a composer being typed into never notifies at all; a bell edge fires
    immediately regardless of idle state; nothing is suppressed for being
    attached — @notify (default off) is the volume control. One refinement
    the sleep-then-done case forces: an idle
    transition below the MIN_BUSY_S gate closes the episode only when the
    pane is back at a shell. A non-shell command that is producing no output
    (`sleep 30`) keeps its episode open, so the episode spans the quiet run
    and the eventual "finished" clears the gate.
    """
    if classify is None:
        classify = classify_session
    events: list[dict] = []

    # Group members share their windows, so the same activity shows under
    # every member's name; only the representative's rows are read. Activity
    # is the max across the session's windows; the command is the active
    # window's, since that pane is what a classification would capture.
    per: dict[str, dict] = {}
    for pane in panes:
        agg = per.setdefault(pane["session"],
                             {"activity": 0, "bell": False, "cmd": ""})
        agg["activity"] = max(agg["activity"], pane["activity"])
        agg["bell"] = agg["bell"] or pane["bell"]
        if pane["window_active"]:
            agg["cmd"] = pane["cmd"]

    seen = set()
    for row in rows:
        if not row["representative"]:
            continue
        name = row["name"]
        agg = per.get(name)
        if agg is None:
            continue
        seen.add(name)
        busy = (now - agg["activity"]) < IDLE_S

        w = WATCHER.get(name)
        if w is None:
            # First sight is baseline only: whatever happened before this
            # server was watching is not something to notify about.
            w = WATCHER[name] = WatchState(
                last_activity=agg["activity"], cmd=agg["cmd"],
                bell=agg["bell"], state="active" if busy else "idle")
            if busy:
                w.busy_started = agg["activity"]
                if agg["cmd"] and agg["cmd"] not in SHELL_COMMANDS:
                    w.episode_cmd = agg["cmd"]
            continue

        # A bell is the program explicitly asking for attention — it fires on
        # the rising edge, whatever the idle state says.
        if agg["bell"] and not w.bell:
            events.extend(_watch_notify(row, w, mono, "bell", "rang the bell"))
        w.bell = agg["bell"]

        if agg["activity"] > w.last_activity:
            # Output resumed: re-arm, open an episode if none is running, and
            # take down any chips the previous prompt put up.
            w.fired = False
            if not w.busy_started:
                w.busy_started = agg["activity"]
                w.episode_cmd = ""
            if w.prompt is not None:
                events.append({"kind": "ws", "session": name, "payload":
                               {"type": "prompt", "options": [], "line": ""}})
                w.prompt = None
            w.last_activity = agg["activity"]

        if w.busy_started and agg["cmd"] and agg["cmd"] not in SHELL_COMMANDS:
            w.episode_cmd = agg["cmd"]
        w.cmd = agg["cmd"]

        if busy:
            w.state = "active"
        elif w.busy_started and not w.fired:
            # The busy→idle transition — the one moment the pane is read. Once
            # per episode: a pane nobody has written to since cannot have
            # started saying something else, and the read costs two tmux calls.
            episode = w.last_activity - w.busy_started
            # A program still holding the pane is read however short the
            # episode was, because a permission dialog three seconds into a
            # tool call is exactly the thing worth pushing. A pane back at a
            # shell keeps the old cheap rule, so no `ls` costs a capture-pane.
            if episode >= MIN_BUSY_S or (agg["cmd"]
                                         and agg["cmd"] not in SHELL_COMMANDS):
                kind, options, line = classify(name)
                if kind in ("prompt", "menu", "waiting", "ready", "drafting"):
                    w.state = "waiting"
                    # Only a real question has anything to tap. "ready" and
                    # "drafting" publish the empty frame instead, which
                    # showPromptChips reads as "take the bar down" — but
                    # w.prompt still has to hold it, or the `w.prompt is None`
                    # branch below would drop the badge back to idle on the
                    # very next tick.
                    asks = kind in ("prompt", "menu", "waiting")
                    w.prompt = {"type": "prompt",
                                "options": options if asks else [],
                                "line": line if asks else ""}
                    events.append({"kind": "ws", "session": name,
                                   "payload": dict(w.prompt)})
                    if asks:
                        body = line or "waiting for input"
                        events.extend(_watch_notify(
                            row, w, mono, "waiting", body,
                            sig=f"{kind}\x00{line}"))
                    elif kind == "ready" and episode >= READY_MIN_BUSY_S:
                        # The composer is empty, so nothing the user types can
                        # move this signature — only the agent producing new
                        # output can, which is also what re-arms "done" for
                        # someone who answered at the physical keyboard.
                        events.extend(_watch_notify(
                            row, w, mono, "ready", line,
                            sig=f"ready\x00{line}"))
                    # "drafting" is a human mid-sentence: badge and chips only.
                else:
                    w.state = "idle"
                    # A finished run is not a question, so these two keep the
                    # original episode floor: reading the pane earlier must not
                    # make "finished" and "went quiet" any chattier.
                    if episode >= MIN_BUSY_S:
                        if w.episode_cmd and agg["cmd"] in SHELL_COMMANDS:
                            body = f"{w.episode_cmd} finished"
                            events.extend(_watch_notify(
                                row, w, mono, "finished", body,
                                sig=f"finished\x00{body}"))
                        elif agg["cmd"] and agg["cmd"] not in SHELL_COMMANDS:
                            events.extend(_watch_notify(
                                row, w, mono, "quiet",
                                "went quiet — may need input",
                                sig="quiet\x00went quiet — may need input"))
                        # Plain shell output that merely stopped: nothing.
                w.fired = True
                # Reading the pane is not the same as closing the episode: a
                # short episode with a program still in the pane keeps its
                # start, so the `sleep 30` refinement below survives being
                # classified — the next burst of output re-arms w.fired and the
                # eventual "finished" still spans the whole run.
                if episode >= MIN_BUSY_S or agg["cmd"] in SHELL_COMMANDS:
                    w.busy_started = 0.0
                    w.episode_cmd = ""
            elif agg["cmd"] in SHELL_COMMANDS:
                # Too brief to mean anything and nothing left running.
                w.state = "idle"
                w.busy_started = 0.0
                w.episode_cmd = ""
            else:
                # Quiet, but a program still holds the pane (`sleep 30`): the
                # episode stays open — see the docstring.
                w.state = "idle"
        elif w.prompt is None:
            w.state = "idle"

    # Sessions that vanished take their state with them.
    for name in list(WATCHER):
        if name not in seen:
            del WATCHER[name]
    return events


def watch_tick_sync() -> list[dict]:
    """One whole tick, run off-loop: poll, update, dispatch the transports.

    Push and ntfy are blocking HTTP and this already runs in a worker thread,
    so they are sent here; the WS chip frames are returned instead, because
    an Attachment's queue belongs to the event loop.
    """
    events = watch_update(session_rows(), watch_panes(),
                          time.time(), time.monotonic())
    ws_events = []
    for event in events:
        if event["kind"] == "push":
            dispatch_notification(event["payload"])
        else:
            ws_events.append(event)
    return ws_events


def notify_session_views(session: str, text: str) -> None:
    """Queue one control frame for every view attached to `session`.

    Live views only: a lingering attachment has no socket to put a frame on,
    and its drain would throw it away anyway.
    """
    for att in list(ATTACHED.values()):
        if att.live and att.session == session:
            att.notify(text)


async def watcher_loop() -> None:
    while True:
        try:
            for event in await asyncio.to_thread(watch_tick_sync):
                notify_session_views(event["session"],
                                     json.dumps(event["payload"]))
        except Exception as e:  # noqa: BLE001 — the watcher outlives any tick
            log(f"watcher error: {e!r}")
        await asyncio.sleep(POLL_S)


# ---------------------------------------------------------------------------
# Notification transports
# ---------------------------------------------------------------------------
# Web Push through pywebpush, which is deliberately optional: the server must
# boot and serve without it (the import is lazy, status answers push:false,
# subscribe answers 503). ntfy is the zero-dependency alternative — one env
# var, plain urllib. Neither transport may ever take a request or the watcher
# down with it: every failure is logged and swallowed.

# VAPID keypair (0600, like the token) and the push subscriptions. Both are
# state this install mints, never something to commit.
VAPID_PATH = HERE / ".vapid.json"
PUSH_SUBS_PATH = HERE / ".push_subs.json"

# One row per paired browser is plenty; 20 leaves room for re-pairs before
# the oldest is evicted.
PUSH_SUBS_MAX = 20
RATE_PUSH = 10


def _webpush_module():
    """pywebpush, or None where it is not installed. The single seam every
    push path goes through, so tests (and a bare install) degrade in one
    place."""
    try:
        import pywebpush
        return pywebpush
    except Exception:  # noqa: BLE001 — a broken wheel reads as "not installed"
        return None


def push_available() -> bool:
    return _webpush_module() is not None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def vapid_keys() -> dict:
    """The VAPID keypair, minted on first need and kept at VAPID_PATH.

    Raw urlsafe-base64: the public half is exactly what pushManager.subscribe
    wants as applicationServerKey, and pywebpush's Vapid.from_string accepts
    the private half as-is. Only reached behind push_available(), so the
    cryptography import (a pywebpush dependency) cannot be the thing that
    breaks a bare install.
    """
    try:
        data = json.loads(VAPID_PATH.read_text(encoding="utf-8"))
        if data.get("private_key") and data.get("public_key"):
            return data
    except (OSError, ValueError):
        pass
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    data = {
        "private_key": _b64url(
            key.private_numbers().private_value.to_bytes(32, "big")),
        "public_key": _b64url(key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)),
    }
    # Mode set before the secret lands, same as write_token.
    fd = os.open(VAPID_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.chmod(VAPID_PATH, 0o600)
    return data


def load_push_subs() -> list[dict]:
    try:
        data = json.loads(PUSH_SUBS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_push_subs(subs: list[dict]) -> None:
    # 0600 like the token: an endpoint URL is a capability to send this
    # phone notifications.
    fd = os.open(PUSH_SUBS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(subs, fh)


# A push is redundant while the user is looking at the app — the in-app
# chips and badges are the signal there. Only an explicit visible=true from
# the client suppresses; the default is hidden, and the state dies with the
# socket. A socket silent longer than this bound is not believed either: a
# dead-but-undetected TCP connection must not swallow notifications forever.
# In practice suppression lifts near-instantly anyway — the client sends
# hidden on lock/app-switch, and iOS kills the socket seconds later.
VISIBLE_STALE_S = 90.0


def visible_devs() -> set[str]:
    """Device names whose attached client is on screen right now.

    Presence is a property of the socket, never of the PTY: an attachment
    lingering after its socket died (see linger_pty) is a phone in a pocket,
    and counting it would swallow exactly the notifications that phone needs.
    `live` is what enforces that — a lingering attachment keeps the visibility
    its device last reported (Attachment.adopt needs it) and is excluded here
    however that reads.

    Read from the watcher's worker thread, written on the event loop — the
    same GIL-protected-dict discipline WATCHER lives under.
    """
    mono = time.monotonic()
    return {att.dev for att in list(ATTACHED.values())
            if att.live and att.visible
            and mono - att.last_seen <= VISIBLE_STALE_S}


def send_webpush_all(payload: dict) -> None:
    """One notification to every stored subscription; 404/410 prunes."""
    mod = _webpush_module()
    if mod is None:
        return
    subs = load_push_subs()
    if not subs:
        return
    try:
        private_key = vapid_keys()["private_key"]
    except Exception as e:  # noqa: BLE001
        log(f"webpush: no vapid keys ({e!r})")
        return
    data = json.dumps(payload)
    visible = visible_devs()
    kept, pruned = [], False
    for entry in subs:
        # This device is in the app right now: hold its send, keep the sub.
        # Subscriptions carry the same dev name the attachment does, which is
        # what makes the hold per-device rather than global.
        if str(entry.get("dev", "")) in visible:
            kept.append(entry)
            log(f"webpush hold dev={entry.get('dev', '')!r} (client visible)")
            continue
        endpoint = str(entry.get("endpoint", ""))
        origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(endpoint))
        try:
            # Apple validates the VAPID claims: a mailto: on a non-public
            # domain (localhost) is a 403 BadJwtToken, and exp must be less
            # than 24 h out — pinned here rather than inherited from library
            # defaults (py_vapid's is exactly 24 h).
            mod.webpush(
                entry.get("subscription") or {}, data,
                vapid_private_key=private_key,
                vapid_claims={"sub": "https://pockettui.com",
                              "aud": origin,
                              "exp": int(time.time()) + 12 * 3600},
                ttl=3600)
            kept.append(entry)
        except Exception as e:  # noqa: BLE001 — one dead sub must not stop the rest
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                # The browser dropped this subscription for good.
                pruned = True
                log(f"webpush prune status={status} endpoint={endpoint[:60]}")
            else:
                kept.append(entry)
                log(f"webpush fail: {e!r}")
    if pruned:
        save_push_subs(kept)


def _ntfy_header(text: str) -> str:
    """A header value urllib will accept: RFC 2047 when it is not latin-1."""
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode()).decode() + "?="


def send_ntfy(payload: dict) -> None:
    """POST the notification to the configured ntfy topic, if there is one.

    Server-side env config, no UI: POCKETTUI_NTFY_URL is the full topic URL
    (any ntfy server). POCKETTUI_APP_URL, when set, makes tapping the ntfy
    notification open the app on that session, the same landing the Web Push
    tap gets.
    """
    url = os.environ.get("POCKETTUI_NTFY_URL", "")
    if not url:
        return
    # ntfy is one topic with no device identity, so any on-screen client
    # holds it back.
    if visible_devs():
        log("ntfy hold (client visible)")
        return
    title = str(payload.get("title", "PocketTUI"))
    body = str(payload.get("body", "") or payload.get("kind", ""))
    headers = {"Title": _ntfy_header(title), "Tags": "bell"}
    if payload.get("silent"):
        headers["Priority"] = "low"
    app_url = os.environ.get("POCKETTUI_APP_URL", "")
    if app_url:
        headers["Click"] = (app_url + "#session="
                            + urllib.parse.quote(str(payload.get("session", ""))))
    req = urllib.request.Request(
        url, data=f"{title}: {body}".encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as e:  # noqa: BLE001 — a transport, never an error source
        log(f"ntfy fail: {e!r}")


def dispatch_notification(payload: dict) -> None:
    """Send one notification through every configured transport."""
    try:
        send_webpush_all(payload)
    except Exception as e:  # noqa: BLE001
        log(f"webpush dispatch error: {e!r}")
    try:
        send_ntfy(payload)
    except Exception as e:  # noqa: BLE001
        log(f"ntfy dispatch error: {e!r}")


@app.get("/api/push/status")
def api_push_status() -> Response:
    """What this install can push with — the client's whole setup question.

    `push` false (pywebpush not installed) is a normal answer, not an error:
    the bell toggle still works, the ntfy transport still fires, and the
    client simply skips the subscribe step.
    """
    ok = push_available()
    key = ""
    if ok:
        try:
            key = vapid_keys()["public_key"]
        except Exception as e:  # noqa: BLE001 — unwritable state dir reads as no push
            log(f"vapid keys unavailable: {e!r}")
            ok = False
    return no_store(JSONResponse({
        "push": ok,
        "vapid_key": key,
        "subscribed": len(load_push_subs()),
        "ntfy": bool(os.environ.get("POCKETTUI_NTFY_URL", "")),
    }))


@app.post("/api/push/subscribe")
def api_push_subscribe(request: Request, body: dict = Body(...)) -> Response:
    """Store (or refresh) one browser's push subscription.

    Keyed by endpoint, and by dev tag when one is given — a phone
    re-registering under a new endpoint must not leave its old subscription
    behind to double-fire every notification.
    """
    refusal = throttled("push", RATE_PUSH, request)
    if refusal is not None:
        return refusal
    if not push_available():
        return JSONResponse({"error": "push_unavailable"}, status_code=503)

    sub = body.get("subscription")
    if not isinstance(sub, dict):
        return JSONResponse({"error": "bad_subscription"}, status_code=400)
    endpoint = str(sub.get("endpoint", ""))
    keys = sub.get("keys")
    if (not endpoint.startswith("https://") or not isinstance(keys, dict)
            or not keys.get("p256dh") or not keys.get("auth")):
        return JSONResponse({"error": "bad_subscription"}, status_code=400)
    dev = str(body.get("dev", ""))
    dev = dev if DEV_RE.match(dev) else ""

    subs = [s for s in load_push_subs()
            if s.get("endpoint") != endpoint
            and not (dev and s.get("dev") == dev)]
    subs.append({"endpoint": endpoint, "subscription": sub, "dev": dev,
                 "added": int(time.time())})
    # Oldest out past the cap — a phone re-pairing must never be refused for
    # the subscriptions its predecessors leaked.
    save_push_subs(subs[-PUSH_SUBS_MAX:])
    return no_store(JSONResponse({"ok": True}))


@app.post("/api/push/unsubscribe")
def api_push_unsubscribe(body: dict = Body(...)) -> Response:
    """Drop one subscription. Idempotent — a gone endpoint is a success."""
    endpoint = str(body.get("endpoint", ""))
    subs = load_push_subs()
    kept = [s for s in subs if s.get("endpoint") != endpoint]
    if len(kept) != len(subs):
        save_push_subs(kept)
    return no_store(JSONResponse({"ok": True}))


@app.post("/api/notify")
def api_notify(body: dict = Body(...)) -> Response:
    """Set a session's notification mode: "off", "on" (sound), or "quiet".

    Stored as the session's own `@notify` option, exactly as @alias is: it
    lives and dies with the session, every device sees the same answer, and
    only the group's representative is a valid target. A shell cached from
    before modes still posts the old boolean body, which maps onto two of the
    three modes.
    """
    name = str(body.get("session", ""))
    row = find_row(session_rows(), name)
    if row is None or not row["representative"]:
        return JSONResponse({"error": "no such session"}, status_code=404)

    if "mode" in body:
        mode = body.get("mode")
    else:
        mode = "on" if body.get("on") else "off"
    if mode not in ("off", "on", "quiet"):
        return JSONResponse({"error": "bad mode"}, status_code=400)
    # No "=" exact-match prefix: set-option rejects it, as noted in enable_mouse.
    if mode == "off":
        rc, _ = tmux("set-option", "-u", "-t", name, "@notify")
    else:
        rc, _ = tmux("set-option", "-t", name, "@notify", mode)
    if rc != 0:
        return JSONResponse({"error": "could not set notify"}, status_code=500)
    return no_store(JSONResponse({"session": name, "notify": mode}))


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def die(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(1)


def rotate_token() -> None:
    """Replace the token on disk and print the new one.

    Every paired phone stored the old one, so this is not a silent operation —
    it is announced as re-pairing work rather than as a routine refresh.
    """
    token = generate_token()
    write_token(token)
    print(f"New PocketTUI token: {format_token(token)}")
    print(f"Stored in {TOKEN_PATH} (mode 0600).")
    print("Every paired phone must be re-paired with this token; the old one no "
          "longer works.")


def main() -> None:
    parser = argparse.ArgumentParser(description="PocketTUI — phone-facing tmux terminal")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5560)
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--rotate-token", action="store_true",
                        help="generate a new pairing token, print it, and exit")
    parser.add_argument("--no-auth", action="store_true",
                        help="serve with no token — refused unless bound to loopback")
    args = parser.parse_args()

    if args.rotate_token:
        rotate_token()
        return

    global AUTH_TOKEN
    if args.no_auth:
        # The escape hatch is for a local-only session, and the default bind is
        # 0.0.0.0 — so without this check the flag would quietly publish an
        # unauthenticated shell to the whole network.
        if args.host not in LOOPBACK_HOSTS:
            die(f"--no-auth refused: --host {args.host} is not loopback.\n"
                f"An unauthenticated PocketTUI must not be reachable off this "
                f"machine. Either re-run with --host 127.0.0.1, or drop "
                f"--no-auth and pair with a token.")
        AUTH_TOKEN = None
        log("running with --no-auth on a loopback bind: no token required")
    else:
        AUTH_TOKEN = read_token()
        if not AUTH_TOKEN:
            die(f"No pairing token found at {TOKEN_PATH}.\n"
                f"PocketTUI bridges a shell to anything that can reach it, so it "
                f"will not start without one.\n"
                f"Generate one with:  {sys.executable} {Path(__file__).resolve()} "
                f"--rotate-token\n"
                f"or re-run ./install.sh, which sets one up for you.\n"
                f"To run without a token, bind to loopback only: --host 127.0.0.1 "
                f"--no-auth")
        log(f"pairing token loaded from {TOKEN_PATH}")

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
