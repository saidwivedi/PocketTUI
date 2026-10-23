const { app, BrowserWindow, Menu, nativeTheme, screen, session, shell } = require("electron");
const fs = require("fs");
const path = require("path");

const APP_URL = process.env.POCKETTUI_APP_URL || "https://pockettui.com/app/";
const APP_ORIGIN = new URL(APP_URL).origin;

// The shell's own Paper backgrounds (src/mobile/boot-theme.js), so the window
// is already the colour the page paints before its first frame.
const BG_LIGHT = "#FAF8F3";
const BG_DARK = "#16140f";

const GRANTED = new Set([
  "media", "notifications", "clipboard-read", "clipboard-sanitized-write", "fullscreen",
]);

const boundsFile = () => path.join(app.getPath("userData"), "window-state.json");

function readBounds() {
  try {
    const b = JSON.parse(fs.readFileSync(boundsFile(), "utf8"));
    if (![b.x, b.y, b.width, b.height].every(Number.isFinite)) return null;
    // A display that has since been unplugged would put the window off screen.
    const area = screen.getDisplayMatching(b).workArea;
    const visible = b.x < area.x + area.width && b.x + b.width > area.x
      && b.y < area.y + area.height && b.y + b.height > area.y;
    return visible ? b : { width: b.width, height: b.height, maximized: b.maximized };
  } catch (e) {
    return null;
  }
}

function saveBounds(win) {
  try {
    const b = { ...win.getNormalBounds(), maximized: win.isMaximized() };
    fs.mkdirSync(path.dirname(boundsFile()), { recursive: true });
    fs.writeFileSync(boundsFile(), JSON.stringify(b));
  } catch (e) { /* a lost size is not worth an error */ }
}

function originOf(url) {
  try { return new URL(url).origin; } catch (e) { return ""; }
}

// Mic, notifications and clipboard for the shell itself only: the browser
// pane's framed pages share this session, and a proxied site asking for the
// mic must not inherit the shell's grant.
function allowed(permission, requestingUrl, details) {
  if (!GRANTED.has(permission) || originOf(requestingUrl) !== APP_ORIGIN) return false;
  if (permission === "media") {
    const types = (details && details.mediaTypes) || [];
    return types.every((t) => t === "audio");
  }
  return true;
}

function setUpSession() {
  const ses = session.defaultSession;
  ses.setPermissionRequestHandler((wc, permission, callback, details) => {
    callback(allowed(permission, details.requestingUrl || wc.getURL(), details));
  });
  ses.setPermissionCheckHandler((wc, permission, requestingOrigin, details) =>
    allowed(permission, requestingOrigin, details));
  // No setSavePath, so Electron shows its save dialog for every download.
  ses.on("will-download", (event, item) => {
    item.once("done", (e, state) => {
      if (state === "completed" && process.platform === "darwin") {
        app.dock.downloadFinished(item.getSavePath());
      }
    });
  });
}

// Which window.open / target=_blank opens where:
// - The shell's own top frame opens outside links (openUrl in 08-links.js,
//   browserOpenOutside in 42-browser.js) with an anchor click whose referrer
//   is the app's origin. Those mean "this device's browser": shell.openExternal.
// - Everything else comes from a frame of the browser pane (a proxied page's
//   own window.open, a sign-in popup) or has no http(s) address yet
//   (about:blank written into by its opener). Those stay in the app as a
//   window of the same session, so the opener keeps its handle and cookies.
function windowOpenHandler(details) {
  const url = details.url || "";
  const fromShell = originOf(details.referrer && details.referrer.url) === APP_ORIGIN;
  if (fromShell && /^https?:/i.test(url)) {
    shell.openExternal(url);
    return { action: "deny" };
  }
  return {
    action: "allow",
    overrideBrowserWindowOptions: {
      autoHideMenuBar: true,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
    },
  };
}

// Chromium's zoom factor range is 0.25..5, which at 1.2 per level is about
// -7.6..8.8; half steps keep inside it.
const ZOOM_MIN = -7.5;
const ZOOM_MAX = 8.5;

