"""The desktop app in desktop/: an Electron window around the hosted shell.

The build config is checked as data, the scripts with `node --check`, and, where
Electron and a display are available, the real main.js is launched against a
local page to prove that the chords a browser keeps for itself (Ctrl+W, T, N,
Shift+N, Tab) reach the page and do not close the window.
"""

import functools
import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DESKTOP = REPO / "desktop"


@pytest.fixture(scope="module")
def pkg():
    return json.loads((DESKTOP / "package.json").read_text())


def test_package_names_the_targets(pkg):
    assert pkg["name"] == "pockettui-desktop"
    for script in ("start", "dist:linux", "dist:mac"):
        assert script in pkg["scripts"]
    for dep in ("electron", "electron-builder"):
        assert pkg["devDependencies"][dep][0].isdigit(), "pin an exact version"
    build = pkg["build"]
    assert build["appId"] == "com.pockettui.desktop"
    assert build["productName"] == "PocketTUI"
    assert (DESKTOP / build["icon"]).is_file()

    def targets(platform):
        return {t["target"]: set(t["arch"]) for t in build[platform]["target"]}

    assert targets("mac") == {"dmg": {"arm64", "x64"}, "zip": {"arm64", "x64"}}
    assert targets("linux") == {"AppImage": {"x64", "arm64"}, "deb": {"x64", "arm64"}}
    # No version in the file name: pockettui.com/download/* redirects to
    # releases/latest/download/<name>, so the names are permanent links.
    assert build["artifactName"] == "PocketTUI-${os}-${arch}.${ext}"
    mac = build["mac"]
    assert mac["notarize"] is False
    assert mac["identity"] == "-"
    assert mac["extendInfo"]["NSMicrophoneUsageDescription"]
    assert "com.apple.security.device.audio-input" in (DESKTOP / mac["entitlements"]).read_text()


