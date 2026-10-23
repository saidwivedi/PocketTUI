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
import importlib.util
import json
import os
import shutil
import ssl
import subprocess
import threading
import time
import urllib.parse
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
    # The Basic-auth prompt main.js opens over a browser-pane guest.
    for f in ("main.js", "preload.js", "auth.html", "auth-preload.js"):
        assert f in build["files"] and (DESKTOP / f).is_file(), f
    mac = build["mac"]
    assert mac["notarize"] is False
    assert mac["identity"] == "-"
    assert mac["extendInfo"]["NSMicrophoneUsageDescription"]
    assert "com.apple.security.device.audio-input" in (DESKTOP / mac["entitlements"]).read_text()


@pytest.mark.parametrize("script", ["main.js", "preload.js", "auth-preload.js"])
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


class _Site(http.server.SimpleHTTPRequestHandler):
    """The harness's page server: static files from the site directory, plus
    /dl (an attachment) and /auth (HTTP Basic, user "u" password "p")."""

    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/dl":
            self._send(200, b"downloaded bytes", [
                ("Content-Type", "application/octet-stream"),
                ("Content-Disposition", 'attachment; filename="file.bin"')])
            return
        if self.path == "/auth":
            want = "Basic " + base64.b64encode(b"u:p").decode()
            if self.headers.get("Authorization") != want:
                self._send(401, b"no", [("WWW-Authenticate", 'Basic realm="harness"')])
                return
            self._send(200, b"<title>authed</title>secret ok",
                       [("Content-Type", "text/html")])
            return
        super().do_GET()


def _run_harness(tmp_path, harness_js, https=False, env_extra=None, page=PAGE,
                 files=None, handler=None):
    """Run harness_js (which loads the real main.js) against `page` and return
    the JSON it prints after RESULT, skipping where Electron cannot run.
    With https the page is served over TLS with a self-signed certificate
    (the harness must pass ignore-certificate-errors itself). `files` are
    more pages beside it, by name; `handler`, a request handler class to
    serve instead of the site directory."""
    electron = _electron()
    if electron is None:
        pytest.skip("Electron is not installed in desktop/node_modules")
    xvfb = shutil.which("xvfb-run")
    if not xvfb and not os.environ.get("DISPLAY"):
        pytest.skip("no display and no xvfb-run")

    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(page)
    for name, text in (files or {}).items():
        (site / name).write_text(text)
    if handler is None:
        handler = functools.partial(_Site, directory=str(site))
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


# The browser pane's <webview> tabs (46-desktop-browser.js). The page plays
# the shell: a webview in the pane's partition, its events recorded, and the
# preload's onOpenTab hooked. The harness then checks, against the real
# main.js, that the guest is in its own session with a UA that does not say
# Electron, that hiding it does not reload it, where its popups go, that a
# download in its session completes, and that a Basic-auth page is answered
# through main.js's prompt window.
WEBVIEW_PAGE = """<!doctype html><title>host</title><body>
<webview id="w" partition="persist:browse" allowpopups src="/inner.html"
  style="display:inline-flex;width:400px;height:300px"></webview>
<script>
window.ev = { starts: 0, navs: [], titles: [], opened: [] };
const w = document.getElementById("w");
w.addEventListener("did-start-loading", () => ev.starts++);
w.addEventListener("did-navigate", (e) => ev.navs.push(e.url));
w.addEventListener("page-title-updated", (e) => ev.titles.push(e.title));
window.ready = new Promise((r) => w.addEventListener("dom-ready", r, { once: true }));
if (window.pockettuiDesktop) pockettuiDesktop.browser.onOpenTab((m) => ev.opened.push(m));
</script></body>"""

INNER_PAGE = """<!doctype html><title>inner</title><body>
<a id="blank" href="/inner.html?tab" target="_blank">tab</a></body>"""

