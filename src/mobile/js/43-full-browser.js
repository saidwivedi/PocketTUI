// ============================================================
// A tab in full mode: a real browser on the computer, streamed
// ============================================================
// The pane above this one (42-browser.js) shows a page by reverse-proxying it
// through the backend and framing the result. That is enough for a dev server
// and an intranet page, and it is not enough for anything with a login, a
// service worker or a top-window expectation: the document has an opaque
// origin, its scripts are rewritten, and no amount of shimming makes it the
// real thing.
//
// Full mode answers that by moving the browser rather than the page. A headless
// Chromium runs on the computer with the user's own profile in it, and what
// travels is pixels: JPEG screencast frames and WebP settle captures over
// ws/browser/<pane> (chromium.py, app.py's BROWSER_OPS), painted onto a canvas
// here. The page executes in a genuine browser at a genuine origin, and this
// file's whole job is to make looking at a picture of it feel like looking at
// it — which is almost entirely a matter of input.
//
// That is where the work is. A canvas cannot be typed into, so the keyboard
// comes off a 1px textarea behind it and every key is forwarded as a protocol
// message. A canvas has no cursor of its own, so the shape under the pointer is
// asked of the page and set here. The clipboard cannot be read across the wire
// inside a gesture, so copy goes through the browser's own copy event with the
// page's selection put into that field for the length of the press. Right-click
// draws the menu here because the browser's own menu is on the computer, where
// nobody can see it. Middle click and Ctrl+click are told to nobody but the
// backend, because a page that hears them follows the link itself.
//
// What the page asks of its browser comes back the other way. A dialog it put
// up, a challenge it hit, a file input it opened: each of those stops the page
// on the computer until an answer goes back over this socket, so each of them is
// drawn on this end — the dialogs in the app's own sheets, the other two over
// the picture of the page they belong to.
//
// This file owns the view and nothing else: which tab is in full mode, what the
// address field says and where the socket comes from are the pane's (item 4).
// A view is handed a transport — anything with send(obj) — and a handful of
// callbacks for the chords that belong to the shell.

// ------------------------------------------------------------
// Every view
// ------------------------------------------------------------

// The live views, for the two questions that are about all of them at once:
// whether the streamed page currently has the keyboard (which is what keeps the
// pane's Escape from closing a pane the user is typing in) and what to do when
// the tab goes to the background.
const fbViews = [];

// A phone's 3x surface of a desktop-sized viewport is four times the pixels of
// a 1.5x one over the same link, and the backend caps it at its own dpr_max
// anyway; asking for more than this is asking for a slower stream and no more
// detail than the eye gets.
const FB_DPR_MAX = 2;

// A seam drag fires a resize per frame; one device-metrics override and one
// screencast restart per frame is a stutter. The page's true size is worth a
// tenth of a second's wait.
const FB_RESIZE_MS = 100;

// How often the pointer's shape is asked for, and how long the pointer has to
// sit still before a [title] becomes a tooltip.
const FB_CURSOR_MS = 50;
const FB_TIP_MS = 700;

// What counts as a double click: a browser's own window, near enough.
const FB_CLICK_MS = 500;
const FB_CLICK_SLOP = 6;

// A line of wheel delta in pixels, for the mice and trackpads that report in
// lines rather than pixels (deltaMode 1). Chrome's own number.
const FB_WHEEL_LINE = 16;

// MouseEvent.button, in the names the protocol uses.
const FB_BUTTONS = ["left", "middle", "right", "back", "forward"];

// How many files one answer to a file dialog carries, app.py's
// BROWSER_FILES_MAX said again: the op drops the rest, and sending fifty would
// be fifty uploads for a page that will be given thirty-two.
const FB_FILES_MAX = 32;

function fullBrowserHasFocus() {
  const at = document.activeElement;
  if (!at) return false;
  // The key proxy, and the fields of the two things the page can put over its
  // own picture: the credentials sheet and the file bar. All three are somebody
  // typing or choosing inside the pane, which is what the pane's Escape stands
  // aside for.
  for (const rec of fbViews) {
    if (rec.focusEl === at || rec.wrap.contains(at)) return true;
  }
  return false;
}

// One page dialog on screen at a time, whichever pane or tab it came from.
// appConfirm and appPrompt share one sheet and one resolver (01-helpers.js), so
// a second question raised over the first would leave the first page stopped
// with nobody left to answer it. Every view's asks queue here.
let fbAsking = false;
const fbAskQueue = [];
function fbAskTurn(run) {
  if (fbAsking) { fbAskQueue.push(run); return; }
  fbAsking = true;
  Promise.resolve().then(run).catch((e) => dbg("full browser: ask failed", e))
    .then(() => {
      fbAsking = false;
      const next = fbAskQueue.shift();
      if (next) fbAskTurn(next);
    });
}

// One file onto the computer, where a page's file input can be given it: the
// raw-body shape /api/fs/upload already takes, and the answer is the path on
// *that* machine which goes back in the `files` op. {path} or {error: what to
// say} — the caller is uploading a handful and says one thing about the lot.
async function fbUpload(file) {
  try {
    const r = await fetch(
      apiURL("api/browser/upload?name=" + encodeURIComponent(file.name)),
      { method: "POST", cache: "no-store", headers: authHeaders(), body: file });
    if (r.status === 401) { rejectToken(); return { error: "" }; }
    if (r.status === 413) return { error: "That file is too large to send (50 MB max)" };
    if (!r.ok) throw new Error("http " + r.status);
    const d = await r.json();
    if (!d || !d.path) throw new Error("bad answer");
    return { path: String(d.path) };
  } catch (e) {
    dbg("browser upload failed", e);
    return { error: "Couldn't send that file to the computer" };
  }
}

// The four booleans as the one bitmask both ends use (chromium.py's MOD_*).
function fbMods(e) {
  return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0)
       | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
}

