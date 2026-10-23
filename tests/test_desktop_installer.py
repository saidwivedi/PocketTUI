"""desktop.sh, the terminal installer for the desktop app, run end to end.

A local HTTP server stands in for the GitHub release (POCKETTUI_DESKTOP_URL)
and serves a zip holding a stub PocketTUI.app, two fake AppImages and an icon.
HOME is a temp dir, and POCKETTUI_DESKTOP_OS / _ARCH pick the branch, so the
macOS path runs on a Linux box too; there ditto is absent and the unzip
fallback does the unpacking. Nothing here reaches the network.
"""

import functools
import http.server
import io
import os
import subprocess
import threading
import zipfile
from pathlib import Path

import pytest

from test_installer import bash32

DESKTOP_SH = Path(__file__).resolve().parent.parent / "desktop.sh"


def app_zip(marker):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("PocketTUI.app/Contents/Info.plist", "<plist/>\n")
        z.writestr("PocketTUI.app/Contents/MacOS/PocketTUI", f"#!/bin/sh\necho {marker}\n")
    return buf.getvalue()


@pytest.fixture
def release(tmp_path):
    """A directory served over HTTP, filled like the release; yields (dir, url)."""
    root = tmp_path / "release"
    root.mkdir()
    for name in ("PocketTUI-mac-arm64.zip", "PocketTUI-mac-x64.zip"):
        (root / name).write_bytes(app_zip(name))
    for name in ("PocketTUI-linux-x86_64.AppImage", "PocketTUI-linux-arm64.AppImage"):
        (root / name).write_text(f"#!/bin/sh\necho {name}\n")
    (root / "icon-512.png").write_bytes(b"\x89PNG fake icon")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def run(tmp_path, url, os_name, arch, shell="bash", args=(), **extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "POCKETTUI_DESKTOP_URL": url,
        "POCKETTUI_ICON_URL": f"{url}/icon-512.png",
        "POCKETTUI_DESKTOP_OS": os_name,
        "POCKETTUI_DESKTOP_ARCH": arch,
        "NO_COLOR": "1",
    })
    env.update(extra)
    return subprocess.run([shell, str(DESKTOP_SH), *args], capture_output=True, text=True,
                          env=env, timeout=60)


def shells():
    """Both shells the script must run under; 3.2 is what macOS runs it with."""
    return ["bash", str(bash32())]


@pytest.mark.parametrize("shell", shells())
def test_linux_installs_appimage_icon_and_menu_entry(tmp_path, release, shell):
    root, url = release
    r = run(tmp_path, url, "Linux", "x86_64", shell=shell)
    assert r.returncode == 0, r.stderr
    home = tmp_path / "home"
    appimage = home / ".local/bin/PocketTUI.AppImage"
    assert appimage.read_text() == (root / "PocketTUI-linux-x86_64.AppImage").read_text()
    assert os.access(appimage, os.X_OK)
    icon = home / ".local/share/icons/hicolor/512x512/apps/pockettui.png"
    assert icon.read_bytes() == b"\x89PNG fake icon"
    entry = (home / ".local/share/applications/pockettui.desktop").read_text()
    assert "Name=PocketTUI\n" in entry
    assert f'Exec="{appimage}" %U\n' in entry
    assert "Icon=pockettui\n" in entry
    assert "Categories=Utility;Development;\n" in entry
    assert str(appimage) in r.stdout
    # The deb is never touched, and nothing is left in the temp dir.
    assert not list(tmp_path.glob("pockettui-desktop.*"))

    again = run(tmp_path, url, "Linux", "x86_64", shell=shell)
    assert again.returncode == 0, again.stderr
    assert "updated" in again.stdout


def test_linux_arm64_gets_the_arm64_appimage(tmp_path, release):
    root, url = release
    r = run(tmp_path, url, "Linux", "aarch64")
    assert r.returncode == 0, r.stderr
    got = (tmp_path / "home/.local/bin/PocketTUI.AppImage").read_text()
    assert got == (root / "PocketTUI-linux-arm64.AppImage").read_text()