WEBVIEW_HARNESS = """
const { app, session, webContents } = require("electron");
const path = require("path");
const fs = require("fs");
app.setPath("userData", process.env.T_USERDATA);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const until = async (f, n = 50) => { for (let i = 0; i < n && !f(); i++) await wait(100); return f(); };
const r = { created: [], windows: [] };
const saveTo = path.join(process.env.T_USERDATA, "..", "file.bin");
let main = null;
app.whenReady().then(() => {
  const browse = session.fromPartition("persist:browse");
  browse.on("will-download", (e, item) => {
    item.setSavePath(saveTo);
    item.once("done", (e2, state) => { r.download = state; });
  });
});
app.on("web-contents-created", (e, c) => {
  r.created.push({ type: c.getType(),
                   browse: c.session === session.fromPartition("persist:browse"),
                   isDefault: c.session === session.defaultSession });
});
app.on("browser-window-created", (e, win) => {
  if (!main) {
    main = win;
    win.webContents.once("did-finish-load", () => run().catch((err) => {
      console.log("RESULT " + JSON.stringify({ error: String(err && err.stack || err) }));
      app.exit(1);
    }));
    return;
  }
  r.windows.push(win);
});
async function run() {
  const host = main.webContents;
  const js = (code) => host.executeJavaScript(code);
  await js("window.ready");
  const guest = webContents.fromId(await js("w.getWebContentsId()"));
  await until(() => guest.getTitle() === "inner");
  r.ua = await guest.executeJavaScript("navigator.userAgent");
  r.appName = app.name;
  r.guestBrowse = guest.session === session.fromPartition("persist:browse");
  r.guestType = guest.getType();
  await wait(300);
  const before = await js("ev.starts");
  await js("w.style.visibility = 'hidden'; w.style.pointerEvents = 'none'");
  await wait(500);
  await js("w.style.visibility = ''; w.style.pointerEvents = ''");
  await wait(300);
  r.startsAcrossHide = [before, await js("ev.starts")];
  r.ev = await js("({ navs: ev.navs, titles: ev.titles })");

  // A sized window.open: a real window in the guest's session.
  await guest.executeJavaScript(
    "window.open('/inner.html?pop', '', 'width=300,height=300'); 0", true);
  await until(() => r.windows.length >= 1);
  r.popups = r.windows.map((w) => ({
    browse: w.webContents.session === session.fromPartition("persist:browse") }));
  // A target=_blank link: no window, the host page is told instead.
  const windowsBefore = r.windows.length;
  await guest.executeJavaScript("document.getElementById('blank').click()", true);
  await wait(1000);
  r.opened = await js("ev.opened");
  r.windowsAfterLink = r.windows.length - windowsBefore;
  r.guestId = guest.id;

  // A download in the guest's session.
  await guest.executeJavaScript("location.href = '/dl'");
  await until(() => r.download);
  r.downloaded = fs.existsSync(saveTo) ? fs.readFileSync(saveTo, "utf8") : null;

  // HTTP Basic: main.js's prompt window, answered from inside it.
  const prompts = r.windows.length;
  guest.loadURL(new URL("/auth", process.env.POCKETTUI_APP_URL).href).catch(() => {});
  await until(() => r.windows.length > prompts);
  const prompt = r.windows[r.windows.length - 1];
  await until(() => prompt.webContents.getURL().endsWith("auth.html"));
  await wait(300);
  r.promptText = await prompt.webContents.executeJavaScript(
    "document.getElementById('host').textContent");
  r.promptModal = prompt.isModal();
  await prompt.webContents.executeJavaScript(
    "document.getElementById('user').value = 'u';" +
    "document.getElementById('pass').value = 'p';" +
    "document.getElementById('f').requestSubmit()");
  await until(() => guest.getTitle() === "authed");
  r.authBody = await guest.executeJavaScript("document.body.textContent");
  r.promptClosed = prompt.isDestroyed();
  delete r.windows;
  console.log("RESULT " + JSON.stringify(r));
  app.exit(0);
}
require(process.env.T_MAIN);
"""


