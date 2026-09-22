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
//
// The pane has tabs, and they are a browser's: a strip of chips above the page,
// one frame per tab kept in the wrap while its tab is off screen, and the last
// tab's cross closing the pane the way a window's does. So everything that was
// once this file's own state — the stack, where in it the frame is, what it
// last loaded — belongs to a tab (browserNewTab below), and browserTab() is the
// one the chrome reads: the address field, the arrows, the zoom key and the
// star are the active tab's. A landing is routed the other way round, to
// whichever tab's frame posted it, so a tab that is not on screen still keeps
// its stack and its chip's name up to date.

let browserDocked = false;      // in the slot beside the terminal, not over it
let browserOpen = false;        // either shape is up
let browserToken = null;        // {token, prefix} once api/browse has answered
// Which screen the full-screen shape covered, to put back when it closes.
let browserOriginFrom = null;
let browserExpanded = cfg.browserExpanded;
// The permission a tab put on the computer's own network is served under,
// asked for when the pane opens rather than at the press: the key should turn a
// page round, not first wait on a round trip to the computer.
let browserTabToken = null;
// This computer serves the shell itself, so it will not grant that (see the
// mint's same-origin gate). Asked once, and the key goes.
let browserTabBlocked = false;

// As many as the strip holds and anybody keeps track of. Eight chips still
// carry a readable name at the docked width; past that a tab is a sliver.
const BROWSER_TAB_MAX = 8;

// One tab. `frame` is made on its first navigation (browserFrame) and stays in
// the wrap, hidden, while another tab is on screen — which is what makes going
// back to a tab free rather than a reload of whatever it was running.
function browserNewTab() {
  return {
    frame: null,
    // Its chip in the strip, made with the tab's first drawing and kept: the
    // strip is updated in place rather than redrawn (renderBrowserTabs), and
    // this is the element that stays put under the keyboard.
    chip: null,
    stack: [],          // the addresses this tab visited, oldest first
    idx: -1,            // where in that list its frame currently is
    // What its frame was last sent to, so showing a tab it is already on costs
    // no reload — and "" for a tab restored from a record, which is how a
    // reload's other tabs stay unloaded until somebody asks for one.
    loaded: "",
    // What the page called itself, and which address said so: a tab that has
    // moved on carries no name until its new page reports one.
    title: "",
    titleFor: "",
    // True from the moment this pane asks the frame to load until the shim
    // reports the landing: that report is the load we asked for, not a page
    // navigating itself, so it corrects the entry the navigation already made
    // instead of adding one.
    navigating: false,
    // The frame has been navigated. Until it has, its one free navigation is
    // worth spending on the first real page: an iframe's first src assignment
    // replaces its initial entry, and every one after that adds a joint history
    // entry that the shell's own back would spend on the frame instead of on
    // the screen.
    primed: false,
    // A fresh token has already been minted for the navigation in flight. A
    // second expiry before anything has loaded is a real refusal, not a stale
    // token.
    reminted: false,
    // The address of the navigation in flight, when this pane picked its scheme
    // rather than reading one. Only then is a refusal worth retrying the other
    // way round — and only until something lands, which is what clears it.
    guessed: "",
    // On the computer's own network: this tab's pages are fetched as pages in
    // their own right rather than as something shown inside the app, so each
    // one is the top window of its own document and may reach the views it
    // writes. Per tab, so one portal can have it while the tab beside it does
    // not, and kept in the pane's record so a reload brings it back.
    lan: false,
  };
}

// Left to right, as the strip draws them, and never empty: a pane with no tab
// is a pane with no address field to type into, so closing the last one closes
// the pane and leaves a fresh tab behind for the next opening.
let browserTabs = [browserNewTab()];
let browserActive = 0;

function browserTab() { return browserTabs[browserActive]; }

// The pane's own life, counted. A navigation goes away to the computer for a
// token and comes back to whatever the pane is by then: the tab it was started
// for may have been closed under it, and the pane itself may have been torn
// down or moved to another computer — either of which leaves the record it was
// holding out of browserTabs and every list rebuilt around it. A frame made for
// such a record is an orphan in the wrap loading a page nobody is looking at,
// so every await in the paths below is followed by this.
let browserGen = 0;
function browserAlive(tab, gen) {
  return gen === browserGen && browserTabs.indexOf(tab) >= 0;
}

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
  browse_unavailable: "Browsing isn't available on this computer yet",
};

// Where a tab is, and where the pane is: the pane is wherever its active tab
// is, which is the rule the whole topbar follows.
function browserUrlIn(tab) {
  return tab && tab.idx >= 0 ? (tab.stack[tab.idx] || "") : "";
}

function browserCurrentUrl() { return browserUrlIn(browserTab()); }

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

