"""The desktop app in desktop/: an Electron window around the hosted shell.

The build config is checked as data, the scripts with `node --check`, and, where
Electron and a display are available, the real main.js is launched against a
local page to prove that the chords a browser keeps for itself (Ctrl+W, T, N,
Shift+N, Tab) reach the page and do not close the window.
"""

import base64
import functools
import hashlib
import http.server
import json
import os
import shutil
import ssl
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


def _self_signed(tmp_path):
    """A throwaway certificate for 127.0.0.1, for serving the page over https."""
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl is not installed")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-subj", "/CN=127.0.0.1"], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


def _run_harness(tmp_path, harness_js, https=False, env_extra=None):
    """Run harness_js (which loads the real main.js) against PAGE and return
    the JSON it prints after RESULT, skipping where Electron cannot run.
    With https the page is served over TLS with a self-signed certificate
    (the harness must pass ignore-certificate-errors itself)."""
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
    if https:
        server.socket = _self_signed(tmp_path).wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    harness = tmp_path / "harness.js"
    harness.write_text(harness_js)
    scheme = "https" if https else "http"
    env = dict(os.environ,
               POCKETTUI_APP_URL=f"{scheme}://127.0.0.1:{server.server_port}/",
               T_USERDATA=str(tmp_path / "userdata"),
               T_MAIN=str(DESKTOP / "main.js"),
               ELECTRON_DISABLE_SECURITY_WARNINGS="1",
               **(env_extra or {}))
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


class _Backend(http.server.BaseHTTPRequestHandler):
    """A plain-http stand-in for the user's backend: GET /ping answers with
    CORS, and /ws completes a WebSocket handshake and sends one text frame."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            accept = base64.b64encode(hashlib.sha1(
                (self.headers["Sec-WebSocket-Key"]
                 + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.write(b"\x81\x05hello")
            self.wfile.flush()
            self.rfile.read(2)  # hold the socket until the client closes
            return
        body = b"pong"
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# The page is https (as pockettui.com/app/ is) and calls a plain-http backend
# by a name that is not loopback (host-resolver-rules points it at the test
# server), so Chromium's mixed-content rule applies to it. The main window
# must get through; a popup from it and a window with default preferences
# must not, which also proves the check can fail. The switches are the
# harness's own, never main.js's.
HTTP_BACKEND_HARNESS = """
const { app, BrowserWindow } = require("electron");
app.setPath("userData", process.env.T_USERDATA);
app.commandLine.appendSwitch("ignore-certificate-errors");
app.commandLine.appendSwitch("host-resolver-rules", "MAP backend.example.net 127.0.0.1");
// The page counts as public, as pockettui.com is, so a Chromium that enforces
// Local Network Access treats the backend call as public-to-local.
app.commandLine.appendSwitch("ip-address-space-overrides",
  "127.0.0.1:" + new URL(process.env.POCKETTUI_APP_URL).port + "=public");