@pytest.fixture(scope="module")
def webview_result(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("webview")
    return _run_harness(tmp_path, WEBVIEW_HARNESS, page=WEBVIEW_PAGE,
                        files={"inner.html": INNER_PAGE})


def test_a_webview_guest_runs_in_its_own_session(webview_result):
    r = webview_result
    guests = [c for c in r["created"] if c["type"] == "webview"]
    assert guests and all(g["browse"] and not g["isDefault"] for g in guests), r["created"]
    assert r["guestType"] == "webview" and r["guestBrowse"]
    assert "Electron/" not in r["ua"] and (r["appName"] + "/") not in r["ua"], r["ua"]
    assert "Chrome/" in r["ua"], r["ua"]
    assert any(u.endswith("/inner.html") for u in r["ev"]["navs"]), r["ev"]
    assert "inner" in r["ev"]["titles"], r["ev"]
    # Hidden with visibility, not display, the guest keeps its page.
    before, after = r["startsAcrossHide"]
    assert before == after, r["startsAcrossHide"]


def test_a_guest_popup_is_a_window_and_a_blank_link_a_tab(webview_result):
    r = webview_result
    assert r["popups"] == [{"browse": True}], r["popups"]
    assert r["windowsAfterLink"] == 0
    assert len(r["opened"]) == 1, r["opened"]
    msg = r["opened"][0]
    assert msg["id"] == r["guestId"] and msg["url"].endswith("/inner.html?tab")
    assert msg["background"] is False


def test_a_guest_download_completes(webview_result):
    assert webview_result["download"] == "completed"
    assert webview_result["downloaded"] == "downloaded bytes"


def test_a_basic_auth_page_is_answered_through_the_prompt(webview_result):
    r = webview_result
    assert "asks for a username and password" in r["promptText"], r["promptText"]
    assert r["promptModal"] is True
    assert r["authBody"] == "secret ok"
    assert r["promptClosed"] is True



# The real shell (the assembled mobile_app.html, served same-origin as app.py
# serves it) in the real main.js, against a stand-in backend: /api/version
# says the computer can browse, /api/browse mints, /b/<tok>/enter is the
# Clear-Site-Data hop and /b/<tok>/h/<host:port>/... a proxied page. A public
# address (pub.example.net, mapped to a local server by the harness) loads in
# the webview as itself; localhost goes through the proxy as the tab flavour.
def _assembled_shell():
    spec = importlib.util.spec_from_file_location("build_mobile", REPO / "build_mobile.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    html = mod.assemble()
    for k, v in (("__CACHE_VERSION__", "test"), ("__APP_VERSION__", ""),
                 ("__BACKEND_URL__", "same-origin"), ("__TEST_GATE__", "")):
        html = html.replace(k, v)
    return html.encode()


def _shell_backend(shell, hits):
    class Backend(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", headers=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path
            hits.append(path)
            if path == "/" or path.startswith("/?"):
                self._send(200, shell, "text/html")
            elif path.startswith("/vendor/"):
                f = REPO / "vendor" / path[len("/vendor/"):].split("?")[0]
                if f.is_file():
                    self._send(200, f.read_bytes(), "text/css" if f.suffix == ".css"
                               else "application/javascript")
                else:
                    self._send(404, b"{}")
            elif path.startswith("/api/version"):
                self._send(200, json.dumps({"version": "0.9.999", "capabilities": {
                    "browse": True, "browse_tab": True}}).encode())
            elif path.startswith("/b/tt/enter?to="):
                to = urllib.parse.unquote(path.split("to=", 1)[1])
                self._send(302, b"", headers=[("Location", "/b/tt" + to),
                                              ("Clear-Site-Data", '"storage"')])
            elif path.startswith("/b/"):
                self._send(200, b"<!doctype html><title>Proxied</title>proxied page",
                           "text/html")
            else:
                self._send(404, b"{}")

        def do_POST(self):
            hits.append("POST " + self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path.startswith("/api/browse"):
                tok = "tt" if body.get("mode") == "tab" else "tp"
                self._send(200, json.dumps({"token": tok, "prefix": "",
                                            "expires": int(time.time()) + 3600}).encode())
            else:
                self._send(404, b"{}")
    return Backend


PUB_ONE = """<!doctype html><title>One</title><body>
<a id="next" href="/two.html">next</a>
<a id="tab" href="/two.html?tab" target="_blank">tab</a></body>"""
PUB_TWO = """<!doctype html><title>Two</title><body>two</body>"""

SHELL_HARNESS = """
const { app, webContents } = require("electron");
app.setPath("userData", process.env.T_USERDATA);
app.commandLine.appendSwitch("host-resolver-rules", "MAP pub.example.net 127.0.0.1");
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const PUB = "http://pub.example.net:" + process.env.T_PUB_PORT;
let main = null;
app.on("browser-window-created", (e, win) => {
  if (main) return;
  main = win;
  win.webContents.once("did-finish-load", () => run().catch((err) => {
    console.log("RESULT " + JSON.stringify({ error: String(err && err.stack || err) }));
    app.exit(1);
  }));
});
async function run() {
  const wc = main.webContents;
  const js = (c) => wc.executeJavaScript(c);
  const until = async (code, n = 80) => {
    for (let i = 0; i < n; i++) {
      if (await js(code)) return true;
      await wait(100);
    }
    return false;
  };
  const r = {};
  // Paired before the boot that matters: a token, then a reload.
  await js("localStorage.setItem('pockettui_token', 'x'); 0");
  const loaded = new Promise((res) => wc.once("did-finish-load", res));
  wc.reload();
  await loaded;
  await js("fetchServerVersion()");
  r.caps = await until("hasCapStrict('browse') && hasCapStrict('browse_tab')");
  const word = "(document.querySelector('#screen-browser .tab-chip.on .tab-word') || {}).textContent";
  const state = `(() => {
    const s = document.getElementById("screen-browser");
    const ws = [...s.querySelectorAll("#browser-wrap webview")];
    return {
      webviews: ws.length,
      off: ws.map((w) => w.classList.contains("off")),
      partitions: ws.map((w) => w.getAttribute("partition")),
      frames: s.querySelectorAll("#browser-wrap iframe").length,
      field: s.querySelector("#browser-url").value,
      back: s.querySelector("#btn-browser-back").disabled,
      fwd: s.querySelector("#btn-browser-fwd").disabled,
      monitor: getComputedStyle(s.querySelector("#btn-browser-full")).display,
      mode: getComputedStyle(s.querySelector(".browser-mode")).display,
      desktop: s.classList.contains("desktop"),
      word: ${word},
      tabs: s.querySelectorAll(".tab-chip").length,
    };
  })()`;
  await js(`openBrowser(${JSON.stringify(PUB + "/one.html")}); 0`);
  r.oneLoaded = await until(word + " === 'One'");
  r.one = await js(state);
  const guest = async () => webContents.fromId(await js(
    "document.querySelector('#screen-browser webview:not(.off)').getWebContentsId()"));
  await (await guest()).executeJavaScript("document.getElementById('next').click()", true);
  r.twoLoaded = await until(word + " === 'Two'");
  r.two = await js(state);
  await js("document.querySelector('#screen-browser #btn-browser-back').click()");
  r.backLoaded = await until(word + " === 'One'");
  r.back = await js(state);
  // A target=_blank link in the guest: a second tab of the pane, in front.
  await (await guest()).executeJavaScript("document.getElementById('tab').click()", true);
  r.tabOpened = await until(word + " === 'Two' && document.querySelectorAll('#screen-browser .tab-chip').length === 2");
  r.newTab = await js(state);
  // An address only the computer reaches: through the proxy, the capsule
  // still naming the address itself.
  await js("openBrowser('http://localhost:4999/x'); 0");
  r.proxiedLoaded = await until(word + " === 'Proxied'");
  r.proxied = await js(state);
  r.guestUrl = (await guest()).getURL();
  r.setting = await js(`browserSyncSetting().then(() => ({
    line: document.getElementById("browser-status-line").textContent,
    row: getComputedStyle(document.getElementById("browser-stream-toggle")
                          .closest(".toggle-row")).display,
    clearHidden: document.getElementById("btn-browser-clear").hidden,
  }))`);
  r.cleared = await js("browserClearProfile()");
  console.log("RESULT " + JSON.stringify(r));
  app.exit(0);
}
require(process.env.T_MAIN);
"""


def test_the_shell_puts_a_webview_in_the_browser_pane(tmp_path):
    pub_dir = tmp_path / "pub"
    pub_dir.mkdir()
    (pub_dir / "one.html").write_text(PUB_ONE)
    (pub_dir / "two.html").write_text(PUB_TWO)
    pub = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Site, directory=str(pub_dir)))
    threading.Thread(target=pub.serve_forever, daemon=True).start()
    hits = []
    try:
        r = _run_harness(tmp_path, SHELL_HARNESS,
                         handler=_shell_backend(_assembled_shell(), hits),
                         env_extra={"T_PUB_PORT": str(pub.server_port)})
    finally:
        pub.shutdown()
    assert "error" not in r, r.get("error")
    assert r["caps"]
    host = f"pub.example.net:{pub.server_port}"
    assert r["oneLoaded"], r["one"]
    one = r["one"]
    assert one["webviews"] == 1 and one["partitions"] == ["persist:browse"], one
    assert one["frames"] == 0, one
    assert one["desktop"] and one["monitor"] == "none" and one["mode"] == "none", one
    assert one["field"] == host, one
    assert (one["back"], one["fwd"]) == (True, True), one
    assert r["twoLoaded"] and (r["two"]["back"], r["two"]["fwd"]) == (False, True), r["two"]
    assert r["backLoaded"] and (r["back"]["back"], r["back"]["fwd"]) == (True, False), r["back"]
    assert r["tabOpened"], r["newTab"]
    assert r["newTab"]["webviews"] == 2 and r["newTab"]["off"] == [True, False], r["newTab"]
    assert r["proxiedLoaded"], r["proxied"]
    assert r["proxied"]["field"] == "localhost:4999", r["proxied"]
    assert "/b/tt/h/localhost:4999/x" in r["guestUrl"], r["guestUrl"]
    # The tab flavour's mint and the /enter hop on the tab's first proxied load.
    assert "POST /api/browse" in hits
    assert any(h.startswith("/b/tt/enter?to=") for h in hits), hits
    # The public pages never went near the backend.
    assert not any("pub.example.net" in h for h in hits), hits
    assert r["setting"] == {"line": "Pages open in this app's own browser",
                            "row": "none", "clearHidden": False}, r["setting"]
    assert r["cleared"] is True
