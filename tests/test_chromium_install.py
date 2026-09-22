"""Downloading a browser: `chromium.py --install`, with no network involved.

Google's version list and the ~190 MB zip are both served by a local HTTP
server, and the "browser" inside the zip is a shell script that answers
`--version` and `--dump-dom` the way the probe expects. That is enough to pin
the parts that are easy to get wrong and impossible to see in a happy-path
download: a connection that dies halfway and a run that has to pick the rest up
with a Range request, a Content-Length that does not match what arrived, an
install that should not download at all, and what is left behind afterwards.

The server answers only the two paths the installer should ask for, so picking
`chrome-headless-shell` instead of `chrome` would fail here with a 404.
"""

import io
import json
import os
import shutil
import socket
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromium as C  # noqa: E402

try:
    PLATFORM = C._download_platform()
except C.InstallError as e:  # pragma: no cover - only on an unsupported machine
    pytest.skip(str(e), allow_module_level=True)

VERSION = "999.0.1.2"
OLDER = "998.0.0.1"

# Where the binary sits inside each platform's zip, as DOWNLOAD_BINARIES expects
# to find it once unpacked.
MEMBER = {
    "linux64": "chrome-linux64/chrome",
    "linux-arm64": "chrome-linux-arm64/chrome",
    "mac-arm64": ("chrome-mac-arm64/Google Chrome for Testing.app/Contents/"
                  "MacOS/Google Chrome for Testing"),
    "mac-x64": ("chrome-mac-x64/Google Chrome for Testing.app/Contents/"
                "MacOS/Google Chrome for Testing"),
}[PLATFORM]

# A browser is a thing that says what version it is and then answers the
# DevTools protocol on fd 3 / fd 4, which is all the probe asks of one.
FAKE_BROWSER = """#!@PY@
import json
import os
import sys

if "--version" in sys.argv:
    print("Google Chrome for Testing @VER@")
    raise SystemExit(0)

buf = b""
while True:
    chunk = os.read(3, 4096)
    if not chunk:
        break
    buf += chunk
    while b"\\0" in buf:
        raw, buf = buf.split(b"\\0", 1)
        msg = json.loads(raw.decode())
        if msg.get("method") == "Browser.getVersion":
            reply = dict(id=msg["id"], result=dict(product="HeadlessChrome/@VER@"))
            os.write(4, json.dumps(reply).encode() + b"\\0")
        elif msg.get("method") == "Browser.close":
            raise SystemExit(0)
""".replace("@PY@", sys.executable).replace("@VER@", VERSION)