@pytest.mark.parametrize("shell", shells())
def test_macos_puts_the_bundle_in_applications_and_updates_it(tmp_path, release, shell):
    _, url = release
    apps = tmp_path / "root" / "Applications"
    apps.mkdir(parents=True)
    r = run(tmp_path, url, "Darwin", "arm64", shell=shell, POCKETTUI_APPS_DIR=str(apps))
    assert r.returncode == 0, r.stderr
    exe = apps / "PocketTUI.app/Contents/MacOS/PocketTUI"
    assert "PocketTUI-mac-arm64.zip" in exe.read_text()
    assert (apps / "PocketTUI.app/Contents/Info.plist").is_file()
    assert f"PocketTUI.app is in {apps}." in r.stdout
    assert sorted(p.name for p in apps.iterdir()) == ["PocketTUI.app"]

    # A file only the old bundle had must be gone after the update.
    (apps / "PocketTUI.app/stale").write_text("old")
    again = run(tmp_path, url, "Darwin", "x86_64", shell=shell, POCKETTUI_APPS_DIR=str(apps))
    assert again.returncode == 0, again.stderr
    assert "updated" in again.stdout
    assert not (apps / "PocketTUI.app/stale").exists()
    assert "PocketTUI-mac-x64.zip" in exe.read_text()
    assert sorted(p.name for p in apps.iterdir()) == ["PocketTUI.app"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only directory")
def test_macos_falls_back_to_home_applications(tmp_path, release):
    _, url = release
    apps = tmp_path / "root" / "Applications"
    apps.mkdir(parents=True)
    apps.chmod(0o555)
    try:
        r = run(tmp_path, url, "Darwin", "arm64", POCKETTUI_APPS_DIR=str(apps))
    finally:
        apps.chmod(0o755)
    assert r.returncode == 0, r.stderr
    home_apps = tmp_path / "home" / "Applications"
    assert (home_apps / "PocketTUI.app/Contents/MacOS/PocketTUI").is_file()
    assert not list(apps.iterdir())
    assert f"{apps} is not writable" in r.stdout


def test_a_failed_download_leaves_the_old_bundle_alone(tmp_path, release):
    root, url = release
    apps = tmp_path / "Applications"
    (apps / "PocketTUI.app").mkdir(parents=True)
    (apps / "PocketTUI.app/old").write_text("still here")
    (root / "PocketTUI-mac-arm64.zip").unlink()
    r = run(tmp_path, url, "Darwin", "arm64", POCKETTUI_APPS_DIR=str(apps))
    assert r.returncode != 0
    assert "could not download" in r.stderr
    assert (apps / "PocketTUI.app/old").read_text() == "still here"

    (root / "PocketTUI-mac-arm64.zip").write_bytes(b"not a zip")
    r = run(tmp_path, url, "Darwin", "arm64", POCKETTUI_APPS_DIR=str(apps))
    assert r.returncode != 0
    assert (apps / "PocketTUI.app/old").read_text() == "still here"
    assert sorted(p.name for p in apps.iterdir()) == ["PocketTUI.app"]


def test_other_systems_are_sent_to_the_browser(tmp_path, release):
    _, url = release
    r = run(tmp_path, url, "MINGW64_NT-10.0", "x86_64")
    assert r.returncode == 1
    assert "no PocketTUI desktop app for MINGW64_NT-10.0" in r.stdout
    assert "https://pockettui.com/app/" in r.stdout
    assert not (tmp_path / "home" / ".local").exists()


def test_help_and_unknown_options(tmp_path, release):
    _, url = release
    r = run(tmp_path, url, "Linux", "x86_64", args=("--help",))
    assert r.returncode == 0 and "desktop.sh | bash" in r.stdout
    assert not (tmp_path / "home" / ".local").exists()
    r = run(tmp_path, url, "Linux", "x86_64", args=("--bogus",))
    assert r.returncode == 1 and "--bogus" in r.stderr