// Ctrl+= / Ctrl++ / Ctrl+- / Ctrl+0 scale the window as in a browser tab. On
// macOS the View menu's zoom roles own Cmd+= / Cmd+- / Cmd+0 instead.
function zoomKey(wc, input) {
  if (input.type !== "keyDown" || !input.control || input.alt || input.meta) return false;
  let step;
  if (input.key === "=" || input.key === "+" || input.code === "NumpadAdd") step = 0.5;
  else if (input.key === "-" || input.code === "NumpadSubtract") step = -0.5;
  else if (input.key === "0" || input.code === "Numpad0") step = 0;
  else return false;
  const level = step === 0 ? 0 : wc.getZoomLevel() + step;
  wc.setZoomLevel(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, level)));
  return true;
}

function createWindow() {
  const saved = readBounds();
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    ...(saved || {}),
    minWidth: 360,
    minHeight: 400,
    title: "PocketTUI",
    backgroundColor: nativeTheme.shouldUseDarkColors ? BG_DARK : BG_LIGHT,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      additionalArguments: [`--pockettui-desktop-version=${app.getVersion()}`],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  if (saved && saved.maximized) win.maximize();
  win.once("ready-to-show", () => win.show());
  // The page's <title> would otherwise replace the window's name.
  win.on("page-title-updated", (e) => e.preventDefault());
  win.on("close", () => saveBounds(win));

  const wc = win.webContents;
  wc.setWindowOpenHandler(windowOpenHandler);
  // The main window stays on the app; a link that would navigate it away
  // opens in the OS browser instead.
  wc.on("will-navigate", (e, url) => {
    if (originOf(url) !== APP_ORIGIN) {
      e.preventDefault();
      if (/^https?:/i.test(url)) shell.openExternal(url);
    }
  });
  wc.on("before-input-event", (e, input) => {
    if (process.platform !== "darwin" && zoomKey(wc, input)) e.preventDefault();
  });
  win.loadURL(APP_URL);
  return win;
}

// Only chords the page has no use for get an accelerator. Close, new window,
// reload and devtools have none, so Ctrl/Cmd+W, T, N, R, Tab and the rest
// reach the terminal. Zoom does get Cmd+= / Cmd+- / Cmd+0: browsers already
// keep those for themselves, so the page never relied on them. On Linux there is no menu at all: Edit-role
// accelerators there would be Ctrl+C/V/Z/A, which the terminal needs (^C is
// SIGINT), and Chromium handles copy and paste in fields without a menu.
function buildMenu() {
  if (process.platform !== "darwin") {
    Menu.setApplicationMenu(null);
    return;
  }
  const focused = () => BrowserWindow.getFocusedWindow();
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: app.name,
      submenu: [
        { role: "about" },
        { type: "separator" },
        { role: "hide" },
        { role: "hideOthers" },
        { role: "unhide" },
        { type: "separator" },
        { role: "quit" },
      ],
    },
    {
      label: "Edit",
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "selectAll" },
      ],
    },
    {
      label: "View",
      submenu: [
        { label: "Reload", click: () => focused() && focused().webContents.reload() },
        { label: "Toggle Developer Tools", click: () => focused() && focused().webContents.toggleDevTools() },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        // The role's accelerator is Cmd+Plus (Cmd+Shift+=); this adds Cmd+=.
        { role: "zoomIn", accelerator: "CommandOrControl+=", visible: false },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    {
      label: "Window",
      submenu: [
        { role: "minimize" },
        { role: "zoom" },
        { label: "New Window", click: () => createWindow() },
        { label: "Close Window", click: () => focused() && focused().close() },
        { type: "separator" },
        { role: "front" },
      ],
    },
  ]));
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const win = BrowserWindow.getAllWindows()[0];
    if (!win) { createWindow(); return; }
    if (win.isMinimized()) win.restore();
    win.focus();
  });

  app.whenReady().then(() => {
    setUpSession();
    buildMenu();
    createWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
}