// What a line typed in the address field means. The field is two fields in one,
// the way every browser's is: an address goes to the page, anything else to the
// search engine — the founder's `how to cook rice` was refused as no address
// at all, and `weather` sent to a host called weather. Chrome's rule, which is
// the one people have in their fingers: a scheme settles it, whitespace settles
// it the other way, and what is left is an address only if its first segment
// reads like a host or a path was typed after it. isPrivateHost is not the test
// here: it answers true for
// every dotless name, which is right for the scheme guess (a dev box speaks
// http) and wrong for this one, where a single word is far more often something
// to look up. A tailnet short name still reaches its box as `mybox/` or
// `mybox:3000`, the same as in Chrome.
function browserTyped(raw) {
  const s = (raw || "").trim();
  if (!s || browserHasScheme(s)) return s;
  if (/\s/.test(s)) return BROWSER_SEARCH + encodeURIComponent(s);
  // Userinfo is not part of the name that has to look like a host.
  const seg = s.split(/[/?#]/)[0];
  const host = seg.slice(seg.lastIndexOf("@") + 1);
  const v6 = host.charAt(0) === "[";
  const name = v6 ? host.slice(0, host.indexOf("]") + 1) : host.split(":")[0];
  const address = v6                    // a bracketed literal is nothing else
      || /:\d+$/.test(host)             // a port is nobody's search term
      || name.indexOf(".") !== -1       // a dot: a public name, or a v4 address
      || name === "localhost"           // the one host that needs none
      || s.indexOf("/") !== -1;         // a path: `mybox/` is meant as one
  return address ? s : BROWSER_SEARCH + encodeURIComponent(s);
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
// the sandboxed one, and a sandboxed page is the one thing a tab in this mode
// must not end up being served.
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
      // The shell came off this same computer, so a page fetched as a page in
      // its own right would land on the origin the pairing lives on, and the
      // computer refuses to allow it. Only this key is affected: every other
      // tab works on every install.
      browserTabBlocked = true;
      syncBrowseCap();
      toast("The local network needs the app from pockettui.com");
    }
    return null;
  }
  browserTabToken = rec;
  return browserTabToken;
}

function browserSetField(url) {
  const f = $("browser-url");
  if (f) f.value = url || "";
}

// A tab's own frame, cut from the template the markup keeps so that every one
// of them carries the same sandbox list — that list and the proxy's CSP header
// have to agree word for word, and a frame built any other way would be a frame
// with other powers. Made on first use rather than with the tab: a tab restored
// from a record has an address and nothing loaded, and making its frame here is
// what keeps that promise.
function browserFrame(tab) {
  if (tab.frame) return tab.frame;
  // A record the strip no longer holds is a tab that has been closed, or one
  // from before a teardown: whatever was still in flight for it gets no frame
  // out of this, whoever asks and however late.
  if (browserTabs.indexOf(tab) < 0) return null;
  const tpl = $("browser-frame-tpl");
  if (!tpl) return null;
  tab.frame = tpl.content.firstElementChild.cloneNode(true);
  // A tab on the computer's own network is this list's absence and nothing
  // else. The sandbox is what gives a proxied document an origin of its own,
  // and an origin of its own is exactly what stops a page being the top window
  // its scripts look for or reaching the views it wrote. Taken off the element
  // before it is in the document, because the attribute is read when a frame
  // loads: a frame that has already loaded cannot change its mind, which is
  // why browserSetLan replaces the element rather than editing it.
  if (tab.lan) tab.frame.removeAttribute("sandbox");
  tab.frame.hidden = tab !== browserTab();
  $("browser-wrap").appendChild(tab.frame);
  return tab.frame;
}

// Every frame this pane holds, gone — for a teardown, a profile switch, or the
// last tab's close. The tabs keep their addresses; what goes is the page.
function browserDropFrames() {
  for (const tab of browserTabs) {
    if (tab.frame) tab.frame.remove();
    tab.frame = null;
    tab.primed = false;
    tab.loaded = "";
    tab.navigating = false;
  }
}

function syncBrowserNav() {
  const tab = browserTab();
  const back = $("btn-browser-back"), fwd = $("btn-browser-fwd");
  if (back) back.disabled = tab.idx <= 0;
  if (fwd) fwd.disabled = tab.idx < 0 || tab.idx >= tab.stack.length - 1;
}

// A landing becomes that tab's newest entry, and everything that was forward of
// it is gone — the same thing a browser's own back-then-elsewhere does. Landing
// again on the entry the tab is already on is a reload, not a new entry.
function browserPush(tab, url) {
  if (!url || tab.stack[tab.idx] === url) return;
  tab.stack = tab.stack.slice(0, tab.idx + 1);
  tab.stack.push(url);
  tab.idx = tab.stack.length - 1;
}

// Where the docked pane was left, against the session it was left in — read
// back at the next boot through the same record the other two panes use
// (cfg.sidePane, 02-debug-log.js). One localStorage write per landing, which
// is cheaper than any other way of not losing the pages on a reload. Every
// tab's address goes in, and `url` stays the active one's: an older shell
// reading this record knows only that key, and the tab it puts back should be
// the tab that was on screen.
function browserRemember() {
  if (!browserDocked) return;
  const rec = cfg.sidePane;
  if (!rec || !rec.rows.includes("browser")) return;
  // A tab with nothing in it yet is not an address to come back to, so it is
  // left out — and the index has to be the one the shortened list spells,
  // which is what the walk below counts.
  const tabs = [];
  let at = 0;
  for (let i = 0; i < browserTabs.length; i++) {
    const u = browserUrlIn(browserTabs[i]);
    if (!u) continue;
    if (i <= browserActive) at = tabs.length;
    // A bare address for an ordinary tab, the pair only where there is
    // something more to say: a shell too old to know about the mode reads the
    // strings and still gets every one of those tabs back.
    tabs.push(browserTabs[i].lan ? { url: u, lan: true } : u);
  }
  // Written back over the record as it stands rather than as a record of its
  // own: the rows and their order are the column's half of this key, and the
  // browser rewriting it on every landing must not be what loses them. Under
  // this pane's own row id, so a second browser in the other row keeps its own
  // strip rather than the two writing over each other.
  const panes = Object.assign({}, rec.panes);
  panes.browser = { url: browserCurrentUrl(), tabs: tabs, tab: at };
  cfg.sidePane = Object.assign({}, rec, { panes: panes });
}

