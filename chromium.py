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
from collections import deque
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
    "jpeg_quality": 70,
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
        # An unbranded build — the Chrome for Testing this installer can
        # download — applies Chromium's field trial *testing* config, a set of
        # random experiments a branded Chrome never runs. It is what hung the
        # installer's own --dump-dom probe, and the pane has no more reason than
        # the probe did to render pages under experiments nobody shipped.
        "--disable-field-trial-config",
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

    def put_frame(self, header: dict, body: bytes) -> int:
        """Queue one frame; the count of stale ones it displaced.

        That count is the honest answer to "can this link carry what we are
        making": nothing is dropped while the writer keeps up, and a frame is
        dropped for no other reason.
        """
        kind = str(header.get("kind") or "")
        tab = header.get("tab")
        self._items.append((kind, tab, pack_frame(header, body)))
        dropped = 0
        if kind == "cast":
            stale = [i for i, (k, t, _) in enumerate(self._items)
                     if k == "cast" and t == tab]
            for i in reversed(stale[:-self._backlog] if self._backlog else stale):
                del self._items[i]
                dropped += 1
        self._ready.set()
        return dropped

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

    def put_frame(self, header: dict, body: bytes) -> int:
        return 0


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

# A capture is a full-size encode with the stream held for the whole of it —
# measured on a laptop at 1200x800 CSS: 112 ms for a JPEG at q75 at 2x, 305 ms
# for the WebP at q85 this used to take. So it is a JPEG, and there is never
# more than one of them inside this gap: a drag would otherwise cost one per
# step and every one of them is a third of a second with no live frames.
SETTLE_QUALITY = 75
SETTLE_MIN_GAP_S = 0.3

# And a page that is painting on its own needs no settled picture at all: this
# many frames inside this window, none of them provoked by the user, means the
# next screencast frame is the settled picture and a capture would only stop
# the stream to send the same thing twice.
PAINTING_FRAMES = 3
PAINTING_WINDOW_S = 0.3

# The input above is not the only way a page comes to rest, and it was the only
# one that ever produced a sharp frame: a page that loads and then sits there —
# a result list, an article, anything nobody has touched yet — was shown at the
# screencast's resolution, which on a 2x client is half the page's own. So
# quiet is a trigger of its own: this long after the last frame went out, with
# nothing waiting on an input's settle, one settled picture, and no further one
# until something happens again. Measured against Chrome 153 on a 1200x800 CSS
# page at 2x: the frames are 1200x800 and the picture is 2400x1600.
IDLE_STILL_S = 0.35

# Taking the picture makes frames of its own: the capture forces a surface
# commit and the screencast reports it, measured at the moment the still goes
# out and again about 90 ms later. They are frames of the picture that was just
# taken, so they are sent (they cost nothing extra — the browser made them
# either way) and they do not start the clock above: winding it on the capture's
# own echo is a page at rest photographing itself every half second for as long
# as it is left alone, which is what a first cut of this did, 10 pictures and
# 3.5 Mbit/s for a page nobody had touched.
#
# And they are not sent either. Measured against Chrome 153 at 1200x800 CSS and
# 2x: the echo about 90 ms after the capture is byte for byte the last
# screencast frame before it, at the CSS size, and the client paints every
# frame newer than the one on screen, so sending it put the soft picture back
# over the sharp one a tenth of a second after the sharp one arrived. A frame
# inside this window that is the same bytes as the last one Chrome sent before
# the capture is that echo and is dropped; any other frame is the page changing
# and goes out, and starts the clock again.
STILL_ECHO_S = 0.5

# And for a tab whose page never fires a load event — an adopted target, a page
# that was already there when the pane found it — the clock is started again
# this long after it was shown, in case the quiet above began before the stream
# did.
SHOW_STILL_S = 1.5

# A native popup — a <select>'s menu, a date picker — is drawn by the browser
# outside the page's surface, so it is in a screenshot and not in a screencast
# frame. While one is open the tab is painted from screenshots instead.
POPUP_FPS = 8
POPUP_MAX_S = 15.0

# Chrome sends no further screencast frame until the last one is acknowledged,
# so the ack is what paces the stream — and it is answered on this backend's
# own clock, never on the phone's. Waiting for the client's ack put the whole
# round trip between one frame and the next: measured against a page painting
# flat out, 60 fps on loopback, 17 fps with 150 ms in the link and 7 fps with
# 400 ms. The client's `ack` still arrives and is still read, for how long
# delivery is taking, which is what the level below is chosen on — but no
# frame waits for it.
#
# The clock is the level's frame rate: a frame that comes too soon is held and
# acknowledged when it goes, so Chrome makes about as many frames as are
# wanted instead of sixty a second that are dropped. This is the longest the
# browser is ever left unacknowledged.
PACE_MAX_S = 0.1

# How long `Page.startScreencast` is given to answer before the tab is taken
# for one with no page committed yet (see `_start_cast`).
CAST_START_S = 2.0

# What the stream runs at, best first: (JPEG quality, frames per second).
#
# A rung buys bytes with the quality of a moving picture and with its rate, and
# never with its sharpness. Measured against Chrome 153: a tab at 1200x800 CSS
# and a device scale factor of 2 renders at 2400x1600 and its screencast
# frames still come back 1200x800, because the screencast is the headless
# window's own surface. So the casts are the CSS size at every rung, and the
# settled picture (a screenshot, taken at the client's own ratio) is what makes
# a page at rest sharp. Rungs used to cap the ratio and shrink the viewport as
# well; a Retina client left at the bottom rung by a quiet link then got 1x
# stills and 840x560 casts stretched over 2400x1600, which is a page that looks
# blurred on a link with nothing wrong with it.
#
# The top rung's quality is 70 rather than the 60 it was: measured on the same
# page, a frame is 74.6 KB at q60 and 85.9 KB at q70, and 15% more bytes on the
# rung that is only chosen while the link has room is worth the edges it keeps.
STREAM_LEVELS = (
    (70, 24),
    (55, 24),
    (45, 16),
    (40, 10),
)

# Delivery is the milliseconds between a cast joining the send queue and the
# client saying it drew it. What the level can do something about is not that
# number but the part of it above the link's own floor: the quickest delivery
# lately is one frame's distance and nothing else, and everything over it is
# the bytes divided by what the link carries. A tailnet with 150 ms of
# distance in it is not a reason to make the picture worse — no frame waits
# for the ack any more — and a link that takes 250 ms *longer* than its floor
# to move one frame is.
#
# Only casts are measured. A settled picture or a popup frame is several times
# a cast's bytes by design, and the client's ack of a frame comes after it is
# decoded and painted, so the cast behind one of them — and the cast it
# displaced — reports the picture's cost, not the link's. A frame the client
# threw away unpainted (`dropped`) says nothing about the link either.
OVER_DOWN_MS = 250.0
DELIVER_ALPHA = 0.3

# The other half of the same question, and the half a link with a ceiling on
# it answers: what share of the frames made in the last second never left,
# because the writer was still sending the one before and SendQueue dropped
# the stale one. Measured against a link carrying 1 Mbit/s, every frame is
# delivered in the same time as every other — its own transfer — so nothing
# above tells the level anything, while nine frames in ten are being thrown
# away. Judged only over a second that made at least `DROP_MIN_FRAMES`, so one
# frame of two is not half the stream refused.
DROP_DOWN = 0.25
DROP_MIN_FRAMES = 4

# The level is judged once every `LEVEL_TICK_S` while the tab is live, whether
# or not frames are flowing: a page at rest sends nothing, and a judgement
# that only ran on frames never ran again once the page went quiet, leaving it
# at whatever rung the last burst had pushed it to. Either signal over its
# threshold on two evaluations in a row is a step down, at most one rung per
# `LEVEL_MIN_GAP_S`; `LEVEL_HOLD_S` with no evaluation asking for one is a step
# back up. A quiet link is not a bad one.
LEVEL_TICK_S = 1.0
LEVEL_DOWN_EVALS = 2
LEVEL_MIN_GAP_S = 2.0
LEVEL_HOLD_S = 5.0

# A tab starts again at the top of the ladder when what it was measured on is
# gone: a navigation (a new page is not the old page's cost), the pane's
# socket coming back, or being shown after this long in the background.
LEVEL_RESET_HIDDEN_S = 30.0

# How long after the last frame the measured rate stops being reported.
RATE_STALE_S = 2.0

# Acks name a frame and everything before it, and frames are dropped on the way
# out (SendQueue) and on the way in (the pace), so the record of what is in
# flight is trimmed by age rather than emptied by acks.
PENDING_MAX = 64

# The level each pane was last streaming at, and when: pane -> (level, time).
# A new tab starts where the pane left off rather than at the top of the ladder
# with a step down to rediscover, which is what made every new tab on a slow
# link stutter for its first seconds — but only for `LEVEL_MEMORY_S`, after
# which what the link was doing then is no evidence of what it is doing now.
_LEVEL_MEMORY: dict = {}
LEVEL_MEMORY_S = 60.0


def _remembered_level(pane: str) -> int:
    level, at = _LEVEL_MEMORY.get(pane, (0, 0.0))
    if time.monotonic() - at > LEVEL_MEMORY_S:
        return 0
    return max(0, min(len(STREAM_LEVELS) - 1, int(level)))

SELECTION_MAX = 1 << 20

# A modal dialog and an HTTP auth prompt both stop the page dead until someone
# answers, and the someone is a phone that may have gone into a pocket. The
# page is let go after this long rather than left waiting for a tab nobody is
# looking at any more.
DIALOG_TIMEOUT_S = 120.0
AUTH_TIMEOUT_S = 120.0
DIALOG_MESSAGE_MAX = 4096

# Request interception pauses *every* request the page makes, so it is turned
# on only for a page that answered 401 and turned off again this long after the
# credentials went in: by then Chrome has cached them for the realm and the
# pause buys nothing.
FETCH_LINGER_S = 60.0

# The favicon travels as a data URL inside a `tab` message, so it is small or
# it is not sent at all.
FAVICON_MAX = 32 << 10
FAVICON_TIMEOUT_S = 5.0

# Download progress arrives per chunk read; this is how often any of it
# reaches the pane.
DOWNLOAD_EVERY_S = 0.25

# Files the pane staged for a page's file input are swept at the next launch:
# they were picked for one upload, and that directory is not a place to keep
# anything.
UPLOAD_MAX_AGE_S = 86400.0


