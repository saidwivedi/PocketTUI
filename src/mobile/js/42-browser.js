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
// The one address a fresh token has already been minted for. A second
// expiry on the same address is a real refusal, not a stale token.
let browserRemintUrl = "";
let browserExpanded = cfg.browserExpanded;

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
// back out of cfg. An address with no scheme is http, the way every address
// bar reads one, and the parser then spells the rest the one way — a default
// port dropped, an empty path a slash. That is the spelling the shim's report
// carries back (its unmap does the same two things), so a typed address and
// its own landing compare equal instead of stacking up as two entries.
function browserNormalize(raw) {
  const s = (raw || "").trim();
  if (!s) return "";
  const full = /^[a-z][a-z0-9+.-]*:/i.test(s) ? s : "http://" + s;
  // Unparseable is left as typed: browserProxied refuses it a step later, and
  // its toast is the one the user should get.
  try { return new URL(full).href; } catch (e) { return full; }
}

// The address as the proxy serves it. Built here rather than asked for per
// navigation: the mint already said what the prefix and the token are, and a
// round trip per link would be felt.
function browserProxied(raw) {
  if (!browserToken) return "";
  let u;
  try { u = new URL(raw); } catch (e) { return ""; }
  if (u.protocol !== "http:" && u.protocol !== "https:") return "";
  const secure = u.protocol === "https:";
  // The port is part of the host segment always, so the proxy never has to
  // guess it back from the scheme. hostname keeps an IPv6 literal's brackets.
  const port = u.port || (secure ? "443" : "80");
  return browserOrigin() + browserToken.prefix + "/b/" + browserToken.token
       + "/" + (secure ? "s" : "h") + "/" + u.hostname + ":" + port
       + u.pathname + u.search + u.hash;
}

// One token per shell load, minted on the first navigation and kept. The
// backend hands the same record back for a second ask, so a reload that
// re-mints lands on the token the persisted URLs were built with.
async function browserEnsureToken(url) {
  if (browserToken) return browserToken;
  try {
    const r = await fetch(apiURL("api/browse"), {
      method: "POST", cache: "no-store",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ url: url, prefix: browserPrefix(), origin: location.origin }),
    });
    const d = await r.json().catch(() => null);
    if (!r.ok || !d || !d.token) {
      toast((d && BROWSE_MINT_ERRORS[d.error]) || "Couldn't open that address");
      return null;
    }
    browserToken = {
      token: d.token,
      prefix: typeof d.prefix === "string" ? d.prefix : browserPrefix(),
    };
    return browserToken;
  } catch (e) {
    dbg("browse: mint failed", e);
    toast("Couldn't reach the computer");
    return null;
  }
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
  const url = browserNormalize(raw);
  if (!url) return;
  if (!await browserEnsureToken(url)) return;
  const target = browserProxied(url);
  if (!target) { toast("That is not an address to open"); return; }
  const frame = $("browser-frame");
  if (!frame) return;
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
    browserRemintUrl = "";     // a page that loaded is a token that works
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
    const url = (typeof d.url === "string" && d.url) || browserCurrentUrl();
    // The token has a sliding expiry, so a pane left open overnight finds it
    // gone. One silent re-mint and one retry, then it is news.
    if (code === "expired" && url && browserRemintUrl !== url) {
      browserRemintUrl = url;
      browserToken = null;
      browserNavigate(url, false);
      return;
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
// The relay path, untouched: this hands the address to the phone's own browser
// exactly as a tapped link used to, port forwarding and all (08-links.js).
$("btn-browser-tab").addEventListener("click", () => {
  const u = browserCurrentUrl();
  if (u) openUrl(u);
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
  $("screen-browser").classList.remove("docked");
  $("screen-browser").classList.remove("active");
  if (wasDocked) { syncBrowserExpand(); sideDrop("browser"); }
}

// Only the docked shape comes back: it is the only one a rail switch can
// happen under. The full-screen pane covers the list the switch is made from,
// so a session is never left with one up.
function browserRestore(s) {
  browserStack = (s.stack || []).slice();
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
  browserStack = [];
  browserIdx = -1;
  browserLoadedUrl = "";
  browserRemintUrl = "";
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
// checked: a server too old for api/browse 404s the mint, and both the header
// button and the pill's globe key are hidden rather than left to fail. The key
// reads its state off the button (buildKeybar, 19-voice-capture.js), so this
// is the one place the answer lands.
function syncBrowseCap() {
  const on = hasCapStrict("browse");
  const btn = $("btn-browser");
  if (btn) btn.hidden = !on;
  const key = $("keybar").querySelector(".k-browser");
  if (key) key.classList.toggle("show", on);
}

syncBrowserNav();
