#!/usr/bin/env python3
"""Headless Chromium on the computer, driven over the DevTools protocol.

The browser pane renders pages through a rewriting proxy, which cannot run a
login flow, a top-window site or anything that needs a real origin. "Full
browser" mode answers that by running a real Chromium on this machine and
streaming its pixels to the phone, so the page executes in a genuine browser
with a genuine origin and only the frames travel.

This module is the floor that mode stands on: find a browser, start it, talk to
it. Nothing here knows about tabs, screencasts or WebSockets — the tab manager
above it does, and it needs only `launch()` and the `CDP`/`Session` pair.

Two choices are worth stating up front:

* The DevTools connection is a *pipe*, not a port. `--remote-debugging-port`
  opens an HTTP server on the machine, and anything that can reach that port
  drives the browser with no token; `--remote-debugging-pipe` hands the
  protocol to this process alone over two inherited fds, so there is nothing to
  reach. The child reads fd 3 and writes fd 4, one JSON message per NUL.
* `--no-sandbox` is never passed. The whole point is running untrusted pages,
  and the sandbox is what keeps a compromised renderer off the rest of the
  machine. A box that cannot start a sandboxed Chromium gets the `--check`
  diagnosis and no browser, not a browser with its guard down.

It imports on a machine with no Chromium at all: every probe is a call away,
nothing runs at import time.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import fcntl
import inspect
import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Anything older than this predates `--headless=new` being the real browser
# rather than the old stripped-down headless shell, and its screencast and
# input handling differ enough that supporting it would mean two code paths.
MIN_MAJOR = 120

# Names worth trying on $PATH, best first. Edge is a Chromium too and speaks the
# same protocol; a box that has it and nothing else still works.
PATH_NAMES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "microsoft-edge-stable",
    "microsoft-edge",
)

MAC_BUNDLES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

# Where a downloaded Chrome for Testing keeps its binary, relative to the
# version directory. The mac entry is a glob because the directory carries the
# architecture (chrome-mac-x64, chrome-mac-arm64).
DOWNLOAD_BINARIES = (
    "chrome-linux64/chrome",
    "chrome-linux-arm64/chrome",
    "chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
)

VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)\.(\d+)")
VERSION_TIMEOUT_S = 10.0

# A found browser is remembered this long. The answer costs a process spawn, the
# tab manager asks for it on every request that might start the browser, and a
# browser does not appear or vanish mid-minute.
FIND_TTL_S = 30.0

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 800

# Chrome writes its own complaints to stderr, and they are the only evidence
# when a launch dies. Kept in one file, truncated rather than rotated: past a
# megabyte the interesting part is the tail anyway.
LOG_MAX_BYTES = 1 << 20

BROWSER_CONFIG_DEFAULTS = {
    "memory_mb": None,  # filled in from this machine's RAM, see load_browser_config
    "freeze_after_s": 300,
    "idle_exit_s": 600,
    "dpr_max": 2,
    "jpeg_quality": 60,
    "binary": None,
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] browser: {msg}", flush=True)


def config_dir() -> Path:
    """`~/.pockettui`, resolved now rather than at import.

    Every path in this module goes through here on purpose: the tests point HOME
    at a temp directory, and a module-level constant would have been frozen at
    import time, before they could.
    """
    return Path(os.path.expanduser("~")) / ".pockettui"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Found:
    path: str
    version: str
    major: int
    source: str  # "config" | "path" | "bundle" | "downloaded"


_find_cache: tuple[float, tuple, "Found | None"] | None = None


def _probe_version(path: str) -> "tuple[str, int] | None":
    """`<bin> --version` parsed, or None when it is not a usable browser.

    Chromium prints to stdout, some builds to stderr, so both are searched.
    """
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=VERSION_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    m = VERSION_RE.search((out.stdout or "") + "\n" + (out.stderr or ""))
    if not m:
        return None
    major = int(m.group(1))
    if major < MIN_MAJOR:
        return None
    return m.group(0), major


def _executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _download_candidates() -> "list[str]":
    """Binaries under `~/.pockettui/chromium`, newest version first.

    `current` wins when present: the installer writes the path it just unpacked
    there, which is the one it means to be used even if a higher version
    directory is lying around half-written.
    """
    root = config_dir() / "chromium"
    out: list[str] = []
    try:
        pinned = (root / "current").read_text(encoding="utf-8").strip()
    except OSError:
        pinned = ""
    if pinned:
        out.append(pinned)
    try:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        dirs = []

    def key(d: Path) -> tuple:
        return tuple(int(p) for p in re.findall(r"\d+", d.name)) or (0,)

    for d in sorted(dirs, key=key, reverse=True):
        for rel in DOWNLOAD_BINARIES:
            if "*" in rel:
                out.extend(str(p) for p in sorted(d.glob(rel)))
            else:
                out.append(str(d / rel))
    return out


def find_chromium(config: "dict | None" = None, force: bool = False) -> "Found | None":
    """The browser this machine should use, or None when there is none.

    Order is deliberate: what the user configured, then whatever the machine
    already has, and only then the copy PocketTUI downloaded for itself. A
    system Chrome gets its own updates; ours would have to be updated by us.
    """
    global _find_cache
    config = config or {}
    binary = config.get("binary") or None
    # Keyed on the two inputs that can change the answer, so a changed config —
    # or a test pointing HOME elsewhere — is never answered from the last call.
    key = (binary, os.path.expanduser("~"))
    if not force and _find_cache is not None:
        when, cached_key, found = _find_cache
        if cached_key == key and time.monotonic() - when < FIND_TTL_S:
            return found

    found = None
    candidates: list[tuple[str, str]] = []
    if binary and _executable(binary):
        candidates.append(("config", binary))
    for name in PATH_NAMES:
        hit = shutil.which(name)
        if hit:
            candidates.append(("path", hit))
    if sys.platform == "darwin":
        candidates += [("bundle", p) for p in MAC_BUNDLES if _executable(p)]
    candidates += [("downloaded", p) for p in _download_candidates() if _executable(p)]

    for source, path in candidates:
        probed = _probe_version(path)
        if probed:
            found = Found(path=path, version=probed[0], major=probed[1], source=source)
            break

    _find_cache = (time.monotonic(), key, found)
    return found


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _total_ram_mb() -> int:
    """This machine's RAM in MB, or 0 when it cannot be told."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=5)
            return int(out.stdout.strip()) // (1 << 20)
        return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) // (1 << 20)
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return 0


