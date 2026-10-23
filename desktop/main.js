const {
  app, BrowserWindow, globalShortcut, ipcMain, Menu, nativeImage, nativeTheme, screen,
  session, shell, Tray,
} = require("electron");
const fs = require("fs");
const path = require("path");

const APP_URL = process.env.POCKETTUI_APP_URL || "https://pockettui.com/app/";
const APP_ORIGIN = new URL(APP_URL).origin;

// The shell's own Paper backgrounds (src/mobile/boot-theme.js), so the window
// is already the colour the page paints before its first frame.
const BG_LIGHT = "#FAF8F3";
const BG_DARK = "#16140f";

// The local-network names are Chromium's Local Network Access gate on a
// public page (pockettui.com) calling the user's own backend on a tailnet,
// LAN or loopback address.
const GRANTED = new Set([
  "media", "notifications", "clipboard-read", "clipboard-sanitized-write", "fullscreen",
  "local-network-access", "local-network", "loopback-network",
]);

const DEFAULT_HOTKEY = "CommandOrControl+Shift+Space";

const boundsFile = () => path.join(app.getPath("userData"), "window-state.json");

// window-state.json holds the window's bounds and, set by hand, `hotkey`.
function readState() {
  try {
    const s = JSON.parse(fs.readFileSync(boundsFile(), "utf8"));
    return s && typeof s === "object" ? s : {};
  } catch (e) {
    return {};
  }
}