// The URL a pane opened with nothing to show should go to: whatever this
// session's record kept. Only that session's — a page is opened beside a
// terminal and belongs to it, the way the folder and the diff do.
function browserRememberedUrl() {
  const rec = cfg.sidePane;
  if (!rec || rec.session !== (currentSession || "")) return "";
  // Keyed by this pane's row: the record holds an entry for every browser row it
  // names and none for a row it does not, so this is also the check that the
  // pane was one of them.
  const p = rec.panes.browser;
  return p ? browserNormalize(p.url || "") : "";
}

// Send a tab's frame somewhere. `push` is false for the moves that are not new
// places: back, forward, reload, and putting a restored tab back on the page it
// was already on. Every part of it that is chrome rather than frame — the
// address field, the arrows, the zoom key — is done only for the tab on screen;
// a background tab that is still loading has no business moving them.
async function browserNavigateIn(tab, raw, push = true) {
  const gen = browserGen;
  const typed = browserHasScheme(String(raw || "").trim());
  const url = browserNormalize(raw);
  if (!url) return;
  // A scheme this pane picked is a guess this navigation may have to take
  // back, and only its failure says so. Held for this navigation alone: the
  // retry below spells its scheme out, and a landing clears it.
  tab.guessed = typed ? "" : url;
  // A new destination is a new navigation, so the one silent re-mint it is
  // allowed comes back. Back, forward and reload keep whatever is left of it.
  if (push) tab.reminted = false;
  // Which permission this tab's pages are fetched under, and where its frame is
  // sent. On the computer's own network it is the other one, and a frame that
  // has not loaded anything yet goes in through the hop that clears this
  // computer's address of whatever a shell it once served left there — before
  // the first document that could read it runs. Past that hop the pages are
  // ordinary ones: going through it again would wipe what the page itself has
  // since put there, which is the session the user just logged in with.
  let target;
  if (tab.lan) {
    const rec = await browserEnsureTabToken();
    if (!browserAlive(tab, gen)) return;
    if (!rec) { toast("Couldn't reach the computer"); return; }
    const proxied = browserProxied(url, rec);
    target = tab.primed ? proxied : browserEnterUrl(proxied, rec);
  } else {
    if (!await browserEnsureToken(url)) return;
    // The mint is a round trip, and this may be a tab the strip has closed or a
    // pane that has been taken down since it was asked for. Nothing below this
    // line is worth doing then, and the frame it would make is worse than
    // nothing.
    if (!browserAlive(tab, gen)) return;
    target = browserProxied(url);
  }
  if (!target) { toast("That is not an address to open"); return; }
  const frame = browserFrame(tab);
  if (!frame) return;
  // Before the load rather than after its landing: the frame's width is the
  // viewport the page lays itself out against, so setting it here is what
  // saves the zoomed page a reflow on its first paint.
  browserApplyZoom(browserZoomFor(browserZoomHost(url)), frame);
  if (tab === browserTab()) syncBrowserZoom(browserZoomHost(url));
  // The landing this load reports is this load, not a page moving itself.
  tab.navigating = true;
  if (tab.primed) {
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
    tab.primed = true;
  }
  tab.loaded = url;
  if (push) browserPush(tab, url);
  if (tab === browserTab()) { browserSetField(url); syncBrowserNav(); }
  renderBrowserTabs();
  browserRemember();
}

// The tab on screen, which is what every control in the topbar acts on.
function browserNavigate(raw, push = true) {
  return browserNavigateIn(browserTab(), raw, push);
}

// ---- the computer's own network --------------------------------------------

// The key's own state, read off the tab on screen — which is what makes the
// mode a tab's rather than the pane's: a switch re-reads it here, and a tab
// that was never turned round is dark beside one that was.
function syncBrowserLan() {
  const btn = $("btn-browser-tab");
  if (!btn) return;
  const on = !!(browserTab() && browserTab().lan);
  btn.classList.toggle("on", on);
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  // A glyph nobody has met before says nothing on its own, so the key says what
  // pressing it would do — and, once it is pressed, what it did. The tooltip
  // and the label are the same words: a reader who hovers and a reader who
  // listens are being told the same thing.
  const said = on ? "This tab uses the computer's network"
                  : "Use the computer's network for this tab";
  btn.setAttribute("aria-label", said);
  btn.setAttribute("title", said);
}

