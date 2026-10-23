// ============================================================
// A browser pane tab in the desktop app: a real <webview>
// ============================================================
// Inside the desktop app (desktop/main.js) a tab of the browser pane is an
// Electron <webview> in its own persistent session ("persist:browse"), so
// logins, cookies, captchas, popups and downloads behave as they do in Chrome.
// Phones and plain browsers never get here: 42-browser.js keeps its proxy
// frames and streamed tabs, and branches on tab.web wherever this replaces
// them. main.js decides what a guest may be (no preload, no Node, sandboxed),
// opens its sized popups as windows, answers its Basic-auth prompts and saves
// its downloads; this file only makes the element and reports what it does.
//
// The app's preload exposes `browser` from desktop app 0.3.0 on. An older app
// that loads this shell has no webview tag, so it keeps the frames.

// A hoisted function rather than the const below, because 42-browser.js reads
// it while it loads (its first pane is made then), before this fragment's
// bindings exist.
function desktopBrowserOn() {
  return !!(window.pockettuiDesktop && window.pockettuiDesktop.browser);
}

const desktopBrowser = desktopBrowserOn() ? window.pockettuiDesktop.browser : null;

// Each guest by its webContents id, which is how main.js names the guest a
// new-tab link came from.
const desktopViews = new Map();

// Chromium's net error codes, in BROWSE_ERRORS' words (42-browser.js).
function desktopBrowserError(code) {
  if (code === -105 || code === -137) return "unknown_host";
  if (code === -102 || code === -109 || code === -106) return "refused";
  if (code === -118 || code === -7) return "timeout";
  if (code <= -200 && code > -300) return "tls";
  return "upstream";
}

// One tab's guest. `cbs` is the pane's: onTab({url, title, canBack, canFwd,
// favicon}) after every landing, onError({code, said}), openTab(url) for a
// link the page meant for a new tab, onClose() for the page's window.close().
function desktopBrowserMake(cbs) {
  const el = document.createElement("webview");
  el.className = "browser-web";
  el.setAttribute("partition", "persist:browse");
  el.setAttribute("allowpopups", "");
  // The guest's methods throw until dom-ready; the first address goes in as
  // src, every later one through loadURL.
  let ready = false;
  let id = 0;
  let zoom = 1;
  let title = "";
  let icon = "";
  // Moved on by every landing, so an icon still loading for the page before
  // is not put on the next one's chip.
  let page = 0;

  const report = () => {
    if (!ready) return;
    cbs.onTab({ url: el.getURL(), title: title, canBack: el.canGoBack(),
                canFwd: el.canGoForward(), favicon: icon });
  };
  const applyZoom = () => { if (ready) el.setZoomFactor(zoom); };

  el.addEventListener("dom-ready", () => {
    if (!ready) {
      ready = true;
      id = el.getWebContentsId();
      desktopViews.set(id, view);
    }
    applyZoom();
    report();
  });
  el.addEventListener("did-navigate", () => {
    page++;
    title = "";
    icon = "";
    applyZoom();
    report();
  });
  el.addEventListener("did-navigate-in-page", (e) => { if (e.isMainFrame) report(); });
  el.addEventListener("page-title-updated", (e) => { title = e.title || ""; report(); });
  // Chromium names /favicon.ico whether or not the site has one; only an icon
  // that loads goes on the chip, never a broken image.
  el.addEventListener("page-favicon-updated", (e) => {
    const url = (e.favicons && e.favicons[0]) || "";
    if (!/^(https?|data):/i.test(url)) return;
    const at = page;
    const img = new Image();
    img.onload = () => {
      if (at !== page) return;
      icon = url;
      report();
    };
    img.src = url;
  });
  el.addEventListener("did-fail-load", (e) => {
    // -3 is a load given up for another one, which is not a failure.
    if (!e.isMainFrame || e.errorCode === -3) return;
    cbs.onError({ code: desktopBrowserError(e.errorCode), said: e.errorDescription || "" });
  });
  el.addEventListener("render-process-gone", () => cbs.onError({ code: "crashed", said: "" }));
  el.addEventListener("close", () => cbs.onClose());

  const view = {
    el: el,
    cbs: cbs,
    navigate(url) {
      if (ready) el.loadURL(url).catch(() => { /* did-fail-load reports it */ });
      else el.src = url;
    },
    back() { if (ready && el.canGoBack()) el.goBack(); },
    fwd() { if (ready && el.canGoForward()) el.goForward(); },
    reload() { if (ready) el.reload(); },
    // Off-screen tabs keep their guest: visibility, never display or removal.
    show() { el.classList.remove("off"); },
    hide() { el.classList.add("off"); },
    setZoom(f) { zoom = f; applyZoom(); },
    destroy() {
      if (id) desktopViews.delete(id);
      el.remove();
    },
  };
  return view;
}

if (desktopBrowser) {
  desktopBrowser.onOpenTab((msg) => {
    const view = msg && desktopViews.get(msg.id);
    if (view && typeof msg.url === "string") view.cbs.openTab(msg.url);
  });
}