def build_zip() -> bytes:
    """A zip with the real internal layout, big enough to resume inside."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        script = zipfile.ZipInfo(MEMBER)
        script.external_attr = 0o100755 << 16
        z.writestr(script, FAKE_BROWSER)
        # Incompressible, so the zip keeps its size and a byte offset into it
        # means something.
        filler = zipfile.ZipInfo(MEMBER.split("/")[0] + "/resources.pak")
        filler.external_attr = 0o100644 << 16
        z.writestr(filler, os.urandom(400_000))
    return buf.getvalue()


ZIP = build_zip()


class Handler(BaseHTTPRequestHandler):
    zip_bytes = ZIP
    versions_json = ""
    break_after = None   # close the connection after this many body bytes, once
    inflate = 0          # bytes to add to the declared length, i.e. a lie
    seen: list = []      # (method, path, Range header, status)

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _note(self, status):
        type(self).seen.append((self.command, self.path,
                                self.headers.get("Range"), status))

    def _serve(self, head: bool):
        cls = type(self)
        if self.path.startswith("/versions"):
            body = cls.versions_json.encode()
            self._note(200)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return
        if not self.path.startswith("/chrome.zip"):
            self._note(404)
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        data = cls.zip_bytes
        total = len(data) + cls.inflate
        start = 0
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            start = int(rng[len("bytes="):].split("-")[0] or 0)
            if start >= len(data):
                self._note(416)
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        body = data[start:]
        status = 206 if start else 200
        self._note(status)
        self.send_response(status)
        if start:
            self.send_header("Content-Range",
                             f"bytes {start}-{len(data) - 1}/{total}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body) + cls.inflate))
        self.end_headers()
        if head:
            return
        cut = cls.break_after
        if cut is not None:
            cls.break_after = None
            self.wfile.write(body[:cut])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(body)


def versions_doc(base: str, version: str = VERSION) -> str:
    entry = {
        "channel": "Stable",
        "version": version,
        "revision": "1",
        "downloads": {
            "chrome": [{"platform": PLATFORM, "url": f"{base}/chrome.zip"}],
            # Taking this one would ask for a path the server answers with 404.
            "chrome-headless-shell": [
                {"platform": PLATFORM, "url": f"{base}/headless-shell.zip"}],
        },
    }
    return json.dumps({"timestamp": "now",
                       "channels": {"Stable": entry},
                       "versions": [entry]})


@pytest.fixture
def server(monkeypatch):
    Handler.zip_bytes = ZIP
    Handler.break_after = None
    Handler.inflate = 0
    Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    Handler.versions_json = versions_doc(base)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(C, "CFT_STABLE_URL", f"{base}/versions.json")
    monkeypatch.setattr(C, "CFT_VERSIONS_URL", f"{base}/versions.json")
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(C, "_find_cache", None)
    monkeypatch.setattr(C, "INSTALL_BACKOFF_S", 0.0)
    return tmp_path


def root(home: Path) -> Path:
    return home / ".pockettui" / "chromium"


def statuses(method="GET", path="/chrome.zip"):
    return [s for m, p, _r, s in Handler.seen if m == method and p == path]


def fake_installed(home: Path, version: str) -> Path:
    """A version directory holding a browser that starts, as an install leaves it."""
    binary = root(home) / version / MEMBER
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(FAKE_BROWSER)
    binary.chmod(0o755)
    return binary


def test_install_picks_platform_and_resumes(home, server, monkeypatch, capsys):
    part = root(home) / f"{VERSION}.zip.part"
    Handler.break_after = 120_000
    monkeypatch.setattr(C, "DOWNLOAD_ATTEMPTS", 1)  # so the first run gives up
    monkeypatch.setattr(C, "PROGRESS_EVERY", 1 << 16)

    assert C.main(["--install"]) == 1
    out = capsys.readouterr()
    assert 0 < part.stat().st_size < len(ZIP)
    assert "MiB of" in out.out          # progress, and no carriage returns in it
    assert "\r" not in out.out
    assert not (root(home) / "current").exists()

    monkeypatch.setattr(C, "DOWNLOAD_ATTEMPTS", 4)
    assert C.main(["--install"]) == 0
    out = capsys.readouterr().out

    binary = root(home) / VERSION / MEMBER
    assert os.access(binary, os.X_OK)
    assert (root(home) / "current").read_text().strip() == str(binary)
    assert not part.exists()
    assert out.strip().endswith(f"installed {VERSION} {binary}")
    # The second run asked for the rest of the file and was given it.
    assert 206 in statuses()
    assert any(r for _m, p, r, _s in Handler.seen if p == "/chrome.zip" and r)
    # The unpacked tree is the found browser on a machine with nothing on $PATH.
    monkeypatch.setattr(C.shutil, "which", lambda name: None)
    found = C.find_chromium({}, force=True)
    assert (found.path, found.source) == (str(binary), "downloaded")


def test_install_rejects_short_download(home, server, monkeypatch, capsys):
    Handler.inflate = 50_000  # Content-Length promises more than it sends
    monkeypatch.setattr(C, "DOWNLOAD_ATTEMPTS", 2)

    assert C.main(["--install"]) == 1
    err = capsys.readouterr().err.strip()
    assert "did not finish" in err
    assert not (root(home) / "current").exists()
    assert not (root(home) / VERSION / MEMBER).exists()


def test_install_skips_when_already_installed(home, server, capsys):
    binary = fake_installed(home, VERSION)
    assert C.main(["--install"]) == 0
    out = capsys.readouterr().out
    assert f"already installed {VERSION}" in out
    assert statuses() == []            # nothing of the zip was fetched
    assert binary.read_text() == FAKE_BROWSER
    assert (root(home) / "current").read_text().strip() == str(binary)


def test_install_prunes_older_versions(home, server):
    fake_installed(home, OLDER)
    (root(home) / f"{OLDER}.zip").write_bytes(b"leftover")
    (root(home) / "997.0.0.1.zip.part").write_bytes(b"leftover")

    assert C.main(["--install"]) == 0
    assert sorted(p.name for p in root(home).iterdir()) == sorted(["current", VERSION])


def test_install_keeps_the_tree_when_the_browser_will_not_start(home, server,
                                                                monkeypatch, capsys):
    # First the probe says no, the way a box with no usable sandbox does.
    monkeypatch.setattr(C, "_check", lambda path: 1)
    assert C.main(["--install"]) == 1
    assert "will not start here" in capsys.readouterr().err
    part = root(home) / f"{VERSION}.zip.part"
    assert part.stat().st_size == len(ZIP)
    assert not (root(home) / "current").exists()

    # Then it says yes, and the run that follows has nothing left to fetch: it
    # HEADs the url, sees the whole file already on disk and goes to work. The
    # unpacked tree is thrown away first, so it is the download being reused.
    shutil.rmtree(root(home) / VERSION)
    Handler.seen = []
    monkeypatch.setattr(C, "_check", lambda path: 0)
    assert C.main(["--install"]) == 0
    assert statuses("GET") == []
    assert statuses("HEAD") == [200]
    assert (root(home) / "current").read_text().strip() == str(
        root(home) / VERSION / MEMBER)
    assert not part.exists()          # swept once the install stands

    # And a run on top of a finished install asks the server for nothing at all.
    Handler.seen = []
    assert C.main(["--install"]) == 0
    assert f"already installed {VERSION}" in capsys.readouterr().out
    assert statuses("GET") == [] and statuses("HEAD") == []


def test_install_offline_is_one_line(home, monkeypatch, capsys):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(C, "CFT_STABLE_URL", f"http://127.0.0.1:{port}/versions.json")

    assert C.main(["--install"]) == 1
    out = capsys.readouterr()
    lines = out.err.strip().splitlines()
    assert len(lines) == 1, lines
    assert lines[0].startswith("could not read the browser version list")


def test_dry_run_prints_plan_without_download(home, server, capsys):
    assert C.main(["--install", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"version {VERSION}" in out
    assert f"platform {PLATFORM}" in out
    assert f"url {server}/chrome.zip" in out
    assert f"size {len(ZIP)} bytes" in out
    assert statuses("GET") == []       # the size came from a HEAD
    assert statuses("HEAD") == [200]
    assert not root(home).exists()


def test_dry_run_can_pin_a_version(home, server, capsys):
    Handler.versions_json = versions_doc(server, OLDER)
    assert C.main(["--install", "--version", OLDER, "--dry-run"]) == 0
    assert f"version {OLDER}" in capsys.readouterr().out
    assert C.main(["--install", "--version", "1.2.3.4", "--dry-run"]) == 1
    assert "no Chrome for Testing 1.2.3.4" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The probe both --check and --install rest on
# ---------------------------------------------------------------------------

def test_check_is_a_verdict_and_not_a_traceback(home, monkeypatch, capsys):
    """Whatever the probe does, `--check` answers 0 or 1 and says why."""
    binary = fake_installed(home, VERSION)
    monkeypatch.setattr(C, "_missing_libs", lambda path: [])

    monkeypatch.setattr(C, "_probe_launch",
                        lambda path, version: (True, "Chrome/999.0.1.2"))
    assert C.main(["--check", str(binary)]) == 0
    assert capsys.readouterr().out.strip() == f"ok {VERSION}"

    # The temp profile of a browser that had to be killed can still be growing
    # as the probe tidies up, which used to come out of here as an OSError.
    def blows_up(path, version):
        raise OSError(39, "Directory not empty", "Default")

    monkeypatch.setattr(C, "_probe_launch", blows_up)
    assert C.main(["--check", str(binary)]) == 1
    assert "could not start it" in capsys.readouterr().err

    monkeypatch.setattr(C, "_probe_launch",
                        lambda path, version: (False, "No usable sandbox! unshare"))
    assert C.main(["--check", str(binary)]) == 1
    err = capsys.readouterr().err
    assert "No usable sandbox" in err and "unprivileged user namespaces" in err

    assert C.main(["--check", "/nowhere/chrome"]) == 1
    assert "not executable" in capsys.readouterr().err


def test_check_needs_a_version_it_can_read(home, tmp_path, capsys):
    mute = tmp_path / "mute"
    mute.write_text("#!/bin/sh\nexit 0\n")
    mute.chmod(0o755)
    assert C.main(["--check", str(mute)]) == 1
    assert "does not answer --version" in capsys.readouterr().err


def test_drop_profile_gives_up_quietly(home, monkeypatch, capsys):
    stuck = home / "stuck"
    stuck.mkdir()
    (stuck / "Default").write_text("x")
    monkeypatch.setattr(C.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    C._drop_profile(stuck)          # returns, and says so once
    assert "left the probe's temp profile behind" in capsys.readouterr().out
    assert stuck.exists()
