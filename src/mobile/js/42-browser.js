// ============================================================
// In-app browser pane
// ============================================================
// A page the workstation can reach, shown inside the app: a dev server on its
// localhost, a LAN box, an intranet host. None of those is reachable from the
// phone directly, and none of them can be framed from pockettui.com either —
// mixed content, and X-Frame-Options besides. So the backend reverse-proxies
// the target over its own origin (POST api/browse mints the token, /b/<tok>/…
// serves the pages) and this file drives the frame that shows it.
//
// The two shapes are the explorer's: docked in the slot beside a terminal on a
// wide layout (26-side-pane.js), full screen with its own history entry
// anywhere else. Everything below that says "docked" or checks browserDocked
// is choosing between those two, exactly as 28-file-explorer.js does.
//
// The pane owns the URL stack rather than the browser's. A cross-origin frame's
// own history is unreadable, and letting it share the shell's would interleave
// page navigations with the screens the app pushes — back out of a terminal
// would first walk back through a dev server. So the proxied page's shim turns
// its anchor clicks into location.replace and its pushState calls into
// replaceState — neither costs a joint history entry — and reports each
// landing as a postMessage, and this file keeps the list.

let browserDocked = false;      // in the slot beside the terminal, not over it
let browserOpen = false;        // either shape is up
let browserToken = null;        // {token, prefix} once api/browse has answered
let browserStack = [];          // the addresses visited, oldest first
let browserIdx = -1;            // where in that list the frame currently is
// True from the moment this pane asks the frame to load until the shim reports
// the landing: that report is the load we asked for, not a page navigating
// itself, so it corrects the entry the navigation already made instead of
// adding one.
let browserNavigating = false;
// Which screen the full-screen shape covered, to put back when it closes.
let browserOriginFrom = null;
// The frame has been navigated. Until it has, its one free navigation is worth
// spending on the first real page: an iframe's first src assignment replaces
// its initial entry, and every one after that adds a joint history entry that
// the shell's own back would spend on the frame instead of on the screen.
let browserPrimed = false;
// What the frame was last sent to, so reopening the pane on the page it is
// already showing costs no reload.
let browserLoadedUrl = "";
// A fresh token has already been minted for the navigation in flight. A second
// expiry before anything has loaded is a real refusal, not a stale token.
let browserReminted = false;
// The address of the navigation in flight, when this pane picked its scheme
// rather than reading one. Only then is a refusal worth retrying the other way
// round — and only until something lands, which is what clears it.
let browserGuessed = "";
let browserExpanded = cfg.browserExpanded;
// The unsandboxed token the "open in a tab" button builds its address from,
// minted beside the pane's when the pane opens: the click has to hand a window
// an address in one step, and a mint inside it would be a round trip the popup
// blocker counts against the gesture.
let browserTabToken = null;
// The tabs this pane has opened. They are the pane's own windows, shown in the
// laptop's browser because the page inside them cannot be framed, so they close
// when the pane does.
let browserTabs = [];
// This computer serves the shell as well, so it will not mint an unsandboxed
// token (see the mint's same-origin gate). Asked once, and the button goes.
let browserTabBlocked = false;

// The backend's origin and path prefix, read off apiURL() at each use rather
// than captured once: a profile switch changes the computer, and `tailscale
// serve` puts the whole app under a prefix the server itself never sees, which
// is why the mint is told what it is.
function browserBase() { return new URL(apiURL(""), location.href); }
function browserOrigin() { return browserBase().origin; }
function browserPrefix() { return browserBase().pathname.replace(/\/$/, ""); }

// What the proxy says went wrong, in the words a user can act on. Anything
// unlisted is a page that did not load, which is all the shell can honestly
// say about it.
const BROWSE_ERRORS = {
  refused: "Nothing is answering at that address",
  timeout: "That page took too long to answer",
  tls: "That site's certificate could not be read",
  unknown_host: "That address did not resolve",
  loop: "That address is PocketTUI itself",
  upstream: "That page could not be loaded",
};

// The failures that mean "not on this scheme, perhaps": nothing listening on
// the port, nothing answering in time, or a plaintext port that turned out to
// want TLS. A guessed scheme gets one retry as the other one on these; the
// rest are the target answering, and answering is not a reason to try again.
const BROWSE_SCHEME_RETRY = { refused: 1, timeout: 1, tls: 1 };

// Why the mint refused, same idea. bad_prefix and bad_origin are the shell's
// own bugs rather than the user's, so they read as one failure.
const BROWSE_MINT_ERRORS = {
  bad_url: "That is not an address to open",
  loop: "That address is PocketTUI itself",
  browse_unavailable: "This computer's pockettui cannot proxy pages yet",
};

function browserCurrentUrl() {
  return browserIdx >= 0 ? (browserStack[browserIdx] || "") : "";
}

// Every address the pane keeps, shows or compares goes through here first:
// typed into the field, tapped in the terminal, reported by the shim, read
// back out of cfg. An address with no scheme gets one guessed for it, and the
// parser then spells the rest the one way — a default port dropped, an empty
// path a slash. That is the spelling the shim's report carries back (its unmap
// does the same two things), so a typed address and its own landing compare
// equal instead of stacking up as two entries. And an address that is really
// one of the proxy's own is unwrapped to the page it stands for, wherever it
// came from: a stale record, a restart's error page, a poisoned stash.
function browserNormalize(raw) {
  const s = (raw || "").trim();
  if (!s) return "";
  const full = browserHasScheme(s) ? s : browserGuessScheme(s) + s;
  // Unparseable is left as typed: browserProxied refuses it a step later, and
  // its toast is the one the user should get.
  let out;
  try { out = new URL(full).href; } catch (e) { return full; }
  // A proxied address is not a place: it is this proxy's way of spelling one,
  // and storing it as the page's address is how the pane ends up asking the
  // backend to proxy itself. Down to the real target, however deep it goes.
  for (let i = 0; i < 8; i++) {
    const inner = browserUnwrap(out);
    if (!inner) break;
    out = inner;
  }
  return out;
}