@pytest.mark.parametrize("script", ["main.js", "preload.js"])
def test_scripts_parse(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    subprocess.run([node, "--check", str(DESKTOP / script)], check=True)


def _electron():
    mod = DESKTOP / "node_modules" / "electron"
    try:
        rel = (mod / "path.txt").read_text().strip()
    except OSError:
        return None
    exe = mod / "dist" / rel
    return exe if exe.is_file() else None


PAGE = """<!doctype html><title>keys</title><body><script>
window.keys = [];
addEventListener("keydown", (e) => keys.push(
  [e.key, e.ctrlKey, e.shiftKey, e.metaKey].join(",")));
</script></body>"""

# Loads the real main.js, presses the chords into its window once the page is
# up, and prints what the page saw and whether the window is still open.
HARNESS = """
const { app } = require("electron");
app.setPath("userData", process.env.T_USERDATA);
app.on("browser-window-created", (e, win) => {
  win.on("closed", () => console.log('RESULT {"open": false, "keys": []}'));
  win.webContents.once("did-finish-load", async () => {
    const wc = win.webContents;
    // Input sent the instant the page loads can land before the renderer
    // takes keys.
    await new Promise((r) => setTimeout(r, 1000));
    wc.focus();
    const press = (key, modifiers) => {
      wc.sendInputEvent({ type: "keyDown", keyCode: key, modifiers });
      wc.sendInputEvent({ type: "keyUp", keyCode: key, modifiers });
    };
    press("w", ["control"]);
    press("t", ["control"]);
    press("n", ["control"]);
    press("n", ["control", "shift"]);
    press("Tab", ["control"]);
    await new Promise((r) => setTimeout(r, 1500));
    const keys = win.isDestroyed() ? [] : await wc.executeJavaScript("window.keys");
    console.log("RESULT " + JSON.stringify({ open: !win.isDestroyed(), keys }));
    app.exit(0);
  });
});
require(process.env.T_MAIN);
"""


ENV_FAILURES = ("Missing X server", "cannot open display", "SUID sandbox",
                "error while loading shared libraries")


def _run_harness(tmp_path, harness_js):
    """Run harness_js (which loads the real main.js) against PAGE and return
    the JSON it prints after RESULT, skipping where Electron cannot run."""
    electron = _electron()
    if electron is None:
        pytest.skip("Electron is not installed in desktop/node_modules")
    xvfb = shutil.which("xvfb-run")
    if not xvfb and not os.environ.get("DISPLAY"):
        pytest.skip("no display and no xvfb-run")

    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(PAGE)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(site))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    harness = tmp_path / "harness.js"
    harness.write_text(harness_js)
    env = dict(os.environ,
               POCKETTUI_APP_URL=f"http://127.0.0.1:{server.server_port}/",
               T_USERDATA=str(tmp_path / "userdata"),
               T_MAIN=str(DESKTOP / "main.js"),
               ELECTRON_DISABLE_SECURITY_WARNINGS="1")
    cmd = [str(electron), str(harness)]
    if xvfb:
        cmd = [xvfb, "-a", "-s", "-screen 0 1280x800x24"] + cmd
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              timeout=60)
    except subprocess.TimeoutExpired:
        pytest.fail("Electron did not finish within 60 s")
    finally:
        server.shutdown()
    lines = [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")]
    if not lines:
        # A machine that cannot run Chromium at all is a missing environment;
        # anything else (main.js throwing, the page never loading) is a failure.
        if any(s in proc.stderr for s in ENV_FAILURES):
            pytest.skip("Electron cannot run here: " + proc.stderr[-300:])
        pytest.fail("no result from Electron:\n" + proc.stderr[-2000:])
    return json.loads(lines[0][len("RESULT "):])


def test_browser_chords_reach_the_page(tmp_path):
    result = _run_harness(tmp_path, HARNESS)
    assert result["open"], "Ctrl+W closed the window"
    for key in ("w,true,false,false", "t,true,false,false",
                "n,true,false,false", "N,true,true,false", "Tab,true,false,false"):
        assert key in result["keys"], (key, result["keys"])


# Ctrl+= zooms in, Ctrl+- out and Ctrl+0 resets, as in a browser tab (the
# Linux path; macOS uses the View menu's zoom roles, which need a real menu
# bar). Each step records the zoom level and what the page measures.
ZOOM_HARNESS = """
const { app } = require("electron");
app.setPath("userData", process.env.T_USERDATA);
app.on("browser-window-created", (e, win) => {
  win.webContents.once("did-finish-load", async () => {
    const wc = win.webContents;
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    await wait(1000);
    wc.focus();
    const steps = [];
    const read = async (name) => steps.push({ name, level: wc.getZoomLevel(),
      ...(await wc.executeJavaScript(
        "({ dpr: devicePixelRatio, width: innerWidth, keys: window.keys.length })")) });
    const press = async (key, modifiers) => {
      wc.sendInputEvent({ type: "keyDown", keyCode: key, modifiers });
      wc.sendInputEvent({ type: "keyUp", keyCode: key, modifiers });
      await wait(500);
    };
    await read("start");
    await press("=", ["control"]);
    await read("in");
    await press("=", ["control", "shift"]);
    await read("in2");
    await press("-", ["control"]);
    await read("out");
    await press("0", ["control"]);
    await read("reset");
    console.log("RESULT " + JSON.stringify(steps));
    app.exit(0);
  });
});
require(process.env.T_MAIN);
"""


def test_ctrl_plus_minus_zero_zoom_the_window(tmp_path):
    steps = {s["name"]: s for s in _run_harness(tmp_path, ZOOM_HARNESS)}
    start, zin, zin2, out, reset = (steps[n] for n in ("start", "in", "in2", "out", "reset"))
    assert start["level"] == 0
    assert zin["level"] == 0.5
    assert zin["dpr"] > start["dpr"] and zin["width"] < start["width"], (start, zin)
    assert zin2["level"] == 1.0
    assert out["level"] == 0.5
    assert reset["level"] == 0
    assert reset["dpr"] == start["dpr"] and reset["width"] == start["width"], (start, reset)
    # The zoom chords are consumed, so the terminal never sees them.
    assert reset["keys"] == start["keys"]