// Which button is being held, out of the mask a pointer event carries. A drag
// has to name its button on every move or the page sees a hover, not a drag.
function fbHeld(buttons) {
  if (buttons & 1) return "left";
  if (buttons & 2) return "right";
  if (buttons & 4) return "middle";
  return "none";
}

// A page's computed cursor, or the nearest keyword that is safe to put in a
// style attribute. `url(...)` cursors point at an image on the page's own
// origin, which this document cannot fetch and must not try to.
function fbSafeCursor(name) {
  const s = String(name || "").trim();
  if (!s || s.indexOf("(") >= 0 || s.length > 32) return "default";
  return /^[a-z-]+$/.test(s) ? s : "default";
}

// The tab going to the background stops every stream; coming back starts the
// ones their panes still want. One listener for every view, because this is one
// fact about the window rather than a fact about a tab.
document.addEventListener("visibilitychange", () => {
  const hidden = document.visibilityState === "hidden";
  for (const rec of fbViews.slice()) rec.visibility(hidden);
});

// ------------------------------------------------------------
// The socket
// ------------------------------------------------------------

// One pane's transport. Item 4 owns how a pane gets one and may pass any object
// with send(obj); this is the real one, and the harness's.
//
// Nothing is queued while the socket is down. A dropped `mouse` is a gesture
// that is over, and the ops that are worth repeating are repeated by the views
// themselves: a (re)connect asks each of them to state what it wants again,
// which is an `open` naming the target the tab was on plus the `show` and the
// zoom it was at. That is one mechanism instead of two, and it is also what
// gets the pages back after a phone reload — the targets on the computer
// outlive the socket (ws_browser's docstring).
function fullBrowserLink(paneId) {
  const views = {};
  let sock = null;
  let retries = 0;
  let timer = 0;
  let closed = false;

  function send(msg) {
    if (!sock || sock.readyState !== 1) return false;
    try { sock.send(JSON.stringify(msg)); } catch (e) { return false; }
    return true;
  }

  // u32-LE header length, that many bytes of JSON, then the image. The header
  // says which tab the picture is of, which is how two tabs' frames share one
  // socket without either of them painting the other's page.
  function onBinary(buf) {
    if (buf.byteLength < 4) return;
    const n = new DataView(buf).getUint32(0, true);
    if (!n || 4 + n > buf.byteLength) return;
    const bytes = new Uint8Array(buf);
    let header;
    try {
      header = JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + n)));
    } catch (e) { return; }
    const view = header && views[header.tab];
    if (view) view.frame(header, bytes.subarray(4 + n));
  }

  function onText(text) {
    let msg = null;
    try { msg = JSON.parse(text); } catch (e) { return; }
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "ready" || msg.type === "status") {
      if (linkApi.onstate) linkApi.onstate(msg);
      return;
    }
    // A tab the computer made rather than the pane: a window a page opened,
    // already open on the other end and shown to nobody. There is no view under
    // that id yet — the pane is what decides whether there is going to be one
    // (browserPopup, 42-browser.js).
    if (msg.type === "newtab") {
      if (linkApi.onnewtab) linkApi.onnewtab(msg);
      return;
    }
    // A download belongs to the browser rather than to a tab: the one that
    // started it may be closed by the time it finishes, and `tab` is then null.
    // So it goes to the pane, which is where the line about it is drawn.
    if (msg.type === "download") {
      if (linkApi.ondownload) linkApi.ondownload(msg);
      return;
    }
    // A failure with no tab on it is the browser itself: every view in this
    // pane is showing a page that is not there any more.
    if (msg.type === "error" && !msg.tab) {
      // Except a restart, which is the memory watchdog having swapped the
      // browser under its tabs and put the live ones back at their addresses
      // (chromium.py's restart_over_cap). Nothing here is broken, so nothing
      // here gets an overlay over a page that is coming back.
      if (msg.code === "restarted") {
        if (linkApi.onrestart) linkApi.onrestart(msg);
        return;
      }
      for (const id of Object.keys(views)) views[id].error(msg);
      return;
    }
    const view = views[msg.tab];
    if (!view) return;
    if (msg.type === "tab") view.tab(msg);
    else if (msg.type === "sel") view.sel(msg.text || "");
    else if (msg.type === "cursor") view.cursor(msg);
    else if (msg.type === "link") view.link(msg);
    else if (msg.type === "hit") view.hit(msg);
    else if (msg.type === "dialog") view.dialog(msg);
    else if (msg.type === "auth") view.auth(msg);
    else if (msg.type === "filechooser") view.chooser(msg);
    else if (msg.type === "error") view.error(msg);
    else if (msg.type === "gone") view.gone();
  }

  function connect() {
    if (closed) return;
    clearTimeout(timer);
    let ws;
    try { ws = new WebSocket(wsURL("ws/browser/" + encodeURIComponent(paneId))); }
    catch (e) { return; }
    ws.binaryType = "arraybuffer";
    sock = ws;
    ws.onopen = () => {
      if (sock !== ws) return;
      retries = 0;
      // The pairing code in the first frame, exactly as the attach socket's
      // does: CORS does not apply to WebSockets, so this is the only thing
      // between the open port and the user's own logins.
      send({ token: cfg.token, dev: cfg.devname });
      for (const id of Object.keys(views)) views[id].reconnected();
    };
    ws.onmessage = (ev) => {
      if (sock !== ws) return;
      if (typeof ev.data === "string") onText(ev.data);
      else onBinary(ev.data);
    };
    ws.onerror = () => {};
    ws.onclose = () => {
      if (sock !== ws) return;
      sock = null;
      if (closed) return;
      const wait = Math.min(8000, 400 * Math.pow(2, Math.min(retries++, 5)));
      timer = setTimeout(connect, wait);
    };
  }

  const linkApi = {
    send: send,
    // Set by whoever wants the `ready`/`status` messages, which are the pane's
    // rather than a tab's.
    onstate: null,
    // The three other messages that are about the pane rather than about one of
    // its views: a window a page opened, a file the browser is saving, and the
    // browser having been restarted under every tab in it.
    onnewtab: null,
    ondownload: null,
    onrestart: null,
    attach: (tabId, view) => { views[tabId] = view; },
    detach: (tabId) => { delete views[tabId]; },
    live: () => !!sock && sock.readyState === 1,
    close: () => {
      closed = true;
      clearTimeout(timer);
      const ws = sock;
      sock = null;
      if (ws) { try { ws.close(); } catch (e) {} }
    },
  };
  connect();
  return linkApi;
}