// Whether an address carries a scheme of its own. A colon alone does not say
// so: "localhost:3000" and "box.example.net:8080" are a host and a port, and
// every character in them is one a scheme may hold — which is why the parser
// reads the first as the scheme "localhost" and hands back exactly what it was
// given. A digit after the colon is a port; anything else is a scheme.
function browserHasScheme(raw) {
  return /^[a-z][a-z0-9+.-]*:(?!\d)/i.test(raw);
}

// The scheme for an address typed without one. A dev server on a private host
// speaks http far more often than https, and everything with a public name is
// the other way round — an intranet portal that only answers https would
// otherwise hang on port 80, which is what the founder hit. isPrivateHost is
// 08-links.js's, the same list the terminal's links are read against.
function browserGuessScheme(raw) {
  let host = raw.split(/[/?#]/)[0];
  host = host.slice(host.lastIndexOf("@") + 1);
  // A v6 literal keeps its brackets and its colons; anything else loses a port.
  const name = host.charAt(0) === "[" ? host.slice(0, host.indexOf("]") + 1)
                                      : host.split(":")[0];
  return isPrivateHost(name) ? "http://" : "https://";
}

// One layer of "this is the proxy's address for somewhere, not somewhere".
// Matched without the token, because a restart mints a new one and the URLs
// the pane is holding still carry the old, and on any host, because the layer
// a runaway wrapped names this backend's own public name. The backend's prefix
// counts on the backend's own origin; a bare /b/ counts anywhere.
function browserUnwrap(url) {
  let u;
  try { u = new URL(url); } catch (e) { return ""; }
  let path = u.pathname;
  const pre = browserPrefix();
  if (pre && u.origin === browserOrigin() && path.indexOf(pre + "/b/") === 0) {
    path = path.slice(pre.length);
  }
  const m = /^\/b\/[^/]+\/([hs])\/([^/]+)(\/.*)?$/.exec(path);
  if (!m) return "";
  const inner = (m[1] === "s" ? "https://" : "http://") + m[2]
              + (m[3] || "/") + u.search + u.hash;
  try { return new URL(inner).href; } catch (e) { return ""; }
}

// The same address the other way round, for the one retry a guessed scheme
// gets. "" when there is nothing to flip.
function browserFlipScheme(url) {
  if (url.indexOf("https://") === 0) return "http://" + url.slice(8);
  if (url.indexOf("http://") === 0) return "https://" + url.slice(7);
  return "";
}

// The address as the proxy serves it. Built here rather than asked for per
// navigation: the mint already said what the prefix and the token are, and a
// round trip per link would be felt.
function browserProxied(raw, rec = browserToken) {
  if (!rec) return "";
  let u;
  try { u = new URL(raw); } catch (e) { return ""; }
  if (u.protocol !== "http:" && u.protocol !== "https:") return "";
  const secure = u.protocol === "https:";
  // The port is part of the host segment always, so the proxy never has to
  // guess it back from the scheme. hostname keeps an IPv6 literal's brackets.
  const port = u.port || (secure ? "443" : "80");
  return browserOrigin() + rec.prefix + "/b/" + rec.token
       + "/" + (secure ? "s" : "h") + "/" + u.hostname + ":" + port
       + u.pathname + u.search + u.hash;
}

// Where a tab actually starts, which is never the proxied page itself. That
// page runs on the computer's own origin without the sandbox, and a browser
// that once opened this shell straight from that address left the pairing
// token in its localStorage. The backend's /enter answers with Clear-Site-Data
// and the redirect onward, so the browser has wiped that storage before the
// page's first script runs — on Chromium and Firefox; Safari ignores the
// header, and there the mint's same-origin refusal is the whole of it.
function browserEnterUrl(proxied, rec) {
  const head = browserOrigin() + rec.prefix + "/b/" + rec.token;
  if (!proxied || !proxied.startsWith(head + "/")) return "";
  return head + "/enter?to=" + encodeURIComponent(proxied.slice(head.length));
}

// Where an empty tab starts: the computer's own page, with a field that types
// into the proxy and the bookmarks this computer keeps. A tab has no chrome of
// ours — the browser's address bar belongs to the laptop, and an address typed
// there would be fetched by the laptop — so this is the tab's home, and the
// browser's own back button is the way back to it. It carries the same
// Clear-Site-Data the /enter hop does, being the entry in its own right.
function browserStartUrl(rec) {
  return browserOrigin() + rec.prefix + "/b/" + rec.token + "/start";
}

// One token per shell load, minted on the first navigation and kept. The
// backend hands the same record back for a second ask, so a reload that
// re-mints lands on the token the persisted URLs were built with.
// One ask, either flavour: "tab" is the unsandboxed token a page opened in the
// laptop's own browser is served under, anything else the pane's own. What
// comes back is the record or a bare {error}, because the two callers answer a
// refusal differently — one toasts it, the other takes its button away.
async function browserMint(url, mode) {
  const body = { url: url, prefix: browserPrefix(), origin: location.origin };
  if (mode) body.mode = mode;
  const r = await fetch(apiURL("api/browse"), {
    method: "POST", cache: "no-store",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => null);
  if (!r.ok || !d || !d.token) return { error: (d && d.error) || "" };
  return {
    token: d.token,
    prefix: typeof d.prefix === "string" ? d.prefix : browserPrefix(),
    expires: typeof d.expires === "number" ? d.expires : 0,
  };
}

async function browserEnsureToken(url) {
  if (browserToken) return browserToken;
  let rec;
  try {
    rec = await browserMint(url);
  } catch (e) {
    dbg("browse: mint failed", e);
    toast("Couldn't reach the computer");
    return null;
  }
  if (!rec.token) {
    toast(BROWSE_MINT_ERRORS[rec.error] || "Couldn't open that address");
    return null;
  }
  browserToken = rec;
  return browserToken;
}

// The other flavour's, kept until it is close to its own expiry rather than
// asked for per press: a token that has expired heals to the pane's, which is
// the sandboxed one, and a sandboxed tab is the one thing this button must not
// end up opening.
async function browserEnsureTabToken() {
  if (browserTabBlocked || !hasCapStrict("browse_tab")) return null;
  const now = Math.round(Date.now() / 1000);
  if (browserTabToken && browserTabToken.expires - now > 60) return browserTabToken;
  let rec;
  try {
    rec = await browserMint(browserCurrentUrl() || BROWSER_HOME, "tab");
  } catch (e) {
    dbg("browse: tab mint failed", e);
    return null;
  }
  if (!rec.token) {
    if (rec.error === "same_origin") {
      // The shell came off this same computer, so a page served without the
      // sandbox would land on the origin the pairing token is kept on, and the
      // computer refuses to mint one for it. Only the button is affected: the
      // pane is sandboxed on every install.
      browserTabBlocked = true;
      syncBrowseCap();
      toast("Opening in a tab needs the app from pockettui.com, not from this computer");
    }
    return null;
  }
  browserTabToken = rec;
  return browserTabToken;
}

// A window this pane opened, minus the ones already closed: a WindowProxy
// stays an object after its window is gone and only .closed says so, so the
// list is pruned wherever it is touched rather than swept.
function browserTrackTab(w) {
  browserTabs = browserTabs.filter((t) => t && !t.closed);
  browserTabs.push(w);
}

// Both tab buttons' shape. The window is opened blank inside the click and
// sent somewhere once the token is in hand: a popup blocker allows the window
// a gesture opens, not one opened a round trip later. `dest` says where, given
// the token, because only the token can spell it. The pre-mint on openBrowser
// means there is usually nothing to wait for; where there is not — a token
// that has just aged out — the window is already open and the blocker has
// already let it through.
function browserOpenTab(dest) {
  const w = window.open("", "_blank");
  if (!w) { toast("This browser blocked the tab"); return; }
  // No handle back on this window: the tab is not sandboxed, and opener is
  // what a page there would navigate the shell through.
  try { w.opener = null; } catch (e) {}
  browserEnsureTabToken().then((rec) => {
    const target = rec ? dest(rec) : "";
    if (!target) {
      try { w.close(); } catch (e) {}
      if (!browserTabBlocked) toast("Couldn't open that in a tab");
      return;
    }
    w.location = target;
    browserTrackTab(w);
  });
}

// Every way out of the pane ends here. A tab is the pane's own window shown
// elsewhere, so it goes when the pane goes — but not on a navigation inside
// the pane, where the tab is still the page the user sent there.
function browserCloseTabs() {
  for (const w of browserTabs) {
    try {
      if (!w || w.closed) continue;
      // Asked rather than closed outright. The tab was opened with its opener
      // nulled and it lives on the computer's origin, not this shell's, and a
      // window that is neither the caller's own nor same-origin refuses
      // close() — the page's shim hears this and closes itself. The direct
      // close is what answers on an install where the two do share an origin.
      w.postMessage("pockettui-close", browserOrigin());
      w.close();
    } catch (e) {}
  }
  browserTabs = [];
}

function browserSetField(url) {
  const f = $("browser-url");
  if (f) f.value = url || "";
}

function syncBrowserNav() {
  const back = $("btn-browser-back"), fwd = $("btn-browser-fwd");
  if (back) back.disabled = browserIdx <= 0;
  if (fwd) fwd.disabled = browserIdx < 0 || browserIdx >= browserStack.length - 1;
}

// A landing becomes the newest entry, and everything that was forward of it is
// gone — the same thing a browser's own back-then-elsewhere does. Landing
// again on the entry we are already on is a reload, not a new entry.
function browserPush(url) {
  if (!url || browserStack[browserIdx] === url) return;
  browserStack = browserStack.slice(0, browserIdx + 1);
  browserStack.push(url);
  browserIdx = browserStack.length - 1;
}

// Where the docked pane was left, against the session it was left in — read
// back at the next boot through the same record the other two panes use
// (cfg.sidePane, 02-debug-log.js). One localStorage write per landing, which
// is cheaper than any other way of not losing the page on a reload.
function browserRemember(url) {
  if (!browserDocked) return;
  const rec = cfg.sidePane;
  if (!rec || rec.owner !== "browser") return;
  cfg.sidePane = { owner: "browser", session: rec.session, url: url || "" };
}

// The URL a pane opened with nothing to show should go to: whatever this
// session's record kept. Only that session's — a page is opened beside a
// terminal and belongs to it, the way the folder and the diff do.
function browserRememberedUrl() {
  const rec = cfg.sidePane;
  return rec && rec.owner === "browser" && rec.session === (currentSession || "")
    ? browserNormalize(rec.url || "") : "";
}

// Send the frame somewhere. `push` is false for the moves that are not new
// places: back, forward, reload, and putting a restored pane back on the page
// it was already on.
async function browserNavigate(raw, push = true) {
  const typed = browserHasScheme(String(raw || "").trim());
  const url = browserNormalize(raw);
  if (!url) return;
  // A scheme this pane picked is a guess this navigation may have to take
  // back, and only its failure says so. Held for this navigation alone: the
  // retry below spells its scheme out, and a landing clears it.
  browserGuessed = typed ? "" : url;
  // A new destination is a new navigation, so the one silent re-mint it is
  // allowed comes back. Back, forward and reload keep whatever is left of it.
  if (push) browserReminted = false;
  if (!await browserEnsureToken(url)) return;
  const target = browserProxied(url);
  if (!target) { toast("That is not an address to open"); return; }
  const frame = $("browser-frame");
  if (!frame) return;
  // Before the load rather than after its landing: the frame's width is the
  // viewport the page lays itself out against, so setting it here is what
  // saves the zoomed page a reflow on its first paint.
  browserApplyZoom(browserZoomFor(browserZoomHost(url)));
  syncBrowserZoom(browserZoomHost(url));
  // The landing this load reports is this load, not a page moving itself.
  browserNavigating = true;
  if (browserPrimed) {
    // replace(), not assign(): a cross-origin frame's navigations still land
    // on the shell's joint history, and back would then walk the page stack
    // instead of leaving the screen. Allowed cross-origin, unlike reading the
    // same location back. The fallback is for a frame whose document has gone
    // away under us.
    try { frame.contentWindow.location.replace(target); }
    catch (e) { frame.src = target; }
  } else {
    // The frame has never navigated, so this assignment replaces its initial
    // entry rather than adding one. Every assignment after that would add one
    // — which is the whole reason the branch above exists, and why nothing
    // else in this file ever writes src.
    frame.src = target;
    browserPrimed = true;
  }
  browserLoadedUrl = url;
  if (push) browserPush(url);
  browserSetField(url);
  syncBrowserNav();
  browserRemember(url);
}

// ---- zoom ------------------------------------------------------------------

// The steps the two buttons walk, a desktop browser's own set. 1 is one of
// them, so a page that has been zoomed always has a step back to its size.
const BROWSER_ZOOMS = [0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2];

// Zoom belongs to the host, not to the page: a dev server read at 125% is read
// at 125% on every page it serves, which is what a desktop browser's per-site
// zoom does. The port is part of the key — :3000 and :8000 are two apps.
function browserZoomHost(url) {
  try { return new URL(url || browserCurrentUrl()).host; } catch (e) { return ""; }
}

function browserZoomFor(host) {
  const z = host ? cfg.browserZoom[host] : 0;
  return typeof z === "number" ? z : 1;
}

// Draw the page at this size, by scaling the frame element rather than the
// document inside it: the frame is laid out at 1/factor of the pane and
// scaled back down, so the page still reflows to a wider viewport the way a
// browser's own zoom does, while keeping a CSS pixel of its own.
//
// Zooming the document instead — `zoom` on its documentElement, which is what
// this used to message the shim to do — is what broke the MPG login page.
// Under CSS zoom a rect comes back multiplied by the factor and a length
// written from it is multiplied again, so every script that reads an
// element's rect and writes the number back as a top (jQuery's .offset() into
// an absolutely positioned overlay: select2, date pickers, tooltips) lands the
// overlay at factor times where it meant to. The institute dropdown opened on
// top of the control it hung from, and the mouseup that ended the click
// landed in the list and dismissed it again.
function browserApplyZoom(factor) {
  const frame = $("browser-frame");
  if (!frame) return;
  const pct = (100 / factor) + "%";
  frame.style.width = pct;
  frame.style.height = pct;
  frame.style.transform = factor === 1 ? "" : "scale(" + factor + ")";
}

// A zoom the user asked for: applied, remembered against the host, and shown
// on the panel that asked for it. No toast — the panel is open, its label is
// the reading, and a step that cannot go further leaves the number where it
// was, which is the honest answer to a press against an end.
function browserSetZoom(factor) {
  browserApplyZoom(factor);
  const host = browserZoomHost();
  if (host) {
    const rec = cfg.browserZoom;
    if (factor === 1) delete rec[host]; else rec[host] = factor;
    cfg.browserZoom = rec;
  }
  syncBrowserZoom();
}

// One button press. The next step past where the host is rather than the step
// after its index, so a remembered factor that is not on the list still moves
// one step the right way. At either end it re-applies what is already set,
// which is the honest answer to a press that cannot go further.
function browserStepZoom(delta) {
  if (!browserZoomHost()) return;          // nothing loaded, nothing to zoom
  const cur = browserZoomFor(browserZoomHost());
  const up = BROWSER_ZOOMS.filter((z) => z > cur + 1e-6);
  const down = BROWSER_ZOOMS.filter((z) => z < cur - 1e-6);
  const next = delta > 0 ? up[0] : down[down.length - 1];
  browserSetZoom(next === undefined ? cur : next);
}

// One key and a panel under it, not two keys: the docked bar is 360px wide and
// the pair took two of its few slots without ever saying what the page was at.
// The file bar's dropdown, drawn where one is needed (showViewMenu,
// 28-file-explorer.js): a class on the wrap, a scrim inside the pane.
function showBrowserZoomMenu(on) {
  $("browser-zoom-wrap").classList.toggle("open", on);
  $("browser-menu-scrim").classList.toggle("show", on);
  $("btn-browser-zoom").setAttribute("aria-expanded", on ? "true" : "false");
  if (on) syncBrowserZoom();
}

// The key's mark and the panel's reading. `host` is passed by the one caller
// that sets a zoom before its page is the current one — browserNavigate, which
// applies the factor ahead of the load so the page lays itself out once.
function syncBrowserZoom(host) {
  const factor = browserZoomFor(host === undefined ? browserZoomHost() : host);
  // A host left anywhere but 100% is a state that outlives the pane being
  // closed, so the key carries it: nothing else on screen would say so.
  $("btn-browser-zoom").classList.toggle("on", factor !== 1);
  $("browser-zoom-pct").textContent = Math.round(factor * 100) + "%";
}

// ---- bookmarks -------------------------------------------------------------
// Kept on the computer rather than in this browser's storage: the phone and the
// laptop reach the same machine, and a page worth keeping is worth keeping from
// both. The whole list goes back on every change — it is a handful of entries
// the shell already holds, so a merge protocol would buy nothing.

let browserMarks = [];           // as the computer holds them, oldest first
let browserMarksAsked = false;   // the GET has been made and answered
// What each page called itself, as its shim reported it. Kept here rather than
// read off the frame: the document is cross-origin and its title is not ours
// to ask for after the fact.
let browserMarkTitles = {};

function browserMarkAt(url) {
  return browserMarks.findIndex((b) => b && b.url === url);
}

// What to write on a chip: the title the page reported, and failing that its
// host — a chip with no words on it is not a chip.
function browserMarkName(url) {
  const t = browserMarkTitles[url];
  if (t) return t;
  try { return new URL(url).host; } catch (e) { return url; }
}

function browserMarkHost(url) {
  try { return new URL(url).host; } catch (e) { return url; }
}

// Asked once per pane opening rather than at boot: a shell that never opens the
// pane should not be asking this computer for a list it will not draw. A failed
// ask is not remembered, so the next opening tries again.
async function browserLoadMarks() {
  if (browserMarksAsked || !hasCapStrict("bookmarks")) return;
  browserMarksAsked = true;
  try {
    const r = await fetch(apiURL("api/browse/bookmarks"), {
      cache: "no-store", headers: authHeaders(),
    });
    const d = await r.json();
    if (!r.ok || !d || !Array.isArray(d.bookmarks)) throw new Error("bad answer");
    browserMarks = d.bookmarks;
  } catch (e) {
    dbg("bookmarks: load failed", e);
    browserMarksAsked = false;
    return;
  }
  renderBrowserMarks();
}

// Optimistic: the bar is already drawn from the change by the time this runs,
// because the alternative is a chip that appears a round trip after the tap. A
// refused write is the one case that reads back — what is on disk is then the
// truth and the bar is showing something that never got there.
async function browserSaveMarks() {
  try {
    const r = await fetch(apiURL("api/browse/bookmarks"), {
      method: "PUT", cache: "no-store",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ bookmarks: browserMarks }),
    });
    if (!r.ok) throw new Error("http " + r.status);
  } catch (e) {
    dbg("bookmarks: save failed", e);
    toast("Could not save bookmark");
    browserMarksAsked = false;
    browserLoadMarks();
  }
}

// The star is a toggle rather than a menu: there are two things anyone wants
// from the page they are on, and the bar underneath says which one happened.
function browserToggleMark() {
  const url = browserCurrentUrl();
  if (!url || !hasCapStrict("bookmarks")) return;
  const i = browserMarkAt(url);
  if (i >= 0) browserMarks.splice(i, 1);
  else browserMarks.push({ url: url, title: browserMarkName(url),
                           added: Math.round(Date.now() / 1000) });
  renderBrowserMarks();
  browserSaveMarks();
}

function renderBrowserMarks() {
  const bar = $("browser-bookmarks");
  const here = browserCurrentUrl();
  bar.textContent = "";
  for (const b of browserMarks) {
    if (!b || typeof b.url !== "string") continue;
    const url = b.url;
    const open = el("button", {
      type: "button", class: "bm-open", title: browserMarkHost(url),
      onclick: () => browserNavigate(url),
    }, b.title || browserMarkName(url));
    const drop = el("button", {
      type: "button", class: "bm-del", "aria-label": "Remove this bookmark",
      onclick: () => {
        const i = browserMarkAt(url);
        if (i < 0) return;
        browserMarks.splice(i, 1);
        renderBrowserMarks();
        syncBrowserStar();
        browserSaveMarks();
      },
    }, "\u00d7");
    bar.appendChild(el("div", { class: "bm-chip" + (url === here ? " on" : "") },
                       open, drop));
  }
  // Absent rather than empty: a pane nobody has bookmarked anything on looks
  // exactly as it did before the bar existed.
  bar.hidden = browserMarks.length === 0;
  syncBrowserStar();
}

// Whether the page on screen is one of them. Re-asked on every landing, since
// the page moves under the key.
function syncBrowserStar() {
  const btn = $("btn-browser-star");
  const on = browserMarkAt(browserCurrentUrl()) >= 0;
  btn.querySelector("use").setAttribute("href", on ? "#i-star-fill" : "#i-star");
  btn.setAttribute("aria-label", on ? "Remove this bookmark" : "Bookmark this page");
  btn.classList.toggle("on", on);
}

// ---- the two shapes --------------------------------------------------------

// Expanded is the pane's own state, but the class lives on #screen-term, for
// the docked explorer's reason: the seam it hides and the width it overrides
// are both read from there.
function syncBrowserExpand() {
  const on = browserDocked && browserExpanded;
  $("screen-term").classList.toggle("side-full", on);
  const btn = $("btn-browser-expand");
  if (!btn) return;
  btn.querySelector("use").setAttribute("href", on ? "#i-collapse" : "#i-expand");
  btn.setAttribute("aria-label", on ? "Shrink the browser pane" : "Expand the browser pane");
}

function openDockedBrowser() {
  browserDocked = true;
  browserOpen = true;
  // Redundant beside a live terminal — it is right there — and the pane has
  // its own cross for leaving.
  $("btn-browser-term").style.display = "none";
  $("screen-browser").classList.add("docked");
  $("screen-browser").classList.add("active");
  syncBrowserExpand();
  sideClaim("browser");
  browserRemember(browserCurrentUrl());
}

// The frame keeps its page: the pane is a tap away again, and reloading a dev
// server every time it is closed would be the wrong trade. Only the slot and
// the classes go back.
function closeDockedBrowser() {
  if (!browserDocked) return;
  browserDocked = false;
  browserOpen = false;
  browserCloseTabs();
  showBrowserZoomMenu(false);
  $("screen-browser").classList.remove("docked");
  $("screen-browser").classList.remove("active");
  syncBrowserExpand();       // takes .side-full off the terminal with it
  sideDrop("browser");
}

function openFullBrowser() {
  if (browserOpen && !browserDocked) return;
  browserOriginFrom = $("screen-term").classList.contains("active")
    ? "screen-term" : "screen-list";
  $(browserOriginFrom).classList.remove("active");
  // Only the terminal is worth a one-tap way back to — the button says
  // terminal, exactly as the explorer's does.
  $("btn-browser-term").style.display =
    browserOriginFrom === "screen-term" ? "" : "none";
  browserDocked = false;
  browserOpen = true;
  $("screen-browser").classList.remove("docked");
  $("screen-browser").classList.add("active");
  syncBrowserExpand();
  syncChrome();
  history.pushState({ browser: true }, "", location.href);
}

// The pop's half: the entry is already spent by the time this runs, so nothing
// here touches history.
function closeFullBrowser() {
  browserCloseTabs();
  showBrowserZoomMenu(false);
  $("screen-browser").classList.remove("active");
  const back = browserOriginFrom || "screen-list";
  browserOriginFrom = null;
  browserOpen = false;
  $(back).classList.add("active");
  syncChrome();
  // The terminal kept its socket while we were away; it only needs its size
  // re-checked, not a reconnect.
  if (back === "screen-term") refit(0);
}

const BROWSER_HOME = "https://www.google.com/";

// Every way in lands here — the globe key, the header button, a tapped private
// URL in the terminal, a restored session — so the two shapes are one entry
// point, the way openExplorer is for the folder.
function openBrowser(url) {
  if (needsSetup()) { openSettings(true); return; }
  if (demoMode) { toast("No browser in the demo"); return; }
  if (isWideLayout() && $("screen-term").classList.contains("active")) openDockedBrowser();
  else openFullBrowser();
  browserLoadMarks();
  // Ahead of any press, so the button's own click has nothing to wait for.
  browserEnsureTabToken();
  const target = browserNormalize(url);
  if (target) { browserNavigate(target); return; }
  // Opened with nothing to go to: the page this pane was last on, whether it
  // is still in the frame (a close keeps it) or only in this session's record
  // (a reload does not).
  // Failing both, a search page: a browser that opens on a blank frame reads
  // as broken, and the address field is one tap away either way.
  const last = browserCurrentUrl() || browserRememberedUrl() || BROWSER_HOME;
  if (last !== browserLoadedUrl) browserNavigate(last, browserIdx < 0);
  else { browserSetField(last); syncBrowserNav(); }
}

// ---- what the proxied page says back ---------------------------------------

window.addEventListener("message", (e) => {
  const frame = $("browser-frame");
  // The source is the identity check: this is the frame this pane owns, not
  // some other window. The origin is only a second gate, and the origin to
  // expect is "null" — the frame is sandboxed without allow-same-origin (the
  // iframe attribute, and the sandbox CSP the proxy puts on every page it
  // serves), so a proxied page sits on an opaque origin and posts from it.
  // The backend's own origin is allowed beside it for a page served without
  // that sandbox.
  if (!frame || e.source !== frame.contentWindow) return;
  if (e.origin !== "null" && e.origin !== browserOrigin()) return;
  const d = e.data;
  if (!d || typeof d !== "object") return;

  if (d.type === "pockettui-nav") {
    const url = browserNormalize(typeof d.url === "string" ? d.url : "");
    if (!url) return;
    // Not while it is being typed into: the user is mid-address and the page
    // finishing its load must not take the field away from them.
    if (document.activeElement !== $("browser-url")) browserSetField(url);
    if (browserNavigating) {
      // The load this pane asked for, so this is the address it landed on
      // rather than somewhere new: it corrects the entry the navigation made,
      // which is how a redirect ends up recorded as where it went.
      browserNavigating = false;
      if (browserIdx < 0) browserPush(url);
      else browserStack[browserIdx] = url;
    } else {
      // The page moved itself. A shim reports a landing more than once (the
      // document is ready, then the load finishes), and the ones after the
      // first name the entry we are already on, which browserPush lets be.
      browserPush(url);
    }
    browserLoadedUrl = url;
    // Zoom belongs to the host and the frame keeps whatever scale it was
    // given, so a landing corrects it — including back to 1 for a host that
    // was never zoomed. A load reports more than once and each report sets
    // the same factor, which is a no-op after the first.
    browserApplyZoom(browserZoomFor(browserZoomHost(url)));
    syncBrowserZoom(browserZoomHost(url));
    // What the page calls itself, for the chip a bookmark of it would carry:
    // the document is cross-origin, so this report is the only time it says.
    if (typeof d.title === "string" && d.title) browserMarkTitles[url] = d.title;
    // The page moved under the panel, and what it was showing was about the
    // page that was there. The bar redraws for the same reason: which chip is
    // the one on screen has just changed.
    showBrowserZoomMenu(false);
    renderBrowserMarks();
    // A page that loaded is a token that works and a scheme that was right.
    browserReminted = false;
    browserGuessed = "";
    syncBrowserNav();
    browserRemember(url);
    return;
  }

  // The frame's own back/forward/go, which the shim sends here rather than
  // letting it walk the shell's joint history and close the pane.
  if (d.type === "pockettui-history") {
    const delta = typeof d.delta === "number" ? d.delta : 0;
    if (Number.isFinite(delta) && delta) browserStep(Math.trunc(delta));
    return;
  }

  if (d.type === "pockettui-browse-error") {
    const code = typeof d.code === "string" ? d.code : "";
    // Where this pane thinks it is, never the address the message carries:
    // that one is the error page's own location, which is a proxied address,
    // and retrying it asks the backend to proxy itself — the layer that made
    // the founder's remembered URL twenty deep.
    const here = browserCurrentUrl();
    // The token has a sliding expiry and a restart mints a new one, so a pane
    // left open finds its own gone. One silent re-mint and one retry per
    // navigation; a second expiry with nothing landed in between is news.
    if (code === "expired" && here && !browserReminted) {
      browserReminted = true;
      browserToken = null;
      browserNavigate(here, false);
      return;
    }
    // A scheme this pane guessed, on a host that does not answer it: the other
    // one, once, in place of this entry rather than after it. The address bar
    // never showed a scheme, so neither should the stack.
    if (BROWSE_SCHEME_RETRY[code] && browserGuessed && browserGuessed === here) {
      const other = browserFlipScheme(here);
      if (other) {
        browserGuessed = "";
        if (browserIdx >= 0) browserStack[browserIdx] = other;
        browserNavigate(other, false);
        return;
      }
    }
    toast(BROWSE_ERRORS[code] || "That page could not be loaded");
  }
});

// ---- the topbar ------------------------------------------------------------

// One step along the pane's own list, for the two buttons and for a page that
// called history.back() inside the frame. A step off either end is no step.
function browserStep(delta) {
  const i = browserIdx + delta;
  if (browserIdx < 0 || i === browserIdx || i < 0 || i >= browserStack.length) return;
  browserIdx = i;
  browserNavigate(browserStack[browserIdx], false);
}

$("btn-browser-back").addEventListener("click", () => browserStep(-1));
$("btn-browser-fwd").addEventListener("click", () => browserStep(1));
$("btn-browser-reload").addEventListener("click", () => {
  const u = browserCurrentUrl();
  if (u) browserNavigate(u, false);
});
$("btn-browser-zoom").addEventListener("click", () => {
  showBrowserZoomMenu(!$("browser-zoom-wrap").classList.contains("open"));
});
// Stepping leaves the panel up: a zoom is walked to, not picked, and the label
// above these two is what says where it got to.
$("browser-zoom-minus").addEventListener("click", () => browserStepZoom(-1));
$("browser-zoom-plus").addEventListener("click", () => browserStepZoom(1));
$("browser-zoom-pct").addEventListener("click", () => browserSetZoom(1));
$("browser-menu-scrim").addEventListener("click", () => showBrowserZoomMenu(false));
$("btn-browser-star").addEventListener("click", () => browserToggleMark());
// The page as a top-level tab in this laptop's own browser, still fetched
// through the computer: the backend serves it under the unsandboxed token, so
// the tab's documents share one real origin and an app written to be the top
// window works — top is the page itself and its frames are its own to script,
// neither of which is true in the pane. The pane's own frame keeps its sandbox.
$("btn-browser-tab").addEventListener("click", () => {
  const url = browserCurrentUrl();
  if (!url) return;
  browserOpenTab((rec) => browserEnterUrl(browserProxied(url, rec), rec));
});
// The same tab with nothing in it yet, which is the computer's start page: an
// address typed there is fetched by the computer, where one typed into the
// browser's own bar would be fetched by the laptop and reach none of this.
$("btn-browser-newtab").addEventListener("click", () => {
  browserOpenTab(browserStartUrl);
});
$("btn-browser-expand").addEventListener("click", () => {
  browserExpanded = !browserExpanded;
  cfg.browserExpanded = browserExpanded;
  syncBrowserExpand();
  refit(0);
});
$("btn-browser-close").addEventListener("click", () => closeDockedBrowser());
$("btn-browser-term").addEventListener("click", () => history.back());

$("browser-url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    const v = $("browser-url").value.trim();
    if (v) browserNavigate(v);
    // Blurred either way: on a phone the address bar is what the keyboard is
    // up for, and the page underneath is what the tap was about.
    $("browser-url").blur();
    return;
  }
  if (e.key === "Escape") {
    e.preventDefault();
    // Not the pane's Escape — this one only puts the field back.
    e.stopPropagation();
    browserSetField(browserCurrentUrl());
    $("browser-url").blur();
  }
});