# Injected into every document the tab loads, before the page's own scripts.
# It answers three questions the protocol cannot: whether a native popup is
# open (see POPUP_FPS above), what the page thinks is selected, which is what
# the pane's copy button copies, and what the title is — `Target.targetInfoChanged`
# does not fire for a title a script sets, so the page has to say so itself.
# All three go out through Runtime bindings, which are real functions in the
# page rather than anything the page could be confused by; nothing else is
# added to the page's globals.
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

  let ttimer = 0, tlast = null, watched = null;
  const title = () => {
    ttimer = 0;
    const now = document.title || '';
    if (now === tlast) return;
    tlast = now;
    try { ptuiTitle(now); } catch (e) {}
  };
  const bump = () => { if (!ttimer) ttimer = setTimeout(title, 100); };
  const watch = () => {
    const head = document.head;
    if (!head || head === watched) return;
    watched = head;
    try {
      new MutationObserver(bump).observe(head,
        { childList: true, subtree: true, characterData: true });
    } catch (e) {}
  };
  watch();
  addEventListener('DOMContentLoaded', () => { watch(); bump(); }, true);
  addEventListener('load', () => { watch(); bump(); }, true);
})();
""" % {"selmax": SELECTION_MAX}


# ---------------------------------------------------------------------------
# Input: what the pane's pointer and keyboard come to
# ---------------------------------------------------------------------------

# The protocol's own modifier bitmask, which is also the one the client sends:
# there is no translation worth doing between a KeyboardEvent's four booleans
# and four bits, and inventing a second spelling of them would only be one more
# place for the two ends to disagree.
MOD_ALT = 1
MOD_CTRL = 2
MOD_META = 4
MOD_SHIFT = 8

# What a client's mouse `kind` is called in the protocol.
MOUSE_TYPES = {"move": "mouseMoved", "down": "mousePressed",
               "up": "mouseReleased", "wheel": "mouseWheel"}

# The buttons the protocol names. Anything else a client reports is no button,
# which is what a move carries.
MOUSE_BUTTONS = ("none", "left", "middle", "right", "back", "forward")

# The virtual key codes the page needs and a browser does not always send.
# `KeyboardEvent.keyCode` is deprecated and some clients report 0 for it, but a
# page's own handlers still read it — jQuery's `which`, every "was that Enter?"
# test written before 2017 — and Chrome's editing commands are driven off it
# too: a rawKeyDown with no windowsVirtualKeyCode moves no caret and deletes no
# character, whatever `key` says. So these are filled in here rather than
# trusted to the client, which is also what keeps two clients from disagreeing.
KEY_CODES = {
    "Backspace": 8, "Tab": 9, "Enter": 13, "Escape": 27, "Space": 32,
    "PageUp": 33, "PageDown": 34, "End": 35, "Home": 36,
    "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40,
    "Insert": 45, "Delete": 46,
    "F1": 112, "F2": 113, "F3": 114, "F4": 115, "F5": 116, "F6": 117,
    "F7": 118, "F8": 119, "F9": 120, "F10": 121, "F11": 122, "F12": 123,
}

# The most a `[title]` is worth showing in a tooltip, and the most a paste is
# worth inserting in one call.
TITLE_MAX = 200
INSERT_MAX = 1 << 20


def host_is_mac(host_platform: str = "") -> bool:
    return (host_platform or sys.platform).startswith("darwin")


def remap_mods(mods: int, host_platform: str = "") -> int:
    """Meta from a Mac client, as the host's own browser would read it.

    The page runs on this computer, so its chords are this computer's: a
    Chromium on Linux copies on Ctrl+C and opens a link in a new tab on
    Ctrl+click, and a phone or a laptop sending Cmd means exactly those. Left
    as Meta it would reach the page as the Super key, which is a modifier no
    page binds anything to — the selection would not copy and the link would
    open in the tab the user was reading. On a macOS host Meta is already the
    key the page means, and moving it would break the same chords the other way
    round.
    """
    mods = int(mods) & 0xF
    if not mods & MOD_META or host_is_mac(host_platform):
        return mods
    return (mods & ~MOD_META) | MOD_CTRL


def viewport_point(msg: dict, zoom: float) -> "tuple[float, float]":
    """A canvas coordinate as a CSS pixel of the overridden viewport.

    The canvas is the page's surface at the pane's own size; the viewport
    behind it was made `zoom` times smaller so everything on it comes back
    bigger (see PaneTab._metrics). So the page's own coordinate for a point on
    the canvas is that point divided by the zoom, and every hit test, click and
    scroll below goes through here rather than doing the division itself.
    """
    z = float(zoom) or 1.0

    def num(key: str) -> float:
        try:
            val = float(msg.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        return 0.0 if val != val else val

    return (num("x") / z, num("y") / z)


def mouse_event_params(msg: dict, zoom: float,
                       host_platform: str = "") -> dict:
    """One client mouse message as Input.dispatchMouseEvent parameters."""
    kind = str(msg.get("kind") or "move")
    if kind not in MOUSE_TYPES:
        raise ValueError(f"{kind!r} is not a mouse event")
    z = float(zoom) or 1.0
    x, y = viewport_point(msg, z)
    button = str(msg.get("button") or "none")
    if button not in MOUSE_BUTTONS:
        button = "none"
    try:
        buttons = int(msg.get("buttons") or 0)
    except (TypeError, ValueError):
        buttons = 0
    try:
        clicks = int(msg.get("clicks") or 0)
    except (TypeError, ValueError):
        clicks = 0
    params = {
        "type": MOUSE_TYPES[kind],
        "x": x,
        "y": y,
        "button": button,
        "buttons": max(0, min(buttons, 31)),
        "clickCount": max(0, min(clicks, 3)),
        "modifiers": remap_mods(msg.get("mods") or 0, host_platform),
    }
    if kind == "wheel":
        # In the viewport's own pixels like the coordinates: a zoomed page is
        # rendered from a smaller viewport, and a wheel notch that was not
        # divided too would scroll it further the more it was zoomed in.
        dx, dy = viewport_point({"x": msg.get("dx"), "y": msg.get("dy")}, z)
        params["deltaX"] = dx
        params["deltaY"] = dy
    return params


def key_event_params(msg: dict, host_platform: str = "") -> dict:
    """One client key message as Input.dispatchKeyEvent parameters.

    The distinction that matters is `keyDown` against `rawKeyDown`: a keyDown
    carries text and is what types a character, and a rawKeyDown carries none
    and is what the page's own handlers see for a chord or an arrow. Sending
    keyDown for everything types the word "ArrowLeft" into a form; sending
    rawKeyDown for everything types nothing at all.
    """
    up = str(msg.get("kind") or "down") == "up"
    key = str(msg.get("key") or "")
    text = str(msg.get("text") or "")
    # Enter's character is a carriage return, not a newline: that is what a
    # browser puts in a KeyboardEvent's text, and a textarea fed "\n" instead
    # gets a line break with no key press behind it.
    if key == "Enter":
        text = "\r"
    try:
        sent_code = int(msg.get("keyCode") or 0)
    except (TypeError, ValueError):
        sent_code = 0
    code_num = KEY_CODES.get(key, sent_code)
    try:
        location = int(msg.get("location") or 0)
    except (TypeError, ValueError):
        location = 0
    params = {
        "type": "keyUp" if up else ("keyDown" if text else "rawKeyDown"),
        "key": key,
        "code": str(msg.get("code") or ""),
        "windowsVirtualKeyCode": code_num,
        "nativeVirtualKeyCode": code_num,
        "modifiers": remap_mods(msg.get("mods") or 0, host_platform),
        "autoRepeat": bool(msg.get("repeat")),
        "location": location,
        "isKeypad": location == 3,
    }
    if text and not up:
        params["text"] = text
        # What the key would have produced with no modifier on it, which is
        # what Chrome matches its editing commands against. The client sends no
        # text at all for a chord, so where there is text there was no Ctrl or
        # Meta and the two are the same string.
        params["unmodifiedText"] = text
    return params


# The three page probes the pane's pointer needs answered. Function expressions
# rather than statements so one Runtime.evaluate can call each with the point
# (probe_call below), and free of `%` on purpose: FULL_PAGE_HELPER above is
# %-formatted, and a probe that grew a percent sign would either break that
# formatting or be quietly rewritten by it.

# What a middle click and a Ctrl+click need to know, and nothing else: the
# address the anchor under the pointer points at.
LINK_PROBE_JS = """(function (x, y) {
  var el = document.elementFromPoint(x, y);
  var a = el && el.closest ? el.closest('a[href]') : null;
  return a ? a.href : '';
})"""

# What the context menu needs: whether there is a link to open, and whether
# there is somewhere for a paste to land. The selection is not asked for here —
# the page reports that of its own accord (FULL_PAGE_HELPER) and the tab
# already holds it.
HIT_PROBE_JS = """(function (x, y) {
  var el = document.elementFromPoint(x, y);
  var a = el && el.closest ? el.closest('a[href]') : null;
  var editable = false;
  for (var n = el; n; n = n.parentElement) {
    var tag = n.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      editable = !n.disabled && !n.readOnly;
      break;
    }
    if (n.isContentEditable) { editable = true; break; }
  }
  return { href: a ? a.href : '', editable: editable };
})"""

# The pointer's own shape, which is the whole of what makes a streamed page feel
# like a page rather than a picture of one: a hand over a link, a bar over text,
# a resize arrow over a splitter. Read the way the browser itself decides it —
# the computed value, and where that is `auto`, whether the point is on text.
HIT_CURSOR_JS = """(function (x, y) {
  var el = document.elementFromPoint(x, y);
  if (!el) return { cursor: 'default', title: '' };
  var title = '';
  for (var n = el; n; n = n.parentElement) {
    if (n.hasAttribute && n.hasAttribute('title')) {
      var t = n.getAttribute('title') || '';
      if (t) { title = t.slice(0, 200); break; }
    }
  }
  var css = '';
  try { css = window.getComputedStyle(el).cursor || ''; } catch (e) { css = ''; }
  var tag = el.tagName;
  var cursor = 'default';
  if (el.closest && el.closest('a[href]')) cursor = 'pointer';
  else if (css && css !== 'auto') cursor = css;
  else if (tag === 'INPUT' || tag === 'TEXTAREA') cursor = 'text';
  else {
    var range = document.caretRangeFromPoint
      ? document.caretRangeFromPoint(x, y) : null;
    var node = range ? range.startContainer : null;
    if (node && node.nodeType === 3) cursor = 'text';
  }
  return { cursor: cursor, title: title };
})"""


def probe_call(js: str, *args) -> str:
    """One of the probes above, as an expression that calls it on a point."""
    return "(" + js + ")(" + ",".join(json.dumps(a) for a in args) + ")"


# The tab's icon, fetched by the page rather than by this process, because the
# icon of a page behind a login is behind that login too: the page's own fetch
# carries its cookies and its origin, and this process has neither. It comes
# back as a data URL or as nothing at all — an icon is decoration, and no
# failure here is worth a word to anybody. `%`-free, like the probes above:
# FULL_PAGE_HELPER is %-formatted and these strings live beside it.
FAVICON_JS = """(async () => {
  var cap = %(cap)d;
  try {
    var href = '';
    var links = document.querySelectorAll('link[rel~="icon"]');
    for (var i = 0; i < links.length; i++) {
      if (links[i].href) { href = links[i].href; break; }
    }
    if (!href) href = new URL('/favicon.ico', location.href).href;
    var res = await fetch(href, { credentials: 'include' });
    if (!res.ok) return '';
    var blob = await res.blob();
    if (!blob.size || blob.size > cap) return '';
    var type = blob.type || 'image/x-icon';
    if (type.indexOf('image/') !== 0) return '';
    var bytes = new Uint8Array(await blob.arrayBuffer());
    var s = '';
    for (var j = 0; j < bytes.length; j++) s += String.fromCharCode(bytes[j]);
    var out = 'data:' + type + ';base64,' + btoa(s);
    return out.length > cap ? '' : out;
  } catch (e) { return ''; }
})()""" % {"cap": FAVICON_MAX}


def _host_of(url: str) -> str:
    """The `scheme://host` of a URL, by hand: only equality is ever asked."""
    head, slashes, rest = url.partition("//")
    if not slashes:
        return ""
    return (head + "//" + rest.split("/", 1)[0]).lower()


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
        # A discarded tab is one the memory watchdog gave the target up for.
        # The record stays — the URL and the title are what the pane shows, and
        # what `show()` loads the page again from.
        self.discarded = False
        # When this tab was last the pane's live one, which is how the watchdog
        # decides whose turn it is: a tab that was never shown sorts oldest.
        self.shown_at = 0.0
        self.url = ""
        self.title = ""
        self.favicon = ""
        self.loading = False
        self.frame_id = ""
        self.selection = ""
        # Told whenever the page's selection changes, so the pane's copy key has
        # the text before it is pressed rather than a round trip after. Set by
        # whoever owns the tab (FullBrowser.open) — the page's binding handler
        # below is in no position to know where the news should go.
        self.on_selection = None
        # The last (cursor, title) this tab's pointer probe answered with. A
        # move over a paragraph asks twenty times a second and the answer is the
        # same every time; only a change is worth a message.
        self.cursor_sent: "tuple[str, str] | None" = None
        # When something was last sent to this page. `_input_at` cannot answer
        # it: the settle timer clears that one, and this is asked long after —
        # it is how a download with no tab of its own finds the tab that most
        # likely started it.
        self.last_input = 0.0

        # One dialog and one auth challenge at a time, because that is how many
        # the page can have: both stop it until they are answered.
        self._dialog: "str | None" = None          # the kind, while one is up
        self._dialog_task: "asyncio.Task | None" = None
        self._auth: "str | None" = None            # the paused requestId
        self._auth_task: "asyncio.Task | None" = None
        self._fetch_on = False
        self._fetch_task: "asyncio.Task | None" = None   # the enable and reload
        self._linger_task: "asyncio.Task | None" = None  # the disable after it
        self._chooser: "int | None" = None         # backendNodeId of the input
        self._icon_task: "asyncio.Task | None" = None

        self._seq = 0
        self._casts = 0                 # frames emitted, for the settle race
        # What the stream is running at, and the measurements it is chosen on.
        self._level = _remembered_level(pane)
        # seq -> (when the cast was queued, whether its delivery is the link's
        # alone to answer for); see `_taint_pending`.
        self._pending: dict = {}
        self._over_ms = 0.0             # smoothed; the level hangs off it
        self._rtt_ms = 0.0              # the quickest delivery lately
        self._measured = 0              # casts measured since the last judgement
        self._bad_evals = 0             # judgements in a row asking for less
        self._good_since = time.monotonic()
        self._level_why = ""
        self._level_task: "asyncio.Task | None" = None
        # The last settled picture or popup frame on its way to the client:
        # (seq, when). Casts queued behind it until the client has it wait on
        # its bytes and its decode, so they are not measured.
        self._heavy: "tuple[int, float] | None" = None
        self._hidden_at = 0.0
        # Main-frame commits so far. The first is the page the tab was opened
        # for, which starts at the pane's remembered level; every later one is
        # a new page and starts at the top.
        self._commits = 0
        # The last screencast frame Chrome sent, and the ones a capture's echo
        # may repeat once the capture is out (see STILL_ECHO_S).
        self._last_cast_body = b""
        self._echo_bodies: tuple = ()
        self._echo_open = False
        self._emits: deque = deque(maxlen=PAINTING_FRAMES)
        self._last_emit = 0.0
        self._cast_w = 0                # what the last cast frame really was
        self._paced: "tuple | None" = None   # a frame waiting for the clock
        self._pace_task: "asyncio.Task | None" = None
        self._rate_at = time.monotonic()
        self._rate_bytes = 0
        self._rate_frames = 0
        self._rate_drops = 0
        self._kbps = 0.0
        self._fps = 0.0
        self._drop_share = 0.0
        self._level_at = 0.0
        self._input_at = 0.0
        self._settle_at = 0.0
        # The settled pictures this tab has sent, and when the last one went.
        self._stills = 0
        self._still_at = 0.0
        # One pointer probe at a time: a move over a paragraph asks twenty
        # times a second and each ask is a round trip into the page.
        self.probe_busy = False
        self._settle_task: "asyncio.Task | None" = None
        self._quiet_task: "asyncio.Task | None" = None   # the quiet page's still
        self._show_task: "asyncio.Task | None" = None    # its fallback, after show
        self._freeze_task: "asyncio.Task | None" = None
        self._state_task: "asyncio.Task | None" = None
        self._popup_open = False
        self._popup_task: "asyncio.Task | None" = None
        self._capturing = False
        self._cast_pending = False      # a stream the page was not ready for
        self._held: list = []           # frames a settle capture provoked

    # -- geometry ----------------------------------------------------------
    def _metrics(self) -> dict:
        """The override that makes CSS pixels, zoom and the phone's DPR agree.

        Zoom is not applied to the page (a page can see its own zoom and some
        of them fight it); the viewport is made *smaller* by the zoom factor
        and rendered at a correspondingly higher scale, so the same surface
        comes back with everything on it bigger. `dpr_max` is the ceiling: a
        3x phone asking for a 3x surface of a desktop-sized viewport is asking
        for four times the pixels of a 1.5x one, over the same link. The
        stream's level is not: no rung changes what the page is rendered at.
        """
        z = self.zoom or 1.0
        dpr = min(self.dpr, self._dpr_cap())
        return {"width": int(round(self.css_w / z)),
                "height": int(round(self.css_h / z)),
                "deviceScaleFactor": dpr * z, "mobile": False}

    def _dpr_cap(self) -> float:
        """The pixel ratio this tab may ask for, which is the config's."""
        return float(self.config.get("dpr_max") or 2)

    def _quality(self) -> int:
        """The JPEG quality to stream at.

        The configured quality is the top of the ladder rather than a fixed
        setting: a user who asked for 80 gets 80 while the link can carry it,
        and the rungs below are this module's numbers.
        """
        top = int(self.config.get("jpeg_quality") or 70)
        return top if self._level == 0 else min(top, STREAM_LEVELS[self._level][0])

    def _frame_gap(self) -> float:
        return 1.0 / float(STREAM_LEVELS[self._level][1])

    def _cast_size(self) -> "tuple[int, int]":
        """The most the screencast may send: the surface, at every level.

        Chrome sends the CSS size whatever this says (see STREAM_LEVELS), and
        the odd frame it does send at the surface — the one that answers a
        capture — is a sharp one worth having rather than one to scale down.
        """
        return self._surface()

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
        await s.send("Runtime.addBinding", {"name": "ptuiTitle"})
        await s.send("Page.addScriptToEvaluateOnNewDocument",
                     {"source": FULL_PAGE_HELPER})
        # Network, for the one response that matters: a 401 with a challenge on
        # it, which is the only warning before the page comes back empty. The
        # interception that answers the challenge is not enabled here — it
        # pauses every request the page makes, so it waits until there is a
        # challenge to answer (see `_on_response`).
        await s.send("Network.enable")
        # A file input the user tapped opens the computer's file dialog, which
        # is on the wrong computer. Intercepted, so the pane can ask the phone
        # instead and hand back paths.
        try:
            await s.send("Page.setInterceptFileChooserDialog", {"enabled": True})
        except (CDPError, CDPClosed, asyncio.TimeoutError) as e:
            log(f"tab {self.tab}: file chooser not intercepted: {e!r}")
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
        s.on("Page.loadEventFired", self._on_load)
        s.on("Page.javascriptDialogOpening", self._on_dialog)
        s.on("Page.javascriptDialogClosed", self._on_dialog_closed)
        s.on("Page.fileChooserOpened", self._on_file_chooser)
        s.on("Network.responseReceived", self._on_response)
        s.on("Fetch.requestPaused", self._on_paused)
        s.on("Fetch.authRequired", self._on_auth_required)

    # -- outbound ----------------------------------------------------------
    def _emit(self, kind: str, body: bytes, fmt: str) -> "int | None":
        if not body or self.dead:
            return None
        self._seq += 1
        w, h = image_size(body) or self._cast_size()
        if kind == "cast":
            # What Chrome actually sent, which is not what it was asked for:
            # the settled picture below is only worth taking where it would be
            # bigger than this.
            self._cast_w = w
        else:
            self._taint_pending()
            self._heavy = (self._seq, time.monotonic())
        self._rate_drops += self.sink.put_frame(
            {"tab": self.tab, "seq": self._seq, "kind": kind, "w": w, "h": h,
             "cssW": self.css_w, "cssH": self.css_h, "zoom": self.zoom,
             "fmt": fmt}, body) or 0
        return self._seq

    def _taint_pending(self) -> None:
        """A settled picture or a popup frame is going out: the casts already
        in flight share its link and its client, so their deliveries are no
        measurement of the stream."""
        for seq, (queued, _clean) in self._pending.items():
            self._pending[seq] = (queued, False)

    def _behind_heavy(self, now: float) -> bool:
        """Is a settled picture or popup frame still on its way to the client?
        Bounded, so a lost ack cannot stop the measuring for good."""
        if self._heavy is not None and now - self._heavy[1] > 2 * LEVEL_TICK_S:
            self._heavy = None
        return self._heavy is not None

    def _say(self, msg: dict) -> None:
        self.sink.put_json(msg)

    def info(self) -> dict:
        return {"pane": self.pane, "tab": self.tab, "url": self.url,
                "title": self.title, "live": self.live, "frozen": self.frozen,
                "discarded": self.discarded,
                # What the stream is doing, for the Settings line and for
                # anyone asked to explain why a page looks soft. The rate is
                # only worth reporting while there is one: a page nobody has
                # touched for a minute is not still running at 24 fps.
                "level": self._level,
                "dpr": round(min(self.dpr, self._dpr_cap()), 2),
                "quality": self._quality(),
                "fps": round(self._fps, 1) if self._streaming() else 0.0,
                "kbps": round(self._kbps) if self._streaming() else 0,
                "rttMs": round(self._rtt_ms),
                "overMs": round(self._over_ms),
                "dropShare": round(self._drop_share, 2),
                "levelReason": self._level_why,
                # The sharp frames: how many this tab has sent and how long ago
                # the last one went. A page that looks soft is a page with no
                # recent still, which is the one question a screenshot of the
                # pane cannot answer. Zero for a tab that has never sent one.
                "stills": self._stills,
                "lastStillMs": (round((time.monotonic() - self._still_at) * 1000)
                                if self._still_at else 0)}

    def _streaming(self) -> bool:
        return self.live and time.monotonic() - self._last_emit < RATE_STALE_S

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
        msg = {"type": "tab", "tab": self.tab, "url": self.url,
               "title": self.title, "loading": self.loading,
               "canBack": can_back, "canFwd": can_fwd,
               "targetId": self.target_id}
        # Only when there is one: an absent key is the pane keeping the icon it
        # already has, and every push would otherwise carry the same kilobytes
        # of data URL again.
        if self.favicon:
            msg["favicon"] = self.favicon
        self._say(msg)

    def _is_main(self, frame_id: str) -> bool:
        return not self.frame_id or frame_id == self.frame_id

    def _on_navigated(self, params: dict) -> None:
        frame = params.get("frame") or {}
        if frame.get("parentId"):
            return
        self.frame_id = frame.get("id") or self.frame_id
        was = self.url
        self.url = frame.get("url") or self.url
        if self._commits:
            self._reset_level("navigation")
        self._commits += 1
        # Another site's icon is worse than none: kept across a navigation
        # within one origin (where it is still right, and the probe may find
        # nothing to replace it with) and dropped when the host changes.
        if _host_of(self.url) != _host_of(was):
            self.favicon = ""
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
            self._arm_idle_still()
            if self.live and self._cast_pending and not self.dead:
                # There is a page to stream now. See `_start_cast`.
                self._cast_pending = False
                asyncio.ensure_future(self._start_cast())

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
        if self.discarded:
            # The watchdog took this tab's target to stay under the cap. Loading
            # the page again is what showing it means now, and everything below
            # is the same as for a tab that never went away.
            if self.owner is None:
                raise CDPClosed(f"tab {self.tab} was discarded")
            await self.owner.revive(self)
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
        if self._hidden_at and time.monotonic() - self._hidden_at > LEVEL_RESET_HIDDEN_S:
            self._reset_level("shown after a long time hidden")
        self._hidden_at = 0.0
        # The canvas this stream is starting for may be a new one — a pane
        # that reloaded, a tab shown again — that never got the last still, so
        # nothing the stream sends first can be that still's echo.
        self._close_echo()
        await self.session.send("Emulation.setDeviceMetricsOverride", self._metrics())
        if self.frozen:
            await self.session.send("Page.setWebLifecycleState", {"state": "active"})
            self.frozen = False
        await self._start_cast()
        self.live = True
        self.shown_at = time.monotonic()
        self._good_since = self.shown_at
        self._bad_evals = 0
        if self._level_task is None or self._level_task.done():
            self._level_task = asyncio.ensure_future(self._level_loop())
        # A page that is already loaded and sitting still gets its sharp
        # picture from the quiet that follows; one still arriving gets it from
        # its load event. The second timer is for a tab that fires neither.
        self._arm_idle_still()
        self._cancel(self._show_task)
        self._show_task = asyncio.ensure_future(self._still_after_show())

    async def hide(self) -> None:
        """Stop the stream, and start the clock on freezing the tab."""
        if not self.live:
            return
        self.live = False
        self._hidden_at = time.monotonic()
        self._popup_open = False
        self._close_echo()
        # No stream to come to rest: a timer left running here would take its
        # picture of whatever the tab is showing when it is next streamed.
        self._cancel(self._quiet_task)
        self._cancel(self._show_task)
        self._cancel(self._level_task)
        self._quiet_task = self._show_task = self._level_task = None
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
        w, h = self._cast_size()
        params = {"format": "jpeg", "quality": self._quality(),
                  "maxWidth": w, "maxHeight": h, "everyNthFrame": 1}
        # A page part-way through a navigation is briefly not the active one —
        # the document going has been detached and the one arriving is not
        # shown yet — and Chrome refuses to cast it, saying so. Showing a tab
        # the instant it was opened lands in exactly that window, so it is
        # waited out rather than reported as a tab that cannot be streamed.
        delay = 0.05
        for _ in range(5):
            try:
                await self.session.send("Page.startScreencast", params,
                                        timeout=CAST_START_S)
                self._cast_pending = False
                return
            except CDPError as e:
                if "active page" not in (e.message or ""):
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.4)
            except asyncio.TimeoutError:
                # A navigation that has not committed yet — a first page behind
                # an auth challenge, a server that has not answered — has no
                # surface to cast, and the call does not come back at all
                # rather than failing. The tab is live either way: the stream
                # is started again when the page finally stops loading, and
                # holding the pane's whole socket here (every op on it is
                # awaited in turn) would stop the user answering the very
                # challenge that is holding the page.
                log(f"tab {self.tab}: nothing to stream yet; waiting for the page")
                self._cast_pending = True
                return
        await self.session.send("Page.startScreencast", params)
        self._cast_pending = False

    async def _stop_cast(self) -> None:
        self._release_paced()
        self._pending.clear()
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
            return
        # Whatever the restart was for, the page gets its full-resolution
        # picture as soon as it is quiet again.
        self._arm_idle_still()

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
        if self._is_echo(body):
            # The capture's echo: the picture the settled one already shows.
            # Sent, it would be painted over the sharp one.
            self._ack_cdp(sid)
            return
        self._echo_open = False
        self._last_cast_body = body
        wait = self._last_emit + self._frame_gap() - time.monotonic()
        if wait > 0:
            # Sooner than this level's frame rate wants it. Held rather than
            # sent, and the browser is left unacknowledged until it goes: an
            # ack now would only buy another frame to drop.
            self._hold_paced(sid, body, min(wait, PACE_MAX_S))
        else:
            self._send_cast(sid, body)
        if self._input_at:
            self._arm_settle()
        else:
            # Nobody asked for this frame, so it is the page painting itself —
            # and the picture it leaves behind, once it stops, is what the
            # settled capture is for. The capture's own echo never gets here.
            self._arm_idle_still()

    def _is_echo(self, body: bytes) -> bool:
        """Is this frame the last capture's echo? See STILL_ECHO_S.

        Two of them, measured: one at the surface's full size as the capture
        is answered, and one ~90 ms later at the CSS size that is byte for
        byte the last frame before the capture. The first is the still's own
        picture again and the second is the soft one it replaced.
        """
        if not self._echo_open or time.monotonic() - self._still_at >= STILL_ECHO_S:
            return False
        if body in self._echo_bodies:
            return True
        size = image_size(body)
        return size is not None and size[0] >= self._surface()[0]

    def _close_echo(self) -> None:
        """No frame from here on is the last still's echo: the stream it was
        taken on is over, and a pane that reconnected never received it."""
        self._echo_open = False
        self._echo_bodies = ()

    def _hold_paced(self, sid, body: bytes, wait: float) -> None:
        if self._paced is not None:     # the browser owes us only one at a time
            self._ack_cdp(self._paced[0])
        self._paced = (sid, body)
        if self._pace_task is None or self._pace_task.done():
            self._pace_task = asyncio.ensure_future(self._pace_later(wait))

    async def _pace_later(self, wait: float) -> None:
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        held, self._paced = self._paced, None
        if held is None:
            return
        if self.dead or not self.live or self._capturing:
            self._ack_cdp(held[0])
            return
        self._send_cast(*held)

    def _release_paced(self) -> None:
        """Give the browser its frame back, unsent. For a stream going away."""
        self._cancel(self._pace_task)
        self._pace_task = None
        held, self._paced = self._paced, None
        if held is not None:
            self._ack_cdp(held[0])

    def _send_cast(self, sid, body: bytes) -> None:
        """One frame onto the queue, and the browser told to make the next.

        The ack goes back here rather than when the client says it drew the
        frame: the queue drops a stale `cast` for a client that is behind, so
        what the network carries is always the newest picture, and Chrome has
        no reason to sit idle for a round trip it cannot see.
        """
        self._casts += 1
        now = self._last_emit = time.monotonic()
        self._emits.append(now)
        seq = self._emit("cast", body, "jpeg")
        if seq is not None:
            self._pending[seq] = (now, not self._behind_heavy(now))
            if len(self._pending) > PENDING_MAX:
                for old in sorted(self._pending)[:-PENDING_MAX // 2]:
                    del self._pending[old]
            self._rate_bytes += len(body)
            self._rate_frames += 1
            self._rate_tick(now)
        self._ack_cdp(sid)

    def _rate_tick(self, now: float) -> None:
        """What the stream is costing, once a second.

        `_rate_frames` counts what left and `_rate_drops` what the queue threw
        away to make room for it, so their ratio is how much of the stream the
        link is refusing — which is the one thing an ack cannot say on a link
        where every frame takes exactly as long as every other.
        """
        span = now - self._rate_at
        if span < 1.0:
            return
        made = self._rate_frames + self._rate_drops
        self._kbps = self._rate_bytes * 8 / 1000.0 / span
        self._fps = self._rate_frames / span
        self._drop_share = ((self._rate_drops / made)
                            if made >= DROP_MIN_FRAMES else 0.0)
        self._rate_bytes = self._rate_frames = self._rate_drops = 0
        self._rate_at = now

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

    def ack(self, seq: int, dropped: bool = False) -> None:
        """The client has drawn everything up to `seq` (or, `dropped`, threw
        `seq` away unpainted).

        Nothing waits for this — the browser was acknowledged when the frame
        went out — but it is the one measurement of how long a frame takes to
        arrive, and that is what the level is chosen on.
        """
        now = time.monotonic()
        if self._heavy is not None and seq >= self._heavy[0]:
            self._heavy = None
        sent = None
        for pending in sorted(s for s in self._pending if s <= seq):
            entry = self._pending.pop(pending)
            if pending == seq:
                sent = entry
        if sent is None or dropped:     # dropped, aged out, or not a cast
            return
        queued, clean = sent
        if clean:
            self._note_delivery(queued, now)

    def _note_delivery(self, sent: float, now: float) -> None:
        """One cast's delivery time, folded into what the level is judged on."""
        ms = (now - sent) * 1000
        # The quickest delivery lately is the link's floor: one frame's
        # distance with nothing queued behind it. It is allowed to drift back
        # up, so a link that got worse is not measured for ever against how
        # good it once was.
        self._rtt_ms = ms if not self._rtt_ms else min(ms, self._rtt_ms * 1.05 + 1)
        over = max(0.0, ms - self._rtt_ms)
        self._over_ms = self._over_ms * (1 - DELIVER_ALPHA) + over * DELIVER_ALPHA
        self._measured += 1

    async def _level_loop(self) -> None:
        """Judge the level once a tick for as long as the tab is live."""
        try:
            while self.live and not self.dead:
                await asyncio.sleep(LEVEL_TICK_S)
                if self.live and not self.dead:
                    self._judge(time.monotonic())
        except asyncio.CancelledError:
            return

    def _judge(self, now: float) -> None:
        """Whether the stream is worth more or less than it is being sent at.

        Two ways for a link to be the limit and they do not overlap: frames
        that sit in the queue behind other frames (`_over_ms`, a latency the
        user feels on every click) and frames that never leave it at all
        (`_drop_share`, a link with a ceiling, where the delivered ones all
        take the same time and say nothing). Either one, on `LEVEL_DOWN_EVALS`
        judgements in a row, is a step down; `LEVEL_HOLD_S` with neither is a
        step back up, frames or no frames.
        """
        if now - self._rate_at >= LEVEL_TICK_S * 0.5:
            self._rate_tick(now)
        # A delivery average with no delivery behind it since the last
        # judgement is history: a page at rest has sent nothing to measure.
        fresh, self._measured = self._measured > 0, 0
        slow = fresh and self._over_ms > OVER_DOWN_MS
        refused = self._drop_share > DROP_DOWN
        if slow or refused:
            self._bad_evals += 1
            self._good_since = now
            if (self._bad_evals >= LEVEL_DOWN_EVALS
                    and now - self._level_at >= LEVEL_MIN_GAP_S
                    and self._level < len(STREAM_LEVELS) - 1):
                self._set_level(self._level + 1,
                                "frames behind" if slow else "frames dropped")
            return
        self._bad_evals = 0
        if self._level > 0 and now - self._good_since >= LEVEL_HOLD_S:
            self._set_level(self._level - 1, "the link has room")

    def _reset_level(self, why: str) -> None:
        """Back to the top of the ladder: what the level was measured on is gone."""
        self._over_ms = 0.0
        self._drop_share = 0.0
        self._bad_evals = 0
        self._good_since = time.monotonic()
        self._set_level(0, why)

    def _set_level(self, level: int, why: str) -> None:
        level = max(0, min(len(STREAM_LEVELS) - 1, level))
        if level == self._level:
            return
        was, self._level = self._level, level
        now = time.monotonic()
        # Remembered for the pane rather than the tab: it is the link that was
        # slow, and the next tab opens on the same one.
        _LEVEL_MEMORY[self.pane] = (level, now)
        # One line with the numbers that moved it, so a report of a soft page
        # can be read off the journal.
        self._level_why = why
        log(f"tab {self.tab}: level {was}->{level} ({why}) "
            f"over={self._over_ms:.0f}ms floor={self._rtt_ms:.0f}ms "
            f"drop={self._drop_share:.2f} kbps={self._kbps:.0f}")
        # The measurements describe the level just left. Put back to neither
        # fast nor slow, so the step is not repeated (or undone) until frames
        # measured at the new level say so — one hiccup costs one rung, not
        # the whole ladder.
        self._over_ms = 0.0
        self._drop_share = 0.0
        self._bad_evals = 0
        self._good_since = now
        self._level_at = now
        if self.live and not self.dead:
            asyncio.ensure_future(self._restart_cast())

    # -- input, settling and popups ---------------------------------------
    def note_input(self, kind: str = "", key: str = "") -> None:
        """Told by the input handlers that something was sent to the page.

        Two things hang off it: the settled picture below, and the end of a
        native popup — the click that picks an option, or the key that
        dismisses the menu, is the last thing the screenshots are needed for.
        """
        self._input_at = self.last_input = time.monotonic()
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
        # An input's own settle supersedes the quiet page's: this one knows
        # what it is waiting for.
        self._cancel(self._quiet_task)
        self._quiet_task = None
        self._cancel(self._settle_task)
        self._settle_task = asyncio.ensure_future(self._settle())

    def _arm_idle_still(self) -> None:
        """Start the clock on the settled picture a page at rest gets.

        One timer, not one per frame: the clock is read from `_last_emit`
        inside it, so a timer already waiting is already waiting for the right
        moment and a page painting at 24 fps costs no task a frame.
        """
        if self.dead or not self.live or self._capturing:
            return
        if self._quiet_task is not None and not self._quiet_task.done():
            return
        self._quiet_task = asyncio.ensure_future(self._idle_still())

    async def _still_after_show(self) -> None:
        """The clock started again a while after `show`. See SHOW_STILL_S.

        A fallback and nothing more: a tab whose page has already been
        photographed since it was shown is left alone, or every show would
        cost a second capture of a picture that had not changed.
        """
        try:
            await asyncio.sleep(SHOW_STILL_S)
        except asyncio.CancelledError:
            return
        if self._still_at >= self.shown_at:
            return
        self._arm_idle_still()

    async def _idle_still(self) -> None:
        """One settled picture for a page that has gone quiet on its own.

        Nothing above this asks for it: the stream is what a page shows while
        it moves, and the picture is what it deserves once it stops.
        """
        try:
            await asyncio.sleep(IDLE_STILL_S)
            # Frames may still be in the air — one held for the pace has not
            # gone out yet — and the quiet is measured from the last one that
            # did. A page that is really painting re-arms this timer with
            # every frame and never reaches the capture below at all.
            while True:
                left = self._last_emit + IDLE_STILL_S - time.monotonic()
                if left <= 0 and self._paced is None:
                    break
                await asyncio.sleep(max(left, 0.05))
        except asyncio.CancelledError:
            return
        if self._input_at or (self._settle_task is not None
                              and not self._settle_task.done()):
            return          # an input's settle is in flight; that one wins
        if self._painting() or not self._still_worth_it():
            return
        await self._take_still()

    def _painting(self) -> bool:
        """Is the page carrying itself? Frames still coming, none asked for."""
        if len(self._emits) < PAINTING_FRAMES:
            return False
        return (time.monotonic() - self._emits[0] < PAINTING_WINDOW_S
                and self._emits[-1] > self._input_at)

    def _still_worth_it(self) -> bool:
        """Whether a capture would show more than the stream already has.

        The settled picture is a screenshot: it is taken at the page's real
        pixel ratio and at the whole viewport, and the stream is neither of
        those. On a 2x client the screenshot is four times the pixels of a
        screencast frame, which is the whole reason it is sharp — and on a 1x
        one, at a level that is not shrinking the picture, it is the same
        picture again in a bigger file. Where a frame has already carried what
        the input did and the capture would be no larger, there is nothing to
        take.
        """
        if not self._emits or self._emits[-1] <= self._input_at:
            return True        # the stream never showed what the input did
        return self._surface()[0] > self._cast_w

    async def _settle(self) -> None:
        try:
            await asyncio.sleep(SETTLE_DELAY_S)
            # A page painting on its own is already showing the settled
            # picture, and a capture would only stop its stream for the
            # length of an encode to send that picture a second time.
            if self._painting() or not self._still_worth_it():
                self._input_at = 0.0
                # The page is carrying itself, or a frame already showed what
                # the input did. Either way the picture is owed to the quiet
                # that follows rather than to this input.
                self._arm_idle_still()
                return
            gap = SETTLE_MIN_GAP_S - (time.monotonic() - self._settle_at)
            if gap > 0:
                await asyncio.sleep(gap)
                if self._painting() or not self._still_worth_it():
                    self._input_at = 0.0
                    self._arm_idle_still()
                    return
        except asyncio.CancelledError:
            return
        self._input_at = 0.0
        await self._take_still()

    async def _take_still(self) -> None:
        """The capture itself: the stream held for one screenshot.

        Both ways in end here — the input that stopped and the page that went
        quiet — so a still costs the same and is decided the same either way.
        """
        if self.dead or not self.live:
            return
        self._settle_at = time.monotonic()
        self._held = []
        self._capturing = True
        try:
            res = await self.session.send(
                "Page.captureScreenshot",
                {"format": "jpeg", "quality": SETTLE_QUALITY,
                 "fromSurface": True,
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
        held, self._held = self._held, []
        try:
            sent = self._emit("still", base64.b64decode(res.get("data") or ""),
                              "jpeg")
        except (ValueError, TypeError):
            return
        if sent is not None:
            self._stills += 1
            self._still_at = time.monotonic()
            self._echo_bodies = tuple(
                {self._last_cast_body, *(b for _sid, b in held)} - {b""})
            self._echo_open = True

    def _flush_held(self) -> None:
        """Send the frames held during a capture, already acknowledged."""
        held, self._held = self._held, []
        for _sid, body in held:
            self._send_cast(None, body)

    def _on_binding(self, params: dict) -> None:
        name = params.get("name")
        payload = params.get("payload") or ""
        if name == "ptuiSel":
            text = payload[:SELECTION_MAX]
            if text == self.selection:
                return
            self.selection = text
            # Pushed rather than polled: the copy key and the context menu's
            # Copy have to act inside the gesture that pressed them — a
            # clipboard write a round trip later is one the browser refuses —
            # so the client is told as the page changes its mind.
            if self.on_selection is not None:
                self.on_selection(self, text)
            return
        if name == "ptuiTitle":
            # Chrome 153 sends no targetInfoChanged for a title a script sets,
            # and a single-page app sets every title it will ever have that
            # way; without this the tab strip keeps the first one forever.
            title = payload[:TITLE_MAX]
            if title and title != self.title:
                self.title = title
                self._schedule_state()
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
        quality = int(self.config.get("jpeg_quality") or 70)
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
            # The menu is gone and the page under it is what the pane is
            # showing again — at the screencast's resolution until this.
            self._arm_idle_still()

    # -- the page asking something -----------------------------------------
    # Four things a page can do that a picture of a page cannot answer: put up
    # a modal dialog, demand HTTP credentials, open a file dialog, and change
    # its icon. Each one is a message to the pane and, for the first three, an
    # answer that has to come back before the page goes on — so each one also
    # has a deadline, because the pane is a phone and phones go into pockets.

    def _on_dialog(self, params: dict) -> None:
        kind = str(params.get("type") or "alert")
        if self._dialog is not None:
            # A page can only have one up at a time; a second means the first
            # was answered by something other than us. Let the pane keep the
            # one it is showing rather than stack them.
            log(f"tab {self.tab}: a second {kind} while one is open")
            return
        self._dialog = kind
        self._say({"type": "dialog", "tab": self.tab, "kind": kind,
                   "message": str(params.get("message") or "")[:DIALOG_MESSAGE_MAX],
                   "default": str(params.get("defaultPrompt") or "")[:DIALOG_MESSAGE_MAX],
                   "url": str(params.get("url") or "")})
        self._dialog_task = asyncio.ensure_future(self._dialog_deadline(kind))

    def _on_dialog_closed(self, params: dict) -> None:
        """Answered by something that was not us — a navigation, mostly."""
        self._dialog = None
        self._cancel(self._dialog_task)
        self._dialog_task = None

    async def _dialog_deadline(self, kind: str) -> None:
        try:
            await asyncio.sleep(DIALOG_TIMEOUT_S)
        except asyncio.CancelledError:
            return
        if self._dialog is None or self.dead:
            return
        # Dismissed, which for an alert *is* its OK button: an alert has no
        # other answer, and leaving it up leaves the page stopped forever.
        log(f"tab {self.tab}: no answer to the {kind}; dismissing it")
        try:
            await self.answer_dialog(kind == "alert")
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass

    async def answer_dialog(self, accept: bool, text: str = "") -> bool:
        """The pane's answer. False when there was nothing left to answer."""
        kind, self._dialog = self._dialog, None
        if kind is None:
            return False
        self._cancel(self._dialog_task)
        self._dialog_task = None
        params: dict = {"accept": bool(accept)}
        # An empty answer to a prompt is an answer: left out, the page gets the
        # default value it suggested rather than the nothing the user typed.
        if text or kind == "prompt":
            params["promptText"] = text
        await self.session.send("Page.handleJavaScriptDialog", params)
        return True

    def _on_response(self, params: dict) -> None:
        """A 401 on the document, which is the only warning of a login box."""
        if str(params.get("type") or "") != "Document" or self._fetch_on:
            return
        frame = params.get("frameId") or ""
        if frame and not self._is_main(frame):
            return
        resp = params.get("response") or {}
        try:
            status = int(resp.get("status") or 0)
        except (TypeError, ValueError):
            return
        if status != 401:
            return
        challenge = ""
        for key, value in (resp.get("headers") or {}).items():
            if str(key).lower() == "www-authenticate":
                challenge = str(value or "").lstrip().lower()
                break
        if not challenge.startswith(("basic", "digest")):
            return
        self._fetch_task = asyncio.ensure_future(self._intercept_and_reload())

    async def _intercept_and_reload(self) -> None:
        """Turn interception on for this tab and ask for the page again.

        Lazily, because `Fetch.enable` pauses every request the page makes and
        hands each one back to this process to release: a page of a hundred
        images would be a hundred round trips through a phone's socket for
        nothing. The 401 already happened, so the cost is one reload.
        """
        if self._fetch_on or self.dead:
            return
        self._fetch_on = True
        try:
            await self.session.send(
                "Fetch.enable",
                {"handleAuthRequests": True,
                 "patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
            # The 401 *is* the navigation's answer, so at this moment the page
            # it belongs to is not the active one yet and Chrome refuses to
            # reload it. Waited out, the way `_start_cast` waits out the same
            # window, rather than reported as a page that cannot be asked for.
            delay = 0.05
            for attempt in range(6):
                try:
                    await self.session.send("Page.reload")
                    return
                except CDPError as e:
                    if "active page" not in (e.message or "") or attempt == 5:
                        raise
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 0.4)
        except (CDPError, CDPClosed, asyncio.TimeoutError) as e:
            log(f"tab {self.tab}: could not ask for credentials: {e!r}")
            self._fetch_on = False
            # Interception that bought nothing is a round trip through the
            # phone's socket per request from here on, so it goes back.
            try:
                await self.session.send("Fetch.disable")
            except (CDPError, CDPClosed, asyncio.TimeoutError):
                pass

    def _on_paused(self, params: dict) -> None:
        """Every intercepted request, let straight through."""
        rid = params.get("requestId")
        if rid:
            asyncio.ensure_future(self._continue(str(rid)))

    async def _continue(self, rid: str) -> None:
        try:
            await self.session.send("Fetch.continueRequest", {"requestId": rid},
                                    timeout=10)
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass

    def _on_auth_required(self, params: dict) -> None:
        rid = str(params.get("requestId") or "")
        if not rid:
            return
        challenge = params.get("authChallenge") or {}
        if self._auth is not None:
            # One prompt at a time. A second challenge while the user is typing
            # into the first is refused rather than queued: two password boxes
            # for one page is not something the pane can show.
            asyncio.ensure_future(self._auth_reply(rid, None, ""))
            return
        self._auth = rid
        self._say({"type": "auth", "tab": self.tab,
                   "host": str(challenge.get("origin") or ""),
                   "realm": str(challenge.get("realm") or "")[:TITLE_MAX],
                   "scheme": str(challenge.get("scheme") or "")})
        self._auth_task = asyncio.ensure_future(self._auth_deadline(rid))

    async def _auth_deadline(self, rid: str) -> None:
        try:
            await asyncio.sleep(AUTH_TIMEOUT_S)
        except asyncio.CancelledError:
            return
        if self._auth != rid or self.dead:
            return
        log(f"tab {self.tab}: no credentials came back; cancelling")
        await self.cancel_auth()

    async def answer_auth(self, user: str, password: str) -> bool:
        rid, self._auth = self._auth, None
        self._cancel(self._auth_task)
        self._auth_task = None
        if rid is None:
            return False
        await self._auth_reply(rid, user, password)
        # Chrome remembers the credentials for the realm, so the interception
        # has done its job; it is left on a little longer only because the very
        # next request is the one being retried with them.
        self._cancel(self._linger_task)
        self._linger_task = asyncio.ensure_future(self._drop_interception())
        return True

    async def cancel_auth(self) -> bool:
        rid, self._auth = self._auth, None
        self._cancel(self._auth_task)
        self._auth_task = None
        if rid is None:
            return False
        await self._auth_reply(rid, None, "")
        # Interception is given back on the same clock as a success: the page
        # is not getting in, and leaving every one of its requests paused for
        # nothing is the cost this was made lazy to avoid.
        self._cancel(self._linger_task)
        self._linger_task = asyncio.ensure_future(self._drop_interception())
        return True

    async def _auth_reply(self, rid: str, user: "str | None",
                          password: str) -> None:
        answer = ({"response": "CancelAuth"} if user is None else
                  {"response": "ProvideCredentials", "username": user,
                   "password": password})
        try:
            await self.session.send("Fetch.continueWithAuth",
                                    {"requestId": rid,
                                     "authChallengeResponse": answer}, timeout=10)
        except (CDPError, CDPClosed, asyncio.TimeoutError) as e:
            log(f"tab {self.tab}: the challenge could not be answered: {e!r}")

    async def _drop_interception(self) -> None:
        try:
            await asyncio.sleep(FETCH_LINGER_S)
        except asyncio.CancelledError:
            return
        if self.dead or not self._fetch_on:
            return
        try:
            await self.session.send("Fetch.disable")
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass
        # Cleared either way: a disable that did not land leaves a tab whose
        # every request is paused, and the next 401 has to be free to enable it
        # again rather than believe it is already on.
        self._fetch_on = False

    def _on_file_chooser(self, params: dict) -> None:
        node = params.get("backendNodeId")
        if not node:
            return
        self._chooser = int(node)
        self._say({"type": "filechooser", "tab": self.tab,
                   "multiple": str(params.get("mode") or "") == "selectMultiple"})

    async def set_files(self, paths: "list[str]") -> bool:
        """Answer the file dialog with paths *on this computer*.

        An empty list is how the pane cancels: the protocol has no other way to
        say so, and an input left with no files is what a cancelled dialog
        leaves behind anyway.
        """
        node, self._chooser = self._chooser, None
        if node is None:
            return False
        files = [str(p) for p in paths]
        try:
            await self.session.send("DOM.enable")
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass
        await self.session.send("DOM.setFileInputFiles",
                                {"files": files, "backendNodeId": node})
        return True

    def _on_load(self, params: dict) -> None:
        if self.dead:
            return
        # The page is there. What the stream showed while it was arriving is
        # the soft half-resolution one, so the quiet after the load is when the
        # sharp picture is owed.
        self._arm_idle_still()
        if self._icon_task is not None and not self._icon_task.done():
            return
        self._icon_task = asyncio.ensure_future(self._read_favicon())

    async def _read_favicon(self) -> None:
        try:
            res = await self.session.send(
                "Runtime.evaluate",
                {"expression": FAVICON_JS, "returnByValue": True,
                 "awaitPromise": True}, timeout=FAVICON_TIMEOUT_S)
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            return
        if res.get("exceptionDetails"):
            return
        icon = str((res.get("result") or {}).get("value") or "")
        if not icon or len(icon) > FAVICON_MAX or icon == self.favicon:
            return
        self.favicon = icon
        self._schedule_state()

    # -- navigation --------------------------------------------------------
    async def nav(self, url: str) -> None:
        if self._commits:
            self._reset_level("navigation")
        await self.session.send("Page.navigate", {"url": url})
        # Recorded now rather than waited for: the page will report its own URL
        # a moment later and correct this, and until it does this is the honest
        # answer to where the tab is — including for a tab the memory watchdog
        # discards before the first frameNavigated arrives, whose record is the
        # only thing left to load it again from.
        self.url = url
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
            # A new size or zoom is a new picture to be sharp about.
            self._arm_idle_still()

    # -- end ---------------------------------------------------------------
    async def close(self) -> None:
        """Close the target. The tab is gone whether or not the call lands."""
        self._teardown()
        try:
            await self.session.cdp.send("Target.closeTarget",
                                        {"targetId": self.target_id}, timeout=5)
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass

    async def discard(self) -> None:
        """Give the target up and keep the tab, for the memory watchdog.

        The difference from `close` is what the pane is left with: a closed tab
        is gone, and a discarded one is still in its tab strip with its title on
        it, one tap from the page it was showing.
        """
        if self.dead or self.discarded:
            return
        self.mark_discarded()
        try:
            await self.session.cdp.send("Target.closeTarget",
                                        {"targetId": self.target_id}, timeout=5)
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass

    def mark_discarded(self) -> None:
        """The record's side of a discard, with no target left to close."""
        if self.dead or self.discarded:
            return
        self.discarded = True
        self._stop_tasks()
        self.frozen = False

    def rebind(self, session: Session, target_id: str) -> None:
        """A discarded tab's new target, before `start` runs on it again."""
        self.session = session
        self.target_id = target_id
        self.discarded = False
        self.frozen = False
        self.live = False
        self.frame_id = ""
        self.selection = ""
        self.cursor_sent = None
        self._casts = 0
        self._pending.clear()
        self._emits.clear()
        self._last_emit = 0.0
        self._cast_w = 0
        self._heavy = None
        self._last_cast_body = b""
        self._echo_bodies = ()
        self._echo_open = False
        self._paced = None
        self._held = []
        self._capturing = False
        self._cast_pending = False
        self._input_at = 0.0
        self._popup_open = False
        # The cancelled tasks of the tab's last life. Dropped rather than left
        # for `done()` to be asked about: a cancel is not done until the loop
        # has been round, and a state push that saw one of these would take it
        # for a push already on its way and send nothing.
        self._settle_task = None
        self._quiet_task = None
        self._show_task = None
        self._freeze_task = None
        self._popup_task = None
        self._level_task = None
        self._pace_task = None
        self._state_task = None
        self._dialog_task = None
        self._auth_task = None
        self._fetch_task = None
        self._linger_task = None
        self._icon_task = None

    @staticmethod
    def _cancel(task) -> None:
        if task is not None and not task.done():
            task.cancel()

    def _stop_tasks(self) -> None:
        """Stop the timers and the stream, without ending the tab."""
        self.live = False
        self._popup_open = False
        # Nothing is waiting on an answer any more: the target these were
        # about is gone, so a dialog to dismiss and a challenge to cancel are
        # both calls into nothing.
        self._dialog = None
        self._auth = None
        self._chooser = None
        self._fetch_on = False
        for task in (self._settle_task, self._quiet_task, self._show_task,
                     self._freeze_task, self._popup_task, self._level_task,
                     self._pace_task, self._state_task, self._dialog_task,
                     self._auth_task, self._fetch_task, self._linger_task,
                     self._icon_task):
            self._cancel(task)
        self._paced = None
        self._pending.clear()

    def _teardown(self) -> None:
        """Stop everything this tab is running. Idempotent."""
        if self.dead:
            return
        self.dead = True
        self._stop_tasks()


# ---------------------------------------------------------------------------
# Files: what the browser puts on this computer, and what the phone hands it
# ---------------------------------------------------------------------------
# Both directories are the user's alone (0700) and both are under
# `~/.pockettui` rather than the real Downloads folder: a page the phone opened
# writing into the folder the user's own browser watches is a surprise nobody
# asked for, and this way one directory holds everything the mode produced.

def downloads_dir() -> Path:
    path = config_dir() / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def uploads_dir() -> Path:
    """Where the pane stages a phone's file before a page's input takes it."""
    return config_dir() / "uploads" / "browser"


def sweep_uploads(max_age: float = UPLOAD_MAX_AGE_S) -> int:
    """Drop yesterday's staged files. Called once per browser launch.

    Each one was picked for a single upload that has long since happened, and
    the alternative is a directory that only grows, holding whatever the user
    last sent to a website.
    """
    root = uploads_dir()
    cutoff = time.time() - max_age
    dropped = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if entry.is_dir():
            shutil.rmtree(entry.path, ignore_errors=True)
        else:
            try:
                os.unlink(entry.path)
            except OSError:
                continue
        dropped += 1
    if dropped:
        log(f"swept {dropped} staged upload(s) from {root}")
    return dropped


def download_name(suggested: str, guid: str) -> str:
    """The site's suggested filename, as something safe to create.

    The site chooses this string, so it is a single path component or it is the
    guid: a name with a separator in it would put the file wherever the page
    liked.
    """
    name = str(suggested or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f").strip()
    if not name or name in (".", ".."):
        return guid
    return name[:128]


def unique_path(folder: Path, name: str) -> Path:
    """`name` in `folder`, with ` (2)` before the extension if it is taken."""
    stem, ext = os.path.splitext(name)
    path = folder / name
    n = 2
    while path.exists() and n < 1000:
        path = folder / f"{stem} ({n}){ext}"
        n += 1
    return path


def _rmtree_twice(path: Path) -> bool:
    """Remove a directory, with one retry.

    A browser on its way out goes on writing its profile for a moment after the
    pipe closes, and a directory that grows while it is being removed is how
    this raises `Directory not empty`.
    """
    for delay in (0.0, 1.0):
        if delay:
            time.sleep(delay)
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
    return False


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
        self.watchdog = Watchdog(self)
        self.mem: "Measurement | None" = None
        self.discards = 0          # tabs given up since this browser started
        self.restarts = 0          # browsers replaced for going over the cap
        self._restarting = False
        self._downloads: dict = {}   # guid -> what the pane is being told
        self._popup_seq = 0          # names the tabs popups become
        self._adopting: set = set()  # popup targets already being attached
        self._crashed: dict = {}     # (pane, tab) -> the target that crashed

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
        self._sinks[pane] = sink
        # Not a reason to keep the browser: a pane with no full tab in it needs
        # no browser, and the next `open` starts one. See `_arm_idle`.
        self._arm_idle()
        # A new socket is a new link, or the old one after whatever broke it:
        # nothing measured on the last one says anything about this one.
        _LEVEL_MEMORY.pop(pane, None)
        for pt in self._tabs.values():
            if pt.pane == pane:
                pt.sink = sink
                pt._reset_level("the pane reconnected")

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

    def _selection_changed(self, pt: PaneTab, text: str) -> None:
        """One tab's selection, on its way to the pane that is showing it."""
        self.to_pane(pt.pane, {"type": "sel", "tab": pt.tab, "text": text})

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
            self.mem = None
            self.discards = 0
            browser.closed.add_done_callback(self._on_browser_gone)
            try:
                await browser.cdp.send("Target.setDiscoverTargets", {"discover": True})
            except (CDPError, CDPClosed, asyncio.TimeoutError):
                pass
            browser.cdp.on("Target.targetInfoChanged", self._on_target_info)
            browser.cdp.on("Target.targetCreated", self._on_target_created)
            browser.cdp.on("Target.targetDestroyed", self._on_target_destroyed)
            browser.cdp.on("Target.targetCrashed", self._on_target_crashed)
            browser.cdp.on("Browser.downloadWillBegin", self._on_download_begin)
            browser.cdp.on("Browser.downloadProgress", self._on_download_progress)
            self._downloads.clear()
            self._crashed.clear()
            sweep_uploads()
            try:
                await browser.cdp.send("Browser.setDownloadBehavior", {
                    "behavior": "allow", "downloadPath": str(downloads_dir()),
                    "eventsEnabled": True})
            except (CDPError, CDPClosed, asyncio.TimeoutError, OSError) as e:
                log(f"downloads will go wherever Chrome puts them: {e!r}")
            soft, cap = caps_mb(self.config)
            log(f"browser up: {found.path} {found.version} pid={browser.pid} "
                f"cap={int(cap)}MB soft={int(soft)}MB")
            self.watchdog.start()
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
        self._crashed.clear()
        self._downloads.clear()
        self.browser = None
        self.watchdog.stop()
        self.mem = None
        self.launch_error = message
        log(message)
        self._to_all({"type": "error", "tab": None, "code": "exited",
                      "message": message})

    async def shutdown(self, timeout: float = 6.0) -> None:
        """Close every tab and stop the browser, for the server going down."""
        end = time.monotonic() + timeout
        self._cancel_idle()
        self.watchdog.stop()
        self.mem = None
        tabs = list(self._tabs.values())
        self._tabs.clear()
        self._by_target.clear()
        self._crashed.clear()
        self._downloads.clear()
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

    def has_tabs(self) -> bool:
        """Whether any pane still has a tab — live, hidden or discarded.

        A discarded one counts: it has no target, but it is in somebody's tab
        strip with a title on it and one tap from its page.
        """
        return any(not pt.dead for pt in self._tabs.values())

    async def reset(self) -> None:
        """Stop the browser and throw the profile away.

        Everything the mode ever stored — cookies, logins, history, the site
        data of every page the phone opened — is in that one directory, so
        this is the whole of "sign me out of everything and start again". The
        caller is the one that refuses while tabs are open; here the profile is
        a directory the browser must not be holding.
        """
        await self.shutdown()
        profile = config_dir() / "chromium-profile"
        if not profile.exists():
            return
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, _rmtree_twice, profile):
            raise OSError(f"the browser profile is still there: {profile}")
        log(f"browser profile removed: {profile}")

    # -- idle --------------------------------------------------------------
    def _cancel_idle(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _targets(self) -> int:
        """Tabs with a target behind them — the only reason to keep a browser.

        A pane with no full tab in it is not one: it costs the browser nothing
        and the next `open` starts one again, so idle is counted in targets and
        not in panes. Discarded records do not count either; they are a URL and
        a title, and reviving one goes through `ensure` like any other open.
        """
        return sum(1 for pt in self._tabs.values()
                   if not pt.dead and not pt.discarded)

    def _arm_idle(self) -> None:
        self._cancel_idle()
        if self.browser is None or self._targets():
            return
        self._idle_task = asyncio.ensure_future(self._idle_exit())

    async def _idle_exit(self) -> None:
        try:
            await asyncio.sleep(float(self.config.get("idle_exit_s") or 0))
        except asyncio.CancelledError:
            return
        if self.browser is None or self._targets():
            return
        log("nothing left open; stopping the browser")
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
        # A tab whose renderer crashed leaves its target behind, sad face and
        # all, and `Target.getTargets` still lists it as a page — so a pane
        # re-opening after a crash would adopt the corpse. It is closed here,
        # at the one moment something is about to take its place.
        corpse = self._crashed.pop((pane, tab), None)
        if corpse is not None:
            if target_id == corpse:
                target_id = None
            await self._close_target(corpse)
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
        pt.on_selection = self._selection_changed
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

    async def _close_target(self, target_id: str) -> None:
        if self.browser is None:
            return
        try:
            await self.browser.cdp.send("Target.closeTarget",
                                        {"targetId": target_id}, timeout=5)
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            pass

    # -- popups ------------------------------------------------------------
    # A page that calls window.open, or a link with target=_blank, makes a
    # second target with the first one named as its opener. In a browser that
    # is a new tab; here the pane has no idea it exists, so it would be a
    # window nobody can see and a click that appeared to do nothing.

    def _on_target_created(self, params: dict) -> None:
        info = params.get("targetInfo") or {}
        target_id = str(info.get("targetId") or "")
        opener = str(info.get("openerId") or "")
        if info.get("type") != "page" or not target_id or not opener:
            return
        if target_id in self._by_target or target_id in self._adopting:
            return
        parent = self._by_target.get(opener)
        if parent is None or parent.dead:
            return
        self._adopting.add(target_id)
        asyncio.ensure_future(self._adopt_popup(parent, target_id,
                                                str(info.get("url") or "")))

    def _tab_id_for_popup(self, pane: str) -> str:
        """A tab id this pane is not using. The pane's own are its to choose."""
        while True:
            self._popup_seq += 1
            tab = f"p{self._popup_seq}"
            if (pane, tab) not in self._tabs:
                return tab

    async def _adopt_popup(self, parent: PaneTab, target_id: str,
                           url: str) -> None:
        try:
            if self.browser is None or parent.dead:
                return
            if self._targets() >= MAX_TARGETS:
                # No room for a tab to put it in. The page it was opening is
                # still what the user asked for, so it is shown in the tab that
                # asked — which is what a browser with no tab strip left can do.
                await self._close_target(target_id)
                if url and url != "about:blank":
                    try:
                        await parent.nav(url)
                    except (CDPError, CDPClosed, asyncio.TimeoutError) as e:
                        log(f"popup into tab {parent.tab} failed: {e!r}")
                return
            tab = self._tab_id_for_popup(parent.pane)
            sess = await self.browser.cdp.attach(target_id)
            pt = PaneTab(parent.pane, tab, sess, target_id,
                         self._sink(parent.pane), self.config,
                         css_w=parent.css_w, css_h=parent.css_h, dpr=parent.dpr,
                         zoom=parent.zoom, owner=self)
            pt.on_selection = self._selection_changed
            pt.url = url if url and url != "about:blank" else ""
            self._tabs[(parent.pane, tab)] = pt
            self._by_target[target_id] = pt
            self._cancel_idle()
            try:
                # Never with a URL: the target is already going where the page
                # sent it, and navigating would take it somewhere else.
                await pt.start(None, self.browser.user_agent
                               if self._ua_needed else None)
            except (CDPError, CDPClosed, asyncio.TimeoutError):
                self._forget(pt)
                await pt.close()
                raise
            # Nothing is shown yet: the pane decides whether this becomes the
            # tab on screen or is closed unseen, and says so with `show` or
            # `close` like any other tab of its own.
            self.to_pane(parent.pane, {"type": "newtab", "tab": tab,
                                       "url": pt.url or url,
                                       "opener": parent.tab,
                                       "targetId": target_id})
        except (CDPError, CDPClosed, asyncio.TimeoutError) as e:
            log(f"a popup of tab {parent.tab} could not be adopted: {e!r}")
        finally:
            self._adopting.discard(target_id)

    # -- downloads ---------------------------------------------------------
    # Chrome saves the file under its guid, not under the name the site
    # suggested (that is what `eventsEnabled` costs), so the rename at the end
    # is not a nicety: without it the user is left with a directory of hex.

    def _download_tab(self, frame_id: str) -> "PaneTab | None":
        """Whose download this is.

        By frame where the frame is a tab's main frame; a download started from
        an iframe carries that iframe's id and matches nothing, so it falls
        back to the tab the user was last typing or clicking in, which is the
        only tab that can have started it.
        """
        for pt in self._tabs.values():
            if frame_id and pt.frame_id == frame_id and not pt.dead:
                return pt
        live = [pt for pt in self._tabs.values()
                if pt.live and not pt.dead and self._sinks.get(pt.pane)]
        if not live:
            return None
        return max(live, key=lambda p: max(p.last_input, p.shown_at))

    def _on_download_begin(self, params: dict) -> None:
        guid = str(params.get("guid") or "")
        if not guid:
            return
        pt = self._download_tab(str(params.get("frameId") or ""))
        rec = {"pane": pt.pane if pt is not None else "",
               "tab": pt.tab if pt is not None else None,
               "name": download_name(params.get("suggestedFilename") or "", guid),
               "url": str(params.get("url") or ""),
               "state": "inProgress", "received": 0, "total": 0,
               "path": "", "said": 0.0}
        self._downloads[guid] = rec
        self._say_download(guid, rec, force=True)

    def _on_download_progress(self, params: dict) -> None:
        guid = str(params.get("guid") or "")
        rec = self._downloads.get(guid)
        if rec is None:
            return
        rec["state"] = str(params.get("state") or "inProgress")
        for key, field in (("receivedBytes", "received"), ("totalBytes", "total")):
            try:
                rec[field] = int(params.get(key) or 0)
            except (TypeError, ValueError):
                pass
        if rec["state"] == "completed":
            asyncio.ensure_future(self._finish_download(guid))
            return
        if rec["state"] == "canceled":
            self._downloads.pop(guid, None)
            self._say_download(guid, rec, force=True)
            return
        self._say_download(guid, rec)

    def _say_download(self, guid: str, rec: dict, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - rec["said"] < DOWNLOAD_EVERY_S:
            return
        rec["said"] = now
        if not rec["pane"]:
            return
        self.to_pane(rec["pane"], {
            "type": "download", "tab": rec["tab"], "guid": guid,
            "name": rec["name"], "url": rec["url"], "state": rec["state"],
            "received": rec["received"], "total": rec["total"],
            "path": rec["path"]})

    async def _finish_download(self, guid: str) -> None:
        rec = self._downloads.pop(guid, None)
        if rec is None:
            return
        loop = asyncio.get_running_loop()
        try:
            rec["path"] = await loop.run_in_executor(
                None, self._rename_download, guid, rec["name"])
        except OSError as e:
            log(f"download {rec['name']!r} could not be renamed: {e!r}")
            rec["path"] = str(downloads_dir() / guid)
        log(f"downloaded {rec['path']}")
        self._say_download(guid, rec, force=True)

    @staticmethod
    def _rename_download(guid: str, name: str) -> str:
        folder = downloads_dir()
        raw = folder / guid
        if not raw.exists():
            # Some builds name it what the site asked for; then there is
            # nothing to do but say where it is.
            done = folder / name
            return str(done if done.exists() else raw)
        target = unique_path(folder, name)
        os.replace(raw, target)
        return str(target)

    # -- memory ------------------------------------------------------------
    async def revive(self, pt: PaneTab) -> None:
        """Give a discarded tab a target again, at the URL it was showing.

        The same `PaneTab` — the pane knows it by its own id, the history it
        shows is this object's, and a new one would be a different tab wearing
        the same name. Only the session and the target id change.
        """
        browser = await self.ensure()
        self._cancel_idle()
        if self._targets() >= MAX_TARGETS:
            raise TabLimit(f"{MAX_TARGETS} tabs are already open on this computer")
        res = await browser.cdp.send("Target.createTarget",
                                     {"url": "about:blank", "newWindow": True})
        target_id = res["targetId"]
        sess = await browser.cdp.attach(target_id)
        if self._ua_needed is None:
            # A relaunched browser has not been asked yet, and the tab coming
            # back is as entitled to a UA without "Headless" in it as a new one.
            self._ua_needed = await self._probe_headless(sess)
        pt.rebind(sess, target_id)
        self._by_target[target_id] = pt
        try:
            await pt.start(pt.url or "about:blank",
                           browser.user_agent if self._ua_needed else None)
        except (CDPError, CDPClosed, asyncio.TimeoutError):
            self._by_target.pop(target_id, None)
            pt.mark_discarded()
            raise

    async def discard_oldest_hidden(self, mem: "Measurement",
                                    soft: float) -> bool:
        """Past the soft cap: the background tab nobody has looked at longest.

        Never the live one. A tab the user is reading is the one thing a memory
        limit must not take, and the browser has a harder answer for the case
        where that tab is the one growing (see `restart_over_cap`).
        """
        hidden = [pt for pt in self._tabs.values()
                  if not pt.live and not pt.dead and not pt.discarded]
        if not hidden:
            return False
        pt = min(hidden, key=lambda p: p.shown_at)
        self._by_target.pop(pt.target_id, None)
        await pt.discard()
        self.discards += 1
        log(f"{mem.mb:.0f} MB over the {soft:.0f} MB soft cap; "
            f"discarded tab {pt.tab} of pane {pt.pane} ({pt.url[:80]!r})")
        self.to_pane(pt.pane, {"type": "tab", "tab": pt.tab, "discarded": True,
                               "url": pt.url, "title": pt.title})
        self._arm_idle()
        return True

    async def restart_over_cap(self, mem: "Measurement", cap: float) -> None:
        """Past the hard cap twice: a new browser, and every tab a record.

        The tabs are not closed, they are unplugged: each record keeps its URL
        and its title, and the one tab each pane was showing is loaded again at
        once so the user is looking at their page and not at an empty canvas.
        """
        if self._restarting:
            return
        self._restarting = True
        try:
            records = dict(self._tabs)
            showing = {pt.pane: pt.tab for pt in records.values() if pt.live}
            for pt in records.values():
                pt.mark_discarded()
            # Out of the way of `shutdown`, which tears down what it finds.
            self._tabs.clear()
            self._by_target.clear()
            self.restarts += 1
            log(f"{mem.mb:.0f} MB over the {cap:.0f} MB cap twice; restarting the "
                f"browser ({len(records)} tabs kept as records)")
            await self.shutdown()
            self._tabs.update(records)
            message = f"Browser restarted: memory cap {int(cap)} MB"
            for pane in list(self._sinks):
                self.to_pane(pane, {"type": "error", "tab": None,
                                    "code": "restarted", "message": message})
            for pane, tab in showing.items():
                if pane not in self._sinks:
                    continue
                pt = self._tabs.get((pane, tab))
                if pt is None or pt.dead:
                    continue
                try:
                    await pt.show()
                except (CDPError, CDPClosed, asyncio.TimeoutError, TabLimit,
                        BrowserUnavailable) as e:
                    log(f"tab {tab} of pane {pane} did not come back: {e!r}")
            # The records that were not revived are still discarded, and the
            # pane has to hear it from somewhere to draw them as such.
            for pt in self._tabs.values():
                if pt.discarded:
                    self.to_pane(pt.pane, {"type": "tab", "tab": pt.tab,
                                           "discarded": True, "url": pt.url,
                                           "title": pt.title})
            self._arm_idle()
        finally:
            self._restarting = False

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
        # The target outlives the renderer that crashed in it, showing a sad
        # face nobody can see. Remembered rather than closed now: the pane
        # answers a crash by opening the tab again, and that is where the dead
        # one is cleared away (see `open`).
        self._crashed[(pt.pane, pt.tab)] = pt.target_id
        self.to_pane(pt.pane, {"type": "error", "tab": pt.tab, "code": "crashed",
                               "message": "the page crashed"})
        self._arm_idle()

    # -- for the status route ---------------------------------------------
    def snapshot(self) -> dict:
        running = self.browser is not None and not self.browser.closed.done()
        soft, cap = caps_mb(self.config)
        mem = self.mem
        return {
            "running": running,
            "pid": self.browser.pid if running else None,
            "version": self.found.version if self.found else "",
            "tabs": [pt.info() for pt in self._tabs.values()],
            "launch_error": self.launch_error,
            # What the watchdog last measured. `memApprox` is what the number
            # is worth: PSS on Linux is real, the macOS estimate is not.
            "memMb": round(mem.mb) if mem is not None else None,
            "memApprox": bool(mem.approx) if mem is not None else False,
            "procs": mem.procs if mem is not None else 0,
            "softMb": round(soft) if soft else None,
            "capMb": self.config.get("memory_mb"),
            "discards": self.discards,
            "restarts": self.restarts,
        }


# ---------------------------------------------------------------------------
# What the browser costs, and what to do about it
# ---------------------------------------------------------------------------

# How often the tree is measured. Five seconds is short enough that a page
# allocating hard is caught before the machine feels it, and long enough that
# the measurement — one pass over /proc, a few dozen small reads — is free.
WATCHDOG_TICK_S = 5.0

# The soft cap, as a fraction of the configured one. Past it a background tab
# is given up, which is the cheap answer: the tab comes back from its URL and
# nothing the user is looking at is touched.
SOFT_FRACTION = 0.8

# How many ticks over the hard cap before the browser is restarted. One is a
# spike — a page loading images, a garbage collection that has not run yet —
# and restarting on it would take the user's tabs for nothing.
HARD_STRIKES = 2


def caps_mb(config: dict) -> "tuple[float, float]":
    """The (soft, hard) cap in MB, or (0, 0) when there is none to enforce."""
    try:
        cap = float(config.get("memory_mb") or 0)
    except (TypeError, ValueError):
        cap = 0.0
    if cap <= 0:
        return (0.0, 0.0)
    return (cap * SOFT_FRACTION, cap)


@dataclasses.dataclass(frozen=True)
class Measurement:
    mb: float
    approx: bool
    procs: int


class ProcReader:
    """Where ProcessTree's numbers come from: this machine.

    An object rather than a handful of module functions so the watchdog can be
    measured against a process tree that does not exist: every number comes
    through one of these methods, and none of them is allowed to raise. A pid
    that vanished between being listed and being read is the normal case, not
    an error — Chrome starts and ends processes while the page runs.
    """

    def __init__(self, platform: str = "") -> None:
        self.platform = platform or sys.platform

    def pids(self) -> "list[int]":
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        return [int(e) for e in entries if e.isdigit()]

    def ppid(self, pid: int) -> "int | None":
        # The comm field is parenthesised and may contain spaces and parens of
        # its own, so the fields are counted from the last ')' rather than by
        # splitting the line: state is first after it, ppid second.
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read()
        except OSError:
            return None
        try:
            return int(raw[raw.rindex(b")") + 1:].split()[1])
        except (ValueError, IndexError):
            return None

    def pss_kb(self, pid: int) -> "int | None":
        return self._field(f"/proc/{pid}/smaps_rollup", "Pss:")

    def rss_kb(self, pid: int) -> "int | None":
        return self._field(f"/proc/{pid}/status", "VmRSS:")

    @staticmethod
    def _field(path: str, prefix: str) -> "int | None":
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith(prefix):
                        return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return None

    def cgroup(self, pid: int) -> str:
        try:
            with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8",
                      errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def ps_text(self) -> str:
        """`ps -axo pid,ppid,rss`, for the platforms with no PSS to read."""
        try:
            out = subprocess.run(["ps", "-axo", "pid,ppid,rss"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout or ""


class ProcessTree:
    """The processes a browser is, and what they really cost.

    RSS is the wrong number here, and not by a little: one tab of Chrome is a
    dozen processes sharing one copy of a large binary, and every one of them
    reports the whole shared mapping as resident. Measured on the machine this
    was written on, a one-tab browser of 18 processes summed to 1748 MB of RSS
    and 410 MB of PSS — RSS overcounts by four, so a cap enforced on it would
    keep restarting a browser costing a quarter of what the number claims. PSS
    divides each shared page by the number of processes mapping it, which is
    the only per-process figure that adds up to the truth.

    macOS has no PSS. `ps` gives RSS per process, and the bulk of what they
    share is the one framework every one of them maps, so the smallest RSS in
    the tree is taken as that baseline and counted once instead of n times. It
    is an approximation and says so (`approx`), which is the honest reason the
    cap is a comfort setting rather than an accounting one.

    Under a systemd scope the tree is not the whole story either: a renderer
    whose parent went away is re-parented to init and walks out of the tree,
    so every process whose cgroup is our scope is counted too.
    """

    @staticmethod
    def measure(root_pid: int, unit: "str | None" = None,
                reader: "ProcReader | None" = None) -> Measurement:
        reader = reader if reader is not None else ProcReader()
        platform = str(getattr(reader, "platform", sys.platform) or "")
        if platform.startswith("darwin"):
            return ProcessTree._measure_ps(root_pid, reader)
        return ProcessTree._measure_proc(root_pid, unit, reader)

    # -- Linux -------------------------------------------------------------
    @staticmethod
    def _measure_proc(root_pid: int, unit: "str | None", reader) -> Measurement:
        total = 0
        procs = 0
        approx = False
        for pid in ProcessTree.pids(root_pid, unit, reader):
            kb = reader.pss_kb(pid)
            if kb is None:
                # No smaps_rollup: a kernel without it, or — far more often —
                # a process that exited while this pass was reading. RSS is
                # the answer that is left, and it is marked as one.
                kb = reader.rss_kb(pid)
                if kb is None:
                    continue
                approx = True
            total += kb
            procs += 1
        return Measurement(mb=total / 1024.0, approx=approx, procs=procs)

    @staticmethod
    def pids(root_pid: int, unit: "str | None" = None,
             reader: "ProcReader | None" = None) -> "list[int]":
        """Every pid of the tree under `root_pid`, plus the scope's strays."""
        reader = reader if reader is not None else ProcReader()
        kids: dict = {}
        listed = reader.pids()
        for pid in listed:
            parent = reader.ppid(pid)
            if parent is not None:
                kids.setdefault(parent, []).append(pid)
        out: list = []
        seen: set = set()
        queue = [int(root_pid)]
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            out.append(pid)
            queue.extend(kids.get(pid, ()))
        if unit:
            for pid in listed:
                if pid not in seen and unit in reader.cgroup(pid):
                    seen.add(pid)
                    out.append(pid)
        return out

    # -- macOS -------------------------------------------------------------
    @staticmethod
    def parse_ps(text: str) -> dict:
        """`pid ppid rss` rows as {pid: (ppid, rss_kb)}, header and all."""
        rows: dict = {}
        for line in (text or "").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                rows[int(parts[0])] = (int(parts[1]), int(parts[2]))
            except ValueError:
                continue   # the header, and anything else that is not numbers
        return rows

    @staticmethod
    def _measure_ps(root_pid: int, reader) -> Measurement:
        rows = ProcessTree.parse_ps(reader.ps_text())
        kids: dict = {}
        for pid, (parent, _rss) in rows.items():
            kids.setdefault(parent, []).append(pid)
        seen: set = set()
        queue = [int(root_pid)]
        sizes: list = []
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            if pid in rows:
                sizes.append(rows[pid][1])
            queue.extend(kids.get(pid, ()))
        if not sizes:
            return Measurement(mb=0.0, approx=True, procs=0)
        total = sum(sizes) - (len(sizes) - 1) * min(sizes)
        return Measurement(mb=max(0, total) / 1024.0, approx=True,
                           procs=len(sizes))


class Watchdog:
    """Keeps the browser inside the cap the config asked for.

    The cap has to be enforced here because the obvious place does not work:
    the transient scope carries MemoryHigh and MemoryMax, and on a user manager
    with no memory controller delegated — which is the machine this was written
    on — `systemd-run -p MemoryMax=1M` succeeds and limits nothing. The scope
    is worth having for its name and for the kill that goes with it; the number
    is ours to keep.

    Two answers, in order of how much they cost the user. Past the soft cap the
    oldest background tab is given up: its target is closed and the record
    keeps the URL and the title, so tapping it loads the page again. Past the
    hard cap twice in a row the browser is restarted, which is the only answer
    left when the tab the user is *looking at* is the one growing.
    """

    def __init__(self, fb: "FullBrowser", tree=None) -> None:
        self.fb = fb
        self.tree = tree if tree is not None else ProcessTree
        self.last: "Measurement | None" = None
        self.over = 0
        self._task: "asyncio.Task | None" = None
        self._epoch = 0

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._epoch += 1
        self.over = 0
        self._task = asyncio.ensure_future(self._loop(self._epoch))

    def stop(self) -> None:
        """Stop ticking. Callable from inside a tick — the restart path is.

        A cancel would land on the task that is running the restart, halfway
        through, so the loop is retired by epoch instead and cancelled only
        when the caller is somebody else.
        """
        task, self._task = self._task, None
        self._epoch += 1
        self.last = None
        self.over = 0
        if task is None or task.done():
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not current:
            task.cancel()

    async def _loop(self, epoch: int) -> None:
        try:
            while epoch == self._epoch:
                await asyncio.sleep(WATCHDOG_TICK_S)
                if epoch != self._epoch:
                    return
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — never stop watching
                    log(f"watchdog tick failed: {e!r}")
        except asyncio.CancelledError:
            return

    async def tick(self) -> "Measurement | None":
        """One measurement, and whatever it calls for. Also the test's handle."""
        fb = self.fb
        browser = fb.browser
        if browser is None or browser.closed.done():
            self.last = None
            self.over = 0
            fb.mem = None
            return None
        pid, unit = browser.pid, browser.unit
        # Off the loop: a pass over /proc is a few dozen small reads, and on a
        # busy machine they are not instant.
        measured = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self.tree.measure(pid, unit))
        self.last = measured
        fb.mem = measured
        soft, cap = caps_mb(fb.config)
        if cap <= 0:
            return measured
        if measured.mb > cap:
            self.over += 1
            if self.over >= HARD_STRIKES:
                self.over = 0
                await fb.restart_over_cap(measured, cap)
                return measured
        else:
            self.over = 0
        if measured.mb > soft:
            await fb.discard_oldest_hidden(measured, soft)
        return measured


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
