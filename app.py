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
import dataclasses
import fcntl
import getpass
import hmac
import json
import math
import os
import pty
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import wave
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

# Cache-busting stamp, injected into the HTML/sw at serve time. Bumping on every
# server start is what makes iOS drop the old PWA shell after a redeploy.
CACHE_VERSION = time.strftime("%Y%m%d-%H%M%S")

# A device name from the client, which becomes part of a tmux session name
# (<device>-<target>). Anything else is treated as absent rather than rejected,
# so a client that sends nothing still gets the single-view behaviour below.
DEV_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,15}$")

app = FastAPI(title="PocketTUI")

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

def tmux(*args: str) -> tuple[int, str]:
    """Run a tmux command, returning (returncode, stdout). Never raises."""
    try:
        p = subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=5
        )
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def session_exists(name: str) -> bool:
    return tmux("has-session", "-t", f"={name}")[0] == 0


def list_sessions() -> list[dict]:
    """All non-phone tmux sessions with their active pane's command, newest first."""
    rc, out = tmux(
        "list-sessions",
        "-F",
        "#{session_name}\t#{session_created}\t#{session_attached}\t#{session_windows}"
        "\t#{session_grouped}\t#{session_group}\t#{@alias}",
    )
    if rc != 0:
        return []

    sessions = []
    for line in out.splitlines():
        # The alias field is still last and empty when unset, so split to a
        # fixed width rather than requiring every field to be present.
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        name, created, attached, windows, grouped, group = parts[:6]
        alias = parts[6] if len(parts) > 6 else ""
        # Hide the grouped clones — this app's own views are born grouped onto
        # their target, which is race-free in a way a marker set after spawn is
        # not. `new-session -t x` names the group after x, so the original keeps
        # name == group and stays listed while every clone of it drops out. A
        # user's own hand-made clone is hidden too, reasonably: it mirrors a
        # session already on the list.
        if grouped == "1" and name != group:
            continue
        cmd, title = active_pane(name)
        sessions.append({
            "name": name,
            "created": int(created or 0),
            "attached": int(attached or 0),
            "windows": int(windows or 0),
            "command": cmd,
            "title": title,
            "alias": alias,
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

    A grouped session shares the target's windows but keeps its own current
    window and size, so the phone client's small size never squeezes the
    laptop's client. Reuse the view across reconnects (-d kicks off any stale
    client of it) so the phone's window selection survives a dropout.
    """
    if session_exists(view):
        return ["tmux", "attach", "-d", "-t", f"={view}"]
    return ["tmux", "new-session", "-s", view, "-t", f"={target}"]


def enable_mouse(view: str) -> None:
    """Turn on mouse reporting for the phone's own session only.

    Drag-to-scroll on the phone works by synthesising SGR wheel events, which
    tmux only acts on with `mouse on`. The option is set on this device's view
    alone (a grouped session carries its own options), so the laptop's client of
    the same windows keeps whatever the user configured.
    """
    # The session only exists once the attach child has spawned it, so retry
    # briefly rather than racing the fork.
    for _ in range(20):
        if session_exists(view):
            # No "=" exact-match prefix here: set-option rejects it outright
            # ("no such session"), unlike the session-target commands above.
            tmux("set-option", "-t", view, "mouse", "on")
            return
        time.sleep(0.05)


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


@app.get("/icon.svg")
def icon_svg() -> Response:
    # Not part of the installed runtime (nothing references it), so answer 404
    # rather than raising when the file is absent.
    path = HERE / "icon.svg"
    if not path.exists():
        return Response(status_code=404)
    return FileResponse(path, media_type="image/svg+xml")


@app.get("/icon-{size}.png")
def icon_png(size: str) -> Response:
    path = HERE / f"icon-{size}.png"
    if not path.exists():
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


@app.get("/api/sessions")
def api_sessions() -> Response:
    return no_store(JSONResponse({"sessions": list_sessions()}))


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
    # A grouped clone is one of this app's views (or the user's own mirror of a
    # listed session), never something the list offers to rename.
    grouped_as = session_group(name)
    if not session_exists(name) or (grouped_as and grouped_as != name):
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
    """
    if parakeet_model_dir() is None:
        return False
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:  # noqa: BLE001 — a broken wheel reads as "not installed"
        return False
    return True


def voice_engine() -> str:
    """Which engine this request will use: "parakeet", "whisper", or "".

    "" means neither is installed, which is the only case that answers
    not_setup. POCKETTUI_VOICE_ENGINE forces a choice, and a forced engine that
    is not actually installed reports "" rather than silently falling through to
    the other one — a machine pinned to an engine should say so plainly instead
    of quietly running the model its operator ruled out.
    """
    forced = os.environ.get("POCKETTUI_VOICE_ENGINE", "").strip().lower()
    if forced == "parakeet":
        return "parakeet" if parakeet_available() else ""
    if forced == "whisper":
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


def run_parakeet(model_dir: Path, wav: Path, hotwords: str | None = None) -> str:
    """The transcript of `wav`, or "" if Parakeet produced nothing usable.

    `hotwords` is the seam for per-request vocabulary biasing — the words the
    prompt does for whisper. It is unused today (callers pass None) and is here
    so that adding it later touches this line and not the pipeline around it;
    the recognizer is already built with the bpe vocabulary that consumes it.
    """
    recognizer = parakeet_recognizer(model_dir)
    with wave.open(str(wav)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            return ""
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    # int16 PCM to the float32 in [-1, 1) sherpa-onnx wants. array rather than
    # numpy: this is the only array work in the server and numpy is not a
    # dependency it otherwise needs.
    pcm = array.array("h")
    pcm.frombytes(frames)
    samples = [s / 32768.0 for s in pcm]
    if not samples:
        return ""

    stream = recognizer.create_stream(hotwords=hotwords) if hotwords \
        else recognizer.create_stream()
    stream.accept_waveform(rate, samples)
    recognizer.decode_stream(stream)
    return " ".join(stream.result.text.split())


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
    content_type = request.headers.get("content-type", "")
    # Reading the body needs the event loop, but ffmpeg and whisper must not
    # hold it for the seconds they take — every other session on this server
    # would stall behind them.
    return await run_in_threadpool(
        transcribe, raw, session, dev if DEV_RE.match(dev) else "",
        content_type=content_type)


def transcribe(raw: bytes, session: str, dev: str, content_type: str = "") -> Response:
    """The transcription pipeline, off the event loop.

    Split from the route so the subprocess work runs in the threadpool the way
    every other handler here does, and so the tests can drive it without HTTP.
    `content_type` is only for the debug log line below (see VOICE_DEBUG) —
    decode_audio never trusts it, since ffmpeg reads the container from the
    bytes themselves.
    """
    # Assets before body: an install without the voice pieces answers the same
    # way whatever it was sent, which lets the phone probe with an empty body
    # before it records rather than telling the user after the fact.
    engine = voice_engine()
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
                log(f"transcribe content-type={content_type!r} bytes={len(raw)} "
                    f"duration={duration_s:.2f}s peak={peak_rms:.4f} "
                    f"max_frame_rms={max_frame_rms:.4f} silent=yes ms=0 raw=''")
                return no_store(JSONResponse({"text": "", "raw": "", "ms": 0}))

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
            history = resolver.history_vocabulary() + resolver.ssh_hosts()
            # Words this user has corrected by hand in past transcripts. Read
            # from the same cached-on-(mtime, size) store the resolver uses, so
            # this is a dict lookup once the file has been read.
            learned = resolver.learned_words()

            decode_started = time.monotonic()
            if engine == "parakeet":
                # No prompt: Parakeet takes none. The vocabulary sources above
                # still feed the resolver, and hotwords — Parakeet's equivalent
                # channel — are the seam left open in run_parakeet().
                text = run_parakeet(parakeet_model_dir(), wav, hotwords=None)
            else:
                text = run_whisper(binary, model, wav,
                                   transcribe_prompt(screen, cwd, history,
                                                     scrollback, learned))
            decode_ms = int((time.monotonic() - decode_started) * 1000)
            ms = int((time.monotonic() - started) * 1000)

        log(f"transcribe content-type={content_type!r} bytes={len(raw)} "
            f"duration={check.duration_s:.2f}s peak={check.peak_rms:.4f} "
            f"max_frame_rms={check.max_frame_rms:.4f} silent=no "
            f"engine={engine} decode_ms={decode_ms} ms={ms} raw={text[:80]!r}")

        if not text:
            return no_store(JSONResponse({"text": "", "raw": "", "ms": ms}))

        result = resolver.resolve(text, screen=screen, cwd=cwd,
                                  tmux_names=tmux_names(),
                                  budget=TRANSCRIBE_BUDGET_S, asr=True,
                                  extra_vocab=history)
        return no_store(JSONResponse(
            {"text": result["text"], "raw": text, "ms": ms}))
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


@app.post("/api/session")
def api_new_session(body: dict = Body(...)) -> Response:
    """Create a detached tmux session the phone can then attach to.

    Detached because this server has no tty to attach from; the phone picks the
    session up over the usual WebSocket route once it appears in the list.
    """
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
def api_file(path: str = "") -> Response:
    """Serve an image or video by absolute path, for tap-to-view in the terminal.

    No restriction on where the file lives beyond the extension allowlist: this
    server already bridges a full shell to the same clients, so reading a file
    off disk crosses no boundary they could not cross by typing `cat`.

    Every rejection answers 404 alike, so a probe learns nothing about which
    check failed — or about what exists. Range requests (which iOS video needs)
    are handled by starlette's FileResponse itself.
    """
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
# PTY <-> WebSocket bridge
# ---------------------------------------------------------------------------

def set_winsize(fd: int, cols: int, rows: int) -> None:
    cols = max(2, min(int(cols), 500))
    rows = max(2, min(int(rows), 300))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


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
# view name serialises attaches, and ATTACHED tracks the live one so a new
# connection can retire its predecessor deliberately instead of racing it.
# Keying on the view rather than the target is what lets two devices watch one
# session: they hold different views, so neither ever retires the other, while
# the same device reconnecting still lands on its own view and retires itself.
ATTACH_LOCKS: dict[str, asyncio.Lock] = {}
ATTACHED: dict[str, "Attachment"] = {}

# Monotonic id per WebSocket, so interleaved connections stay tellable apart in
# the log — the flapping this guards against is only legible with these.
CONN_SEQ = 0


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Attachment:
    """The one live PTY this server holds for a phone session.

    `retire()` is what a newer connection calls to take over: it wakes the old
    connection's own handler, which then tears its PTY down and closes its
    WebSocket on its own thread of control. Reaping another connection's fd from
    the outside is not enough — closing an fd out from under an add_reader does
    not reliably fire the callback, so the old handler would block forever on
    its queue and leave the browser holding a socket that never closes.
    """

    __slots__ = ("pid", "fd", "done", "retired")

    def __init__(self, pid: int, fd: int) -> None:
        self.pid = pid
        self.fd = fd
        self.done = asyncio.Event()
        self.retired = asyncio.Event()

    def retire(self) -> None:
        self.retired.set()


def attach_lock(name: str) -> asyncio.Lock:
    lock = ATTACH_LOCKS.get(name)
    if lock is None:
        lock = ATTACH_LOCKS[name] = asyncio.Lock()
    return lock


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

    # Retire the previous attachment and spawn ours as one atomic step, so two
    # connections can never have a live PTY for the same view at once.
    async with attach_lock(view):
        prev = ATTACHED.pop(view, None)
        if prev is not None:
            log(f"conn {cid} retiring previous attachment pid={prev.pid}")
            prev.retire()
            # Let the old handler finish its own teardown before this attach
            # runs, so `tmux attach -d` never has a live client to kick.
            try:
                await asyncio.wait_for(prev.done.wait(), timeout=3)
            except asyncio.TimeoutError:
                log(f"conn {cid} previous attachment slow to exit; continuing")
        pid, fd = spawn_pty(attach_argv(session_name, view), cols, rows)
        os.set_blocking(fd, False)
        me = Attachment(pid, fd)
        ATTACHED[view] = me

    # Off-thread: enable_mouse polls for the just-spawned session.
    asyncio.create_task(asyncio.to_thread(enable_mouse, view))

    # PTY reads land in this queue via add_reader; None marks the PTY closing.
    out: asyncio.Queue = asyncio.Queue()

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

    loop.add_reader(fd, on_readable)

    async def pump_out() -> None:
        while True:
            data = await out.get()
            if data is None:
                break
            await ws.send_bytes(data)

    async def pump_in() -> None:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if (data := msg.get("bytes")) is not None:
                os.write(fd, data)
                continue
            text = msg.get("text")
            if text is None:
                continue
            # Text frames are either a resize control message or raw keystrokes.
            if text.startswith("{"):
                try:
                    ctl = json.loads(text)
                except json.JSONDecodeError:
                    ctl = None
                if ctl and ctl.get("type") == "resize":
                    set_winsize(fd, ctl.get("cols", 80), ctl.get("rows", 24))
                    continue
            os.write(fd, text.encode("utf-8"))

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
        try:
            loop.remove_reader(fd)
        except (OSError, ValueError):
            pass
        # Drop the slot only if we still own it. No lock here: the retiring
        # connection holds it while waiting on me.done, so taking it would
        # deadlock. Identity is enough — only we ever remove ourselves, and a
        # newer connection has already replaced the entry by this point.
        if ATTACHED.get(view) is me:
            del ATTACHED[view]
        await reap(pid, fd)
        reason = "superseded" if me.retired.is_set() else "client-or-pty-gone"
        log(f"conn {cid} close session={session_name} reason={reason}")
        # Unblocks the newer connection, which is waiting for our PTY to be gone.
        me.done.set()
        try:
            await ws.close()
        except RuntimeError:
            pass


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