// ---- leaving ---------------------------------------------------------------

// A hardware keyboard's Escape does what the close button does, the docked
// explorer's rule and for its reasons: on capture so the field's own handler
// below it keeps its own Escape, and, docked, only for a press aimed inside
// the pane — the terminal beside it is live and Escape is one of its keys.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("screen-browser").classList.contains("active")) return;
  if (e.target === $("browser-url")) return;
  if (browserDocked && !$("screen-browser").contains(e.target)) return;
  // Ahead of the pane's own Escape: an open panel is the top thing to dismiss,
  // exactly as the file bar's dropdowns are.
  if ($("browser-zoom-wrap").classList.contains("open")) {
    e.preventDefault();
    showBrowserZoomMenu(false);
    return;
  }
  if ($("sheet-scrim").classList.contains("show")) return;
  e.preventDefault();
  if (browserDocked) closeDockedBrowser();
  else history.back();
}, true);

window.addEventListener("popstate", () => {
  // The docked pane pushed nothing, so no pop is ever its own.
  if (browserDocked) return;
  if (!$("screen-browser").classList.contains("active")) return;
  closeFullBrowser();
});

// The left-edge back gesture the other full-screen views carry
// (attachEdgeSwipe, 28-file-explorer.js). Docked it never fires: the pane is
// beside the terminal, not over it, and the gesture belongs to the screen.
attachEdgeSwipe($("screen-browser"), () => history.back());

