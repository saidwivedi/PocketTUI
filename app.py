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
import asyncio
import fcntl
import hmac
import json
import os
import pty
import re
import secrets
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
import uvicorn

HERE = Path(__file__).resolve().parent
HTML_PATH = HERE / "mobile_app.html"
VENDOR_DIR = HERE / "vendor"
TOKEN_PATH = HERE / ".token"

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