// Turn one tab round, or back. The frame is replaced rather than edited: what
// the mode comes down to is one attribute on it, and that attribute is read
// when a frame loads, so a frame already running cannot change its mind. The
// replacement also throws away the document that was running under the old
// powers, which is the point — the page is fetched again as the other kind.
function browserSetLan(tab, on) {
  if (!!tab.lan === !!on) return;
  tab.lan = !!on;
  if (tab.frame) { tab.frame.remove(); tab.frame = null; }
  tab.primed = false;
  tab.loaded = "";
  tab.navigating = false;
  tab.reminted = false;
  if (tab === browserTab()) syncBrowserLan();
  renderBrowserTabs();
  browserRemember();
  const url = browserUrlIn(tab);
  // In place rather than as a new entry: this is the page the tab is already
  // on, fetched with other powers.
  if (url) browserNavigateIn(tab, url, false);
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
//
// The frame is named rather than looked up: zoom belongs to the host, a
// landing in a tab that is not on screen is still that host's page, and the
// frame it has to be applied to is that tab's.
function browserApplyZoom(factor, frame = browserTab().frame) {
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
    // The star before the title does what a favicon does on a desktop
    // browser's own bar: it is what makes a row of words read as a row of
    // saved pages rather than as more tabs. There are no favicons to fetch
    // through this proxy, so every one of them wears the key that saved it.
    const open = el("button", {
      type: "button", class: "bm-open", title: browserMarkHost(url),
      onclick: () => browserNavigate(url),
    }, svgIcon("i-star"), el("span", { class: "bm-name" },
                             b.title || browserMarkName(url)));
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

// ---- the tabs --------------------------------------------------------------

// What a chip is called: what the page called itself, failing that the host it
// came from, failing that a tab that has been opened and not yet sent anywhere.
function browserTabName(tab) {
  const url = browserUrlIn(tab);
  // The name only while it is still this page's name. A navigation that never
  // landed reported none — the proxy's failure page is not the page — and the
  // chip would otherwise sit at an address it could not reach under the name of
  // the page its tab has left.
  if (tab.title && tab.titleFor === url) return tab.title;
  if (!url) return "New tab";
  try { return new URL(url).host; } catch (e) { return url; }
}

// The strip. Each tab keeps its own chip and this updates it in place — the
// words, the title, which one is on — rather than drawing the row again:
// a chip is a button somebody may have tabbed to, and an element replaced under
// the keyboard takes the focus with it, after which Escape is the document's
// and not the pane's and the pane stops closing. Elements are made and dropped
// only where tabs are. The "+" after them is markup and is not touched, being
// outside the row they live in.
function renderBrowserTabs() {
  const bar = $("browser-tab-row");
  const plus = $("btn-browser-newtab");
  if (!bar || !plus) return;
  browserTabs.forEach((tab, i) => {
    const name = browserTabName(tab);
    if (!tab.chip) {
      // The tab rather than its index: where a tab sits changes every time one
      // before it is closed, and a handler that captured the old number would
      // act on its neighbour.
      const open = el("button", {
        type: "button", class: "tab-name",
        onclick: () => browserShowTab(browserTabs.indexOf(tab)),
      });
      const shut = el("button", {
        type: "button", class: "tab-del", "aria-label": "Close this tab",
        onclick: (e) => { e.stopPropagation(); browserCloseTab(browserTabs.indexOf(tab)); },
      }, "×");
      tab.chip = el("div", { class: "tab-chip" }, open, shut);
    }
    const open = tab.chip.firstElementChild;
    if (open.textContent !== name) {
      open.textContent = name;
      open.setAttribute("title", name);
    }
    tab.chip.classList.toggle("on", i === browserActive);
    // Which tabs are on the computer's own network. The key above says it for
    // the tab on screen; this says it for the rest, so a strip of eight still
    // reads at a glance.
    tab.chip.classList.toggle("lan", !!tab.lan);
    if (bar.children[i] !== tab.chip) bar.insertBefore(tab.chip, bar.children[i] || null);
    // A strip wider than the pane can leave the tab being switched to off the
    // end of it. Scrolled to only when it is: an active chip already in view
    // stays where the reader left the row.
    if (i === browserActive) {
      const chip = tab.chip;
      requestAnimationFrame(() => {
        const r = chip.getBoundingClientRect(), b = bar.getBoundingClientRect();
        if (r.left < b.left || r.right > b.right) {
          chip.scrollIntoView({ block: "nearest", inline: "nearest" });
        }
      });
    }
  });
  // Whatever is left at the end of the row belongs to a tab that is gone — a
  // close, a seeded strip, a switch of computer.
  while (bar.children.length > browserTabs.length) bar.lastElementChild.remove();
  // Dimmed rather than disabled at the cap: the press is the only place the
  // reason can be said, and a disabled button never gets one.
  plus.classList.toggle("off", browserTabs.length >= BROWSER_TAB_MAX);
}

// The one-shot recovery a new tab arms on the address field (browserAddTab
// below), as a way to take it back off. Every blur the user asked for looks
// exactly like the frame stealing the keyboard, so the places that mean the
// field to lose the focus — Enter and Escape in it, and any move away from the
// tab it was armed for — say so here rather than being read as the frame.
let browserGrabOff = null;
function browserCancelGrab() {
  if (browserGrabOff) { browserGrabOff(); browserGrabOff = null; }
}

// Bring a tab to the front. Everything the topbar shows is the active tab's, so
// all of it is re-read here; the page itself is already in its own frame, and
// showing it is one attribute.
function browserShowTab(i) {
  const tab = browserTabs[i];
  if (!tab) return;
  browserCancelGrab();
  browserActive = i;
  for (const t of browserTabs) if (t.frame) t.frame.hidden = t !== tab;
  showBrowserZoomMenu(false);
  const url = browserUrlIn(tab);
  browserSetField(url);
  syncBrowserNav();
  syncBrowserLan();
  renderBrowserTabs();
  // Which chip carries the page on screen has just changed, and the star with
  // it: both are about the page this tab is on.
  renderBrowserMarks();
  browserRemember();
  // A tab a reload or a session switch put back carries its address and no
  // frame. This is where it loads — the first time anybody has asked to see
  // it, rather than at the boot that restored it: eight dev servers reloading
  // themselves because a shell was refreshed is not what reopening a pane
  // should cost.
  if (url && url !== tab.loaded) browserNavigateIn(tab, url, false);
  else {
    browserApplyZoom(browserZoomFor(browserZoomHost(url)), tab.frame);
    syncBrowserZoom(browserZoomHost(url));
  }
}

// A tab made and brought to the front, with nothing in it yet: the "+" and a
// page that asked for a window of its own both start here, and what they do
// differs only in where the tab is sent and whether the address field is
// waiting for it. The caller navigates.
function browserMakeTab(lan) {
  browserCancelGrab();
  const tab = browserNewTab();
  tab.lan = !!lan;
  browserTabs.push(tab);
  browserActive = browserTabs.length - 1;
  for (const t of browserTabs) if (t.frame) t.frame.hidden = true;
  showBrowserZoomMenu(false);
  browserSetField("");
  syncBrowserNav();
  syncBrowserLan();
  renderBrowserTabs();
  renderBrowserMarks();
  return tab;
}

// The "+". A browser's new tab opens on its home page with the address field
// waiting, and so does this one.
function browserAddTab() {
  if (browserTabs.length >= BROWSER_TAB_MAX) {
    toast("Eight tabs is as many as this pane holds");
    return;
  }
  const gen = browserGen;
  const tab = browserMakeTab(false);
  const f = $("browser-url");
  // Focused inside the click rather than after the navigation: on a phone the
  // keyboard comes up for a focus a gesture asked for and for no other, and the
  // address only lands a turn later. Selected once it has, so the first
  // keystroke replaces it the way typing into a new tab does.
  if (f) f.focus();
  const opened = Date.now();
  browserNavigateIn(tab, BROWSER_HOME).then(() => {
    if (!f || document.activeElement !== f) return;   // the user has moved on
    // The tab this was opened for, not whichever is in front now: a load takes
    // a moment, and in it the tab can be closed, switched away from or taken
    // down with the pane.
    if (!browserAlive(tab, gen) || tab !== browserTab()) return;
    f.select();
    // A frame takes the focus a moment after its document lands, and in a tab
    // opened to be typed into that takes the keyboard away from the field it
    // was opened for. Taken back once, and only where the focus went to that
    // frame — or to nothing at all, which is how a browser reports a move into
    // a cross-origin document — inside the seconds the first page was still
    // coming up. A tap into the page after that is somebody choosing to read
    // it, and it keeps what it was given.
    const regrab = (e) => {
      browserGrabOff = null;                          // spent, once either way
      if (e.relatedTarget && e.relatedTarget !== tab.frame) return;
      if (Date.now() - opened > 5000) return;
      if (!browserAlive(tab, gen) || tab !== browserTab()) return;
      f.focus();
      f.select();
    };
    f.addEventListener("focusout", regrab, { once: true });
    browserGrabOff = () => f.removeEventListener("focusout", regrab);
  });
}

// A chip's cross. The last one is the window's last tab: the pane goes with it,
// and so does the page — which is what makes it different from closing the
// pane, where every tab is still there when it is opened again.
function browserCloseTab(i) {
  const tab = browserTabs[i];
  if (!tab) return;
  browserCancelGrab();
  if (browserTabs.length === 1) {
    browserDropFrames();
    browserTabs = [browserNewTab()];
    browserActive = 0;
    browserSetField("");
    syncBrowserNav();
    renderBrowserTabs();
    if (browserDocked) closeDockedBrowser();
    else history.back();
    return;
  }
  const wasActive = i === browserActive;
  if (tab.frame) tab.frame.remove();
  browserTabs.splice(i, 1);
  if (browserActive > i) browserActive--;
  // The tab that slid into the closed one's place, or the last one if it was
  // the end of the row — a browser's own choice.
  if (wasActive) browserShowTab(Math.min(i, browserTabs.length - 1));
  else { renderBrowserTabs(); browserRemember(); }
}

// The tabs a record kept, as tabs again: addresses and nothing else, so only
// the one that comes up loads anything (browserShowTab does the rest, when and
// if a chip is tapped).
function browserSeedTabs(urls, active) {
  const list = [];
  const want = Math.max(0, active | 0);
  let at = 0;
  const raw = urls.slice(0, BROWSER_TAB_MAX);
  for (let i = 0; i < raw.length; i++) {
    // Either shape the record may hold (cfg.sidePane, 02-debug-log.js): the
    // address alone, or the address and the mode it was left in.
    const e = raw[i];
    const url = browserNormalize(typeof e === "string" ? e : (e && e.url) || "");
    if (!url) continue;
    // The index moves with the list, the way browserRemember's walk counts it
    // out: an address dropped here is a tab the strip never gets, and an index
    // counted against the record's own list would name the tab beside the one
    // that was on screen — which the pane would then send to the address it
    // kept beside them, showing the same page in two tabs.
    if (i <= want) at = list.length;
    const tab = browserNewTab();
    tab.lan = !!(e && e.lan);
    tab.stack = [url];
    tab.idx = 0;
    list.push(tab);
  }
  if (!list.length) return;
  browserDropFrames();
  browserTabs = list;
  browserActive = Math.min(at, list.length - 1);
}

// ---- the two shapes --------------------------------------------------------

// Expanded is the pane's own state, but the class lives on #screen-term, for
// the docked explorer's reason: the seam it hides and the width it overrides
// are both read from there.
function syncBrowserExpand() {
  const on = browserDocked && browserExpanded;
  sideSetFull("browser", on);
  const btn = $("btn-browser-expand");
  if (!btn) return;
  btn.querySelector("use").setAttribute("href", on ? "#i-collapse" : "#i-expand");
  btn.setAttribute("aria-label", on ? "Shrink the browser pane" : "Expand the browser pane");
}

// The expand set from outside the pane, the docked explorer's filesSetExpanded
// for the same reason: only one row of the column fills the main area, and the
// other row's arrival folds this one away (sideSetFull, 26-side-pane.js).
function browserSetExpanded(v) {
  browserExpanded = v;
  cfg.browserExpanded = v;
  syncBrowserExpand();
}

// This pane as a row of the column: the one screen it puts in its row, the way it
// closes, and its expand, all of which are the column's to ask for and this
// module's to do (26-side-pane.js).
sideRegister("browser", {
  type: "browser",
  els: () => [$("screen-browser")],
  close: () => closeDockedBrowser("browser"),
  setExpanded: (on) => browserSetExpanded(on),
});

// False is the column refusing the row — the pane it would have taken is a
// docked editor with unsaved work whose owner said stay (sideClaim,
// 26-side-pane.js) — and nothing about the browser has moved by then.
function openDockedBrowser(opts) {
  if (!sideClaim("browser", opts)) return false;
  browserDocked = true;
  browserOpen = true;
  // Redundant beside a live terminal — it is right there — and the pane has
  // its own cross for leaving.
  $("btn-browser-term").style.display = "none";
  $("screen-browser").classList.add("docked");
  $("screen-browser").classList.add("active");
  syncBrowserExpand();
  browserRemember();
  return true;
}

// The frame keeps its page: the pane is a tap away again, and reloading a dev
// server every time it is closed would be the wrong trade. Only the slot and
// the classes go back.
// `id` is which of the column's rows this is about, and this build has one
// browser whose id is "browser": anything else names a row this module has no
// pane for, and there is nothing here to close for it.
function closeDockedBrowser(id) {
  if (id && id !== "browser") return;
  if (!browserDocked) return;
  browserDocked = false;
  browserOpen = false;
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

// DuckDuckGo rather than Google: the proxy fetches every page from the
// computer, so a search engine sees the computer's public address, and Google
// rate-limits /search for a shared one (an institute NAT, a large office) with
// a captcha whose site key refuses to run on any origin but Google's own —
// which through the proxy it never is. DuckDuckGo has no such wall and its
// results page renders in the sandboxed frame. Google is still one typed
// address away.
const BROWSER_HOME = "https://duckduckgo.com/";
// Where a line that is not an address goes (browserTyped). Beside the home
// page so the two stay the same engine.
const BROWSER_SEARCH = "https://duckduckgo.com/?q=";

// Every way in lands here — the globe key, the header button, a tapped private
// URL in the terminal, a restored session — so the two shapes are one entry
// point, the way openExplorer is for the folder. `tabs` and `at` are the strip
// a reload is putting back (restoreFileView, 09-image-viewer.js); every other
// caller opens the pane on whatever it was left holding. `id` is which of the
// column's rows it opens as — this build has one browser, whose id is "browser",
// and an id naming another row has no pane here to open — and `opts` is the
// column's own (sideClaim's `keep`).
function openBrowser(url, tabs, at, id, opts) {
  if (id && id !== "browser") return;
  if (needsSetup()) { openSettings(true); return; }
  if (demoMode) { toast("No browser in the demo"); return; }
  if (Array.isArray(tabs) && tabs.length) browserSeedTabs(tabs, at);
  // A refused row is no pane at all: seeding a tab into one and sending it
  // somewhere would be a page loading where nothing opened.
  if (isWideLayout() && $("screen-term").classList.contains("active")) {
    if (!openDockedBrowser(opts)) return;
  } else openFullBrowser();
  browserLoadMarks();
  // Ahead of any press, so the key's own click has nothing to wait for.
  browserEnsureTabToken();
  renderBrowserTabs();
  // Every way in passes here, including the one a reload takes: a strip seeded
  // from the record (browserSeedTabs) never goes through browserShowTab, so
  // this is where a tab put back on the computer's own network lights its key.
  syncBrowserLan();
  const target = browserNormalize(url);
  if (target) { browserNavigate(target); return; }
  // Opened with nothing to go to: the page the active tab was last on, whether
  // it is still in its frame (a close keeps it) or only in this session's
  // record (a reload does not).
  // Failing both, a search page: a browser that opens on a blank frame reads
  // as broken, and the address field is one tap away either way.
  const tab = browserTab();
  const last = browserCurrentUrl() || browserRememberedUrl() || BROWSER_HOME;
  if (last !== tab.loaded) browserNavigate(last, tab.idx < 0);
  else { browserSetField(last); syncBrowserNav(); }
}

// The globe key's own way in and out, the folder key's toggle (openFilesAtCwd,
// 28-file-explorer.js) mirrored: the pane the key put up is the pane it puts
// away. Full screen there is nothing to toggle — back is how that one leaves —
// so openBrowser stays the way in for everything else, and a tapped URL still
// lands in a pane that is already open.
function toggleBrowserPane() {
  const id = sideFocusedOf("browser");
  if (id) { closeDockedBrowser(id); return; }
  openBrowser();
}

// ---- what the proxied page says back ---------------------------------------

window.addEventListener("message", (e) => {
  // Which tab said it. The source is the identity check: this is a frame this
  // pane owns, not some other window, and it names the tab the report belongs
  // to rather than assuming the one on screen — a tab loading in the
  // background keeps its own stack and its own chip's name. The origin is only
  // a second gate, and the origin to expect is "null" — the frame is sandboxed
  // without allow-same-origin (the iframe attribute, and the sandbox CSP the
  // proxy puts on every page it serves), so a proxied page sits on an opaque
  // origin and posts from it. The backend's own origin is allowed beside it
  // for a page served without that sandbox.
  const tab = browserTabs.find((t) => t.frame && e.source === t.frame.contentWindow);
  if (!tab) return;
  if (e.origin !== "null" && e.origin !== browserOrigin()) return;
  const d = e.data;
  if (!d || typeof d !== "object") return;
  const live = tab === browserTab();

  if (d.type === "pockettui-nav") {
    const url = browserNormalize(typeof d.url === "string" ? d.url : "");
    if (!url) return;
    // Not while it is being typed into: the user is mid-address and the page
    // finishing its load must not take the field away from them.
    if (live && document.activeElement !== $("browser-url")) browserSetField(url);
    if (tab.navigating) {
      // The load this pane asked for, so this is the address it landed on
      // rather than somewhere new: it corrects the entry the navigation made,
      // which is how a redirect ends up recorded as where it went.
      tab.navigating = false;
      if (tab.idx < 0) browserPush(tab, url);
      else tab.stack[tab.idx] = url;
    } else {
      // The page moved itself. A shim reports a landing more than once (the
      // document is ready, then the load finishes), and the ones after the
      // first name the entry we are already on, which browserPush lets be.
      browserPush(tab, url);
    }
    tab.loaded = url;
    // Zoom belongs to the host and the frame keeps whatever scale it was
    // given, so a landing corrects it — including back to 1 for a host that
    // was never zoomed. A load reports more than once and each report sets
    // the same factor, which is a no-op after the first.
    browserApplyZoom(browserZoomFor(browserZoomHost(url)), tab.frame);
    if (live) syncBrowserZoom(browserZoomHost(url));
    // What the page calls itself, for this tab's chip and for the chip a
    // bookmark of it would carry: the document is cross-origin, so this report
    // is the only time it says. A tab that has moved to another page drops the
    // old name first, so a chip never carries the title of a page its tab has
    // left.
    if (url !== tab.titleFor) { tab.title = ""; tab.titleFor = url; }
    if (typeof d.title === "string" && d.title) {
      browserMarkTitles[url] = d.title;
      tab.title = d.title;
    }
    renderBrowserTabs();
    if (live) {
      // The page moved under the panel, and what it was showing was about the
      // page that was there. The bar redraws for the same reason: which chip is
      // the one on screen has just changed.
      showBrowserZoomMenu(false);
      renderBrowserMarks();
      syncBrowserNav();
    }
    // A page that loaded is a token that works and a scheme that was right.
    tab.reminted = false;
    tab.guessed = "";
    browserRemember();
    return;
  }

  // A link the page asked to open in a window of its own, or a window it asked
  // for by script. The laptop's browser is not where a pane's pages go: this
  // opens a tab the way a browser answers a _blank click — in front, carrying
  // the page, and with the address field left alone, because what was asked
  // for is a page to read and not somewhere to type. The sender's own
  // permission comes with it: a portal's views belong on its network too.
  if (d.type === "pockettui-open") {
    const url = browserNormalize(typeof d.url === "string" ? d.url : "");
    if (!url) return;
    // At the cap there is nowhere to put one, and spending the tab the link
    // was clicked in is better than the click going nowhere.
    if (browserTabs.length >= BROWSER_TAB_MAX) { browserNavigateIn(tab, url); return; }
    browserNavigateIn(browserMakeTab(tab.lan), url);
    return;
  }

  // The frame's own back/forward/go, which the shim sends here rather than
  // letting it walk the shell's joint history and close the pane.
  if (d.type === "pockettui-history") {
    const delta = typeof d.delta === "number" ? d.delta : 0;
    if (Number.isFinite(delta) && delta) browserStepIn(tab, Math.trunc(delta));
    return;
  }

  if (d.type === "pockettui-browse-error") {
    const code = typeof d.code === "string" ? d.code : "";
    // Where this tab thinks it is, never the address the message carries:
    // that one is the error page's own location, which is a proxied address,
    // and retrying it asks the backend to proxy itself — the layer that made
    // the founder's remembered URL twenty deep.
    const here = browserUrlIn(tab);
    // The token has a sliding expiry and a restart mints a new one, so a pane
    // left open finds its own gone. One silent re-mint and one retry per
    // navigation; a second expiry with nothing landed in between is news.
    if (code === "expired" && here && !tab.reminted) {
      tab.reminted = true;
      // Whichever of the two this tab's pages are fetched under is the one that
      // has aged out.
      if (tab.lan) browserTabToken = null; else browserToken = null;
      browserNavigateIn(tab, here, false);
      return;
    }
    // A scheme this pane guessed, on a host that does not answer it: the other
    // one, once, in place of this entry rather than after it. The address bar
    // never showed a scheme, so neither should the stack.
    if (BROWSE_SCHEME_RETRY[code] && tab.guessed && tab.guessed === here) {
      const other = browserFlipScheme(here);
      if (other) {
        tab.guessed = "";
        if (tab.idx >= 0) tab.stack[tab.idx] = other;
        browserNavigateIn(tab, other, false);
        return;
      }
    }
    // Only for the tab being read: a toast names no tab, and one raised by a
    // page loading out of sight would be about something the user is not
    // looking at.
    if (live) toast(BROWSE_ERRORS[code] || "That page could not be loaded");
  }
});

// ---- the topbar ------------------------------------------------------------

// One step along a tab's own list, for the two buttons and for a page that
// called history.back() inside the frame. A step off either end is no step, and
// a page stepping in a background tab moves that tab and nothing else.
function browserStepIn(tab, delta) {
  const i = tab.idx + delta;
  if (tab.idx < 0 || i === tab.idx || i < 0 || i >= tab.stack.length) return;
  tab.idx = i;
  browserNavigateIn(tab, tab.stack[i], false);
}

function browserStep(delta) { browserStepIn(browserTab(), delta); }

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
// This tab, on the computer's own network: the same address in the same frame,
// fetched this time as a page in its own right. An app written to be the top
// window then works — top is the page itself, the views it writes are its own
// to reach, and the storage and cookies a real visit would have are there —
// none of which is true of a tab in the ordinary mode. A second press puts the
// tab back, and the tab beside it is unaffected either way.
$("btn-browser-tab").addEventListener("click", async () => {
  const tab = browserTab();
  if (tab.lan) { browserSetLan(tab, false); return; }
  if (!browserUrlIn(tab)) return;
  // Asked before anything changes: without the computer's permission there is
  // nothing to turn the page round with, and a tab left in a mode that was
  // refused is a tab with nothing in it.
  if (!await browserEnsureTabToken()) {
    if (!browserTabBlocked) toast("Couldn't reach the computer");
    return;
  }
  // The ask is a round trip, and the strip may have moved on inside it.
  if (tab !== browserTab()) return;
  browserSetLan(tab, true);
});
// Another tab in the pane, at the end of the strip where a browser keeps it.
$("btn-browser-newtab").addEventListener("click", () => browserAddTab());
$("btn-browser-expand").addEventListener("click", () => {
  browserSetExpanded(!browserExpanded);
  refit(0);
});
$("btn-browser-close").addEventListener("click", () => closeDockedBrowser());
$("btn-browser-term").addEventListener("click", () => history.back());

$("browser-url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    const v = $("browser-url").value.trim();
    if (v) browserNavigate(browserTyped(v));
    // Blurred either way: on a phone the address bar is what the keyboard is
    // up for, and the page underneath is what the tap was about. A new tab's
    // recovery goes with it — this blur is the user's own, and taking the field
    // back from the page they just asked for would be the opposite of the help
    // it was armed to give.
    browserCancelGrab();
    $("browser-url").blur();
    return;
  }
  if (e.key === "Escape") {
    e.preventDefault();
    // Not the pane's Escape — this one only puts the field back.
    e.stopPropagation();
    browserSetField(browserCurrentUrl());
    browserCancelGrab();
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
// The addresses, not the pages: the frames go with the pane (browserTeardown
// below), so the session that comes back gets its tabs, its stacks and its
// names, and the tab it is left on loads. Keeping the frames alive instead
// would keep every session's pages in the document at once, for a switch that
// costs one page load.
function browserStash() {
  return {
    docked: browserDocked, active: browserActive,
    tabs: browserTabs.map((t) => ({
      stack: t.stack.slice(), idx: t.idx, title: t.title, titleFor: t.titleFor,
      lan: t.lan,
    })),
  };
}

function browserTeardown() {
  const wasDocked = browserDocked;
  // Anything still in flight for the tabs being put away belongs to the pane
  // that is going, and must not come back and build a frame in the one that
  // takes its place.
  browserGen++;
  browserCancelGrab();
  browserDocked = false;
  browserOpen = false;
  browserOriginFrom = null;
  browserDropFrames();
  showBrowserZoomMenu(false);
  $("screen-browser").classList.remove("docked");
  $("screen-browser").classList.remove("active");
  if (wasDocked) { syncBrowserExpand(); sideDrop("browser"); }
}

// Only the docked shape comes back: it is the only one a rail switch can
// happen under. The full-screen pane covers the list the switch is made from,
// so a session is never left with one up.
function browserRestore(s) {
  const recs = Array.isArray(s.tabs) ? s.tabs : [];
  browserDropFrames();
  // Through the normaliser rather than as they were kept: a stash written
  // before a restart can hold a proxied address, and restoring one as a page
  // address is what the pane would then navigate to.
  browserTabs = recs.length ? recs.map((r) => {
    const tab = browserNewTab();
    tab.stack = (r.stack || []).map((u) => browserNormalize(u));
    tab.idx = typeof r.idx === "number" ? r.idx : tab.stack.length - 1;
    tab.title = typeof r.title === "string" ? r.title : "";
    tab.titleFor = typeof r.titleFor === "string" ? r.titleFor : "";
    tab.lan = !!r.lan;
    return tab;
  }) : [browserNewTab()];
  browserActive = Math.min(Math.max(0, s.active | 0), browserTabs.length - 1);
  renderBrowserTabs();
  syncBrowserLan();
  if (!s.docked) { syncBrowserNav(); return; }
  openDockedBrowser();
  // The tab that was on screen, and it alone: the rest load when a chip asks
  // for one, for browserShowTab's reason.
  const u = browserCurrentUrl();
  if (u) browserNavigate(u, false);
  else { browserSetField(""); syncBrowserNav(); }
}

// Everything the pane holds about the computer being left, for a switch to
// another one (switchProfile, 40-profiles.js). The token is that machine's
// process, and a page it was serving is not a page on the next one.
function browserResetForProfile() {
  if (browserDocked) closeDockedBrowser();
  else browserTeardown();
  // Again on its own account: the docked path above goes through
  // closeDockedBrowser, which is not a teardown, and a mint in flight for the
  // machine being left must not land in the pane the next one gets.
  browserGen++;
  browserCancelGrab();
  browserToken = null;
  // The next computer answers for itself, both on the flavour and on whether
  // it is the one that served this shell.
  browserTabToken = null;
  browserTabBlocked = false;
  // Every tab with it, frames and all: they are pages on the machine being
  // left. A fresh one takes their place, the way a closed pane's does.
  browserDropFrames();
  browserTabs = [browserNewTab()];
  browserActive = 0;
  renderBrowserTabs();
  syncBrowserLan();
  // Another computer keeps its own list, and the one on screen is this one's.
  browserMarks = [];
  browserMarksAsked = false;
  browserMarkTitles = {};
  renderBrowserMarks();
  showBrowserZoomMenu(false);
  // Nothing is blanked through a frame's location any more: browserDropFrames
  // takes the elements out of the document, which discards their browsing
  // contexts and the joint history entries with them. Assigning src is still
  // the thing never done — that would spend an entry the shell's back needs.
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
  // server too old for the mode answers with the pane's own permission, and a
  // tab turned round on that is a tab that cannot do the one thing it was
  // turned round for.
  // Opening another tab is not gated: it is a frame in here, which every
  // computer that can show a page at all can serve.
  $("btn-browser-tab").hidden = !hasCapStrict("browse_tab") || browserTabBlocked;
}

renderBrowserTabs();
syncBrowserNav();
syncBrowserLan();