// ---- per-session and per-computer state ------------------------------------

// The pane goes down with the session it belongs to (fileViews,
// 09-image-viewer.js). No history is touched here: the caller is replacing
// every screen at once and spends the entries itself.
function browserStash() {
  return {
    docked: browserDocked, url: browserCurrentUrl(),
    stack: browserStack.slice(), idx: browserIdx,
  };
}

function browserTeardown() {
  const wasDocked = browserDocked;
  browserDocked = false;
  browserOpen = false;
  browserOriginFrom = null;
  browserCloseTabs();
  showBrowserZoomMenu(false);
  $("screen-browser").classList.remove("docked");
  $("screen-browser").classList.remove("active");
  if (wasDocked) { syncBrowserExpand(); sideDrop("browser"); }
}

// Only the docked shape comes back: it is the only one a rail switch can
// happen under. The full-screen pane covers the list the switch is made from,
// so a session is never left with one up.
function browserRestore(s) {
  // Through the normaliser rather than as they were kept: a stash written
  // before a restart can hold a proxied address, and restoring one as a page
  // address is what the pane would then navigate to.
  browserStack = (s.stack || []).map((u) => browserNormalize(u));
  browserIdx = typeof s.idx === "number" ? s.idx : browserStack.length - 1;
  if (!s.docked) { syncBrowserNav(); return; }
  openDockedBrowser();
  const u = browserCurrentUrl();
  if (u && u !== browserLoadedUrl) browserNavigate(u, false);
  else { browserSetField(u); syncBrowserNav(); }
}