def default_memory_mb() -> int:
    """A quarter of RAM, never above 2 GB and never below 512 MB.

    The cap is what keeps a browsing session from competing with the work the
    machine is actually for; the floor is the least a real page needs, and it is
    also the answer when the RAM cannot be read at all — claiming more than we
    can justify on an unknown machine is the worse mistake.
    """
    total = _total_ram_mb()
    if total <= 0:
        return 512
    return max(512, min(total // 4, 2048))


def load_browser_config() -> dict:
    """`~/.pockettui/browser.json` over the defaults, per key.

    A bad file, or a bad value in a good file, falls back rather than refusing
    to start: this is a comfort setting, and a browser that will not open
    because a number was mistyped is worse than one that opens with the default.
    """
    cfg = dict(BROWSER_CONFIG_DEFAULTS)
    cfg["memory_mb"] = default_memory_mb()

    path = config_dir() / "browser.json"
    raw: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("not an object")
            raw = loaded
        except (OSError, ValueError) as e:
            log(f"{path} unreadable ({e}); using defaults")

    def number(key: str, cast, low, high=None):
        if key not in raw:
            return
        try:
            val = cast(raw[key])
        except (TypeError, ValueError):
            val = None
        if val is None or val < low or (high is not None and val > high):
            log(f"browser.json: {key}={raw[key]!r} out of range; using {cfg[key]}")
            return
        cfg[key] = val

    number("memory_mb", int, 128)
    number("freeze_after_s", int, 0)
    number("idle_exit_s", int, 0)
    number("dpr_max", float, 1, 4)
    number("jpeg_quality", int, 1, 100)
    if "binary" in raw:
        if isinstance(raw["binary"], str) or raw["binary"] is None:
            cfg["binary"] = raw["binary"] or None
        else:
            log(f"browser.json: binary={raw['binary']!r} is not a path; ignoring")

    env = os.environ.get("POCKETTUI_BROWSER_MEM_MB")
    if env:
        try:
            cfg["memory_mb"] = max(128, int(env))
        except ValueError:
            log(f"POCKETTUI_BROWSER_MEM_MB={env!r} is not a number; ignoring")
    return cfg


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def user_agent_for(version: str) -> str:
    """The UA a real Chrome of this version would send.

    Headless Chrome says `HeadlessChrome/<version>` and sites treat that as a
    bot: some serve a stripped page, some a block page. The browser is real, the
    person driving it is real, and the only thing headless about it is that its
    window is on a phone instead of this screen, so it identifies as Chrome.
    """
    token = ("Macintosh; Intel Mac OS X 10_15_7" if sys.platform == "darwin"
             else "X11; Linux x86_64")
    return (f"Mozilla/5.0 ({token}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version} Safari/537.36")


def dehead(ua: str) -> str:
    """A UA string with the headless marker taken out."""
    return ua.replace("HeadlessChrome/", "Chrome/")


def launch_flags(found: Found, config: dict, profile_dir: str,
                 width: int, height: int, ua: str) -> "list[str]":
    """The command line, without the binary and without a URL to open.

    No start page: the tab manager creates every target itself, and a browser
    that opened one of its own would leave a target nothing on the phone owns.

    `found` and `config` are taken for symmetry with `launch()`; the list they
    would vary is fixed today.
    """
    return [
        "--headless=new",
        "--remote-debugging-pipe",
        f"--user-data-dir={profile_dir}",
        # The debugging pipe alone sets navigator.webdriver, which is enough for
        # a site to refuse the page. Nothing here automates anything.
        "--disable-blink-features=AutomationControlled",
        f"--user-agent={ua}",
        "--renderer-process-limit=4",
        "--js-flags=--max-old-space-size=512",
        "--disable-gpu",
        "--mute-audio",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-extensions",
        # /dev/shm is small in a container and Chrome falls over when it fills.
        "--disable-dev-shm-usage",
        f"--window-size={width},{height}",
    ]


_scope_ok: "bool | None" = None
_unit_seq = itertools.count(1)


def systemd_scope_available() -> bool:
    """Whether a memory-capped transient scope can be had, probed once.

    The cap is the reason to want one: a page that eats the machine takes the
    scope's limit instead. Not every box has a user manager with the memory
    controller delegated, hence the probe rather than a version check.
    """
    global _scope_ok
    if _scope_ok is not None:
        return _scope_ok
    _scope_ok = False
    if shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR"):
        try:
            rc = subprocess.run(
                ["systemd-run", "--user", "--scope", "--quiet",
                 "-p", "MemoryMax=1M", "--", "/bin/true"],
                capture_output=True, timeout=VERSION_TIMEOUT_S).returncode
            _scope_ok = rc == 0
        except (OSError, subprocess.SubprocessError):
            _scope_ok = False
    return _scope_ok


def _scope_prefix(memory_mb: int) -> "tuple[list[str], str]":
    unit = f"pockettui-browser-{os.getpid()}-{next(_unit_seq)}"
    cmd = ["systemd-run", "--user", "--scope", "--quiet", f"--unit={unit}",
           "-p", f"MemoryHigh={int(memory_mb)}M",
           "-p", f"MemoryMax={int(memory_mb * 1.2)}M", "--"]
    return cmd, unit


def _pipe_preexec(read_fd: int, write_fd: int):
    """Put the child's ends of the two pipes on fds 3 and 4, where Chrome looks.

    Both ends are copied out of the way first, to fds the kernel picks as free
    — not to fixed numbers. The two pipes can land anywhere, including on 3, 4
    or on whatever staging number this code might have chosen, and one clobbered
    fd there gives Chrome two copies of the same pipe end: it starts, reads its
    own writes, and reports the connection terminated a second later.

    Everything above 2 that is not in `pass_fds` is closed after this runs,
    which is why 3 and 4 have to be passed.
    """
    def fn() -> None:
        a = fcntl.fcntl(read_fd, fcntl.F_DUPFD, 5)
        b = fcntl.fcntl(write_fd, fcntl.F_DUPFD, 5)
        os.dup2(a, 3)
        os.dup2(b, 4)
        os.close(a)
        os.close(b)
    return fn


def _stderr_file(path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        over = path.exists() and path.stat().st_size > LOG_MAX_BYTES
    except OSError:
        over = False
    try:
        return open(path, "wb" if over else "ab")
    except OSError:
        return subprocess.DEVNULL


class Browser:
    """One running Chromium and the protocol connection to it."""

    def __init__(self, found: Found, config: dict, profile_dir: Path, user_agent: str,
                 proc: subprocess.Popen, unit: "str | None", cdp: "CDP",
                 transports: tuple) -> None:
        self.found = found
        self.config = config
        self.profile_dir = profile_dir
        self.user_agent = user_agent
        self.pid = proc.pid
        self.unit = unit
        self.cdp = cdp
        self.closed: asyncio.Future = asyncio.get_running_loop().create_future()
        self._proc = proc
        self._transports = transports
        self._close_task: "asyncio.Task | None" = None

    # -- lifetime ----------------------------------------------------------
    def _mark_gone(self) -> None:
        """Called on pipe EOF and on process exit; both mean the same thing."""
        self.cdp.eof()
        if not self.closed.done():
            self.closed.set_result(self._proc.poll())

    async def _wait_exit(self, timeout: float) -> bool:
        """True once the process is reaped, False if it is still there.

        Polled rather than waited on: a thread is already blocked in `wait()`
        for this process (see `launch`), and `poll()` is the one call that can
        ask without fighting it for the reap.
        """
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._proc.poll() is not None:
                return True
            await asyncio.sleep(0.05)
        return self._proc.poll() is not None

    def _signal_group(self, sig: int) -> None:
        # start_new_session put the child in its own process group, so this
        # reaches every renderer and zygote it made, not just the parent.
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
        except (OSError, ProcessLookupError):
            pass

    async def close(self) -> None:
        """Stop the browser. Idempotent, and safe once it is already gone."""
        if self._close_task is None:
            self._close_task = asyncio.ensure_future(self._close())
        await self._close_task

    async def _close(self) -> None:
        try:
            await self.cdp.send("Browser.close", timeout=3)
        except (CDPError, CDPClosed, asyncio.TimeoutError, OSError):
            pass
        for t in self._transports:
            try:
                t.close()
            except Exception:
                pass
        self.cdp.eof()
        if not await self._wait_exit(3.0):
            self._signal_group(signal.SIGTERM)
            if not await self._wait_exit(3.0):
                self._signal_group(signal.SIGKILL)
                await self._wait_exit(3.0)
        if self.unit:
            # A stray child can hold the scope open after its main process is
            # gone, and a scope left behind keeps its name, which the next
            # launch would collide with.
            await asyncio.get_running_loop().run_in_executor(None, _kill_unit, self.unit)
        self._mark_gone()


def _kill_unit(unit: str) -> None:
    try:
        subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", unit],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


async def launch(found: Found, config: "dict | None" = None,
                 width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT) -> Browser:
    """Start `found` and return a `Browser` with a live protocol connection."""
    config = config if config is not None else load_browser_config()
    profile_dir = config_dir() / "chromium-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    # The profile holds cookies and logins for every site the phone visits, so
    # it is the user's alone even on a machine with other accounts on it.
    try:
        os.chmod(profile_dir, 0o700)
    except OSError:
        pass

    ua = user_agent_for(found.version)
    cmd = [found.path] + launch_flags(found, config, str(profile_dir), width, height, ua)
    unit = None
    if systemd_scope_available():
        prefix, unit = _scope_prefix(int(config.get("memory_mb") or default_memory_mb()))
        cmd = prefix + cmd

    r_in, w_in = os.pipe()    # this process writes w_in, the child reads fd 3
    r_out, w_out = os.pipe()  # the child writes fd 4, this process reads r_out
    errf = _stderr_file(config_dir() / "chromium.log")
    try:
        proc = subprocess.Popen(
            cmd, preexec_fn=_pipe_preexec(r_in, w_out), pass_fds=(3, 4),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errf,
            close_fds=True, start_new_session=True)
    except Exception:
        for fd in (r_in, w_in, r_out, w_out):
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    finally:
        if errf is not subprocess.DEVNULL:
            errf.close()
    os.close(r_in)
    os.close(w_out)

    loop = asyncio.get_running_loop()
    write_transport, _ = await loop.connect_write_pipe(
        asyncio.BaseProtocol, os.fdopen(w_in, "wb", buffering=0))
    cdp = CDP(lambda data: write_transport.write(data))
    # Built before the read pipe is connected, so that an EOF arriving on the
    # first turn of the loop — a browser that died between the spawn and here —
    # still resolves `closed` rather than landing on nothing.
    browser = Browser(found, config, profile_dir, ua, proc, unit, cdp,
                      (write_transport,))
    read_transport, _ = await loop.connect_read_pipe(
        lambda: _PipeReader(cdp, browser._mark_gone), os.fdopen(r_out, "rb", buffering=0))
    browser._transports = (write_transport, read_transport)
    # The pipe closes when Chrome dies, which is the signal that matters; the
    # reaper is here so the exit status is collected and no zombie is left when
    # the process goes without the pipe noticing (a scope killed from outside).
    threading.Thread(target=_reap, args=(proc, loop, browser._mark_gone),
                     daemon=True).start()

    try:
        info = await cdp.send("Browser.getVersion", timeout=10)
    except (CDPError, CDPClosed, asyncio.TimeoutError):
        info = {}
    real = dehead(info.get("userAgent") or "")
    if real and real != ua:
        # What the browser will actually send, minus the headless marker. The
        # constructed string is a good guess; this one is the truth, and the tab
        # manager passes it to Emulation.setUserAgentOverride per target.
        browser.user_agent = real
    return browser


def _reap(proc: subprocess.Popen, loop: asyncio.AbstractEventLoop, on_exit) -> None:
    try:
        proc.wait()
    except Exception:
        pass
    try:
        loop.call_soon_threadsafe(on_exit)
    except RuntimeError:
        pass


class _PipeReader(asyncio.Protocol):
    def __init__(self, cdp: "CDP", on_eof) -> None:
        self._cdp = cdp
        self._on_eof = on_eof

    def data_received(self, data: bytes) -> None:
        self._cdp.feed(data)

    def eof_received(self) -> bool:
        self._on_eof()
        return False

    def connection_lost(self, exc: "Exception | None") -> None:
        self._on_eof()


# ---------------------------------------------------------------------------
# DevTools protocol
# ---------------------------------------------------------------------------

class CDPError(Exception):
    """The browser answered a call with an error."""

    def __init__(self, code, message: str, method: str = "") -> None:
        self.code = code
        self.message = message
        self.method = method
        super().__init__(f"{method or '?'}: {message} ({code})")


class CDPClosed(Exception):
    """The connection to the browser is gone."""


class Session:
    """A protocol session attached to one target (one tab)."""

    def __init__(self, cdp: "CDP", sid: str, target_id: str) -> None:
        self.cdp = cdp
        self.id = sid
        self.target_id = target_id
        self.alive = True

    async def send(self, method: str, params: "dict | None" = None, timeout: float = 15):
        if not self.alive:
            raise CDPClosed(f"session {self.id} detached")
        return await self.cdp.send(method, params, session=self.id, timeout=timeout)

    def on(self, event: str, callback) -> None:
        self.cdp.on(event, callback, session=self.id)

    def off(self, event: str, callback) -> None:
        self.cdp.off(event, callback, session=self.id)

    async def detach(self) -> None:
        if not self.alive:
            return
        try:
            await self.cdp.send("Target.detachFromTarget", {"sessionId": self.id},
                                timeout=5)
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass
        self._die()

    def _die(self) -> None:
        if not self.alive:
            return
        self.alive = False
        self.cdp._session_died(self)


class CDP:
    """The protocol client: NUL-delimited JSON over the browser's pipe.

    It is fed bytes (`feed`) and hands out bytes (the `write` it was built
    with), which keeps the transport out of it: the launcher gives it an asyncio
    pipe, a test gives it a list.

    Everything reachable from the phone goes through here, so the reader is
    written to survive whatever comes back: a message split across reads, three
    messages in one read, a reply to a call that already timed out, or a line
    that is not JSON at all. It logs and carries on — a dead reader would strand
    every pending call and every tab at once.
    """

    def __init__(self, write) -> None:
        self._write = write
        self._buf = bytearray()
        self._ids = itertools.count(1)
        self._pending: dict = {}          # id -> (future, method, sessionId|None)
        self._handlers: dict = {}         # (event, sessionId|None) -> [callback]
        self._sessions: dict = {}         # sessionId -> Session
        self._closed = False

    # -- plumbing ----------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    def feed(self, data: bytes) -> None:
        self._buf += data
        while True:
            i = self._buf.find(b"\0")
            if i < 0:
                return
            raw = bytes(self._buf[:i])
            del self._buf[:i + 1]
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
                if not isinstance(msg, dict):
                    raise ValueError("not an object")
            except ValueError as e:
                log(f"unparsable protocol message ({e}): {raw[:120]!r}")
                continue
            try:
                self._dispatch(msg)
            except Exception as e:  # a handler's bug must not stop the reader
                log(f"protocol dispatch failed: {e!r}")

    def eof(self) -> None:
        """The pipe is gone: fail everything still waiting on it."""
        if self._closed:
            return
        self._closed = True
        for fut, method, _sid in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(CDPClosed(f"{method}: browser connection closed"))
        self._pending.clear()
        for sess in list(self._sessions.values()):
            sess.alive = False
        self._sessions.clear()
        self._handlers.clear()

    # -- calls -------------------------------------------------------------
    async def send(self, method: str, params: "dict | None" = None,
                   session: "str | None" = None, timeout: float = 15):
        if self._closed:
            raise CDPClosed(f"{method}: browser connection closed")
        mid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self._pending[mid] = (fut, method, session)
        try:
            self._write(json.dumps(msg).encode() + b"\0")
        except Exception as e:
            self._pending.pop(mid, None)
            raise CDPClosed(f"{method}: {e!r}") from e
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    # -- events ------------------------------------------------------------
    @staticmethod
    def _sid(session) -> "str | None":
        return session.id if isinstance(session, Session) else session

    def on(self, event: str, callback, session=None) -> None:
        self._handlers.setdefault((event, self._sid(session)), []).append(callback)

    def off(self, event: str, callback, session=None) -> None:
        key = (event, self._sid(session))
        try:
            self._handlers[key].remove(callback)
        except (KeyError, ValueError):
            return
        if not self._handlers[key]:
            del self._handlers[key]

    def _dispatch(self, msg: dict) -> None:
        mid = msg.get("id")
        if mid is not None:
            entry = self._pending.pop(mid, None)
            if entry is None:
                return  # a reply to a call that already gave up
            fut, method, _sid = entry
            if fut.done():
                return
            if "error" in msg:
                err = msg.get("error") or {}
                fut.set_exception(CDPError(err.get("code"), err.get("message", ""),
                                           method))
            else:
                fut.set_result(msg.get("result") or {})
            return

        method = msg.get("method")
        if not method:
            return
        sid = msg.get("sessionId")
        params = msg.get("params") or {}
        if method == "Target.detachedFromTarget":
            sess = self._sessions.get(params.get("sessionId"))
            if sess is not None:
                sess._die()
        for cb in list(self._handlers.get((method, sid), ())):
            self._fire(cb, params, method)

    def _fire(self, cb, params: dict, method: str) -> None:
        try:
            res = cb(params)
        except Exception as e:
            log(f"{method} handler raised: {e!r}")
            return
        if inspect.isawaitable(res):
            try:
                asyncio.ensure_future(self._await_handler(res, method))
            except RuntimeError:  # fed from outside a loop; nothing to run it on
                log(f"{method} handler could not be scheduled")

    @staticmethod
    async def _await_handler(awaitable, method: str) -> None:
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"{method} handler raised: {e!r}")

    # -- targets -----------------------------------------------------------
    async def attach(self, target_id: str, timeout: float = 15) -> Session:
        """Attach to a target, flattened onto this one connection.

        `flatten` is what makes every session share the pipe and carry a
        sessionId, rather than tunnelling messages inside Target.sendMessage.
        """
        res = await self.send("Target.attachToTarget",
                              {"targetId": target_id, "flatten": True}, timeout=timeout)
        sess = Session(self, res["sessionId"], target_id)
        self._sessions[sess.id] = sess
        return sess

    def session(self, sid: str) -> "Session | None":
        return self._sessions.get(sid)

    def _session_died(self, sess: Session) -> None:
        self._sessions.pop(sess.id, None)
        for mid, (fut, method, sid) in list(self._pending.items()):
            if sid == sess.id:
                self._pending.pop(mid, None)
                if not fut.done():
                    fut.set_exception(CDPClosed(f"{method}: target detached"))
        for key in [k for k in self._handlers if k[1] == sess.id]:
            del self._handlers[key]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEB_PACKAGES = ("libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 "
                "libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 "
                "libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0")
RPM_PACKAGES = ("nss atk at-spi2-atk cups-libs libdrm libxkbcommon libXcomposite "
                "libXdamage libXfixes libXrandr mesa-libgbm alsa-lib pango cairo "
                "at-spi2-core")
SANDBOX_MARKERS = ("No usable sandbox", "unshare", "CLONE_NEWUSER")
SANDBOX_HINT = ("hint: enable unprivileged user namespaces "
                "(sysctl kernel.unprivileged_userns_clone=1) or install chrome's "
                "sandbox helper")


def _package_line() -> str:
    if os.path.exists("/etc/debian_version"):
        return f"apt install {DEB_PACKAGES}"
    if os.path.exists("/etc/redhat-release") or os.path.exists("/etc/fedora-release"):
        return f"dnf install {RPM_PACKAGES}"
    return ("install chromium's runtime libraries with this system's package "
            "manager (nss, atk, at-spi2, cups, drm, xkbcommon, x11 libs, gbm, "
            "alsa, pango, cairo)")


def _missing_libs(path: str) -> "list[str]":
    if sys.platform != "linux":
        return []
    try:
        out = subprocess.run(["ldd", path], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    libs = []
    for line in (out.stdout or "").splitlines():
        if "=> not found" in line:
            libs.append(line.split("=>")[0].strip())
    return libs


def _check(path: str) -> int:
    if not _executable(path):
        print(f"not executable: {path}", file=sys.stderr)
        return 1
    missing = _missing_libs(path)
    if missing:
        print("missing: " + " ".join(missing))
        print(_package_line())
    with tempfile.TemporaryDirectory(prefix="pockettui-check-") as tmp:
        try:
            out = subprocess.run(
                [path, "--headless=new", "--disable-gpu", "--no-first-run",
                 f"--user-data-dir={tmp}", "--dump-dom", "about:blank"],
                capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            print("timed out after 15s starting the browser", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"could not run it: {e}", file=sys.stderr)
            return 1
    if out.returncode != 0:
        err = (out.stderr or "").strip()
        for line in err.splitlines()[-5:] or [f"exited {out.returncode} saying nothing"]:
            print(line, file=sys.stderr)
        if any(m in err for m in SANDBOX_MARKERS):
            print(SANDBOX_HINT, file=sys.stderr)
        return 1
    probed = _probe_version(path)
    print(f"ok {probed[0] if probed else 'unknown'}")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="Find and check the Chromium the browser pane streams from.")
    ap.add_argument("--find", action="store_true",
                    help="print the browser this machine would use")
    ap.add_argument("--check", metavar="BIN",
                    help="report what stops BIN from starting headless here")
    ap.add_argument("--install", action="store_true",
                    help="download a private Chrome for Testing (not implemented yet)")
    args = ap.parse_args(argv)

    if args.find:
        found = find_chromium(load_browser_config(), force=True)
        if not found:
            print("not found", file=sys.stderr)
            return 1
        print(f"{found.path}\t{found.version}")
        return 0
    if args.check:
        return _check(args.check)
    if args.install:
        print("not implemented")
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