function readBounds() {
  try {
    const b = readState();
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
    const b = { ...readState(), ...win.getNormalBounds(), maximized: win.isMaximized() };
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

// No setSavePath, so Electron shows its save dialog for every download.
function onDownload(event, item) {
  item.once("done", (e, state) => {
    if (state === "completed" && process.platform === "darwin") {
      app.dock.downloadFinished(item.getSavePath());
    }
  });
}

function setUpSession() {
  const ses = session.defaultSession;
  ses.setPermissionRequestHandler((wc, permission, callback, details) => {
    callback(allowed(permission, details.requestingUrl || wc.getURL(), details));
  });
  ses.setPermissionCheckHandler((wc, permission, requestingOrigin, details) =>
    allowed(permission, requestingOrigin, details));
  ses.on("will-download", onDownload);
}

// The browser pane's <webview> tabs: their own persistent session, so logins
// and cookies survive a restart and never mix with the shell's.
const BROWSE_PARTITION = "persist:browse";
const BROWSE_GRANTED = new Set(["fullscreen", "clipboard-sanitized-write"]);
let browse = null;

function setUpBrowse() {
  browse = session.fromPartition(BROWSE_PARTITION);
  browse.setPermissionRequestHandler((wc, permission, callback) =>
    callback(BROWSE_GRANTED.has(permission)));
  browse.setPermissionCheckHandler((wc, permission) => BROWSE_GRANTED.has(permission));
  browse.on("will-download", onDownload);
  // Sites (Google's sign-in among them) refuse a browser that names itself
  // Electron; the app's own name/version token goes with it.
  const name = app.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  browse.setUserAgent(app.userAgentFallback.replace(
    new RegExp(" (" + name + "|Electron)/\\S+", "g"), ""));
}

// A guest's window.open / target=_blank. A sized popup (an OAuth sign-in) is a
// real window in the same session, so the opener keeps its handle; a link
// meant for a new tab becomes a tab of the pane that holds the guest.
function guestOpen(contents, details) {
  const url = details.url || "";
  const tab = details.disposition === "foreground-tab" || details.disposition === "background-tab";
  if (tab && contents.getType() === "webview" && contents.hostWebContents
      && /^https?:/i.test(url)) {
    contents.hostWebContents.send("pockettui:browser-open", {
      id: contents.id, url, background: details.disposition === "background-tab",
    });
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

// HTTP Basic/Digest auth for a guest: a small modal over the window holding
// it. The prompt's preload talks back on channels keyed by its own
// webContents, so each answer reaches only the request that asked.
const authPrompts = new Map();

function guestLogin(contents, event, details, authInfo, callback) {
  event.preventDefault();
  const host = contents.hostWebContents || contents;
  const parent = BrowserWindow.fromWebContents(host);
  const win = new BrowserWindow({
    parent: parent || undefined,
    modal: !!parent,
    width: 380,
    height: 250,
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    autoHideMenuBar: true,
    title: "Sign in",
    backgroundColor: nativeTheme.shouldUseDarkColors ? BG_DARK : BG_LIGHT,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "auth-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  const id = win.webContents.id;
  let done = false;
  const answer = (user, pass) => {
    if (done) return;
    done = true;
    authPrompts.delete(id);
    if (typeof user === "string") callback(user, typeof pass === "string" ? pass : "");
    else callback();
    if (!win.isDestroyed()) win.close();
  };
  authPrompts.set(id, {
    info: { host: authInfo.host + (authInfo.port ? ":" + authInfo.port : ""),
            realm: authInfo.realm || "", proxy: !!authInfo.isProxy },
    answer,
  });
  win.on("closed", () => answer());
  win.once("ready-to-show", () => win.show());
  win.loadFile(path.join(__dirname, "auth.html"));
}

ipcMain.handle("pockettui:auth-info", (e) => {
  const p = authPrompts.get(e.sender.id);
  return p ? p.info : null;
});
ipcMain.on("pockettui:auth-submit", (e, user, pass) => {
  const p = authPrompts.get(e.sender.id);
  if (p) p.answer(String(user || ""), String(pass || ""));
});
ipcMain.on("pockettui:auth-cancel", (e) => {
  const p = authPrompts.get(e.sender.id);
  if (p) p.answer();
});

// Settings' "Clear browsing data", for the pane's own session only.
ipcMain.handle("pockettui:browser-clear", async (e) => {
  if (originOf(e.sender.getURL()) !== APP_ORIGIN || !browse) return false;
  await browse.clearStorageData();
  await browse.clearCache();
  await browse.clearAuthCache();
  return true;
});

app.on("web-contents-created", (e, contents) => {
  if (!browse || contents.session !== browse) return;
  contents.setWindowOpenHandler((details) => guestOpen(contents, details));
  contents.on("login", (event, details, authInfo, callback) =>
    guestLogin(contents, event, details, authInfo, callback));
});

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
      // A child window inherits its opener's webPreferences; the main
      // window's insecure-content allowance is for the shell alone.
      webPreferences: {
        contextIsolation: true, nodeIntegration: false, sandbox: true,
        allowRunningInsecureContent: false, webviewTag: false,
      },
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

// Windows createWindow made, as opposed to browser-pane popups.
const mainWindows = new Set();
let tray = null;
let quitting = false;

function mainWindow() {
  for (const w of mainWindows) if (!w.isDestroyed()) return w;
  return null;
}

function showWindow(win) {
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

// The global hotkey and the tray: bring the window forward, or put it away
// when it is already in front. Returns what it did, for the tests.
function toggleWindow() {
  const win = mainWindow();
  if (!win) { createWindow(); return "created"; }
  if (win.isVisible() && win.isFocused()) {
    // app.hide hands focus back to the app that had it before.
    if (process.platform === "darwin") app.hide(); else win.hide();
    return "hidden";
  }
  showWindow(win);
  return "shown";
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
      // The page is https and the user's backend is often plain http on the
      // tailnet or LAN; Chromium would block those calls as mixed content.
      allowRunningInsecureContent: true,
      // The browser pane's tabs (46-desktop-browser.js); will-attach-webview
      // below decides what a guest may be.
      webviewTag: true,
    },
  });
  mainWindows.add(win);
  win.on("closed", () => mainWindows.delete(win));
  if (saved && saved.maximized) win.maximize();
  win.once("ready-to-show", () => win.show());
  // The page's <title> would otherwise replace the window's name.
  win.on("page-title-updated", (e) => e.preventDefault());
  win.on("close", (e) => {
    saveBounds(win);
    // On Linux, with a tray to come back through, closing puts the window
    // away instead of quitting. macOS keeps the app in the dock either way.
    if (tray && !quitting && process.platform !== "darwin") {
      e.preventDefault();
      win.hide();
    }
  });

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
  // Only the app's own page gets guests, only in the pane's session, and
  // never with a preload or Node.
  wc.on("will-attach-webview", (e, prefs, params) => {
    if (originOf(wc.getURL()) !== APP_ORIGIN || params.partition !== BROWSE_PARTITION) {
      e.preventDefault();
      return;
    }
    delete prefs.preload;
    delete prefs.preloadURL;
    prefs.nodeIntegration = false;
    prefs.contextIsolation = true;
    prefs.sandbox = true;
    prefs.allowRunningInsecureContent = false;
    prefs.webviewTag = false;
    // Chromium's PDF viewer is a plugin.
    prefs.plugins = true;
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

function prettyAccelerator(acc) {
  const mod = process.platform === "darwin" ? "Cmd" : "Ctrl";
  return acc.replace(/CommandOrControl|CmdOrCtrl/g, mod);
}

function registerHotkey() {
  const set = readState().hotkey;
  const hotkey = typeof set === "string" && set ? set : DEFAULT_HOTKEY;
  let ok = false;
  try { ok = globalShortcut.register(hotkey, toggleWindow); } catch (e) { /* bad accelerator */ }
  return { hotkey, ok };
}

function createTray(hotkey) {
  // trayTemplate.png is a black silhouette that macOS tints for the menu
  // bar; Linux panels get the coloured icon.
  const file = process.platform === "darwin" ? "trayTemplate.png" : "tray.png";
  tray = new Tray(nativeImage.createFromPath(path.join(__dirname, "tray", file)));
  tray.setToolTip("PocketTUI");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Show PocketTUI", click: () => {
      const win = mainWindow();
      if (win) showWindow(win); else createWindow();
    } },
    { label: hotkey.ok ? `Show or hide: ${prettyAccelerator(hotkey.hotkey)}`
      : `Hotkey ${prettyAccelerator(hotkey.hotkey)} is unavailable`, enabled: false },
    { type: "separator" },
    { label: "Quit", click: () => app.quit() },
  ]));
}

// A notification click from the page (30-notify.js) brings its window up.
ipcMain.on("pockettui:focus", (e) => {
  const win = BrowserWindow.fromWebContents(e.sender);
  if (win && mainWindows.has(win) && originOf(e.sender.getURL()) === APP_ORIGIN) showWindow(win);
});

if (require.main !== module) module.exports = { toggleWindow, mainWindow, registerHotkey };

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const win = mainWindow();
    if (!win) { createWindow(); return; }
    showWindow(win);
  });

  app.whenReady().then(() => {
    setUpSession();
    setUpBrowse();
    buildMenu();
    createWindow();
    createTray(registerHotkey());
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on("before-quit", () => { quitting = true; });
  app.on("will-quit", () => globalShortcut.unregisterAll());

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
}
