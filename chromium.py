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
import base64
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
        # Without this Chrome looks for a keyring to encrypt the profile's
        # cookies with, and on a headless box there is none: it either blocks
        # on a prompt nobody can answer or gives up and loses every login
        # between runs. "basic" keeps them in the profile directory, which is
        # already this user's alone (0700, see launch).
        "--password-store=basic",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-extensions",
        # /dev/shm is small in a container and Chrome falls over when it fills.
        "--disable-dev-shm-usage",
        f"--window-size={width},{height}",
    ]


def launch_env(base: "dict | None" = None) -> dict:
    """The child's environment: ours, minus the session bus.

    Chrome asks the session bus to move it into a scope of its own
    (`app-com.google.Chrome-<pid>.scope`) a second or two after it starts, and
    it succeeds: the transient scope this launcher put it in is left empty, and
    the memory limit and the kill that go with it apply to nothing. With no
    `DBUS_SESSION_BUS_ADDRESS` to find, and no X11 to autolaunch one from,
    there is nobody to ask and the browser stays where it was put.

    systemd-run is unaffected — it reaches the user manager through
    `XDG_RUNTIME_DIR/bus`, which is also what `systemd_scope_available`
    requires before a scope is attempted at all.
    """
    env = dict(os.environ if base is None else base)
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    return env


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
            close_fds=True, start_new_session=True, env=launch_env())
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
# Frames on the wire
# ---------------------------------------------------------------------------

# A frame is one length-prefixed JSON header followed by the image bytes, on
# the same socket as the JSON control messages. One socket rather than two
# because the two streams have to stay in order with each other: a `tab` saying
# the page navigated must not overtake the last frame of the page before it.
HEADER_MAX = 1 << 16


def pack_frame(header: dict, body: bytes) -> bytes:
    """u32 header length, the header as UTF-8 JSON, then the image."""
    raw = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(raw) > HEADER_MAX:
        raise ValueError(f"frame header of {len(raw)} bytes is too long")
    return len(raw).to_bytes(4, "little") + raw + body