// Everything the pane holds about the computer being left, for a switch to
// another one (switchProfile, 40-profiles.js). The token is that machine's
// process, and a page it was serving is not a page on the next one.
function browserResetForProfile() {
  if (browserDocked) closeDockedBrowser();
  else browserTeardown();
  browserToken = null;
  // The next computer answers for itself, both on the flavour and on whether
  // it is the one that served this shell.
  browserTabToken = null;
  browserTabBlocked = false;
  browserStack = [];
  browserIdx = -1;
  browserLoadedUrl = "";
  browserReminted = false;
  browserGuessed = "";
  browserApplyZoom(1);
  // Another computer keeps its own list, and the one on screen is this one's.
  browserMarks = [];
  browserMarksAsked = false;
  browserMarkTitles = {};
  renderBrowserMarks();
  showBrowserZoomMenu(false);
  // Blanked through the frame's own location, not through src: the pane may
  // well be reopened on the new computer, and an src assignment would put an
  // entry on the shell's history that back would spend on the frame.
  const frame = $("browser-frame");
  if (frame && browserPrimed) {
    try { frame.contentWindow.location.replace("about:blank"); }
    catch (e) { frame.removeAttribute("src"); browserPrimed = false; }
  }
  browserSetField("");
  syncBrowserNav();
}