const B = "http://backend.example.net:" + process.env.T_BACKEND_PORT;
const CHECK = `(async () => {
  const out = {};
  try { out.fetch = await (await fetch("${B}/ping")).text(); }
  catch (e) { out.fetch = "error"; }
  out.ws = await new Promise((res) => {
    try {
      const ws = new WebSocket("${B.replace("http", "ws")}/ws");
      ws.onmessage = (e) => { res(e.data); ws.close(); };
      ws.onerror = () => res("error");
      setTimeout(() => res("timeout"), 5000);
    } catch (e) { res("error"); }
  });
  return out;
})()`;
let first = true;
app.on("browser-window-created", (e, win) => {
  if (!first) return;
  first = false;
  win.webContents.once("did-finish-load", async () => {
    const wc = win.webContents;
    const result = { main: await wc.executeJavaScript(CHECK) };
    // A popup as the browser pane opens one: no referrer from the shell, so
    // windowOpenHandler keeps it in the app.
    const popup = await new Promise((res) => {
      wc.once("did-create-window", (child) =>
        child.webContents.once("did-finish-load", () => res(child)));
      wc.executeJavaScript(
        "window.open(location.href + '?popup', '_blank', 'noreferrer')", true);
    });
    result.popup = await popup.webContents.executeJavaScript(CHECK);
    const plain = new BrowserWindow({ show: false,
      webPreferences: { sandbox: true, contextIsolation: true } });
    await plain.loadURL(process.env.POCKETTUI_APP_URL);
    result.plain = await plain.webContents.executeJavaScript(CHECK);
    console.log("RESULT " + JSON.stringify(result));
    app.exit(0);
  });
});
require(process.env.T_MAIN);
"""


def test_https_page_reaches_a_plain_http_backend(tmp_path):
    backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Backend)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    try:
        result = _run_harness(tmp_path, HTTP_BACKEND_HARNESS, https=True,
                              env_extra={"T_BACKEND_PORT": str(backend.server_port)})
    finally:
        backend.shutdown()
    assert result["main"] == {"fetch": "pong", "ws": "hello"}, result
    assert result["popup"] == {"fetch": "error", "ws": "error"}, result
    assert result["plain"] == {"fetch": "error", "ws": "error"}, result


# The page's notification permission, granted to the app's origin by main.js
# without a prompt; and the bridge the page's notification click calls.
NOTIFY_HARNESS = """
const { app } = require("electron");
app.setPath("userData", process.env.T_USERDATA);
app.on("browser-window-created", (e, win) => {
  win.webContents.once("did-finish-load", async () => {
    const result = await win.webContents.executeJavaScript(`(async () => ({
      before: Notification.permission,
      asked: await Notification.requestPermission(),
      after: Notification.permission,
      focus: typeof (window.pockettuiDesktop || {}).focus,
    }))()`);
    console.log("RESULT " + JSON.stringify(result));
    app.exit(0);
  });
});
require(process.env.T_MAIN);
"""


def test_notifications_are_granted_to_the_app(tmp_path):
    result = _run_harness(tmp_path, NOTIFY_HARNESS)
    assert result == {"before": "granted", "asked": "granted", "after": "granted",
                      "focus": "function"}, result


# The global hotkey is registered (the default, or window-state.json's
# `hotkey`), and the function it calls hides a focused window and brings a
# hidden one back. Pressing the real chord needs an X server that delivers
# grabbed keys, so where xdotool is present it is pressed too. Closing the
# window on Linux, with the tray up, hides it rather than quitting, and the
# bounds saved on the way keep the hand-set hotkey.
HOTKEY_HARNESS = """
const { app, globalShortcut } = require("electron");
const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");
app.setPath("userData", process.env.T_USERDATA);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const until = async (f) => { for (let i = 0; i < 30 && !f(); i++) await wait(100); return f(); };
let main;
app.on("browser-window-created", (e, win) => {
  win.once("ready-to-show", async () => {
    await until(() => win.isVisible());
    win.focus();
    await until(() => win.isFocused());
    const r = { focused: win.isFocused(),
                registered: globalShortcut.isRegistered(process.env.T_HOTKEY),
                defaultRegistered: globalShortcut.isRegistered("CommandOrControl+Shift+Space") };
    r.first = main.toggleWindow();
    r.hiddenAfterFirst = await until(() => !win.isVisible());
    r.second = main.toggleWindow();
    r.visibleAfterSecond = await until(() => win.isVisible());
    if (process.env.T_XDOTOOL) {
      win.focus();
      await until(() => win.isFocused());
      await new Promise((res) => execFile(process.env.T_XDOTOOL,
        ["key", "ctrl+shift+space"], () => res()));
      r.chordHid = await until(() => !win.isVisible());
    }
    win.show();
    win.close();
    await wait(300);
    r.closeKeptApp = !win.isDestroyed() && !win.isVisible();
    r.saved = JSON.parse(fs.readFileSync(
      path.join(process.env.T_USERDATA, "window-state.json"), "utf8"));
    console.log("RESULT " + JSON.stringify(r));
    app.exit(0);
  });
});
main = require(process.env.T_MAIN);
"""


@pytest.mark.parametrize("hotkey", [None, "CommandOrControl+Shift+K"])
def test_hotkey_toggles_the_window(tmp_path, hotkey):
    userdata = tmp_path / "userdata"
    userdata.mkdir()
    if hotkey:
        (userdata / "window-state.json").write_text(json.dumps({"hotkey": hotkey}))
    expect = hotkey or "CommandOrControl+Shift+Space"
    xdotool = shutil.which("xdotool")
    r = _run_harness(tmp_path, HOTKEY_HARNESS, env_extra={
        "T_HOTKEY": expect, "T_XDOTOOL": xdotool if xdotool and not hotkey else ""})
    assert r["registered"], r
    assert r["defaultRegistered"] is (hotkey is None), r
    assert r["focused"], r
    assert (r["first"], r["hiddenAfterFirst"]) == ("hidden", True), r
    assert (r["second"], r["visibleAfterSecond"]) == ("shown", True), r
    if xdotool and not hotkey:
        assert r["chordHid"], r
    assert r["closeKeptApp"], r
    assert r["saved"].get("hotkey") == hotkey and "width" in r["saved"], r