def unpack_frame(buf: bytes) -> "tuple[dict, bytes]":
    """The inverse, for the client's test double and for this module's tests."""
    if len(buf) < 4:
        raise ValueError("short frame")
    n = int.from_bytes(buf[:4], "little")
    if n > HEADER_MAX or len(buf) < 4 + n:
        raise ValueError("truncated frame")
    header = json.loads(buf[4:4 + n].decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("frame header is not an object")
    return header, bytes(buf[4 + n:])


def image_size(data: bytes) -> "tuple[int, int] | None":
    """The pixel size of a JPEG, PNG or WebP, or None when it cannot be read.

    The client needs the true size of each image to draw it, and the browser
    does not say: a screencast frame carries page metadata, not its own
    dimensions, and Chrome is free to scale a frame down to the maximum it was
    given. Reading it out of the bytes is the only answer that cannot drift
    from what actually arrived; the caller falls back to the size we asked for.
    """
    try:
        if data[:2] == b"\xff\xd8":                       # JPEG
            i, n = 2, len(data)
            while i + 3 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = int.from_bytes(data[i + 2:i + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    return (int.from_bytes(data[i + 7:i + 9], "big"),
                            int.from_bytes(data[i + 5:i + 7], "big"))
                if seg < 2:
                    return None
                i += 2 + seg
            return None
        if data[:8] == b"\x89PNG\r\n\x1a\n":               # PNG
            return (int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"))
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # WebP, three flavours
            chunk = data[12:16]
            if chunk == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            if chunk == b"VP8 ":
                return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                        int.from_bytes(data[28:30], "little") & 0x3FFF)
        return None
    except (IndexError, ValueError):
        return None


class SendQueue:
    """One socket's outbound queue, with a rule about what may be dropped.

    A phone on a slow link cannot be allowed to make the backend hold every
    frame the page produced while it was behind: the *newest* picture is the
    only one worth sending, so a backlog of screencast frames for one tab is
    thinned to the last two. Nothing else is ever dropped — a `still` is the
    settled picture the cast frames were approximations of, a `popup` is the
    only picture of a menu that screencast cannot see at all, and a JSON
    message is state the client has no other way of learning.
    """

    CAST_BACKLOG = 2

    def __init__(self, backlog: int = CAST_BACKLOG) -> None:
        self._items: list = []   # (kind, tab, payload) — payload: dict or bytes
        self._backlog = backlog
        self._ready = asyncio.Event()
        self._closed = False

    def put_json(self, msg: dict) -> None:
        self._items.append(("json", None, msg))
        self._ready.set()

    def put_frame(self, header: dict, body: bytes) -> None:
        kind = str(header.get("kind") or "")
        tab = header.get("tab")
        self._items.append((kind, tab, pack_frame(header, body)))
        if kind == "cast":
            stale = [i for i, (k, t, _) in enumerate(self._items)
                     if k == "cast" and t == tab]
            for i in reversed(stale[:-self._backlog] if self._backlog else stale):
                del self._items[i]
        self._ready.set()

    def close(self) -> None:
        self._closed = True
        self._ready.set()

    def drain(self) -> list:
        """Everything queued, oldest first. For the writer and for tests."""
        out, self._items = self._items, []
        return [payload for _kind, _tab, payload in out]

    async def get(self):
        """The next thing to send, or None once the queue is closed."""
        while True:
            if self._items:
                return self._items.pop(0)[2]
            if self._closed:
                return None
            self._ready.clear()
            await self._ready.wait()


class _NullSink:
    """Where a detached pane's frames go: nowhere, without a special case."""

    def put_json(self, msg: dict) -> None:
        pass

    def put_frame(self, header: dict, body: bytes) -> None:
        pass


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

class TabLimit(Exception):
    """This computer is already running as many tabs as it will."""


class BrowserUnavailable(Exception):
    """There is no browser to run, or it would not start."""


# Every target the whole backend may have open at once. The pane caps itself at
# eight tabs; this is the second cap, on two panes plus whatever a stale pane
# left behind, because each target is a renderer process on someone's laptop.
MAX_TARGETS = 16

# How long after the last input (or the last frame that followed one) the
# settled picture is taken. Short enough to feel immediate, long enough that a
# scroll or a drag does not take a screenshot per animation frame.
SETTLE_DELAY_S = 0.15

# A native popup — a <select>'s menu, a date picker — is drawn by the browser
# outside the page's surface, so it is in a screenshot and not in a screencast
# frame. While one is open the tab is painted from screenshots instead.
POPUP_FPS = 8
POPUP_MAX_S = 15.0

# Chrome sends no further screencast frame until the last one is acknowledged,
# so an ack that never comes stops the stream. The client's own ack is the
# honest one — it means the picture is on screen — and this is the backstop.
ACK_TIMEOUT_S = 1.0

# What counts as a client that cannot keep up, and how long it has to be quiet
# before the full pixel ratio is given back.
SLOW_ACK_MS = 300
SLOW_ACK_STREAK = 3
DPR_RECOVER_S = 10.0

SELECTION_MAX = 1 << 20


# Injected into every document the tab loads, before the page's own scripts.
# It answers two questions the protocol cannot: whether a native popup is open
# (see POPUP_FPS above), and what the page thinks is selected, which is what
# the pane's copy button copies. Both go out through Runtime bindings, which
# are real functions in the page rather than anything the page could be
# confused by; nothing else is added to the page's globals.
FULL_PAGE_HELPER = r"""
(() => {
  const PICKERS = ['date', 'time', 'datetime-local', 'month', 'week', 'color'];
  let open = false;
  const say = (next) => {
    if (next === open) return;
    open = next;
    try { ptuiPopup(JSON.stringify({ open: next })); } catch (e) {}
  };
  const pops = (el) => {
    if (!el || el.nodeType !== 1) return false;
    if (el.tagName === 'SELECT') return !el.multiple && (el.size || 1) <= 1;
    if (el.tagName !== 'INPUT') return false;
    if (el.hasAttribute('list')) return true;
    return PICKERS.indexOf((el.type || '').toLowerCase()) >= 0;
  };
  addEventListener('mousedown', (e) => { if (pops(e.target)) say(true); }, true);
  addEventListener('focusin', (e) => { if (pops(e.target)) say(true); }, true);
  addEventListener('focusout', () => say(false), true);
  addEventListener('change', (e) => { if (pops(e.target)) say(false); }, true);
  addEventListener('keydown', (e) => { if (e.key === 'Escape') say(false); }, true);

  let timer = 0, last = null;
  const report = () => {
    timer = 0;
    let text = '';
    try {
      const el = document.activeElement;
      if (el && typeof el.selectionStart === 'number' &&
          el.selectionEnd > el.selectionStart) {
        text = String(el.value).slice(el.selectionStart, el.selectionEnd);
      } else {
        text = String(getSelection());
      }
    } catch (e) { text = ''; }
    if (text.length > %(selmax)d) text = text.slice(0, %(selmax)d);
    if (text === last) return;
    last = text;
    try { ptuiSel(text); } catch (e) {}
  };
  document.addEventListener('selectionchange',
    () => { if (!timer) timer = setTimeout(report, 50); }, true);
})();
""" % {"selmax": SELECTION_MAX}


class PaneTab:
    """One tab: a target, the session on it, and the stream out of it.

    Two states, `hidden` and `live`. A live tab is screencasting to its pane; a
    hidden one is still loaded and still navigable, and after
    `freeze_after_s` it is frozen, which is what stops a background tab's
    timers from costing the laptop anything. Nothing about the tab is tied to
    a socket: a phone that reloads gets its tabs back by target id.
    """

    def __init__(self, pane: str, tab: str, session: Session, target_id: str,
                 sink, config: dict, css_w: int = DEFAULT_WIDTH,
                 css_h: int = DEFAULT_HEIGHT, dpr: float = 1.0, zoom: float = 1.0,
                 owner: "FullBrowser | None" = None) -> None:
        self.pane = pane
        self.tab = tab
        self.session = session
        self.target_id = target_id
        self.sink = sink or _NullSink()
        self.config = config
        self.owner = owner

        self.css_w = int(css_w)
        self.css_h = int(css_h)
        self.dpr = float(dpr)
        self.zoom = float(zoom) or 1.0

        self.live = False
        self.frozen = False
        self.dead = False
        self.url = ""
        self.title = ""
        self.loading = False
        self.frame_id = ""
        self.selection = ""

        self._seq = 0
        self._casts = 0                 # frames emitted, for the settle race
        self._unacked: dict = {}        # seq -> (screencast sessionId, sent, timer)
        self._slow = 0
        self._dpr_forced = False
        self._dpr_since = 0.0
        self._dpr_task: "asyncio.Task | None" = None
        self._input_at = 0.0
        self._settle_task: "asyncio.Task | None" = None
        self._freeze_task: "asyncio.Task | None" = None
        self._state_task: "asyncio.Task | None" = None
        self._popup_open = False
        self._popup_task: "asyncio.Task | None" = None
        self._capturing = False
        self._held: list = []           # frames a settle capture provoked

    # -- geometry ----------------------------------------------------------
    def _metrics(self) -> dict:
        """The override that makes CSS pixels, zoom and the phone's DPR agree.

        Zoom is not applied to the page (a page can see its own zoom and some
        of them fight it); the viewport is made *smaller* by the zoom factor
        and rendered at a correspondingly higher scale, so the same surface
        comes back with everything on it bigger. `dpr_max` is the ceiling: a
        3x phone asking for a 3x surface of a desktop-sized viewport is asking
        for four times the pixels of a 1.5x one, over the same link.
        """
        z = self.zoom or 1.0
        dpr = 1.0 if self._dpr_forced else min(
            self.dpr, float(self.config.get("dpr_max") or 2))
        return {"width": int(round(self.css_w / z)),
                "height": int(round(self.css_h / z)),
                "deviceScaleFactor": dpr * z, "mobile": False}

    def _surface(self) -> "tuple[int, int]":
        """The device pixels that viewport comes to, which is the frame size.

        Derived from the override rather than from css*dpr directly, so the
        screencast maximum is exactly the surface: one pixel out and Chrome
        scales every frame down to fit, off the aspect ratio it was given.
        """
        m = self._metrics()
        return (max(1, int(round(m["width"] * m["deviceScaleFactor"]))),
                max(1, int(round(m["height"] * m["deviceScaleFactor"]))))

    # -- setup -------------------------------------------------------------
    async def start(self, url: "str | None" = None,
                    user_agent: "str | None" = None) -> None:
        """Enable what this tab needs, install the helper, and go.

        `url` is None when the target was adopted rather than created: the page
        is already there and navigating would throw away where the user was.
        """
        s = self.session
        await s.send("Page.enable")
        await s.send("Runtime.enable")
        await s.send("Page.setLifecycleEventsEnabled", {"enabled": True})
        await s.send("Emulation.setDeviceMetricsOverride", self._metrics())
        if user_agent:
            await s.send("Emulation.setUserAgentOverride", {"userAgent": user_agent})
        await s.send("Runtime.addBinding", {"name": "ptuiPopup"})
        await s.send("Runtime.addBinding", {"name": "ptuiSel"})
        await s.send("Page.addScriptToEvaluateOnNewDocument",
                     {"source": FULL_PAGE_HELPER})
        try:
            tree = await s.send("Page.getFrameTree")
            self.frame_id = ((tree.get("frameTree") or {}).get("frame") or {}).get("id", "")
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass
        self._wire()
        if url:
            await self.nav(url)
        else:
            self._schedule_state()

    def _wire(self) -> None:
        s = self.session
        s.on("Page.screencastFrame", self._on_cast)
        s.on("Runtime.bindingCalled", self._on_binding)
        s.on("Page.frameNavigated", self._on_navigated)
        s.on("Page.navigatedWithinDocument", self._on_in_document)
        s.on("Page.frameStartedLoading", self._on_started)
        s.on("Page.frameStoppedLoading", self._on_stopped)

    # -- outbound ----------------------------------------------------------
    def _emit(self, kind: str, body: bytes, fmt: str) -> "int | None":
        if not body or self.dead:
            return None
        self._seq += 1
        w, h = image_size(body) or self._surface()
        self.sink.put_frame({"tab": self.tab, "seq": self._seq, "kind": kind,
                             "w": w, "h": h, "cssW": self.css_w,
                             "cssH": self.css_h, "zoom": self.zoom, "fmt": fmt},
                            body)
        return self._seq

    def _say(self, msg: dict) -> None:
        self.sink.put_json(msg)

    def info(self) -> dict:
        return {"pane": self.pane, "tab": self.tab, "url": self.url,
                "title": self.title, "live": self.live, "frozen": self.frozen}

    # -- state -------------------------------------------------------------
    def _schedule_state(self) -> None:
        """Push `tab` once the burst of events that provoked it has passed.

        A single navigation fires four of these, and each would otherwise cost
        a round trip for the history; coalescing them costs 50 ms of staleness.
        """
        if self.dead or (self._state_task is not None and not self._state_task.done()):
            return
        self._state_task = asyncio.ensure_future(self._push_state())

    async def _push_state(self) -> None:
        await asyncio.sleep(0.05)
        if self.dead:
            return
        can_back = can_fwd = False
        try:
            hist = await self.session.send("Page.getNavigationHistory", timeout=5)
            entries = hist.get("entries") or []
            i = int(hist.get("currentIndex", -1))
            can_back, can_fwd = i > 0, 0 <= i < len(entries) - 1
            if 0 <= i < len(entries):
                self.url = entries[i].get("url") or self.url
                self.title = entries[i].get("title") or self.title
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass
        self._say({"type": "tab", "tab": self.tab, "url": self.url,
                   "title": self.title, "loading": self.loading,
                   "canBack": can_back, "canFwd": can_fwd,
                   "targetId": self.target_id})

    def _is_main(self, frame_id: str) -> bool:
        return not self.frame_id or frame_id == self.frame_id

    def _on_navigated(self, params: dict) -> None:
        frame = params.get("frame") or {}
        if frame.get("parentId"):
            return
        self.frame_id = frame.get("id") or self.frame_id
        self.url = frame.get("url") or self.url
        self._schedule_state()

    def _on_in_document(self, params: dict) -> None:
        if not self._is_main(params.get("frameId", "")):
            return
        self.url = params.get("url") or self.url
        self._schedule_state()

    def _on_started(self, params: dict) -> None:
        if self._is_main(params.get("frameId", "")):
            self.loading = True
            self._schedule_state()

    def _on_stopped(self, params: dict) -> None:
        if self._is_main(params.get("frameId", "")):
            self.loading = False
            self._schedule_state()

    def on_info(self, info: dict) -> None:
        """Target.targetInfoChanged for this tab: the title arrives here."""
        changed = False
        for key in ("url", "title"):
            value = info.get(key) or ""
            if value and value != getattr(self, key):
                setattr(self, key, value)
                changed = True
        if changed:
            self._schedule_state()

    # -- streaming ---------------------------------------------------------
    async def show(self, css_w: "int | None" = None, css_h: "int | None" = None,
                   dpr: "float | None" = None, zoom: "float | None" = None) -> None:
        """Make this the pane's live tab and start the stream."""
        if self.dead:
            raise CDPClosed(f"tab {self.tab} is gone")
        if self.owner is not None:
            await self.owner.hide_others(self.pane, self.tab)
        if css_w:
            self.css_w = int(css_w)
        if css_h:
            self.css_h = int(css_h)
        if dpr:
            self.dpr = float(dpr)
        if zoom:
            self.zoom = float(zoom)
        self._cancel(self._freeze_task)
        self._freeze_task = None
        await self.session.send("Emulation.setDeviceMetricsOverride", self._metrics())
        if self.frozen:
            await self.session.send("Page.setWebLifecycleState", {"state": "active"})
            self.frozen = False
        await self._start_cast()
        self.live = True

    async def hide(self) -> None:
        """Stop the stream, and start the clock on freezing the tab."""
        if not self.live:
            return
        self.live = False
        self._popup_open = False
        await self._stop_cast()
        if not self.dead:
            self._freeze_task = asyncio.ensure_future(self._freeze_later())

    async def _freeze_later(self) -> None:
        try:
            await asyncio.sleep(float(self.config.get("freeze_after_s") or 0))
        except asyncio.CancelledError:
            return
        if self.live or self.dead or self.frozen:
            return
        try:
            await self.session.send("Page.setWebLifecycleState", {"state": "frozen"})
            self.frozen = True
        except (CDPError, CDPClosed, asyncio.TimeoutError) as e:
            log(f"tab {self.tab}: freeze failed: {e!r}")

    async def _start_cast(self) -> None:
        w, h = self._surface()
        params = {"format": "jpeg",
                  "quality": int(self.config.get("jpeg_quality") or 60),
                  "maxWidth": w, "maxHeight": h, "everyNthFrame": 1}
        # A page part-way through a navigation is briefly not the active one —
        # the document going has been detached and the one arriving is not
        # shown yet — and Chrome refuses to cast it, saying so. Showing a tab
        # the instant it was opened lands in exactly that window, so it is
        # waited out rather than reported as a tab that cannot be streamed.
        delay = 0.05
        for _ in range(5):
            try:
                await self.session.send("Page.startScreencast", params)
                return
            except CDPError as e:
                if "active page" not in (e.message or ""):
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.4)
        await self.session.send("Page.startScreencast", params)

    async def _stop_cast(self) -> None:
        for seq in list(self._unacked):
            sid, _sent, timer = self._unacked.pop(seq)
            self._cancel(timer)
            self._ack_cdp(sid)
        try:
            await self.session.send("Page.stopScreencast")
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass

    async def _restart_cast(self) -> None:
        if not self.live or self.dead:
            return
        try:
            await self._stop_cast()
            await self.session.send("Emulation.setDeviceMetricsOverride",
                                    self._metrics())
            await self._start_cast()
        except (CDPError, CDPClosed, asyncio.TimeoutError) as e:
            log(f"tab {self.tab}: could not restart the stream: {e!r}")

    def _on_cast(self, params: dict) -> None:
        sid = params.get("sessionId")
        try:
            body = base64.b64decode(params.get("data") or "")
        except (ValueError, TypeError):
            body = b""
        if self._capturing:
            # Taking a screenshot forces a surface commit, so the screencast
            # answers one with a frame of the very picture being captured.
            # Held rather than sent: the settled frame about to go out is that
            # same picture in a better format, and two of them on the wire is
            # twice the bytes for one state. `_settle` decides which to keep.
            self._held.append((sid, body))
            self._ack_cdp(sid)
            return
        self._casts += 1
        seq = self._emit("cast", body, "jpeg")
        if seq is None:
            self._ack_cdp(sid)
        else:
            self._unacked[seq] = (sid, time.monotonic(),
                                  asyncio.ensure_future(self._ack_later(seq)))
        if self._input_at:
            self._arm_settle()

    def _ack_cdp(self, sid) -> None:
        if sid is None or self.dead:
            return
        asyncio.ensure_future(self._ack_send(sid))

    async def _ack_send(self, sid) -> None:
        try:
            await self.session.send("Page.screencastFrameAck", {"sessionId": sid},
                                    timeout=5)
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass

    async def _ack_later(self, seq: int) -> None:
        try:
            await asyncio.sleep(ACK_TIMEOUT_S)
        except asyncio.CancelledError:
            return
        entry = self._unacked.pop(seq, None)
        if entry is not None:
            self._ack_cdp(entry[0])

    def ack(self, seq: int) -> None:
        """The client has drawn everything up to `seq`."""
        now = time.monotonic()
        for pending in sorted(s for s in self._unacked if s <= seq):
            sid, sent, timer = self._unacked.pop(pending)
            self._cancel(timer)
            self._ack_cdp(sid)
            if pending == seq:
                if (now - sent) * 1000 > SLOW_ACK_MS:
                    self._slow += 1
                else:
                    self._slow = 0
        if self._slow >= SLOW_ACK_STREAK and not self._dpr_forced:
            # Three frames in a row that took longer to reach the screen than
            # they took to make: the link, not the browser, is the limit, so
            # send a quarter of the pixels until the user stops touching it.
            log(f"tab {self.tab}: client is behind; streaming at 1x")
            self._dpr_forced = True
            self._dpr_since = now
            self._slow = 0
            asyncio.ensure_future(self._restart_cast())
            if self._dpr_task is None or self._dpr_task.done():
                self._dpr_task = asyncio.ensure_future(self._dpr_restore())

    async def _dpr_restore(self) -> None:
        try:
            while self._dpr_forced and not self.dead:
                quiet = time.monotonic() - max(self._input_at, self._dpr_since)
                if quiet >= DPR_RECOVER_S:
                    self._dpr_forced = False
                    await self._restart_cast()
                    return
                await asyncio.sleep(min(1.0, DPR_RECOVER_S - quiet))
        except asyncio.CancelledError:
            return

    # -- input, settling and popups ---------------------------------------
    def note_input(self, kind: str = "", key: str = "") -> None:
        """Told by the input handlers that something was sent to the page.

        Two things hang off it: the settled picture below, and the end of a
        native popup — the click that picks an option, or the key that
        dismisses the menu, is the last thing the screenshots are needed for.
        """
        self._input_at = time.monotonic()
        self._arm_settle()
        if self._popup_open and (kind == "mousePressed" or
                                 (kind == "keyUp" and key in ("Escape", "Enter", "Tab"))):
            self._popup_open = False

    def _arm_settle(self) -> None:
        """(Re)start the clock on the settled picture.

        The screencast is lossy in a way that matters: Chrome produces no new
        frame while the last one is unacknowledged and never goes back for the
        one it skipped, so the final state of a drag or a menu can simply never
        appear in the stream. A screenshot once the input stops is what makes
        the last thing the user did the thing they see.
        """
        if self.dead or not self.live:
            return
        self._cancel(self._settle_task)
        self._settle_task = asyncio.ensure_future(self._settle())

    async def _settle(self) -> None:
        try:
            await asyncio.sleep(SETTLE_DELAY_S)
        except asyncio.CancelledError:
            return
        self._input_at = 0.0
        if self.dead or not self.live:
            return
        self._held = []
        self._capturing = True
        try:
            res = await self.session.send(
                "Page.captureScreenshot",
                {"format": "webp", "quality": 85, "fromSurface": True,
                 "captureBeyondViewport": False}, timeout=10)
        except asyncio.CancelledError:
            # A fresh input arrived and re-armed the timer under this capture.
            # The flag has to come down either way: left up, every screencast
            # frame from here on would be held and none of them sent.
            self._capturing = False
            self._flush_held()
            raise
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            self._capturing = False
            self._flush_held()
            return
        self._capturing = False
        # One held frame is the capture's own echo and the settled picture
        # supersedes it. More than one means the page was painting on its own
        # while the picture was being taken, and its frames are the newer
        # truth: send those and throw the picture away.
        if self.dead or not self.live or len(self._held) > 1:
            self._flush_held()
            return
        self._held = []
        try:
            self._emit("still", base64.b64decode(res.get("data") or ""), "webp")
        except (ValueError, TypeError):
            pass

    def _flush_held(self) -> None:
        """Send the frames held during a capture, already acknowledged."""
        held, self._held = self._held, []
        for _sid, body in held:
            self._casts += 1
            self._emit("cast", body, "jpeg")

    def _on_binding(self, params: dict) -> None:
        name = params.get("name")
        payload = params.get("payload") or ""
        if name == "ptuiSel":
            self.selection = payload[:SELECTION_MAX]
            return
        if name != "ptuiPopup":
            return
        try:
            self._popup_open = bool((json.loads(payload) or {}).get("open"))
        except (ValueError, AttributeError):
            return
        if self._popup_open and self.live and (
                self._popup_task is None or self._popup_task.done()):
            self._popup_task = asyncio.ensure_future(self._popup_loop())

    async def _popup_loop(self) -> None:
        """Paint from screenshots while a native popup is up.

        A <select>'s menu is the browser's own window, drawn over the page's
        surface and not into it: it is in `captureScreenshot {fromSurface}` and
        it is in no screencast frame at all. Without this the user opens a
        dropdown and sees nothing happen.
        """
        end = time.monotonic() + POPUP_MAX_S
        quality = int(self.config.get("jpeg_quality") or 60)
        try:
            while (self._popup_open and self.live and not self.dead
                   and time.monotonic() < end):
                res = await self.session.send(
                    "Page.captureScreenshot",
                    {"format": "jpeg", "quality": quality, "fromSurface": True,
                     "optimizeForSpeed": True}, timeout=10)
                self._emit("popup", base64.b64decode(res.get("data") or ""), "jpeg")
                await asyncio.sleep(1.0 / POPUP_FPS)
        except asyncio.CancelledError:
            raise
        except (CDPError, CDPClosed, asyncio.TimeoutError, ValueError, TypeError):
            pass
        finally:
            self._popup_open = False

    # -- navigation --------------------------------------------------------
    async def nav(self, url: str) -> None:
        await self.session.send("Page.navigate", {"url": url})
        self.loading = True
        self._schedule_state()

    async def back(self) -> None:
        await self._history_step(-1)

    async def fwd(self) -> None:
        await self._history_step(1)

    async def _history_step(self, delta: int) -> None:
        hist = await self.session.send("Page.getNavigationHistory")
        entries = hist.get("entries") or []
        i = int(hist.get("currentIndex", -1)) + delta
        if 0 <= i < len(entries):
            await self.session.send("Page.navigateToHistoryEntry",
                                    {"entryId": entries[i]["id"]})
        self._schedule_state()

    async def reload(self) -> None:
        await self.session.send("Page.reload")
        self._schedule_state()

    async def stop(self) -> None:
        await self.session.send("Page.stopLoading")
        self.loading = False
        self._schedule_state()

    async def set_zoom(self, z: float) -> None:
        """Change the zoom factor. Named apart from the `zoom` it sets."""
        self.zoom = float(z) or 1.0
        await self._apply_geometry()

    async def resize(self, css_w: int, css_h: int, dpr: "float | None" = None) -> None:
        self.css_w = int(css_w)
        self.css_h = int(css_h)
        if dpr:
            self.dpr = float(dpr)
        await self._apply_geometry()

    async def _apply_geometry(self) -> None:
        await self.session.send("Emulation.setDeviceMetricsOverride", self._metrics())
        if self.live:
            await self._stop_cast()
            await self._start_cast()

    # -- end ---------------------------------------------------------------
    async def close(self) -> None:
        """Close the target. The tab is gone whether or not the call lands."""
        self._teardown()
        try:
            await self.session.cdp.send("Target.closeTarget",
                                        {"targetId": self.target_id}, timeout=5)
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass

    @staticmethod
    def _cancel(task) -> None:
        if task is not None and not task.done():
            task.cancel()

    def _teardown(self) -> None:
        """Stop everything this tab is running. Idempotent."""
        if self.dead:
            return
        self.dead = True
        self.live = False
        self._popup_open = False
        for task in (self._settle_task, self._freeze_task, self._popup_task,
                     self._dpr_task, self._state_task):
            self._cancel(task)
        for _seq, (_sid, _sent, timer) in list(self._unacked.items()):
            self._cancel(timer)
        self._unacked.clear()


# ---------------------------------------------------------------------------
# The browser the tabs live in
# ---------------------------------------------------------------------------

class FullBrowser:
    """One Chromium for this whole backend, and every pane's tabs in it.

    One browser rather than one per pane because the profile is the point:
    the cookies, the logins and the history are the user's, and two browsers
    on one profile directory corrupt it. Panes are separated by which tabs
    they own, not by which browser they talk to.

    Nothing starts until something is opened, and nothing stays started: with
    no tabs left and no pane connected the browser is shut down again after
    `idle_exit_s`, because a headless Chrome nobody is looking at is still a
    few hundred megabytes of someone's laptop.
    """

    _instance: "FullBrowser | None" = None

    def __init__(self, config: "dict | None" = None) -> None:
        self.config = config if config is not None else load_browser_config()
        self.browser: "Browser | None" = None
        self.found: "Found | None" = None
        self.launch_error = ""
        self._tabs: dict = {}        # (pane, tab) -> PaneTab
        self._by_target: dict = {}   # targetId -> PaneTab
        self._sinks: dict = {}       # pane -> sink
        self._lock = asyncio.Lock()
        self._ua_needed: "bool | None" = None
        self._idle_task: "asyncio.Task | None" = None

    @classmethod
    def get(cls) -> "FullBrowser":
        """The one instance. Cheap: it holds no browser until something opens."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # -- panes -------------------------------------------------------------
    def _sink(self, pane: str):
        return self._sinks.get(pane) or _NullSink()

    def attach_pane(self, pane: str, sink) -> None:
        """A socket for `pane` is up: its tabs send through this one now."""
        self._cancel_idle()
        self._sinks[pane] = sink
        for pt in self._tabs.values():
            if pt.pane == pane:
                pt.sink = sink

    async def detach_pane(self, pane: str) -> None:
        """The socket went away. The tabs do not: a reload adopts them back."""
        self._sinks.pop(pane, None)
        for pt in list(self._tabs.values()):
            if pt.pane == pane:
                pt.sink = _NullSink()
                try:
                    await pt.hide()
                except (CDPError, CDPClosed, asyncio.TimeoutError):
                    pass
        self._arm_idle()

    def to_pane(self, pane: str, msg: dict) -> None:
        self._sink(pane).put_json(msg)

    def _to_all(self, msg: dict) -> None:
        for sink in list(self._sinks.values()):
            sink.put_json(msg)

    # -- lifetime ----------------------------------------------------------
    async def ensure(self) -> Browser:
        """The running browser, started now if it is not running yet."""
        async with self._lock:
            if self.browser is not None and not self.browser.closed.done():
                return self.browser
            self.browser = None
            self._ua_needed = None
            found = find_chromium(self.config)
            if found is None:
                self.launch_error = "no Chromium, Chrome or Edge on this computer"
                raise BrowserUnavailable(self.launch_error)
            try:
                browser = await launch(found, self.config)
            except Exception as e:  # noqa: BLE001 — every failure is the same news
                self.launch_error = f"{found.path} would not start: {e}"
                log(self.launch_error)
                raise BrowserUnavailable(self.launch_error) from e
            self.found = found
            self.browser = browser
            self.launch_error = ""
            browser.closed.add_done_callback(self._on_browser_gone)
            try:
                await browser.cdp.send("Target.setDiscoverTargets", {"discover": True})
            except (CDPError, CDPClosed, asyncio.TimeoutError):
                pass
            browser.cdp.on("Target.targetInfoChanged", self._on_target_info)
            browser.cdp.on("Target.targetDestroyed", self._on_target_destroyed)
            browser.cdp.on("Target.targetCrashed", self._on_target_crashed)
            log(f"browser up: {found.path} {found.version} pid={browser.pid}")
            return browser

    def _on_browser_gone(self, fut: asyncio.Future) -> None:
        if self.browser is None or self.browser.closed is not fut:
            return
        code = fut.result() if not fut.cancelled() and fut.exception() is None else None
        message = f"the browser exited ({code})" if code is not None else \
            "the browser exited"
        for pt in list(self._tabs.values()):
            pt._teardown()
        self._tabs.clear()
        self._by_target.clear()
        self.browser = None
        self.launch_error = message
        log(message)
        self._to_all({"type": "error", "tab": None, "code": "exited",
                      "message": message})

    async def shutdown(self, timeout: float = 6.0) -> None:
        """Close every tab and stop the browser, for the server going down."""
        end = time.monotonic() + timeout
        self._cancel_idle()
        tabs = list(self._tabs.values())
        self._tabs.clear()
        self._by_target.clear()
        browser, self.browser = self.browser, None
        for pt in tabs:
            pt._teardown()
        if browser is None:
            return
        if tabs:
            closing = [browser.cdp.send("Target.closeTarget",
                                        {"targetId": pt.target_id}, timeout=1.5)
                       for pt in tabs]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*closing, return_exceptions=True),
                    min(1.5, timeout / 3))
            except asyncio.TimeoutError:
                pass
        try:
            browser.closed.remove_done_callback(self._on_browser_gone)
        except (ValueError, AttributeError):
            pass
        try:
            # Shielded, so running out of budget stops the *waiting* rather
            # than the closing: a cancelled close would leave Chrome half torn
            # down. Past the budget the server has to go anyway, and the pipe
            # closing already took the browser with it — the kill is the belt.
            await asyncio.wait_for(asyncio.shield(browser.close()),
                                   max(1.0, end - time.monotonic()))
        except asyncio.TimeoutError:
            log("the browser did not stop in time; killing it")
            browser._signal_group(signal.SIGKILL)

    # -- idle --------------------------------------------------------------
    def _cancel_idle(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _arm_idle(self) -> None:
        self._cancel_idle()
        if self._tabs or self._sinks or self.browser is None:
            return
        self._idle_task = asyncio.ensure_future(self._idle_exit())

    async def _idle_exit(self) -> None:
        try:
            await asyncio.sleep(float(self.config.get("idle_exit_s") or 0))
        except asyncio.CancelledError:
            return
        if self._tabs or self._sinks or self.browser is None:
            return
        log("no tabs and no panes; stopping the browser")
        await self.shutdown()

    # -- targets -----------------------------------------------------------
    async def _target_alive(self, target_id: str) -> bool:
        try:
            res = await self.browser.cdp.send("Target.getTargets", timeout=5)
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            return False
        return any(t.get("targetId") == target_id and t.get("type") == "page"
                   for t in res.get("targetInfos") or [])

    async def _probe_headless(self, sess: Session) -> bool:
        try:
            res = await sess.send("Runtime.evaluate",
                                  {"expression": "navigator.userAgent",
                                   "returnByValue": True}, timeout=5)
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            return False
        return "Headless" in str((res.get("result") or {}).get("value") or "")

    async def open(self, pane: str, tab: str, url: str,
                   target_id: "str | None" = None, css_w: int = DEFAULT_WIDTH,
                   css_h: int = DEFAULT_HEIGHT, dpr: float = 1.0,
                   zoom: float = 1.0) -> PaneTab:
        """Open (or take back) one tab of one pane.

        `target_id` is what a phone that reloaded sends: the target is still
        there with the page still on it, so the tab is adopted rather than made
        again. A target that has since gone — the browser was restarted under
        it — falls through to a new one, which is why the caller passes the URL
        either way.
        """
        browser = await self.ensure()
        self._cancel_idle()
        existing = self._tabs.get((pane, tab))
        if existing is not None and not existing.dead:
            # Already ours — a phone that reloaded, or a second `open` for a
            # tab that never went away. It gets its state pushed rather than a
            # silent no-op, because the shell has just been rebuilt and knows
            # nothing about the page it is looking at.
            existing._schedule_state()
            return existing
        adopt = bool(target_id) and await self._target_alive(target_id)
        if not adopt:
            if len(self._tabs) >= MAX_TARGETS:
                raise TabLimit(f"{MAX_TARGETS} tabs are already open on this computer")
            res = await browser.cdp.send("Target.createTarget",
                                         {"url": "about:blank", "newWindow": True})
            target_id = res["targetId"]
        sess = await browser.cdp.attach(target_id)
        if self._ua_needed is None:
            self._ua_needed = await self._probe_headless(sess)
        pt = PaneTab(pane, tab, sess, target_id, self._sink(pane), self.config,
                     css_w=css_w, css_h=css_h, dpr=dpr, zoom=zoom, owner=self)
        self._tabs[(pane, tab)] = pt
        self._by_target[target_id] = pt
        try:
            await pt.start(None if adopt else url,
                           browser.user_agent if self._ua_needed else None)
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            self._forget(pt)
            await pt.close()
            raise
        return pt

    def tab(self, pane: str, tab: str) -> "PaneTab | None":
        pt = self._tabs.get((pane, tab))
        return None if pt is not None and pt.dead else pt

    async def hide_others(self, pane: str, tab: str) -> None:
        """One live tab per pane: the pane has one canvas to paint on."""
        for pt in list(self._tabs.values()):
            if pt.pane == pane and pt.tab != tab and pt.live:
                await pt.hide()

    async def close_tab(self, pane: str, tab: str) -> None:
        pt = self._tabs.get((pane, tab))
        if pt is None:
            return
        self._forget(pt)
        await pt.close()
        self._arm_idle()

    def _forget(self, pt: PaneTab) -> None:
        self._tabs.pop((pt.pane, pt.tab), None)
        self._by_target.pop(pt.target_id, None)

    def _on_target_info(self, params: dict) -> None:
        info = params.get("targetInfo") or {}
        pt = self._by_target.get(info.get("targetId"))
        if pt is not None and not pt.dead:
            pt.on_info(info)

    def _on_target_destroyed(self, params: dict) -> None:
        pt = self._by_target.get(params.get("targetId"))
        if pt is None:
            return
        self._forget(pt)
        pt._teardown()
        self.to_pane(pt.pane, {"type": "gone", "tab": pt.tab})
        self._arm_idle()

    def _on_target_crashed(self, params: dict) -> None:
        pt = self._by_target.get(params.get("targetId"))
        if pt is None:
            return
        self._forget(pt)
        pt._teardown()
        self.to_pane(pt.pane, {"type": "error", "tab": pt.tab, "code": "crashed",
                               "message": "the page crashed"})
        self._arm_idle()

    # -- for the status route ---------------------------------------------
    def snapshot(self) -> dict:
        running = self.browser is not None and not self.browser.closed.done()
        return {
            "running": running,
            "pid": self.browser.pid if running else None,
            "version": self.found.version if self.found else "",
            "tabs": [pt.info() for pt in self._tabs.values()],
            "launch_error": self.launch_error,
            # Filled in by the memory watchdog; the cap is already knowable.
            "memMb": None,
            "capMb": self.config.get("memory_mb"),
        }


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


PROBE_TIMEOUT_S = 15.0
PROBE_POLL_S = 0.05


def _one_line(e: BaseException) -> str:
    """An exception as one line, because these are printed one to a line."""
    return " ".join(str(e).split()) or e.__class__.__name__


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _drop_profile(path: Path) -> None:
    """Remove the probe's throwaway profile, and never fail over it.

    A browser that had to be killed goes on writing its profile for a moment,
    and a directory that grows while it is being removed is how this used to
    raise `Directory not empty` out of `--check`. One retry, then it stays and
    is mentioned; it is a few files in the temp directory either way.
    """
    for delay in (0.0, 1.0):
        if delay:
            time.sleep(delay)
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
    log(f"left the probe's temp profile behind: {path}")


def _wait_gone(proc: subprocess.Popen, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            return True
        time.sleep(PROBE_POLL_S)
    return proc.poll() is not None


def _stop_probe(proc: subprocess.Popen, write_fd: int) -> None:
    """Ask the browser to close, then insist, the way `Browser.close` does."""
    try:
        os.write(write_fd,
                 json.dumps({"id": 2, "method": "Browser.close"}).encode() + b"\0")
    except OSError:
        pass
    for sig in (None, signal.SIGTERM, signal.SIGKILL):
        if sig is not None:
            try:
                # start_new_session put it in its own group, so this reaches the
                # zygote and the renderers too.
                os.killpg(os.getpgid(proc.pid), sig)
            except OSError:
                pass
        if _wait_gone(proc, 3.0):
            return


def _ask_version(write_fd: int, read_fd: int,
                 proc: subprocess.Popen) -> "tuple[bool, str]":
    """Send `Browser.getVersion` down the pipe and wait for its reply."""
    try:
        os.write(write_fd,
                 json.dumps({"id": 1, "method": "Browser.getVersion"}).encode() + b"\0")
    except OSError as e:
        return False, f"could not speak to it: {_one_line(e)}"
    os.set_blocking(read_fd, False)
    buf = b""
    end = time.monotonic() + PROBE_TIMEOUT_S
    while time.monotonic() < end:
        if proc.poll() is not None:
            return False, f"it exited {proc.returncode} without answering"
        try:
            chunk = os.read(read_fd, 1 << 16)
        except BlockingIOError:
            time.sleep(PROBE_POLL_S)
            continue
        except OSError as e:
            return False, f"the protocol pipe broke: {_one_line(e)}"
        if not chunk:
            return False, "the protocol pipe closed without an answer"
        buf += chunk
        while b"\0" in buf:
            raw, buf = buf.split(b"\0", 1)
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if msg.get("id") != 1:
                continue
            if "error" in msg:
                return False, f"it refused the protocol: {msg['error']}"
            return True, (msg.get("result") or {}).get("product") or ""
    return False, f"it did not answer the protocol within {PROBE_TIMEOUT_S:.0f}s"


def _probe_launch(path: str, version: str) -> "tuple[bool, str]":
    """Start `path` the way the pane does, and ask it who it is.

    `--dump-dom about:blank` was this probe once, and it is not a question every
    browser answers: a downloaded Chrome for Testing is not an official build,
    so it applies the field trial testing config that official Chrome ignores,
    and under that config the one-shot dump never prints and never exits — while
    the same binary launches, navigates and evaluates over the debugging pipe in
    under a second. The pipe is the only way this program ever drives a browser,
    so the pipe is what gets probed: the flags `launch_flags` produces, the two
    fds Chrome expects them on, and one `Browser.getVersion`.

    A throwaway profile, not the pane's: the pane's may be in use by a browser
    that is running right now. No systemd scope either — whether a scope can be
    had is `systemd_scope_available`'s question, not this binary's fault.
    """
    found = Found(path=path, version=version, major=int(version.split(".")[0]),
                  source="check")
    profile = Path(tempfile.mkdtemp(prefix="pockettui-check-"))
    errf = tempfile.NamedTemporaryFile(prefix="pockettui-check-", suffix=".log",
                                       delete=False)
    err_path = Path(errf.name)
    cmd = [path] + launch_flags(found, {}, str(profile), DEFAULT_WIDTH,
                                DEFAULT_HEIGHT, user_agent_for(version))
    r_in, w_in = os.pipe()    # the child reads this on fd 3
    r_out, w_out = os.pipe()  # the child writes this on fd 4
    proc = None
    try:
        try:
            proc = subprocess.Popen(
                cmd, preexec_fn=_pipe_preexec(r_in, w_out), pass_fds=(3, 4),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errf,
                close_fds=True, start_new_session=True, env=launch_env())
        finally:
            errf.close()
            os.close(r_in)
            os.close(w_out)
        ok, detail = _ask_version(w_in, r_out, proc)
    finally:
        if proc is not None:
            _stop_probe(proc, w_in)
        for fd in (w_in, r_out):
            try:
                os.close(fd)
            except OSError:
                pass
        said = ""
        try:
            said = err_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
        _unlink(err_path)
        _drop_profile(profile)
    if ok:
        return True, detail
    return False, (detail + "\n" + said).strip()


def _check(path: str) -> int:
    if not _executable(path):
        print(f"not executable: {path}", file=sys.stderr)
        return 1
    missing = _missing_libs(path)
    if missing:
        print("missing: " + " ".join(missing))
        print(_package_line())
    probed = _probe_version(path)
    if not probed:
        print(f"it does not answer --version as a Chromium {MIN_MAJOR} or newer: "
              f"{path}", file=sys.stderr)
        return 1
    # Nothing below is allowed to raise: `--check` is a verdict, and both the
    # installer and `--install` read it as one.
    try:
        ok, detail = _probe_launch(path, probed[0])
    except OSError as e:
        print(f"could not start it: {_one_line(e)}", file=sys.stderr)
        return 1
    if not ok:
        for line in detail.splitlines()[-6:] or ["it did not start"]:
            print(line, file=sys.stderr)
        if any(m in detail for m in SANDBOX_MARKERS):
            print(SANDBOX_HINT, file=sys.stderr)
        return 1
    print(f"ok {probed[0]}")
    return 0


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
# A machine with no Chrome, Chromium or Edge gets one of Google's own Chrome for
# Testing builds, unpacked under `~/.pockettui/chromium/<version>/` where
# `find_chromium` looks last. The full `chrome` build is taken and not
# `chrome-headless-shell`: the shell is a stripped browser that cannot carry a
# sign-in, and a persistent profile that can be signed in to is a goal of full
# browser mode.
#
# Nothing here trusts the network to behave. The zip is ~190 MB, so it is
# streamed to a `.part` file and resumed with a Range request when an earlier
# attempt left one behind; the finished file is checked against Content-Length
# (Google publishes no checksum for these) and unzipped only after `unzip -t`
# agrees it is whole; and the unpacked binary has to pass the same probe
# `--check` runs before `current` names it. A failure leaves the version
# directory and the `.part` alone, so the next run has little or nothing left to
# fetch — `current` is the one thing written last, because it is what the rest
# of the module reads.

# The install path is the only thing in this module that fetches a URL or opens
# a zip, so its imports live with it rather than at the top.
import http.client
import platform
import stat
import urllib.error
import urllib.request
import zipfile

CFT_STABLE_URL = ("https://googlechromelabs.github.io/chrome-for-testing/"
                  "last-known-good-versions-with-downloads.json")
CFT_VERSIONS_URL = ("https://googlechromelabs.github.io/chrome-for-testing/"
                    "known-good-versions-with-downloads.json")

INSTALL_TIMEOUT_S = 15.0
JSON_ATTEMPTS = 3
DOWNLOAD_ATTEMPTS = 4  # the first try and three retries
INSTALL_BACKOFF_S = 1.0
PROGRESS_EVERY = 5 << 20
DOWNLOAD_CHUNK = 1 << 16

# zipfile stores the unix mode but does not apply it, so everything comes out
# 0644 and a browser that cannot be executed is not a browser. The mode in the
# member is honoured below; these are the names that get the executable bit
# whether the zip remembered it or not.
EXEC_NAMES = ("chrome", "chrome_crashpad_handler", "chrome_sandbox",
              "Google Chrome for Testing")
EXEC_SUFFIX_RE = re.compile(r"\.(so|dylib)(\.\d+)*$")


class InstallError(Exception):
    """Something the user should read as one line, not as a traceback."""


class _Retry(Exception):
    """This attempt failed in a way another attempt might survive."""


def _download_platform() -> str:
    """The Chrome for Testing platform name for this machine.

    Windows is not among them on purpose: nothing else in PocketTUI runs there.
    """
    mach = (platform.machine() or "").lower()
    if sys.platform.startswith("linux"):
        if mach in ("x86_64", "amd64"):
            return "linux64"
        if mach in ("aarch64", "arm64"):
            return "linux-arm64"
    elif sys.platform == "darwin":
        if mach in ("arm64", "aarch64"):
            return "mac-arm64"
        if mach in ("x86_64", "amd64"):
            return "mac-x64"
    raise InstallError(
        f"there is no Chrome for Testing build for {sys.platform} "
        f"{mach or 'unknown'}; install Chrome or Chromium with this system's "
        "package manager instead")


def _fetch_json(url: str) -> dict:
    last = ""
    for attempt in range(1, JSON_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=INSTALL_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except (OSError, http.client.HTTPException, ValueError) as e:
            last = _one_line(e)
            if attempt < JSON_ATTEMPTS:
                time.sleep(INSTALL_BACKOFF_S * attempt)
    raise InstallError(f"could not read the browser version list: {last}")


def _pick_download(data: dict, plat: str, version: "str | None") -> "tuple[str, str]":
    """The version and zip URL to fetch, from either version-list endpoint."""
    if version:
        entry = next((v for v in data.get("versions") or []
                      if v.get("version") == version), None)
        if not entry:
            raise InstallError(f"no Chrome for Testing {version} in the version list")
    else:
        entry = (data.get("channels") or {}).get("Stable")
        if not entry:
            raise InstallError("the version list has no Stable channel")
    ver = str(entry.get("version") or "")
    # The version becomes a directory name, so it is checked rather than trusted.
    if not re.fullmatch(r"\d+(\.\d+)*", ver):
        raise InstallError(f"the version list gave an odd version: {ver!r}")
    builds = (entry.get("downloads") or {}).get("chrome") or []
    url = next((str(b.get("url") or "") for b in builds
                if b.get("platform") == plat), "")
    if not url:
        raise InstallError(f"Chrome for Testing {ver} has no {plat} build")
    # Google serves the list and the zips over https; a plain-http entry could
    # only come from a mirror, and anything else is not a URL at all.
    if not url.startswith(("https://", "http://")):
        raise InstallError(f"the version list gave an odd url: {url!r}")
    return ver, url


def _head_size(url: str) -> "int | None":
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=INSTALL_TIMEOUT_S) as resp:
            return int(resp.headers.get("Content-Length") or 0) or None
    except (OSError, http.client.HTTPException, ValueError):
        return None


def _total_size(resp, status: int, have: int) -> "int | None":
    """The size of the whole file, whichever way this response describes it."""
    if status == 206:
        m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range") or "")
        if m:
            return int(m.group(1))
        length = int(resp.headers.get("Content-Length") or 0)
        return have + length if length else None
    return int(resp.headers.get("Content-Length") or 0) or None


def _fetch_into(url: str, part: Path) -> "int | None":
    """One attempt at filling `part`, resuming it when the server agrees to."""
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", f"bytes={have}-")
    try:
        resp = urllib.request.urlopen(req, timeout=INSTALL_TIMEOUT_S)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:
            # The part is at or past the end of what the server has: it is not a
            # prefix of this file at all, so it goes and the next attempt starts over.
            _unlink(part)
            raise _Retry("the server refused to resume the download")
        raise _Retry(f"the server answered {e.code}")
    except (OSError, http.client.HTTPException) as e:
        raise _Retry(_one_line(e))

    with resp:
        status = getattr(resp, "status", None) or resp.getcode()
        total = _total_size(resp, status, have)
        if have and status != 206:
            have = 0  # the range was ignored, so this is the whole file again
        mode = "ab" if have else "wb"
        with open(part, mode) as fh:
            done = have
            mark = done + PROGRESS_EVERY
            while True:
                try:
                    chunk = resp.read(DOWNLOAD_CHUNK)
                except http.client.IncompleteRead as e:
                    fh.write(e.partial)
                    raise _Retry("the connection dropped mid-download")
                except (OSError, http.client.HTTPException) as e:
                    raise _Retry(_one_line(e))
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if done >= mark:
                    _progress(done, total)
                    mark = done + PROGRESS_EVERY
    return total


def _progress(done: int, total: "int | None") -> None:
    """One plain line, no carriage returns: this ends up in the install log."""
    if total:
        print(f"  {done / (1 << 20):.1f} MiB of {total / (1 << 20):.1f} MiB "
              f"({done * 100 // total}%)", flush=True)
    else:
        print(f"  {done / (1 << 20):.1f} MiB", flush=True)


def _download(url: str, part: Path) -> None:
    last = ""
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            total = _fetch_into(url, part)
            have = part.stat().st_size
            if total is None:
                raise _Retry("the server never said how big the download is")
            if have == total:
                return
            # No checksum is published, so the byte count is the only thing
            # there is to hold the download to.
            raise _Retry(f"got {have} of {total} bytes")
        except _Retry as e:
            last = str(e)
            if attempt < DOWNLOAD_ATTEMPTS:
                print(f"  {last}, retrying", flush=True)
                time.sleep(INSTALL_BACKOFF_S * 2 ** (attempt - 1))
    raise InstallError(f"the download did not finish: {last}")


def _verify_zip(path: Path) -> None:
    unzip = shutil.which("unzip")
    if unzip:
        try:
            out = subprocess.run([unzip, "-t", str(path)], capture_output=True,
                                 text=True, timeout=600)
        except (OSError, subprocess.SubprocessError) as e:
            raise InstallError(f"could not test the download: {_one_line(e)}")
        if out.returncode != 0:
            tail = ((out.stdout or "") + (out.stderr or "")).strip().splitlines()
            raise InstallError("the download is damaged: "
                               + (tail[-1] if tail else "unzip -t said no"))
        return
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
    except (OSError, zipfile.BadZipFile) as e:
        raise InstallError(f"the download is not a readable zip: {_one_line(e)}")
    if bad:
        raise InstallError(f"the download is damaged at {bad}")


def _member_path(dest: Path, name: str) -> Path:
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if name.startswith("/") or ".." in parts or not parts:
        raise InstallError(f"the download holds an unsafe path: {name!r}")
    return dest.joinpath(*parts)


def _wants_exec(name: str) -> bool:
    base = name.rstrip("/").rsplit("/", 1)[-1]
    return base in EXEC_NAMES or bool(EXEC_SUFFIX_RE.search(base))


def _extract(zip_path: Path, dest: Path) -> None:
    """Unpack the zip, keeping the modes and the symlinks zipfile would drop.

    The mac build is an .app bundle whose Frameworks directory is held together
    by symlinks, which `ZipFile.extract` would write out as small text files.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            out = _member_path(dest, info.filename)
            full = info.external_attr >> 16
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(full):
                target = z.read(info).decode("utf-8", "replace")
                if out.is_symlink() or out.exists():
                    out.unlink()
                os.symlink(target, out)
                continue
            with z.open(info) as src, open(out, "wb") as fh:
                shutil.copyfileobj(src, fh)
            if full & 0o111 or _wants_exec(info.filename):
                out.chmod(0o755)
            else:
                out.chmod(0o644)
    for root, dirs, _files in os.walk(dest):
        for d in [root] + [os.path.join(root, x) for x in dirs]:
            try:
                os.chmod(d, 0o755)
            except OSError:
                pass


def _installed_binary(vdir: Path) -> "str | None":
    for rel in DOWNLOAD_BINARIES:
        if "*" in rel:
            for p in sorted(vdir.glob(rel)):
                if _executable(str(p)):
                    return str(p)
        else:
            p = vdir / rel
            if _executable(str(p)):
                return str(p)
    return None


def _prune(root: Path, keep: str) -> None:
    """Leave the version just installed, `current`, and nothing else of ours."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for p in entries:
        try:
            if p.is_dir():
                if p.name != keep:
                    shutil.rmtree(p, ignore_errors=True)
            elif p.name.endswith(".zip") or p.name.endswith(".zip.part"):
                _unlink(p)
        except OSError:
            pass


def _install(version: "str | None" = None, dry_run: bool = False) -> int:
    try:
        return _install_run(version, dry_run)
    except InstallError as e:
        print(str(e), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"could not write the browser into {config_dir() / 'chromium'}: "
              f"{_one_line(e)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 1


def _install_run(version: "str | None", dry_run: bool) -> int:
    plat = _download_platform()
    root = config_dir() / "chromium"

    # A pinned version that is already here can be answered without the network.
    if version and not dry_run:
        have = _already(root, version)
        if have:
            _adopt(root, version, have)
            print(f"already installed {version}")
            return 0

    data = _fetch_json(CFT_VERSIONS_URL if version else CFT_STABLE_URL)
    ver, url = _pick_download(data, plat, version)

    if dry_run:
        size = _head_size(url)
        print(f"version {ver}")
        print(f"platform {plat}")
        print(f"url {url}")
        print(f"size {size} bytes ({size / (1 << 20):.1f} MiB)" if size
              else "size unknown")
        return 0

    have = _already(root, ver)
    if have:
        _adopt(root, ver, have)
        print(f"already installed {ver}")
        return 0

    vdir = root / ver
    part = root / f"{ver}.zip.part"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o755)
    except OSError:
        pass

    # A `.part` left by an earlier run is worth a HEAD: it may already be the
    # whole file, in which case there is nothing to download.
    whole = _head_size(url) if part.exists() else None
    if whole is not None and part.stat().st_size > whole:
        _unlink(part)
    if whole is None or part.stat().st_size != whole:
        print(f"downloading Chrome for Testing {ver} for {plat}", flush=True)
        _download(url, part)
    print("checking the download", flush=True)
    _verify_zip(part)

    if vdir.exists():
        shutil.rmtree(vdir, ignore_errors=True)
    print(f"unpacking into {vdir}", flush=True)
    _extract(part, vdir)
    if sys.platform == "darwin":
        try:
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(vdir)],
                           capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass

    binary = _installed_binary(vdir)
    if not binary:
        raise InstallError(f"the download unpacked without a browser in {vdir}")
    # The same probe `--check` runs: a browser that cannot start here is not
    # worth pointing `current` at, and its complaint is the useful output.
    if _check(binary) != 0:
        # The tree and the `.part` stay: nothing about them is known to be
        # wrong, and the next run has nothing left to download.
        print("the downloaded browser will not start here", file=sys.stderr)
        return 1

    _adopt(root, ver, binary)
    print(f"installed {ver} {binary}")
    return 0


def _already(root: Path, ver: str) -> "str | None":
    """The browser already unpacked for `ver`, if there is one and it starts."""
    binary = _installed_binary(root / ver)
    return binary if binary and _check(binary) == 0 else None


def _adopt(root: Path, ver: str, binary: str) -> None:
    """Name this binary in `current` and sweep away everything older.

    The two go together, and both go last: until `current` names it, nothing
    else in the module is looking at a half-finished install.
    """
    (root / "current").write_text(binary + "\n", encoding="utf-8")
    _prune(root, ver)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="Find and check the Chromium the browser pane streams from.")
    ap.add_argument("--find", action="store_true",
                    help="print the browser this machine would use")
    ap.add_argument("--check", metavar="BIN",
                    help="report what stops BIN from starting headless here")
    ap.add_argument("--install", action="store_true",
                    help="download a private Chrome for Testing under ~/.pockettui")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --install, print what it would download and stop")
    ap.add_argument("--version", metavar="VER",
                    help="with --install, pin the Chrome for Testing version")
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
        return _install(version=args.version, dry_run=args.dry_run)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
