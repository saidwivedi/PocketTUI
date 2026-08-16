#!/usr/bin/env python
"""PocketTUI — phone-facing tmux terminal.

Serves a single-page mobile web app that lists the workstation's tmux sessions
and attaches to them over a WebSocket-bridged PTY. Reached both directly at
http://<host>:5560/ and behind `tailscale serve` at https://<host>/pockettui/,
so every URL the frontend uses is relative — the proxy strips the /pockettui
prefix and this server always sees paths rooted at /.

Attach uses a *grouped* session (tmux new-session -t <target>): the phone gets
an independent view of the same windows, so attaching from the phone never
resizes or detaches the client already attached on the laptop.
"""

import argparse
import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import subprocess
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

# Cache-busting stamp, injected into the HTML/sw at serve time. Bumping on every
# server start is what makes iOS drop the old PWA shell after a redeploy.
CACHE_VERSION = time.strftime("%Y%m%d-%H%M%S")

# Sessions this app creates for its own grouped views — hidden from the list so
# the phone never shows its own reflections.
PHONE_PREFIX = "phone-"

app = FastAPI(title="PocketTUI")

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
        "\t#{@alias}",
    )
    if rc != 0:
        return []

    sessions = []
    for line in out.splitlines():
        # The alias field is last and empty when unset, so split to a fixed width
        # rather than requiring every field to be present.
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, created, attached, windows = parts[:4]
        alias = parts[4] if len(parts) > 4 else ""
        if name.startswith(PHONE_PREFIX):
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


def attach_argv(target: str) -> list[str]:
    """Command that gives the phone its own view of `target`.

    A grouped session shares the target's windows but keeps its own current
    window and size, so the phone client's small size never squeezes the
    laptop's client. Reuse the phone session across reconnects (-d kicks off any
    stale client of it) so the phone's window selection survives a dropout.
    """
    phone = PHONE_PREFIX + target
    if session_exists(phone):
        return ["tmux", "attach", "-d", "-t", f"={phone}"]
    return ["tmux", "new-session", "-s", phone, "-t", f"={target}"]


def enable_mouse(target: str) -> None:
    """Turn on mouse reporting for the phone's own session only.

    Drag-to-scroll on the phone works by synthesising SGR wheel events, which
    tmux only acts on with `mouse on`. The option is set on the phone-* session
    alone (a grouped session carries its own options), so the laptop's client of
    the same windows keeps whatever the user configured.
    """
    phone = PHONE_PREFIX + target
    # The session only exists once the attach child has spawned it, so retry
    # briefly rather than racing the fork.
    for _ in range(20):
        if session_exists(phone):
            # No "=" exact-match prefix here: set-option rejects it outright
            # ("no such session"), unlike the session-target commands above.
            tmux("set-option", "-t", phone, "mouse", "on")
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
    if name.startswith(PHONE_PREFIX):
        return "", f"Session name cannot start with '{PHONE_PREFIX}' — that prefix is reserved."
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
    if name.startswith(PHONE_PREFIX) or not session_exists(name):
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
TERMINFO_DIRS = ":".join([
    str(Path.home() / ".terminfo"),
    "/etc/terminfo",
    "/lib/terminfo",
    "/usr/share/terminfo",
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


# Attaching is not safe to do concurrently for one session: `tmux attach -d`
# detaches whichever client is already there, so two overlapping attaches kick
# each other, and each kicked client's PTY dies, closing its WebSocket, whose
# browser then reconnects and kicks the other one back. That ping-pong is what
# made opening a session flap several times before settling. One lock per
# session name serialises attaches, and ATTACHED tracks the live one so a new
# connection can retire its predecessor deliberately instead of racing it.
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

    if not session_exists(session_name):
        log(f"conn {cid} close session={session_name} reason=no-such-session")
        await ws.close(code=4404, reason=f"no tmux session {session_name!r}")
        return

    # The client sends its size as the first frame; use it for the initial
    # winsize so tmux never paints at the default 80x24 and then reflows.
    cols, rows = 80, 24
    try:
        first = await asyncio.wait_for(ws.receive(), timeout=5)
        if first.get("type") == "websocket.disconnect":
            return
        text = first.get("text")
        if text:
            msg = json.loads(text)
            if msg.get("type") == "resize":
                cols, rows = msg.get("cols", cols), msg.get("rows", rows)
    except (asyncio.TimeoutError, ValueError, KeyError, json.JSONDecodeError):
        pass

    loop = asyncio.get_running_loop()

    # Retire the previous attachment and spawn ours as one atomic step, so two
    # connections can never have a live PTY for the same session at once.
    async with attach_lock(session_name):
        prev = ATTACHED.pop(session_name, None)
        if prev is not None:
            log(f"conn {cid} retiring previous attachment pid={prev.pid}")
            prev.retire()
            # Let the old handler finish its own teardown before this attach
            # runs, so `tmux attach -d` never has a live client to kick.
            try:
                await asyncio.wait_for(prev.done.wait(), timeout=3)
            except asyncio.TimeoutError:
                log(f"conn {cid} previous attachment slow to exit; continuing")
        pid, fd = spawn_pty(attach_argv(session_name), cols, rows)
        os.set_blocking(fd, False)
        me = Attachment(pid, fd)
        ATTACHED[session_name] = me

    # Off-thread: enable_mouse polls for the just-spawned session.
    asyncio.create_task(asyncio.to_thread(enable_mouse, session_name))

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
        if ATTACHED.get(session_name) is me:
            del ATTACHED[session_name]
        await reap(pid, fd)
        reason = "superseded" if me.retired.is_set() else "client-or-pty-gone"
        log(f"conn {cid} close session={session_name} reason={reason}")
        # Unblocks the newer connection, which is waiting for our PTY to be gone.
        me.done.set()
        try:
            await ws.close()
        except RuntimeError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="PocketTUI — phone-facing tmux terminal")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5560)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