// Whether the computer on the other end can proxy pages at all. Strictly
// checked: a server too old for api/browse 404s the mint, and the pill's globe
// key stays hidden rather than being left to fail. The header button is not a
// control any more (the pane is a two-pane feature) — it is what buildKeybar
// reads the answer off on a rebuild, which is why its hidden state is still
// kept, and this is the one place either of them is set.
function syncBrowseCap() {
  const on = hasCapStrict("browse");
  const btn = $("btn-browser");
  if (btn) btn.hidden = !on;
  const key = $("keybar").querySelector(".k-browser");
  if (key) key.classList.toggle("show", on);
  // Its own capability, not the proxy's: a server can store bookmarks without
  // httpx to fetch pages with, and one too old for the route would answer the
  // save with a 404 the user only learns about after tapping the star.
  $("btn-browser-star").hidden = !hasCapStrict("bookmarks");
  // Strictly checked too, and with the computer's own refusal on top of it: a
  // server too old for the mode answers with the pane's sandboxed token, and a
  // tab opened on that cannot do the one thing it was opened for.
  const tab = !hasCapStrict("browse_tab") || browserTabBlocked;
  $("btn-browser-tab").hidden = tab;
  // The empty tab is the same flavour of token on the same route, so it stands
  // or falls with the button beside it.
  $("btn-browser-newtab").hidden = tab;
}

syncBrowserNav();