// ------------------------------------------------------------
// One tab's view
// ------------------------------------------------------------

// `pane` is the pane's row id, `tabId` the id the protocol knows this tab by,
// `link` the transport above (or item 4's own), and `cbs` the chords that are
// the shell's: {focusAddress, reload, zoomStep, openTab, retry, onTab, onError,
// onAsk}. All of them optional — a view with none of them still shows a page.
function fullBrowserMake(pane, tabId, link, cbs) {
  const cb = cbs || {};
  const tpl = $("browser-full-tpl");
  const wrap = tpl.content.firstElementChild.cloneNode(true);
  const canvas = wrap.querySelector(".fb-canvas");
  const ctx = canvas.getContext("2d", { alpha: false });
  const focusEl = wrap.querySelector(".fb-focus");
  const menu = wrap.querySelector(".fb-menu");
  const tip = wrap.querySelector(".fb-tip");
  const errEl = wrap.querySelector(".fb-err");
  const authEl = wrap.querySelector(".fb-auth");
  const askEl = wrap.querySelector(".fb-ask");

  // The last picture, kept so a resize of this element can repaint rather than
  // wait for the computer's next frame.
  let bmp = null;
  let bmpW = 0, bmpH = 0, bmpCssW = 0, bmpCssH = 0;
  let drawn = 0;                 // the newest decode, so a slow one never wins
  // What the computer has been told, so a resize that changes nothing is not
  // sent at all — a device-metrics override restarts the screencast.
  let sentW = 0, sentH = 0, sentDpr = 0;
  // What this view wants, which is what a reconnect re-states.
  let wantUrl = "";
  let wantTarget = "";
  let wantShown = false;
  let zoom = 1;
  // The page's own selection, pushed by the backend as it changes (`sel`). The
  // copy key needs it in hand: a clipboard write a round trip after the press
  // is one the browser refuses.
  let selection = "";
  let info = null;
  let destroyed = false;

  let roTimer = 0;
  let movePt = null, moveMods = 0, moveButtons = 0, moveRaf = 0;
  let probeAt = 0, probeX = -1, probeY = -1;
  let tipTitle = "", tipTimer = 0;
  let menuPt = null, menuOn = false;
  // The press that dismissed the menu is not a press for the page: a browser's
  // own menu swallows it too.
  let menuSwallow = false;
  let clickAt = 0, clickX = 0, clickY = 0, clickN = 0, clickBtn = -1;
  // A middle or Ctrl click asked the backend what is under it; the answer is a
  // new tab rather than a fact for anybody else.
  let openPending = false;
  // The press whose release goes nowhere, because its press did not go to the
  // page either.
  let skipUp = false;
  // What the page is stopped waiting for an answer to: a modal dialog, an HTTP
  // challenge, a file dialog. One of each at most — the browser sends no second
  // one for a tab that has not answered the first.
  let askDialog = null;
  let askAuth = null;
  let askFiles = null;

  const send = (msg) => (link && link.send ? link.send(msg) : false);

  // ---- geometry ------------------------------------------------------------
  function geom() {
    const r = wrap.getBoundingClientRect();
    return {
      cssW: Math.max(1, Math.round(r.width)),
      cssH: Math.max(1, Math.round(r.height)),
      dpr: Math.min(window.devicePixelRatio || 1, FB_DPR_MAX),
    };
  }

  // A pane the column has folded away is display:none, so its box is nothing
  // at all — and a resize to 1x1 would throw away the page's layout for the
  // moment it is hidden.
  function boxed() {
    const r = wrap.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function measure(force) {
    if (destroyed || !boxed()) return;
    const g = geom();
    if (!force && g.cssW === sentW && g.cssH === sentH && g.dpr === sentDpr) return;
    sentW = g.cssW; sentH = g.cssH; sentDpr = g.dpr;
    send({ type: "resize", tab: tabId, cssW: g.cssW, cssH: g.cssH, dpr: g.dpr });
  }

  const ro = new ResizeObserver(() => {
    clearTimeout(roTimer);
    roTimer = setTimeout(() => measure(false), FB_RESIZE_MS);
  });
  ro.observe(wrap);

  // ---- painting ------------------------------------------------------------
  function paint() {
    if (!bmp) return;
    if (canvas.width !== bmpW || canvas.height !== bmpH) {
      canvas.width = bmpW;
      canvas.height = bmpH;
    }
    // The size the computer was rendering for, not this element's size: a drag
    // of the seam is a round trip ahead of the frames, and stretching the old
    // picture to the new box would show the page distorted rather than a
    // moment behind.
    canvas.style.width = bmpCssW + "px";
    canvas.style.height = bmpCssH + "px";
    ctx.drawImage(bmp, 0, 0);
  }

  async function frame(header, bytes) {
    if (destroyed) return;
    const gen = ++drawn;
    const type = header.fmt === "webp" ? "image/webp" : "image/jpeg";
    let next = null;
    try {
      next = await createImageBitmap(new Blob([bytes], { type: type }));
    } catch (e) {
      // Acknowledged even so: Chrome sends no further screencast frame until
      // the last one is answered, and a frame this browser could not decode
      // would otherwise stop the stream dead.
      send({ type: "ack", tab: tabId, seq: header.seq });
      return;
    }
    if (destroyed || gen !== drawn) {
      if (next.close) next.close();
      send({ type: "ack", tab: tabId, seq: header.seq });
      return;
    }
    if (bmp && bmp.close) bmp.close();
    bmp = next;
    bmpW = header.w || next.width;
    bmpH = header.h || next.height;
    bmpCssW = header.cssW || bmpW;
    bmpCssH = header.cssH || bmpH;
    paint();
    send({ type: "ack", tab: tabId, seq: header.seq });
  }

  // ---- the stream's state --------------------------------------------------
  function sendOpen() {
    const g = geom();
    sentW = g.cssW; sentH = g.cssH; sentDpr = g.dpr;
    const msg = { type: "open", tab: tabId, url: wantUrl || "about:blank",
                  cssW: g.cssW, cssH: g.cssH, dpr: g.dpr, zoom: zoom };
    if (wantTarget) msg.targetId = wantTarget;
    send(msg);
  }

  function open(url, targetId) {
    if (url) wantUrl = url;
    if (targetId !== undefined) wantTarget = targetId || "";
    hideError();
    sendOpen();
    show();
  }

  // Every navigation after the first. `open` is not that: it is what makes the
  // tab — or takes back the target a reload left behind — and the computer
  // answers a second one for a tab it already has by describing the page that is
  // on it rather than by going anywhere (chromium.py's FullBrowser.open). So the
  // pane's second address goes as its own op, and the `tab` message it provokes
  // is what moves the address field.
  function navigate(url) {
    if (!url) return;
    wantUrl = url;
    hideError();
    send({ type: "nav", tab: tabId, url: url });
  }

  function show() {
    wantShown = true;
    if (document.visibilityState === "hidden") return;
    const g = geom();
    sentW = g.cssW; sentH = g.cssH; sentDpr = g.dpr;
    send({ type: "show", tab: tabId, cssW: g.cssW, cssH: g.cssH, dpr: g.dpr,
           zoom: zoom });
    // A dialog this tab was stopped on while it was in the background: the tab
    // is in front now, so there is somebody to ask.
    pumpDialog();
  }

  function sendHide() { send({ type: "hide", tab: tabId }); }

  function hide() {
    wantShown = false;
    sendHide();
  }

  function visibility(hidden) {
    if (!wantShown) return;
    if (hidden) sendHide();
    else show();
  }

  function setZoom(z) {
    zoom = Number(z) || 1;
    send({ type: "zoom", tab: tabId, z: zoom });
  }

  function reconnected() {
    if (!wantUrl && !wantTarget) return;
    sendOpen();
    if (wantShown) show();
  }

  // ---- the keyboard --------------------------------------------------------
  // Which chords are not the page's. Everything unlisted is, including Tab,
  // Escape, the arrows and Ctrl+A — a page in a browser gets those, and a page
  // in here that did not would be a page nobody can use a form in.
  //
  //   "app"      the shell's own chords, and the ones the browser reserves:
  //              left entirely alone, so they reach whoever claims them.
  //   "clip"     copy and cut: forwarded to the page AND left to run, because
  //              the browser's own copy event is how the text reaches the
  //              clipboard without a permission prompt.
  //   "address" / "reload" / "zoom*"   the pane's, via a callback.
  //   "page"     everything else.
  function keyOwner(e) {
    const mod = e.ctrlKey || e.metaKey;
    const code = e.code || "";
    // The app's three-key chords (31-wide-layout.js, 37-git-diff.js,
    // 38-scrollback-search.js). They are claimed on capture at the document,
    // so by the time this runs they may already be prevented; either way they
    // are none of the page's business.
    if (e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey
        && (code === "KeyD" || code === "KeyG" || code === "KeyF"
            || code === "KeyL")) return "app";
    if (!mod) return e.key === "F5" ? "reload" : "page";
    // Ctrl+W is the browser's own and never reaches a page anywhere, so there
    // is nothing to be gained by claiming it here. Ctrl+V is left alone too:
    // preventing it would stop the paste event this reads the text off.
    if (code === "KeyW" || code === "KeyV") return "app";
    if (code === "KeyC" || code === "KeyX") return "clip";
    if (e.shiftKey) return "page";
    if (code === "KeyL") return "address";
    if (code === "KeyR") return "reload";
    if (code === "Equal" || code === "NumpadAdd") return "zoomin";
    if (code === "Minus" || code === "NumpadSubtract") return "zoomout";
    if (code === "Digit0" || code === "Numpad0") return "zoomreset";
    return "page";
  }

  function forwardKey(e) {
    const mod = e.ctrlKey || e.metaKey;
    // The character the key produces, and nothing where it produces none: a
    // chord carries no text, which is what makes the backend send it as a
    // rawKeyDown and the page see Ctrl+C rather than the letter c.
    const text = !mod && e.key && e.key.length === 1 ? e.key : "";
    send({ type: "key", tab: tabId, kind: e.type === "keyup" ? "up" : "down",
           key: e.key || "", code: e.code || "", keyCode: e.keyCode || 0,
           text: text, mods: fbMods(e), repeat: !!e.repeat,
           location: e.location || 0 });
  }

  // Copy and cut reach the clipboard through the browser's own event, which
  // fires only where something is selected — and nothing in this field ever is,
  // because the selection is the page's. So the page's text is put in the field
  // and selected for the length of the press, and taken back out after it.
  function armClipboard() {
    if (!selection) return;
    focusEl.value = selection;
    focusEl.select();
    setTimeout(() => { if (!destroyed) focusEl.value = ""; }, 0);
  }

  function onKey(e) {
    if (destroyed) return;
    // The IME's own keys, and the dead key that is half of a character: both
    // finish as an `input` or a `compositionend` below, and a preventDefault
    // here would stop them arriving at all.
    if (e.isComposing) return;
    if (e.key === "Dead" || e.key === "Process" || e.key === "Unidentified") return;
    if (menuOn && e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      if (e.type === "keydown") closeMenu();
      return;
    }
    const own = keyOwner(e);
    if (own === "app") return;
    if (e.defaultPrevented) return;
    if (own !== "page" && own !== "clip") {
      e.preventDefault();
      if (e.type !== "keydown") return;
      if (own === "address" && cb.focusAddress) cb.focusAddress();
      else if (own === "reload" && cb.reload) cb.reload();
      else if (own === "zoomin" && cb.zoomStep) cb.zoomStep(1);
      else if (own === "zoomout" && cb.zoomStep) cb.zoomStep(-1);
      else if (own === "zoomreset" && cb.zoomStep) cb.zoomStep(0);
      return;
    }
    if (own === "clip" && e.type === "keydown") armClipboard();
    forwardKey(e);
    if (own !== "clip") e.preventDefault();
  }

  focusEl.addEventListener("keydown", onKey);
  focusEl.addEventListener("keyup", onKey);

  // A dead key's letter, and anything else the field is given with no key
  // press of its own. Composition is left to compositionend, which carries the
  // whole string rather than the piece this event was fired for.
  focusEl.addEventListener("input", (e) => {
    // Mid-composition the field holds the IME's own working text; emptying it
    // here would take the candidate away from under the user.
    if (e.isComposing || e.inputType === "insertCompositionText") return;
    const text = focusEl.value;
    focusEl.value = "";
    if (destroyed || !text) return;
    if (e.inputType && e.inputType.indexOf("insert") !== 0) return;
    send({ type: "text", tab: tabId, text: text });
  });

  focusEl.addEventListener("compositionend", (e) => {
    const text = e.data || "";
    focusEl.value = "";
    if (!destroyed && text) send({ type: "text", tab: tabId, text: text });
  });

  // A paste is text with no keys behind it: Input.insertText, not a synthesised
  // keyDown per character, which would fire the page's own key handlers for it.
  focusEl.addEventListener("paste", (e) => {
    const text = e.clipboardData ? e.clipboardData.getData("text/plain") : "";
    e.preventDefault();
    focusEl.value = "";
    if (!destroyed && text) send({ type: "text", tab: tabId, text: text });
  });

  function onClip(e) {
    if (destroyed || !selection) return;
    try { e.clipboardData.setData("text/plain", selection); } catch (err) { return; }
    // The field's own content is not what should travel; the page's selection
    // is, and it has just been written. The page still gets the key press, so
    // a page with its own copy handler runs it, and its own Ctrl+X does the
    // deletion this cannot.
    e.preventDefault();
    focusEl.value = "";
  }
  focusEl.addEventListener("copy", onClip);
  focusEl.addEventListener("cut", onClip);

  // Escape inside one of the overlays answers it. The pane's own Escape stands
  // aside while the keyboard is anywhere in this view (fullBrowserHasFocus), so
  // this is the key's only meaning here — and the page's own Escape still goes
  // to the page, which is the field's own listener above.
  wrap.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || e.target === focusEl) return;
    if (!askAuth && !askFiles) return;
    e.preventDefault();
    e.stopPropagation();
    if (askAuth) answerAuth(null, "");
    else sendFiles([]);
  });

  // ---- the pointer ---------------------------------------------------------
  function at(e) {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  // Click counts, because a pointer event does not carry one (PointerEvent's
  // detail is 0 by specification, and preventing pointerdown — which this must,
  // or the shell scrolls and the canvas is dragged — suppresses the mousedown
  // that would have). A browser's own rule: the same button, near the same
  // place, inside half a second.
  function clicksFor(e) {
    if (e.detail) {
      clickN = e.detail;
    } else {
      const now = e.timeStamp || Date.now();
      const near = Math.abs(e.clientX - clickX) < FB_CLICK_SLOP
                && Math.abs(e.clientY - clickY) < FB_CLICK_SLOP;
      clickN = (e.button === clickBtn && now - clickAt < FB_CLICK_MS && near)
        ? clickN + 1 : 1;
      clickAt = now;
    }
    clickBtn = e.button;
    clickX = e.clientX;
    clickY = e.clientY;
    return Math.min(clickN, 3);
  }

  // A middle click and a Ctrl/Cmd+click open a new tab, and the page is told
  // nothing: told, it would follow the link in the tab it is in.
  function opensTab(e) {
    return e.button === 1 || (e.button === 0 && (e.ctrlKey || e.metaKey));
  }

  canvas.addEventListener("pointerdown", (e) => {
    if (destroyed) return;
    // The shell must not scroll and the canvas must not be dragged as an image;
    // the keyboard goes to the field behind it instead.
    e.preventDefault();
    focusEl.focus({ preventScroll: true });
    hideTip();
    if (menuOn) closeMenu();
    if (menuSwallow) { menuSwallow = false; return; }
    const p = at(e);
    if (opensTab(e)) {
      skipUp = true;
      openPending = true;
      send({ type: "link", tab: tabId, x: p.x, y: p.y });
      return;
    }
    skipUp = false;
    if (e.button === 2) menuPt = p;
    const clicks = clicksFor(e);
    try { canvas.setPointerCapture(e.pointerId); } catch (err) {}
    send({ type: "mouse", tab: tabId, kind: "down", x: p.x, y: p.y,
           button: FB_BUTTONS[e.button] || "none", buttons: e.buttons,
           clicks: clicks, mods: fbMods(e) });
  });

  // One move per animation frame. A trackpad reports a hundred a second and
  // each one is a protocol message and a hit test; the page only ever sees the
  // newest of them anyway.
  function flushMove() {
    moveRaf = 0;
    if (destroyed || !movePt) return;
    send({ type: "mouse", tab: tabId, kind: "move", x: movePt.x, y: movePt.y,
           button: fbHeld(moveButtons), buttons: moveButtons, clicks: 0,
           mods: moveMods });
  }

  canvas.addEventListener("pointermove", (e) => {
    if (destroyed) return;
    const p = at(e);
    movePt = p;
    moveMods = fbMods(e);
    moveButtons = e.buttons;
    if (!moveRaf) moveRaf = requestAnimationFrame(flushMove);
    probeCursor(p);
    armTip(e.clientX, e.clientY);
  });

  canvas.addEventListener("pointerup", (e) => {
    if (destroyed) return;
    e.preventDefault();
    try { canvas.releasePointerCapture(e.pointerId); } catch (err) {}
    if (skipUp) { skipUp = false; return; }
    const p = at(e);
    send({ type: "mouse", tab: tabId, kind: "up", x: p.x, y: p.y,
           button: FB_BUTTONS[e.button] || "none", buttons: e.buttons,
           clicks: Math.min(clickN || 1, 3), mods: fbMods(e) });
    // The menu is drawn on the answer rather than on the press: what goes in it
    // is what is under the pointer, and only the computer knows that.
    if (e.button === 2) {
      menuPt = p;
      send({ type: "hit", tab: tabId, x: p.x, y: p.y });
    }
  });

  canvas.addEventListener("pointercancel", (e) => {
    try { canvas.releasePointerCapture(e.pointerId); } catch (err) {}
    skipUp = false;
  });

  canvas.addEventListener("pointerleave", () => {
    hideTip();
    probeX = probeY = -1;
  });

  // The page scrolls, never the shell: this pane is a page's worth of content
  // in a fixed column, and a wheel that reached the document would move the
  // column instead of the page in it.
  canvas.addEventListener("wheel", (e) => {
    if (destroyed) return;
    e.preventDefault();
    closeMenu();
    let dx = e.deltaX;
    let dy = e.deltaY;
    if (e.deltaMode === 1) { dx *= FB_WHEEL_LINE; dy *= FB_WHEEL_LINE; }
    else if (e.deltaMode === 2) {
      const g = geom();
      dx *= g.cssW;
      dy *= g.cssH;
    }
    const p = at(e);
    send({ type: "mouse", tab: tabId, kind: "wheel", x: p.x, y: p.y,
           button: "none", buttons: e.buttons || 0, clicks: 0,
           mods: fbMods(e), dx: dx, dy: dy });
  }, { passive: false });

  // The browser's own menu is on the computer, where nobody can see it.
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());

  // ---- the cursor and the tooltip -----------------------------------------
  function probeCursor(p) {
    const now = Date.now();
    if (now - probeAt < FB_CURSOR_MS) return;
    if (Math.abs(p.x - probeX) < 1 && Math.abs(p.y - probeY) < 1) return;
    probeAt = now;
    probeX = p.x;
    probeY = p.y;
    send({ type: "cursor", tab: tabId, x: p.x, y: p.y });
  }

  function cursor(msg) {
    if (destroyed) return;
    canvas.style.cursor = fbSafeCursor(msg && msg.cursor);
    tipTitle = (msg && msg.title) || "";
    if (!tipTitle) hideTip();
  }

  function hideTip() {
    clearTimeout(tipTimer);
    tip.hidden = true;
  }

  // A [title] is the page's tooltip and the page cannot draw it: its pixels are
  // a picture and the browser they come from has no pointer in it.
  function armTip(clientX, clientY) {
    clearTimeout(tipTimer);
    tip.hidden = true;
    tipTimer = setTimeout(() => {
      if (destroyed || !tipTitle) return;
      const r = wrap.getBoundingClientRect();
      tip.textContent = tipTitle;
      tip.hidden = false;
      const w = tip.offsetWidth, h = tip.offsetHeight;
      tip.style.left = Math.max(0, Math.min(clientX - r.left + 12,
                                            r.width - w - 4)) + "px";
      tip.style.top = Math.max(0, Math.min(clientY - r.top + 18,
                                           r.height - h - 4)) + "px";
    }, FB_TIP_MS);
  }

  // ---- the context menu ---------------------------------------------------
  function outsideMenu(e) {
    if (menu.contains(e.target)) return;
    if (canvas.contains(e.target)) menuSwallow = true;
    closeMenu();
  }
  function menuKey(e) {
    if (e.key === "Escape") { e.preventDefault(); closeMenu(); }
  }

  function closeMenu() {
    if (!menuOn) return;
    menuOn = false;
    menu.hidden = true;
    menu.textContent = "";
    document.removeEventListener("pointerdown", outsideMenu, true);
    document.removeEventListener("keydown", menuKey, true);
  }

  function writeClip(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(() => toast("Couldn't copy that"));
      return;
    }
    toast("Couldn't copy that");
  }

  // Read inside the click, which is the only time a browser grants it.
  function pasteClip() {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      toast("This browser won't share the clipboard");
      return;
    }
    navigator.clipboard.readText().then((text) => {
      if (!destroyed && text) send({ type: "text", tab: tabId, text: text });
    }).catch(() => toast("Couldn't read the clipboard"));
  }

  function openMenu(p, href, editable, sel) {
    closeMenu();
    const item = (label, fn) => menu.appendChild(el("button", {
      class: "fb-item", type: "button",
      onclick: (ev) => { ev.preventDefault(); closeMenu(); fn(); },
    }, label));
    item("Back", () => send({ type: "back", tab: tabId }));
    item("Forward", () => send({ type: "fwd", tab: tabId }));
    item("Reload", () => send({ type: "reload", tab: tabId }));
    if (href || sel || editable) menu.appendChild(el("div", { class: "fb-sep" }));
    if (href) {
      item("Open link in new tab", () => { if (cb.openTab) cb.openTab(href); });
      item("Copy link", () => writeClip(href));
    }
    if (sel) item("Copy", () => writeClip(sel));
    if (editable) item("Paste", () => pasteClip());
    menu.hidden = false;
    menuOn = true;
    const r = wrap.getBoundingClientRect();
    menu.style.left = Math.max(0, Math.min(p.x, r.width - menu.offsetWidth - 2)) + "px";
    menu.style.top = Math.max(0, Math.min(p.y, r.height - menu.offsetHeight - 2)) + "px";
    document.addEventListener("pointerdown", outsideMenu, true);
    document.addEventListener("keydown", menuKey, true);
  }

  // ---- the replies --------------------------------------------------------
  function onHit(msg) {
    if (destroyed) return;
    if (typeof msg.selection === "string") selection = msg.selection;
    if (!menuPt) return;
    const p = menuPt;
    menuPt = null;
    openMenu(p, String(msg.href || ""), !!msg.editable, selection);
  }

  function onLink(msg) {
    if (destroyed) return;
    const href = String((msg && msg.href) || "");
    if (!openPending) return;
    openPending = false;
    // No href under the pointer is a middle click on nothing, which is what a
    // browser does with it too.
    if (href && cb.openTab) cb.openTab(href);
  }

  function sel(text) {
    selection = typeof text === "string" ? text : "";
  }

  function onTab(msg) {
    info = msg || null;
    // A page that finished loading is the answer to whatever the last failure
    // was, so the overlay goes with it.
    if (msg && msg.url && !msg.loading) hideError();
    if (cb.onTab) cb.onTab(msg);
  }

  function hideError() { errEl.hidden = true; errEl.textContent = ""; }

  function error(msg) {
    if (destroyed) return;
    // The pane first, because some failures are not this view's to show: a
    // computer with no browser it can start has nothing to retry, and the tab
    // is better off as a proxy tab (42-browser.js). A handler that says it took
    // the failure has usually just destroyed this view.
    if (cb.onError && cb.onError(msg)) return;
    const text = (msg && msg.message) ? String(msg.message)
                                      : "That page could not be shown";
    errEl.textContent = "";
    errEl.appendChild(el("div", {}, text));
    errEl.appendChild(el("button", {
      class: "fb-retry", type: "button",
      onclick: () => {
        hideError();
        if (cb.retry) cb.retry();
        else open(wantUrl);
      },
    }, "Retry"));
    errEl.hidden = false;
  }

  function gone() {
    error({ code: "gone", message: "This tab was closed by the browser" });
  }

  // ---- what the page is asking --------------------------------------------
  // Three things a page in a real browser does that a picture of one cannot
  // answer: a modal dialog, an HTTP challenge and a file dialog. Each of them
  // stops the page until something answers, and the answer has to be able to
  // come from here.
  //
  // The dialog is the app's own — appConfirm and appPrompt, because a page's
  // confirm() is the same question in the same words as every other question the
  // app asks, and a second idiom for it would be a second thing to read. The
  // other two are drawn in the pane, over the picture of the page they belong
  // to: a challenge and a file dialog are that tab's, and with two panes open
  // and tabs behind them there would otherwise be no saying whose.

  // Whether this tab is waiting on the reader, which is what puts the mark on
  // its chip while it is not the tab on screen (renderBrowserTabs).
  function asking() { return !!(askDialog || askAuth || askFiles); }

  function askChanged() { if (cb.onAsk) cb.onAsk(); }

  // Whether an answer can be asked for at all: a sheet about a page nobody is
  // looking at is a sheet about nothing, so a background tab's dialog waits for
  // the tab to come to the front.
  function onScreen() { return !destroyed && wantShown && !wrap.hidden; }

  function dialog(msg) {
    askDialog = msg || {};
    askChanged();
    pumpDialog();
  }

  function pumpDialog() {
    const msg = askDialog;
    if (!msg || !onScreen()) return;
    fbAskTurn(async () => {
      // The turn comes round after whatever was in front of it, and in that
      // time the tab can have been closed, gone to the background, or had its
      // dialog answered by the page itself (a navigation does that).
      if (askDialog !== msg || !onScreen()) return;
      const text = String(msg.message || "");
      let accept = false;
      let typed = "";
      if (msg.kind === "prompt") {
        const answer = await appPrompt(text || "This page is asking for something",
                                       { value: String(msg.default || "") });
        accept = answer !== null;
        typed = answer || "";
      } else if (msg.kind === "alert") {
        // An alert has one answer. The page goes on either way — which is what
        // the browser itself does with one that was dismissed — so the sheet
        // offers the one button rather than a choice it does not have.
        await appConfirm(text, { confirmLabel: "OK", okOnly: true, danger: false });
        accept = true;
      } else if (msg.kind === "beforeunload") {
        accept = await appConfirm("Leave this page?",
                                  { confirmLabel: "Leave", danger: false });
      } else {
        accept = await appConfirm(text, { confirmLabel: "OK", danger: false });
      }
      if (askDialog === msg) { askDialog = null; askChanged(); }
      // Sent whatever has become of this view in the meantime: the page on the
      // computer is stopped until something answers, and an answer that arrived
      // too late is one the backend drops.
      send({ type: "dialog", tab: tabId, accept: accept, text: typed });
    });
  }

  // The credentials for a Basic or Digest challenge. Drawn here rather than in a
  // sheet for the reason above, and cleared the moment it is answered: a
  // password is not left in the document a press longer than it has to be.
  function auth(msg) {
    askAuth = msg || {};
    askChanged();
    drawAuth();
  }

  function drawAuth() {
    authEl.textContent = "";
    if (!askAuth) { authEl.hidden = true; return; }
    const host = String(askAuth.host || "This site");
    const realm = String(askAuth.realm || "");
    const user = el("input", {
      type: "text", class: "fb-field", placeholder: "User name",
      "aria-label": "User name", autocomplete: "off", autocapitalize: "off",
      autocorrect: "off", spellcheck: "false",
    });
    const pass = el("input", {
      type: "password", class: "fb-field", placeholder: "Password",
      "aria-label": "Password", autocomplete: "off", spellcheck: "false",
    });
    const submit = () => answerAuth(user.value, pass.value);
    const enter = (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      submit();
    };
    user.addEventListener("keydown", enter);
    pass.addEventListener("keydown", enter);
    authEl.appendChild(el("div", { class: "fb-card" },
      el("p", { class: "fb-card-title" }, host + " asks you to sign in"),
      realm ? el("p", { class: "fb-card-note" }, realm) : null,
      user, pass,
      el("div", { class: "fb-acts" },
         el("button", { type: "button", class: "fb-btn",
                        onclick: () => answerAuth(null, "") }, "Cancel"),
         el("button", { type: "button", class: "fb-btn go",
                        onclick: submit }, "Sign in"))));
    authEl.hidden = false;
    user.focus();
  }

  // Null for the user is the cancel the protocol asks for by name; the page then
  // gets the server's own 401 body, exactly as it would in a browser.
  function answerAuth(user, password) {
    if (!askAuth) return;
    askAuth = null;
    if (user === null) send({ type: "auth", tab: tabId, cancel: true });
    else {
      send({ type: "auth", tab: tabId, user: String(user || ""),
             password: String(password || "") });
    }
    drawAuth();
    askChanged();
    focusEl.focus({ preventScroll: true });
  }

  // A file input the page opened. The dialog is the device's own — nothing else
  // can read this phone's files — and a file input needs a gesture, which the
  // press that opened it spent on the canvas. So the pane asks for one of its
  // own: the bar's button is the gesture that opens the picker.
  function chooser(msg) {
    askFiles = msg || {};
    askChanged();
    drawAsk();
  }

  function drawAsk() {
    askEl.textContent = "";
    if (!askFiles) { askEl.hidden = true; return; }
    const many = !!askFiles.multiple;
    askEl.appendChild(el("span", { class: "fb-ask-said" },
                         many ? "This page asks for files" : "This page asks for a file"));
    askEl.appendChild(el("button", {
      type: "button", class: "fb-btn",
      onclick: () => sendFiles([]),
    }, "Cancel"));
    askEl.appendChild(el("button", {
      type: "button", class: "fb-btn go",
      onclick: () => pickFiles(many),
    }, many ? "Choose files" : "Choose file"));
    askEl.hidden = false;
  }

  // Inside the click, which is the only time a browser opens one.
  function pickFiles(many) {
    const input = el("input", { type: "file", class: "fb-file" });
    if (many) input.multiple = true;
    input.addEventListener("change", () => {
      const files = Array.prototype.slice.call(input.files || [], 0, FB_FILES_MAX);
      input.remove();
      sendFiles(files);
    });
    // The dialog dismissed with nothing chosen, where the browser says so. Where
    // it does not, the page keeps waiting — which is what a file dialog nobody
    // answered does in a browser as well.
    input.addEventListener("cancel", () => {
      input.remove();
      sendFiles([]);
    });
    wrap.appendChild(input);
    input.click();
  }

  // An empty list is the cancel: the protocol has no other way to say so, and an
  // input left with no files is what a cancelled dialog leaves behind anyway.
  async function sendFiles(files) {
    if (!askFiles) return;
    askFiles = null;
    drawAsk();
    askChanged();
    const paths = [];
    let said = "";
    for (let i = 0; i < files.length; i++) {
      holdToast(files.length > 1
        ? "Sending " + (i + 1) + " of " + files.length + " to the computer…"
        : "Sending " + files[i].name + " to the computer…");
      const rec = await fbUpload(files[i]);
      if (rec.path) paths.push(rec.path);
      else if (!said) said = rec.error;
    }
    if (!files.length) hideToast();
    else if (paths.length === files.length) {
      toast(paths.length === 1 ? "Sent " + files[0].name
                               : "Sent " + paths.length + " files");
    } else if (said) toast(said);
    else hideToast();
    send({ type: "files", tab: tabId, paths: paths });
    if (!destroyed) focusEl.focus({ preventScroll: true });
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    ro.disconnect();
    clearTimeout(roTimer);
    clearTimeout(tipTimer);
    if (moveRaf) cancelAnimationFrame(moveRaf);
    closeMenu();
    if (bmp && bmp.close) bmp.close();
    bmp = null;
    if (link && link.detach) link.detach(tabId);
    const i = fbViews.indexOf(rec);
    if (i >= 0) fbViews.splice(i, 1);
    wrap.remove();
  }

  // What the list of views needs of one, which is not what the pane needs. The
  // wrap is in it because the keyboard can be in one of the overlays over the
  // canvas rather than in the field behind it (fullBrowserHasFocus).
  const rec = { focusEl: focusEl, wrap: wrap, visibility: visibility };
  fbViews.push(rec);

  const api = {
    id: tabId,
    pane: pane,
    el: wrap,
    // The backend tab is not closed here: a hidden tab is still a page the user
    // can come back to, and closing it belongs to whoever closes the pane's tab.
    open: open,
    navigate: navigate,
    show: show,
    hide: hide,
    resize: () => measure(true),
    setZoom: setZoom,
    destroy: destroy,
    focus: () => focusEl.focus({ preventScroll: true }),
    hasFocus: () => document.activeElement === focusEl,
    info: () => info,
    selection: () => selection,
    // Whether the page is stopped waiting on the reader, for the mark its chip
    // wears while it is not the tab on screen.
    asking: asking,
    // What the transport routes into this view.
    frame: frame,
    tab: onTab,
    sel: sel,
    cursor: cursor,
    link: onLink,
    hit: onHit,
    dialog: dialog,
    auth: auth,
    chooser: chooser,
    error: error,
    gone: gone,
    reconnected: reconnected,
  };
  if (link && link.attach) link.attach(tabId, api);
  return api;
}
