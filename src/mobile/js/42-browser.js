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
//
// Two of these panes can be up at once: the column beside the terminal holds
// two rows and either of them can be a browser (26-side-pane.js). So what
// follows is a factory rather than a module — one call of makeBrowserPane is
// one pane — and what sits above it is what the two of them genuinely share,
// which is the computer's own answers (the token it mints, the list of
// bookmarks it keeps) and the words and paces that are constants either way.

// ------------------------------------------------------------
// Every pane of this kind
// ------------------------------------------------------------

// As many as the strip holds and anybody keeps track of. Eight chips still
// carry a readable name at the docked width; past that a tab is a sliver.
const BROWSER_TAB_MAX = 8;

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

// The steps the two buttons walk, a desktop browser's own set. 1 is one of
// them, so a page that has been zoomed always has a step back to its size.
const BROWSER_ZOOMS = [0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2];

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

// One token per computer rather than per pane: the mint is the machine's
// answer about itself, and a second pane asking for one of its own would be
// a second ask for the record the backend hands back either way.
let browserToken = null;        // {token, prefix} once api/browse has answered
// The permission a tab put on the computer's own network is served under,
// asked for when the pane opens rather than at the press: the key should turn a
// page round, not first wait on a round trip to the computer.
let browserTabToken = null;
// This computer serves the shell itself, so it will not grant that (see the
// mint's same-origin gate). Asked once, and the key goes.
let browserTabBlocked = false;

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
  browserRenderMarks();
}

// Optimistic: every pane's bar is drawn from the change before the write goes
// out, because the alternative is a chip that appears a round trip after the
// tap. A refused write is the one case that reads back — what is on disk is then
// the truth and the bars are showing something that never got there.
async function browserSaveMarks() {
  browserRenderMarks();
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

// ---- full mode -------------------------------------------------------------
// A tab is one of two things: a page fetched through the backend's proxy and
// framed here, or the same page open in a real browser on the computer with its
// pixels streamed over (43-full-browser.js). The proxy is what a tab is — the
// page runs in the browser it is being read in, which is why it is quick and why
// its video and its sound are the device's own — and the stream is where the
// pages the proxy cannot serve go: a site that checks the top window, a sign-in,
// a search page that wants a real browser.
//
// Which pages those are is not a guess anybody should have to make twice, so it
// is remembered per host, the way the zoom is: the key on the address row puts a
// site on the stream and takes it off again, the proxy hands a page over by
// itself where it knows it cannot serve it (browserHandOff), and every tab
// opened on a remembered host is streamed from the first navigation.

// Which host a page belongs to, for the record below: the zoom's own key, port
// and all, since :3000 and :8000 are two apps there and here. Spelled out rather
// than taken from browserZoomHost, which is a pane's and falls back to the page
// that pane is on — this one answers about an address and nothing else.
function browserStreamHost(url) {
  try { return url ? new URL(url).host : ""; } catch (e) { return ""; }
}

function browserStreamsHost(url) {
  const host = browserStreamHost(url);
  return !!host && cfg.browserStreamHosts[host] === true;
}

// This host on the stream, or off it. The write is the whole record back, the
// way the zoom's is — a set with one member changed.
function browserRememberStream(url, on) {
  const host = browserStreamHost(url);
  if (!host) return;
  const rec = cfg.browserStreamHosts;
  if (on) rec[host] = true;
  else delete rec[host];
  cfg.browserStreamHosts = rec;
  browserSyncStreamHosts();
}

// What a tab sent to this address would be. Strictly checked, like every other
// capability the shell sends *to* the computer: a server too old for the route
// has no socket to open, and a tab waiting on one would be a tab with nothing in
// it. Without an address it answers for a tab with nothing in it yet, which is
// what the key on the row is drawn from before anything has been opened.
function browserFullWanted(url) {
  if (!hasCapStrict("browser_full")) return false;
  return cfg.browserStreamAll || browserStreamsHost(url);
}

// The id the computer knows a streamed tab by. Made with the tab and kept in the
// record a reload reads, because it is also how the page comes back: the backend
// holds its tabs under (pane, id) and answers a second open for one it already
// has by describing the page that is on it rather than by loading anything
// (chromium.py's FullBrowser.open).
let browserFidN = 0;
function browserFid() {
  browserFidN += 1;
  return "t" + browserFidN + Math.random().toString(36).slice(2, 8);
}

// Which browser the computer found, out of the path it found it at — the same
// four answers the installer's own search can give. The version string is the
// whole four-part number and the major is what anybody reads.
function browserFullProduct(path) {
  const p = String(path || "");
  if (/\.pockettui\/chromium\//.test(p)) return "Chrome for Testing";
  if (/edge/i.test(p)) return "Microsoft Edge";
  if (/chromium/i.test(p)) return "Chromium";
  if (/chrome/i.test(p)) return "Google Chrome";
  return "A browser";
}

// The Browser group in Settings: the switch's state, and the computer's own
// answer about what a tab would run in. Asked when the section is looked at
// rather than at boot — it spawns `--version` on the computer the first time —
// and hidden outright on a computer that cannot browse at all, where there is
// nothing for either line to be about.
async function browserSyncSetting() {
  const block = $("browser-setting");
  if (!block) return;
  const on = !demoMode && !needsSetup() && hasCapStrict("browse");
  block.hidden = !on;
  $("browser-stream-toggle").checked = cfg.browserStreamAll;
  browserSyncStreamHosts();
  // Nothing to clear on a computer with no browser to keep a profile in.
  $("btn-browser-clear").hidden = !on || !hasCapStrict("browser_full");
  const line = $("browser-status-line");
  if (!on || !line) return;
  if (!hasCapStrict("browser_full")) {
    line.textContent = "No browser on the computer: run `pockettui browser install` there";
    return;
  }
  let d = null;
  try {
    const r = await fetch(apiURL("api/browser/status"),
                          { cache: "no-store", headers: authHeaders() });
    d = await r.json();
    if (!r.ok || !d) throw new Error("http " + r.status);
  } catch (e) {
    dbg("browser status: ask failed", e);
    line.textContent = "Couldn't ask the computer about its browser";
    return;
  }
  if (!d.found) {
    line.textContent = d.launch_error
      || "No browser on the computer: run `pockettui browser install` there";
    return;
  }
  const major = String(d.found.version || "").split(".")[0];
  const cap = typeof d.capMb === "number" ? d.capMb : 0;
  line.textContent = browserFullProduct(d.found.path)
    + (major ? " " + major : "") + " on the computer"
    + (cap ? ", memory cap " + cap + " MB" : "")
    // A browser that could not be started says so, whatever was found.
    + (d.launch_error ? " — " + d.launch_error : "");
}

// The sites that are on the stream, in the group under the switch: the record is
// made one press at a time and this is the only place it can be read back, or a
// host taken off it without opening that host again. Nothing to say with none of
// them, and nothing to say at all on a computer with no browser to stream from.
function browserSyncStreamHosts() {
  const line = $("browser-stream-hosts");
  if (!line) return;
  const hosts = Object.keys(cfg.browserStreamHosts).sort();
  line.hidden = !hosts.length || !hasCapStrict("browser_full");
  line.textContent = "";
  if (line.hidden) return;
  line.appendChild(document.createTextNode("Streamed sites: "));
  for (const host of hosts) {
    const chip = document.createElement("span");
    chip.className = "stream-host";
    chip.appendChild(document.createTextNode(host));
    const del = document.createElement("button");
    del.type = "button";
    del.dataset.host = host;
    del.setAttribute("aria-label", "Stop streaming " + host);
    del.textContent = "✕";
    chip.appendChild(del);
    line.appendChild(chip);
  }
}

// A cross in that line: the host off the record, and the line redrawn without
// it. The tabs already open are left alone — a page being read is not something
// a preference should re-fetch under the reader, which is the rule the switch
// above it follows too.
document.addEventListener("click", (e) => {
  const del = e.target && e.target.closest("#browser-stream-hosts button[data-host]");
  if (!del) return;
  const rec = cfg.browserStreamHosts;
  delete rec[del.dataset.host];
  cfg.browserStreamHosts = rec;
  browserSyncStreamHosts();
});

// ---- what the streamed browser downloads ------------------------------------
// A file a streamed page saves is saved where the browser is, which is the
// computer: chromium.py puts it in ~/.pockettui/downloads under the name the
// site suggested and reports it here as it goes. So the pane says what is
// happening while it happens, and then brings the file over — because the device
// the user asked from is the device they wanted it on.
//
// Up to this size it comes down without being asked for, through the explorer's
// own download (downloadFile, 28-file-explorer.js). Past it the transfer is
// itself worth a question: a phone on a hotel link should not spend ten minutes
// fetching a disk image it was only told about, and the file is on the computer
// either way.
const BROWSER_DL_MAX = 200 * 1024 * 1024;

function browserDownloaded(msg) {
  if (!msg || !msg.guid) return;
  const name = String(msg.name || "file");
  const total = Number(msg.total) || 0;
  const got = Number(msg.received) || 0;
  if (msg.state === "inProgress") {
    // The held line, the explorer's own transfers' line: no clock of its own, so
    // it stays until the outcome replaces it.
    holdToast("Downloading " + name + "… " + (total
      ? Math.min(99, Math.floor(got * 100 / total)) + "%" : fmtSize(got)));
    return;
  }
  if (msg.state === "canceled") { toast("Download cancelled"); return; }
  if (msg.state !== "completed") return;
  const path = String(msg.path || "");
  // Finished, and the computer could not say where it put it: it is in the
  // downloads folder there, and that is the whole of what can be said.
  if (!path) { toast("Downloaded " + name + " on the computer"); return; }
  if (total && total <= BROWSER_DL_MAX) { downloadFile(path, name, total); return; }
  hideToast();
  appConfirm(name + " was downloaded on the computer. Save it to this device too?",
             { confirmLabel: "Save", danger: false }).then((yes) => {
    // Without a size the browser has to do the fetching rather than the page
    // (downloadFile), which is what the undefined asks for.
    if (yes) downloadFile(path, name, total || undefined);
  });
}

// The panes the column has, by the id it knows each of them by ("browser" for
// the one the markup ships, "browser#2" for the copy made beside it). Everything
// below the factory that the rest of the app calls is a dispatcher over this.
const browserPanes = {};

function browserOpenIds() {
  return Object.keys(browserPanes).filter((id) => browserPanes[id].isOpen());
}

function browserAnyOpen() { return browserOpenIds().length > 0; }

function browserAnyDocked() {
  return Object.values(browserPanes).some((pane) => pane.isDocked());
}

// Whether there is a row of the column to open into. Off a wide layout, or with
// no terminal behind it, the pane's only shape is the full-screen one — which is
// the markup's own pane and never a copy of it.
function browserDockable() {
  return isWideLayout() && $("screen-term").classList.contains("active");
}

// The bar in every pane. A bookmark is the computer's rather than a pane's, so a
// page starred in one row shows up in the other row's bar too; which chip is
// drawn as the page on screen is each pane's own answer (renderBrowserMarks, in
// the factory).
function browserRenderMarks() {
  for (const pane of Object.values(browserPanes)) pane.renderMarks();
}

// ------------------------------------------------------------
// One pane
// ------------------------------------------------------------

// One call of this is one pane. The body below is the module this file used to
// be, wrapped rather than rewritten — it is not indented into the function,
// because every line of it would then have moved and the change would read as a
// rewrite of a pane that has not changed. What the wrapper buys is that each
// `let` is now that pane's own, and a mint landing from a navigation started
// before the user touched the other pane still builds its frame in the pane it
// was asked for, which no swapped "current pane" pointer can promise across an
// await. `root` is that pane's own screen and `q` is how every id inside it is
// reached, since the copy carries the same ids as the original; `id` is what the
// column calls it, and what the body branches on wherever a copy is not the
// markup's own pane (the expand it remembers, the full-screen shape it never
// takes, its own half of the record). What the rest of the app calls is the
// dispatchers under the factory.
function makeBrowserPane(id, root) {

const q = (name) => root.querySelector("#" + name);

let browserDocked = false;      // in the slot beside the terminal, not over it
let browserOpen = false;        // either shape is up
// Which screen the full-screen shape covered, to put back when it closes.
let browserOriginFrom = null;
// The copy never arrives expanded and never writes the preference back: what
// is remembered is the markup's own pane's expand, the docked explorer's rule
// (filesExpanded, 28-file-explorer.js).
let browserExpanded = id === "browser" ? cfg.browserExpanded : false;

// One tab. `frame` is made on its first navigation (browserFrame) and stays in
// the wrap, hidden, while another tab is on screen — which is what makes going
// back to a tab free rather than a reload of whatever it was running.
function browserNewTab() {
  return {
    frame: null,
    // Which of the two kinds of tab this is, and the view when it is the
    // streamed one. Null is a tab nothing has been asked of yet: whether the
    // computer has a browser to stream from is not known at the load that makes
    // this pane's first tab, so the mode is settled by the first navigation
    // (browserIsFull, browserNavigateIn) rather than guessed here.
    fullMode: null,
    // The live view, made on the tab's first navigation the way `frame` is, and
    // kept while another tab is on screen — a hidden view stops its stream and
    // keeps its page (43-full-browser.js).
    full: null,
    // What the computer calls this tab on the wire, and which target its page is
    // on: the pair that gets the page back after a reload rather than loading it
    // again (browserRemember, browserFullView).
    fid: browserFid(),
    target: "",
    // Where its own history can go, as the computer last reported it: a streamed
    // tab's history is the real browser's, and only it knows how far back it
    // goes. The two arrows read these instead of the stack below.
    canBack: false,
    canFwd: false,
    // The factor its view was last told, so a landing that reports itself four
    // times does not restart the stream four times (browserApplyZoom).
    zoomAt: 0,
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
    // The page's own icon as a data URL, where the computer could read one, and
    // whether the computer has given this tab's page up to stay inside its
    // memory cap. Both are the streamed mode's alone: there are no favicons to
    // fetch through the proxy, and nothing of a framed page for a watchdog to
    // discard (browserFullTab).
    icon: "",
    discarded: false,
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
    // Whether the proxy has already offered this tab's page to the stream. Only
    // read where there is no browser to stream from: the first offer arrives
    // with the page and is the backend's own reading, the second is the user
    // pressing the button on it, and that one is worth an answer
    // (browserHandOff).
    streamAsked: false,
    // The landing watchdog's two: which landing in this tab's frame is the one
    // being waited on, and when this tab's frame last said anything. A proxied
    // document reports itself; one that has left the proxy cannot
    // (browserWatchLanding).
    landGen: 0,
    heardAt: 0,
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
  const f = q("browser-url");
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
  const tpl = q("browser-frame-tpl");
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
  // Every document the proxy serves says so, and a landing in this frame that
  // says nothing is a document the proxy did not serve: the watchdog is what
  // notices (browserWatchLanding). On the element rather than inside it — the
  // document is cross-origin and there is nothing of it to listen to from here.
  tab.frame.addEventListener("load", () => browserWatchLanding(tab));
  tab.frame.hidden = tab !== browserTab();
  q("browser-wrap").appendChild(tab.frame);
  return tab.frame;
}

// ---- full mode, in this pane -----------------------------------------------

// One socket per pane, made with its first streamed tab and kept: every tab in
// the pane shares it, and each binary frame names the tab it is a picture of
// (43-full-browser.js). Lazily, because a pane whose tabs are all proxy tabs —
// or a computer with no browser to stream from — should not be holding one open.
let fullLink = null;

function browserFullLink() {
  if (!fullLink) {
    fullLink = fullBrowserLink(id);
    // The three things the socket carries that are not about one view: a window
    // a page opened, a file the browser is saving, and the browser having been
    // swapped under all of its tabs to stay inside its memory cap. The last one
    // is a line and nothing else — the tab each pane was showing is already
    // being loaded again on the computer (chromium.py's restart_over_cap).
    fullLink.onnewtab = (msg) => browserPopup(msg);
    fullLink.ondownload = (msg) => browserDownloaded(msg);
    fullLink.onrestart = (msg) => toast(String((msg && msg.message)
                                               || "The computer's browser restarted"));
  }
  return fullLink;
}

// The socket, gone, for a pane that is going with it: a teardown or a switch of
// computer. Not for a pane that is merely closed — its tabs still hold their
// pages, and the socket is what they hold them through. A reopened pane makes
// another one with its next streamed tab.
function browserDropLink() {
  if (!fullLink) return;
  fullLink.close();
  fullLink = null;
}

// Whether this tab is the streamed kind. A tab that has not been sent anywhere
// has no mode of its own yet and reads as whatever its first navigation would
// give it, so the key on the row says what it would do rather than nothing.
function browserIsFull(tab) {
  if (!tab) return false;
  return tab.fullMode === null ? browserFullWanted() : tab.fullMode;
}

// One message about one streamed tab. Nothing is sent for a tab with no view:
// there is no page on the computer to act on until the view has opened one.
function browserFullSend(tab, type) {
  if (!tab.full || !fullLink) return;
  fullLink.send({ type: type, tab: tab.fid });
}

// This tab's view, made on first use the way browserFrame makes its frame — and
// refused for the same reason, a record the strip no longer holds.
function browserFullView(tab) {
  if (tab.full) return tab.full;
  if (browserTabs.indexOf(tab) < 0) return null;
  const link = browserFullLink();
  const view = fullBrowserMake(id, tab.fid, link, {
    // The chords that belong to the shell rather than to the page, which has the
    // keyboard for everything else.
    focusAddress: () => { const f = q("browser-url"); if (f) { f.focus(); f.select(); } },
    reload: () => browserFullSend(tab, "reload"),
    zoomStep: (delta) => { if (delta) browserStepZoom(delta); else browserSetZoom(1); },
    // A middle click or a Ctrl+click in the page, which is the same ask as a
    // proxied page's window.open.
    openTab: (href) => browserOpenFrom(tab, href),
    retry: () => { const u = browserUrlIn(tab); if (u) browserNavigateIn(tab, u, false); },
    onError: (msg) => browserFullFailed(tab, msg),
    onTab: (msg) => browserFullTab(tab, msg),
    // The page has put a question up, or had one answered: the chip is where a
    // tab that is not on screen says so.
    onAsk: () => renderBrowserTabs(),
  });
  tab.full = view;
  tab.zoomAt = 0;                       // a fresh view is at 100% whatever was
  view.el.hidden = tab !== browserTab();
  q("browser-wrap").appendChild(view.el);
  return view;
}

// This tab's view, gone, and the page on the computer with it. The view's own
// destroy leaves the tab open there — a hidden tab is still a page to come back
// to — so closing it is the pane's, wherever the pane is done with the tab.
function browserDropFull(tab) {
  if (!tab.full) return;
  tab.full.destroy();
  browserFullSend(tab, "close");        // while the tab still has one to name
  tab.full = null;
  tab.target = "";
  tab.zoomAt = 0;
  tab.canBack = false;
  tab.canFwd = false;
  // Both were the streamed page's: a tab with no page on the computer has no
  // icon of its own and nothing left there to have been discarded.
  tab.icon = "";
  tab.discarded = false;
}

// Every page this pane holds, gone — for a teardown, a profile switch, or the
// last tab's close: the frames out of the document, the streamed tabs closed on
// the computer. The tabs keep their addresses; what goes is the page.
function browserDropPages() {
  for (const tab of browserTabs) {
    if (tab.frame) tab.frame.remove();
    tab.frame = null;
    browserDropFull(tab);
    tab.primed = false;
    tab.loaded = "";
    tab.navigating = false;
  }
}

// The streamed tab on screen stops streaming when nobody is looking at the pane.
// A frame costs nothing while its pane is closed; a picture of a page arriving
// many times a second costs the link it arrives over.
function browserHideFulls() {
  for (const tab of browserTabs) if (tab.full) tab.full.hide();
}

function syncBrowserNav() {
  const tab = browserTab();
  const back = q("btn-browser-back"), fwd = q("btn-browser-fwd");
  // A streamed tab's history is the real browser's, not the list below: it has
  // entries this pane never saw — a page's own pushState, a redirect chain — and
  // the computer says on every `tab` message how far each way it goes.
  const full = browserIsFull(tab) && tab.full;
  if (back) back.disabled = full ? !tab.canBack : tab.idx <= 0;
  if (fwd) fwd.disabled = full ? !tab.canFwd
                               : (tab.idx < 0 || tab.idx >= tab.stack.length - 1);
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
  if (!rec || !rec.rows.includes(id)) return;
  // A tab with nothing in it yet is not an address to come back to, so it is
  // left out — and the index has to be the one the shortened list spells,
  // which is what the walk below counts.
  const tabs = [];
  let at = 0;
  for (let i = 0; i < browserTabs.length; i++) {
    const u = browserUrlIn(browserTabs[i]);
    if (!u) continue;
    if (i <= browserActive) at = tabs.length;
    // A bare address for an ordinary tab, a record only where there is
    // something more to say: a shell too old to know about the modes reads the
    // strings and still gets every one of those tabs back. A streamed tab keeps
    // the pair that gets its page back rather than loading it again — the id the
    // computer holds it under, and the target its page is on.
    const t = browserTabs[i];
    if (browserIsFull(t)) {
      tabs.push({ url: u, full: true, fid: t.fid, targetId: t.target || "" });
    } else tabs.push(t.lan ? { url: u, lan: true } : u);
  }
  // Written back over the record as it stands rather than as a record of its
  // own: the rows and their order are the column's half of this key, and the
  // browser rewriting it on every landing must not be what loses them. Under
  // this pane's own row id, so a second browser in the other row keeps its own
  // strip rather than the two writing over each other.
  const panes = Object.assign({}, rec.panes);
  panes[id] = { url: browserCurrentUrl(), tabs: tabs, tab: at };
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
  const p = rec.panes[id];
  return p ? browserNormalize(p.url || "") : "";
}

// Send a tab somewhere — its frame, or the browser on the computer holding its
// page. `push` is false for the moves that are not new places: back, forward,
// reload, and putting a restored tab back on the page it was already on. Every
// part of it that is chrome rather than page — the address field, the arrows, the
// zoom key — is done only for the tab on screen; a background tab that is still
// loading has no business moving them.
async function browserNavigateIn(tab, raw, push = true) {
  const gen = browserGen;
  const typed = browserHasScheme(String(raw || "").trim());
  const url = browserNormalize(raw);
  if (!url) return;
  // The first thing asked of a tab settles what kind of tab it is. Not at the
  // load that made it: whether this computer has a browser to stream from is an
  // answer that arrives after the pane's first tab does, and which kind of tab
  // this is depends on where it is being sent.
  if (tab.fullMode === null) {
    tab.fullMode = browserFullWanted(url);
    // A streamed tab is on the computer's network already, so the permission the
    // tab may have inherited from the one it was opened from has nothing to
    // grant (browserSwapMode does the same for a tab that has been somewhere).
    if (tab.fullMode) tab.lan = false;
  } else if (!tab.fullMode && browserStreamsHost(url)) {
    // A proxy tab sent to a host that is on the stream: the mode changes under
    // this navigation rather than after it, so the address is fetched once, in
    // the browser it was going to have to be fetched in. The other way round is
    // not automatic — a streamed tab sent somewhere unremembered stays streamed,
    // since stepping it down mid-session is the key's business or a new tab's.
    browserSwapMode(tab, true);
  }
  // A scheme this pane picked is a guess this navigation may have to take
  // back, and only its failure says so. Held for this navigation alone: the
  // retry below spells its scheme out, and a landing clears it.
  tab.guessed = typed ? "" : url;
  // A new destination is a new navigation, so the one silent re-mint it is
  // allowed comes back. Back, forward and reload keep whatever is left of it.
  if (push) tab.reminted = false;
  // The computer and the frame, or a test standing in for both: everything
  // under this line — the stack, the chip, the address field and the record —
  // is the pane's own bookkeeping and happens either way, which is what makes
  // the hook worth having rather than a stub over the whole function
  // (side_column_smoke.mjs). Null in every build that is not being driven by
  // one.
  if (api.navigateHook) api.navigateHook(url, push);
  else if (tab.fullMode) { if (!browserPointFull(tab, url)) return; }
  else if (!await browserPointFrame(tab, url, gen)) return;
  tab.loaded = url;
  if (push) browserPush(tab, url);
  // The key's own words name the host it would remember, so they follow the
  // address the tab is on rather than only the mode it is in.
  if (tab === browserTab()) {
    browserSetField(url);
    syncBrowserNav();
    syncBrowserFull();
  }
  renderBrowserTabs();
  browserRemember();
}

// Where a streamed tab is sent: browserPointFrame's other half, and the same
// contract — false is a navigation that got nowhere and its caller writes
// nothing down. The first one opens the tab on the computer (or takes back the
// target a reload left its page on, which is what makes a reload cost no page
// load); every one after that is an op on a tab that is already there. The zoom
// follows rather than leads, because the message that sets it would be an error
// on a tab the computer has not been told about yet — where browserPointFrame's
// order is the opposite, a frame having to be sized before the page lays itself
// out in it.
function browserPointFull(tab, url) {
  const view = browserFullView(tab);
  if (!view) return false;
  if (tab.loaded) view.navigate(url);
  else view.open(url, tab.target);
  browserApplyZoom(browserZoomFor(browserZoomHost(url)), tab);
  if (tab === browserTab()) syncBrowserZoom(browserZoomHost(url));
  return true;
}

// Which permission this tab's pages are fetched under, and where its frame is
// sent. On the computer's own network it is the other one, and a frame that has
// not loaded anything yet goes in through the hop that clears this computer's
// address of whatever a shell it once served left there — before the first
// document that could read it runs. Past that hop the pages are ordinary ones:
// going through it again would wipe what the page itself has since put there,
// which is the session the user just logged in with. False is a navigation that
// got nowhere, and its caller writes nothing down.
async function browserPointFrame(tab, url, gen) {
  let target;
  if (tab.lan) {
    const rec = await browserEnsureTabToken();
    if (!browserAlive(tab, gen)) return false;
    if (!rec) { toast("Couldn't reach the computer"); return false; }
    const proxied = browserProxied(url, rec);
    target = tab.primed ? proxied : browserEnterUrl(proxied, rec);
  } else {
    if (!await browserEnsureToken(url)) return false;
    // The mint is a round trip, and this may be a tab the strip has closed or a
    // pane that has been taken down since it was asked for. Nothing below this
    // line is worth doing then, and the frame it would make is worse than
    // nothing.
    if (!browserAlive(tab, gen)) return false;
    target = browserProxied(url);
  }
  if (!target) { toast("That is not an address to open"); return false; }
  const frame = browserFrame(tab);
  if (!frame) return false;
  // Before the load rather than after its landing: the frame's width is the
  // viewport the page lays itself out against, so setting it here is what
  // saves the zoomed page a reflow on its first paint.
  browserApplyZoom(browserZoomFor(browserZoomHost(url)), tab);
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
  return true;
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
  const btn = q("btn-browser-tab");
  if (!btn) return;
  // Not for a streamed tab: its page is fetched by a browser running on the
  // computer, so it is already on the computer's network and there is nothing
  // this key could add. Hidden here rather than in browserSyncCap, because it
  // now depends on the tab on screen as well as on the computer's answers.
  btn.hidden = !hasCapStrict("browse_tab") || browserTabBlocked
               || browserIsFull(browserTab());
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

// ---- which browser this tab is ---------------------------------------------

// The key's own state, the network key's rule: read off the tab on screen, so a
// tab put on the stream is lit beside a tab that was left on the proxy. What it
// says is the whole of the bargain — a press is about this site and not only
// about this tab, since the host it names is what the record keeps.
function syncBrowserFull() {
  const btn = q("btn-browser-full");
  if (!btn) return;
  const on = browserIsFull(browserTab());
  btn.classList.toggle("on", on);
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  // What pressing it would do, and once it is pressed what it did — the same
  // words in the tooltip and in the label, as the key beside it does.
  const host = browserStreamHost(browserUrlIn(browserTab()));
  const said = on
    ? "This site streams from the computer's Chrome; press to use the lightweight proxy"
    : "Stream this site from the computer's Chrome"
      + (host ? " (remembered for " + host + ")" : "");
  btn.setAttribute("aria-label", said);
  btn.setAttribute("title", said);
}

// Move one tab between the two kinds. Whatever the tab was showing goes — a
// frame's document cannot become a streamed page and a streamed page cannot
// become a frame's document — and the page is fetched again as the other kind,
// in place rather than as a new entry. What carries over is the address, the
// name and the zoom; the history does not, because the list the tab had was the
// other browser's.
function browserSetFull(tab, on) {
  if (browserIsFull(tab) === !!on) return;
  browserSwapMode(tab, on);
  browserRemember();
  const url = browserUrlIn(tab);
  if (url) browserNavigateIn(tab, url, false);
  else if (tab === browserTab()) syncBrowserNav();
}

// The tab's half of that: the mode flipped and whatever the tab was showing
// given up, with nothing said about where the tab then goes. Its own function
// because a navigation onto a remembered host does this much and then carries on
// with the load it was already making, rather than starting a second one
// (browserNavigateIn).
function browserSwapMode(tab, on) {
  tab.fullMode = !!on;
  if (on) {
    if (tab.frame) { tab.frame.remove(); tab.frame = null; }
    tab.primed = false;
    tab.reminted = false;
    // The streamed tab is on the computer's own network already, so the
    // permission the key beside this one grants has nothing left to grant.
    tab.lan = false;
  } else browserDropFull(tab);
  tab.loaded = "";
  tab.navigating = false;
  if (tab === browserTab()) { syncBrowserFull(); syncBrowserLan(); }
  renderBrowserTabs();
}

// The computer has no browser it can start, and this tab has never shown
// anything: the proxy is what it is the fallback for, so the tab steps down and
// the navigation is made again. One toast, because the step down is not what the
// user asked for. A tab that was reading something keeps the overlay and its
// Retry — stepping it down under the reader would lose the page it is on. True
// says the failure has been dealt with and no overlay is wanted
// (43-full-browser.js).
function browserFullFailed(tab, msg) {
  if (!msg || msg.code !== "unavailable" || tab.loaded) return false;
  toast("The computer's browser could not start; using the proxy");
  browserSetFull(tab, false);
  return true;
}

// How long a landing has to report itself before the pane treats it as a page
// that has left the proxy, and how far ahead of the element's load event a
// report may arrive and still count as that landing's. The shim reports when its
// document is ready and again when the load finishes, and that second one is the
// same moment as the element's load event, either side of it by a tick — so the
// grace is a tick's worth and no more, and a report that comes later than the
// landing counts for the whole of the window anyway.
const BROWSER_LAND_WAIT = 1200;
const BROWSER_LAND_GRACE = 150;

// A document landed in a proxy tab's frame. Everything the proxy serves reports
// itself here shortly afterwards — the shim's `pockettui-nav`, or the error
// page's own message — so a landing that says nothing is a document the proxy
// did not serve: a page that set location.href to a root-relative path, which in
// a sandboxed frame there is no hook to catch. Under `tailscale serve` that path
// is outside the app's mount and what lands is the front's bare "404 page not
// found", inside the frame, with the address bar still showing the site.
//
// There is nothing to fetch it with here, so the tab goes to the browser that
// can: the computer's own Chrome, on the address this pane last knew the tab to
// be at — which for an app that moves itself with pushState is where the user
// actually is. The host is remembered as if the key had been pressed, since the
// next visit will leave the proxy in the same place.
function browserWatchLanding(tab) {
  const gen = ++tab.landGen;
  const paneGen = browserGen;
  const at = Date.now();
  setTimeout(() => {
    // A newer landing in the same frame is the one being waited on now, and this
    // one is nobody's business any more.
    if (tab.landGen !== gen || !browserAlive(tab, paneGen)) return;
    if (tab.heardAt >= at - BROWSER_LAND_GRACE) return;   // the proxy's own page
    if (browserIsFull(tab)) return;
    // A load this pane started and has had no report of yet — including the
    // blank document a fresh frame is made on, whose load event arrives before
    // the address it was made for. Its own report is what will clear this, and
    // until then there is nothing to say the frame has gone anywhere.
    if (tab.navigating) return;
    const url = browserUrlIn(tab);
    // Nowhere to hand it to: the page in the frame is wrong and saying so would
    // not make it right, and the frame is still showing whatever it landed on.
    if (!url || !hasCapStrict("browser_full")) return;
    browserRememberStream(url, true);
    toast("This site left the proxy; streaming it from the computer");
    browserSwapMode(tab, true);
    browserNavigateIn(tab, url, false);
  }, BROWSER_LAND_WAIT);
}

// Why the proxy gave a page up, in the words the person reading it needs: what
// happened and what is being done about it, once.
const BROWSER_STREAM_SAID = {
  google_sorry: "Google wants a real browser here; streaming it from the computer",
  login: "Sign-in pages stream from the computer's Chrome",
};

// The other direction of the step the key makes, asked for by the page rather
// than by the user: the backend answers a document it knows the proxy cannot
// serve with one that says so (`pockettui-stream`), and the tab it is in moves
// to the stream on that address. The host goes on the record with it, so the
// second visit needs none of this.
//
// On a computer with no browser to stream from there is nothing to move to, and
// the page stays where it is — it carries its own button, and pressing that is
// the second of these messages, which is the one that gets an answer.
function browserHandOff(tab, d) {
  const url = browserNormalize(typeof d.url === "string" ? d.url : "")
              || browserUrlIn(tab);
  if (!url) return;
  if (!hasCapStrict("browser_full")) {
    if (tab.streamAsked) toast("The computer has no browser to stream from");
    tab.streamAsked = true;
    return;
  }
  browserRememberStream(url, true);
  toast(BROWSER_STREAM_SAID[typeof d.reason === "string" ? d.reason : ""]
        || "Streaming this page from the computer's Chrome");
  if (!browserIsFull(tab)) browserSwapMode(tab, true);
  browserNavigateIn(tab, url, false);
}

// What the computer says about a streamed tab: where it is, what it calls
// itself, and how far its own history goes each way. The proxy's tabs learn all
// of that from the shim's pockettui-nav report, and this is the same bookkeeping
// off the other mode's report — one address at a time, since a `tab` message is
// sent for every step of a load.
function browserFullTab(tab, msg) {
  if (!msg) return;
  const live = tab === browserTab();
  // Which target its page is on, which is what gets the page back after a reload
  // rather than loading it again (browserRemember).
  if (typeof msg.targetId === "string" && msg.targetId) tab.target = msg.targetId;
  tab.canBack = !!msg.canBack;
  tab.canFwd = !!msg.canFwd;
  // The page's own icon. An absent key is "nothing new", not "no icon": the
  // probe runs on a load and its answer rides the next message, so a `tab`
  // message without it must leave the mark the chip is already wearing alone.
  // The computer sends "" itself when the tab moves to another host.
  if (typeof msg.favicon === "string") tab.icon = msg.favicon;
  // Whether the computer still has a page for this tab: the memory watchdog
  // gives up the oldest background one and keeps the record, and showing it
  // again is what loads it (chromium.py's discard_oldest_hidden).
  if (typeof msg.discarded === "boolean") tab.discarded = msg.discarded;
  const url = browserNormalize(typeof msg.url === "string" ? msg.url : "");
  // about:blank is the target before it has been sent anywhere, not a page the
  // tab is on: a browser's own address field is empty there too.
  if (url && url !== "about:blank") {
    // Not while it is being typed into: the user is mid-address and a page
    // finishing its load must not take the field away from them.
    if (live && document.activeElement !== q("browser-url")) browserSetField(url);
    browserPush(tab, url);
    tab.loaded = url;
    // A tab that has moved on drops the old name first, so a chip never carries
    // the title of a page its tab has left.
    if (url !== tab.titleFor) { tab.title = ""; tab.titleFor = url; }
    browserApplyZoom(browserZoomFor(browserZoomHost(url)), tab);
    if (live) syncBrowserZoom(browserZoomHost(url));
  }
  const title = typeof msg.title === "string" ? msg.title : "";
  if (title && tab.titleFor) {
    tab.title = title;
    browserMarkTitles[tab.titleFor] = title;
  }
  renderBrowserTabs();
  if (live) { renderBrowserMarks(); syncBrowserNav(); }
  browserRemember();
}

// A link a page asked to open in a window of its own: a proxied page's
// window.open, and a middle or Ctrl+click in a streamed one. A tab of its own in
// front, in whatever mode a new tab is in — and at the cap the tab the link was
// clicked in, because spending it is better than the click going nowhere.
function browserOpenFrom(tab, raw) {
  const url = browserNormalize(typeof raw === "string" ? raw : "");
  if (!url) return;
  if (browserTabs.length >= BROWSER_TAB_MAX) { browserNavigateIn(tab, url); return; }
  browserNavigateIn(browserMakeTab(tab.lan), url);
}

// A window a streamed page opened for itself: window.open, or a link with
// target=_blank. The computer has already made it — the page is loading in a
// target of its own by the time this arrives — and it is shown to nobody until
// the pane says what becomes of it (chromium.py's _adopt_popup). A browser's
// answer is a tab in front, which is what this makes: the record carries the id
// the computer minted for it (`p1`, which no id this pane mints can collide
// with — browserFid's are `t…`) and the target its page is already on, so the
// navigation below takes that page over rather than loading it again
// (browserPointFull).
function browserPopup(msg) {
  if (!msg || !msg.tab) return;
  const url = browserNormalize(typeof msg.url === "string" ? msg.url : "");
  const from = browserTabs.find((t) => t.fid === msg.opener);
  if (browserTabs.length >= BROWSER_TAB_MAX) {
    // No chip left to put it in. The page is still what was asked for, so the
    // tab it was asked from goes there instead — a browser with no room in its
    // strip can do that much — and the window on the computer is closed, since
    // nothing here can show it.
    if (fullLink) fullLink.send({ type: "close", tab: msg.tab });
    if (from && url) browserNavigateIn(from, url);
    return;
  }
  const tab = browserMakeTab(false);
  // Streamed whatever a new tab would otherwise be here: the page is already
  // open in the computer's browser, and the preference for the proxy has nothing
  // to say about a window that exists.
  tab.fullMode = true;
  tab.fid = String(msg.tab);
  tab.target = String(msg.targetId || "");
  if (url) { browserNavigateIn(tab, url); return; }
  // A window the page means to write into itself (window.open() with no
  // address). There is nowhere to navigate, so the view takes the target as it
  // stands — and the tab counts as loaded, so the next address typed into it is
  // a navigation of the page rather than a second open.
  const view = browserFullView(tab);
  if (!view) return;
  view.open("", tab.target);
  tab.loaded = "about:blank";
  renderBrowserTabs();
  browserRemember();
}

// ---- zoom ------------------------------------------------------------------

// Zoom belongs to the host, not to the page: a dev server read at 125% is read
// at 125% on every page it serves, which is what a desktop browser's per-site
// zoom does. The port is part of the key — :3000 and :8000 are two apps.
//
// A host that has never been zoomed is drawn at the last factor the user set
// anywhere (cfg.browserZoomDefault), so a new tab opens at the size they are
// reading at rather than back at 100%; a host's own factor still wins. This is
// a desktop browser's default zoom plus its per-site list.
function browserZoomHost(url) {
  try { return new URL(url || browserCurrentUrl()).host; } catch (e) { return ""; }
}

function browserZoomFor(host) {
  const own = host ? cfg.browserZoom[host] : undefined;
  if (typeof own === "number") return own;
  const def = cfg.browserZoomDefault;
  return typeof def === "number" ? def : 1;
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
// A streamed tab is zoomed on the computer instead, where the browser showing
// the page does it the way Chrome's own zoom does (a device-metrics override,
// chromium.py) — there is no element here to scale, only a picture of one.
//
// The tab is named rather than looked up: zoom belongs to the host, a landing
// in a tab that is not on screen is still that host's page, and the frame or the
// view it has to be applied to is that tab's.
function browserApplyZoom(factor, tab = browserTab()) {
  if (tab.full) {
    // Only when it has changed: a landing reports itself three or four times and
    // each zoom message restarts the stream.
    if (tab.zoomAt === factor) return;
    tab.zoomAt = factor;
    tab.full.setZoom(factor);
    return;
  }
  const frame = tab.frame;
  if (!frame) return;
  const pct = (100 / factor) + "%";
  frame.style.width = pct;
  frame.style.height = pct;
  frame.style.transform = factor === 1 ? "" : "scale(" + factor + ")";
}

// A zoom the user asked for: applied, remembered against the host and as the
// default for hosts without one of their own, and shown
// on the panel that asked for it. No toast — the panel is open, its label is
// the reading, and a step that cannot go further leaves the number where it
// was, which is the honest answer to a press against an end.
function browserSetZoom(factor) {
  browserApplyZoom(factor);
  const host = browserZoomHost();
  if (host) {
    const rec = cfg.browserZoom;
    // 1 is written like any other factor rather than dropped: it is what holds
    // this host at 100% against a default that is not.
    rec[host] = factor;
    cfg.browserZoom = rec;
  }
  // And it is the default from here on, for every host with no factor of its own.
  cfg.browserZoomDefault = factor;
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
  q("browser-zoom-wrap").classList.toggle("open", on);
  q("browser-menu-scrim").classList.toggle("show", on);
  q("btn-browser-zoom").setAttribute("aria-expanded", on ? "true" : "false");
  if (on) syncBrowserZoom();
}

// The key's mark and the panel's reading. `host` is passed by the one caller
// that sets a zoom before its page is the current one — browserNavigate, which
// applies the factor ahead of the load so the page lays itself out once.
function syncBrowserZoom(host) {
  const factor = browserZoomFor(host === undefined ? browserZoomHost() : host);
  // A host left anywhere but 100% is a state that outlives the pane being
  // closed, so the key carries it: nothing else on screen would say so.
  q("btn-browser-zoom").classList.toggle("on", factor !== 1);
  q("browser-zoom-pct").textContent = Math.round(factor * 100) + "%";
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
  browserSaveMarks();
}

function renderBrowserMarks() {
  const bar = q("browser-bookmarks");
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
  const btn = q("btn-browser-star");
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
  const bar = q("browser-tab-row");
  const plus = q("btn-browser-newtab");
  if (!bar || !plus) return;
  browserTabs.forEach((tab, i) => {
    const name = browserTabName(tab);
    if (!tab.chip) {
      // The tab rather than its index: where a tab sits changes every time one
      // before it is closed, and a handler that captured the old number would
      // act on its neighbour.
      // The name in a span of its own, because a chip can carry a picture
      // before it: the page's icon goes in ahead of the words and the words are
      // still the one thing rewritten in place.
      const open = el("button", {
        type: "button", class: "tab-name",
        onclick: () => browserShowTab(browserTabs.indexOf(tab)),
      }, el("span", { class: "tab-word" }));
      const shut = el("button", {
        type: "button", class: "tab-del", "aria-label": "Close this tab",
        onclick: (e) => { e.stopPropagation(); browserCloseTab(browserTabs.indexOf(tab)); },
      }, "×");
      tab.chip = el("div", { class: "tab-chip" }, open, shut);
    }
    const open = tab.chip.firstElementChild;
    const word = open.querySelector(".tab-word");
    if (word.textContent !== name) {
      word.textContent = name;
      open.setAttribute("title", name);
    }
    // The page's own icon, in place of nothing — a favicon is what a browser's
    // chip carries, and only a streamed tab ever has one to carry.
    let icon = open.querySelector(".tab-icon");
    if (tab.icon) {
      if (!icon) {
        icon = el("img", { class: "tab-icon", alt: "" });
        open.insertBefore(icon, word);
      }
      if (icon.getAttribute("src") !== tab.icon) icon.setAttribute("src", tab.icon);
    } else if (icon) icon.remove();
    tab.chip.classList.toggle("on", i === browserActive);
    // Waiting to be answered, and given up by the computer: both are about a tab
    // the reader is not looking at, which is the only place there is to say it
    // (43-full-browser.js, and chromium.py's watchdog).
    tab.chip.classList.toggle("ask", !!(tab.full && tab.full.asking()));
    tab.chip.classList.toggle("dim", !!tab.discarded);
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
  for (const t of browserTabs) {
    if (t.frame) t.frame.hidden = t !== tab;
    // A streamed view that is not on screen is hidden and told to stop: the
    // computer has one page per pane to paint, and the one it paints is this one.
    if (t.full) {
      t.full.el.hidden = t !== tab;
      if (t !== tab) t.full.hide();
    }
  }
  if (tab.full) tab.full.show();
  showBrowserZoomMenu(false);
  const url = browserUrlIn(tab);
  browserSetField(url);
  syncBrowserNav();
  syncBrowserFull();
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
    browserApplyZoom(browserZoomFor(browserZoomHost(url)), tab);
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
  // Which kind of tab it is waits for the address it is opened on: a new tab is
  // a proxy tab, and the hosts that are not are known by their host
  // (browserNavigateIn settles it). The network the tab it was opened from was
  // on is inherited meanwhile, and dropped there if the tab turns out to be a
  // streamed one, which is on the computer's network already.
  tab.lan = !!lan;
  browserTabs.push(tab);
  browserActive = browserTabs.length - 1;
  for (const t of browserTabs) {
    if (t.frame) t.frame.hidden = true;
    if (t.full) { t.full.el.hidden = true; t.full.hide(); }
  }
  showBrowserZoomMenu(false);
  browserSetField("");
  syncBrowserNav();
  syncBrowserFull();
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
  browserHomeTab(browserMakeTab(false));
}

// A tab sent to the home page with the address field waiting on it. The "+"
// opens one, and so does a copy of this pane the split menu has just made —
// which is a second window of the same browser, and a window opens the way its
// first tab does.
function browserHomeTab(tab) {
  const gen = browserGen;
  const f = q("browser-url");
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
    browserDropPages();
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
  // The page on the computer goes with the chip: a closed tab is not a tab to
  // come back to, which is what tells this apart from closing the pane.
  browserDropFull(tab);
  browserTabs.splice(i, 1);
  if (browserActive > i) browserActive--;
  // The tab that slid into the closed one's place, or the last one if it was
  // the end of the row — a browser's own choice.
  if (wasActive) browserShowTab(Math.min(i, browserTabs.length - 1));
  else { renderBrowserTabs(); browserRemember(); }
}

// Which kind of tab a record asked for. A streamed tab comes back with the id
// the computer holds it under and the target its page is on, so the tab it gets
// is the page it left rather than a reload of it — and as a proxy tab where the
// computer has no browser to stream from any more, which is the honest answer
// and the one that still shows the page.
function browserSeedMode(tab, rec) {
  const full = !!(rec && rec.full);
  if (full && hasCapStrict("browser_full")) {
    tab.fullMode = true;
    if (typeof rec.fid === "string" && rec.fid) tab.fid = rec.fid;
    tab.target = typeof rec.targetId === "string" ? rec.targetId : "";
    return;
  }
  tab.lan = !!(rec && rec.lan);
  // A record that named a mode is honoured as far as this computer can: a
  // streamed tab with no browser left to stream from is a proxy tab, and so is
  // one that was turned round onto the computer's network. A record that named
  // none — an older shell's, which only ever wrote addresses — is whatever a new
  // tab is here.
  tab.fullMode = full || tab.lan ? false : null;
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
    browserSeedMode(tab, e);
    tab.stack = [url];
    tab.idx = 0;
    list.push(tab);
  }
  if (!list.length) return;
  browserDropPages();
  browserTabs = list;
  browserActive = Math.min(at, list.length - 1);
}

// ---- the two shapes --------------------------------------------------------

// Expanded is the pane's own state, but the class lives on #screen-term, for
// the docked explorer's reason: the seam it hides and the width it overrides
// are both read from there.
function syncBrowserExpand() {
  const on = browserDocked && browserExpanded;
  sideSetFull(id, on);
  const btn = q("btn-browser-expand");
  if (!btn) return;
  btn.querySelector("use").setAttribute("href", on ? "#i-collapse" : "#i-expand");
  btn.setAttribute("aria-label", on ? "Shrink the browser pane" : "Expand the browser pane");
}

// The expand set from outside the pane, the docked explorer's filesSetExpanded
// for the same reason: only one row of the column fills the main area, and the
// other row's arrival folds this one away (sideSetFull, 26-side-pane.js).
function browserSetExpanded(v) {
  browserExpanded = v;
  if (id === "browser") cfg.browserExpanded = v;
  syncBrowserExpand();
}

// This pane as a row of the column: the one screen it puts in its row, the way it
// closes, and its expand, all of which are the column's to ask for and this
// module's to do (26-side-pane.js).
// Whether the column has this row folded away behind the other one's expand.
// The row is display:none then: the page is still in the pane and nobody can see
// it, so a streamed tab stops streaming exactly as it does when the pane is
// closed, and starts again when the fold comes off.
let browserFolded = false;
function browserSetFolded(on) {
  if (browserFolded === !!on) return;
  browserFolded = !!on;
  if (on) { browserHideFulls(); return; }
  const tab = browserTab();
  if (browserOpen && tab.full) tab.full.show();
}

sideRegister(id, {
  type: "browser",
  els: () => [root],
  close: () => closeDockedBrowser(),
  setExpanded: (on) => browserSetExpanded(on),
  onFold: (hidden) => browserSetFolded(hidden),
});

// False is the column refusing the row — the pane it would have taken is a
// docked editor with unsaved work whose owner said stay (sideClaim,
// 26-side-pane.js) — and nothing about the browser has moved by then.
function openDockedBrowser(opts) {
  if (!sideClaim(id, opts)) return false;
  browserDocked = true;
  browserOpen = true;
  // Redundant beside a live terminal — it is right there — and the pane has
  // its own cross for leaving.
  q("btn-browser-term").style.display = "none";
  root.classList.add("docked");
  root.classList.add("active");
  syncBrowserExpand();
  browserRemember();
  return true;
}

// The frame keeps its page: the pane is a tap away again, and reloading a dev
// server every time it is closed would be the wrong trade. Only the slot and
// the classes go back.
function closeDockedBrowser() {
  if (!browserDocked) return;
  browserDocked = false;
  browserOpen = false;
  // A frame costs nothing while nobody is looking at it; a streamed page arriving
  // many times a second costs the link it arrives over.
  browserHideFulls();
  showBrowserZoomMenu(false);
  root.classList.remove("docked");
  root.classList.remove("active");
  syncBrowserExpand();       // takes .side-full off the terminal with it
  sideDrop(id);
}

function openFullBrowser() {
  if (browserOpen && !browserDocked) return;
  browserOriginFrom = $("screen-term").classList.contains("active")
    ? "screen-term" : "screen-list";
  $(browserOriginFrom).classList.remove("active");
  // Only the terminal is worth a one-tap way back to — the button says
  // terminal, exactly as the explorer's does.
  q("btn-browser-term").style.display =
    browserOriginFrom === "screen-term" ? "" : "none";
  browserDocked = false;
  browserOpen = true;
  root.classList.remove("docked");
  root.classList.add("active");
  syncBrowserExpand();
  syncChrome();
  history.pushState({ browser: true }, "", location.href);
}

// The pop's half: the entry is already spent by the time this runs, so nothing
// here touches history.
function closeFullBrowser() {
  browserHideFulls();
  showBrowserZoomMenu(false);
  root.classList.remove("active");
  const back = browserOriginFrom || "screen-list";
  browserOriginFrom = null;
  browserOpen = false;
  $(back).classList.add("active");
  syncChrome();
  // The terminal kept its socket while we were away; it only needs its size
  // re-checked, not a reconnect.
  if (back === "screen-term") refit(0);
}

// Every way in lands here — the globe key, the header button, a tapped private
// URL in the terminal, a restored session — so the two shapes are one entry
// point, the way openExplorer is for the folder. `tabs` and `at` are the strip
// a reload is putting back (restoreFileView, 09-image-viewer.js); every other
// caller opens the pane on whatever it was left holding; `opts` is the column's
// own (sideClaim's `keep`). Which of the column's rows this is, is the factory's
// `id` — the dispatcher under it is what picks the pane a caller means.
function openBrowser(url, tabs, at, opts) {
  if (needsSetup()) { openSettings(true); return; }
  if (demoMode) { toast("No browser in the demo"); return; }
  if (Array.isArray(tabs) && tabs.length) browserSeedTabs(tabs, at);
  // A refused row is no pane at all: seeding a tab into one and sending it
  // somewhere would be a page loading where nothing opened.
  if (browserDockable()) {
    if (!openDockedBrowser(opts)) return;
  } else openFullBrowser();
  browserLoadMarks();
  // Ahead of any press, so the key's own click has nothing to wait for.
  browserEnsureTabToken();
  renderBrowserTabs();
  // Every way in passes here, including the one a reload takes: a strip seeded
  // from the record (browserSeedTabs) never goes through browserShowTab, so
  // this is where a tab put back in either mode lights its own key.
  syncBrowserFull();
  syncBrowserLan();
  // A streamed page stopped when the pane was closed (browserHideFulls); the tab
  // on screen asks for it again. Before the navigation below rather than instead
  // of it: a tab whose page is still on the computer needs nothing else, and one
  // that is being sent somewhere is shown by the open that sends it.
  if (browserTab().full) browserTab().full.show();
  const target = browserNormalize(url);
  if (target) { browserNavigate(target); return; }
  // Opened with nothing to go to: the page the active tab was last on, whether
  // it is still in its frame (a close keeps it) or only in this session's
  // record (a reload does not).
  // Failing both, a search page: a browser that opens on a blank frame reads
  // as broken, and the address field is one tap away either way.
  const tab = browserTab();
  const last = browserCurrentUrl() || browserRememberedUrl() || BROWSER_HOME;
  // A copy with nothing in it at all is a second window rather than this one
  // reopened — the split menu has just made it — and a window opens on its home
  // page with the address field waiting, never on a copy of the other pane's
  // strip.
  if (id !== "browser" && tab.idx < 0 && last === BROWSER_HOME) {
    browserHomeTab(tab);
    return;
  }
  if (last !== tab.loaded) browserNavigate(last, tab.idx < 0);
  else { browserSetField(last); syncBrowserNav(); }
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
  // Whatever it says, that it says anything is what the landing watchdog is
  // listening for: only a document the proxy served can speak here
  // (browserWatchLanding).
  if (typeof d.type === "string" && d.type.indexOf("pockettui-") === 0) {
    tab.heardAt = Date.now();
  }

  if (d.type === "pockettui-nav") {
    const url = browserNormalize(typeof d.url === "string" ? d.url : "");
    if (!url) return;
    // Not while it is being typed into: the user is mid-address and the page
    // finishing its load must not take the field away from them.
    if (live && document.activeElement !== q("browser-url")) browserSetField(url);
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
    browserApplyZoom(browserZoomFor(browserZoomHost(url)), tab);
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
      // A page that moved itself onto another host is another host's page, and
      // the key names the host it would remember.
      syncBrowserFull();
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
  // for is a page to read and not somewhere to type (browserOpenFrom).
  if (d.type === "pockettui-open") {
    browserOpenFrom(tab, d.url);
    return;
  }

  // A page the proxy knows it cannot serve, saying so from the document it put
  // up in its place: a search page that wants a real browser, a sign-in. The
  // stream is what that page needs and the host is worth remembering, so the tab
  // changes mode under the user rather than leaving them to find the key.
  if (d.type === "pockettui-stream") {
    browserHandOff(tab, d);
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
  // A streamed tab steps in the browser that holds its page, which is the only
  // one that knows what its history is: what comes back is a `tab` message, and
  // that is what moves the field and the arrows (browserFullTab). One step
  // either way — the deltas above are a proxied page's own history calls.
  if (browserIsFull(tab)) {
    browserFullSend(tab, delta < 0 ? "back" : "fwd");
    return;
  }
  const i = tab.idx + delta;
  if (tab.idx < 0 || i === tab.idx || i < 0 || i >= tab.stack.length) return;
  tab.idx = i;
  browserNavigateIn(tab, tab.stack[i], false);
}

function browserStep(delta) { browserStepIn(browserTab(), delta); }

q("btn-browser-back").addEventListener("click", () => browserStep(-1));
q("btn-browser-fwd").addEventListener("click", () => browserStep(1));
q("btn-browser-reload").addEventListener("click", () => {
  const tab = browserTab();
  // The page the computer is holding, reloaded there: a navigation from here
  // would be a second open for a tab that already has the page on it.
  if (browserIsFull(tab)) { browserFullSend(tab, "reload"); return; }
  const u = browserCurrentUrl();
  if (u) browserNavigate(u, false);
});
q("btn-browser-zoom").addEventListener("click", () => {
  showBrowserZoomMenu(!q("browser-zoom-wrap").classList.contains("open"));
});
// Stepping leaves the panel up: a zoom is walked to, not picked, and the label
// above these two is what says where it got to.
q("browser-zoom-minus").addEventListener("click", () => browserStepZoom(-1));
q("browser-zoom-plus").addEventListener("click", () => browserStepZoom(1));
q("browser-zoom-pct").addEventListener("click", () => browserSetZoom(1));
q("browser-menu-scrim").addEventListener("click", () => showBrowserZoomMenu(false));
q("btn-browser-star").addEventListener("click", () => browserToggleMark());
// Which browser this tab is, and which browser this site is from here on.
// Pressed, the page is streamed from the computer's own Chrome and the host goes
// on the record, so every tab opened there is streamed without being asked
// again; pressed again the tab steps down to the proxy and the host comes off
// it, on the same address and under the same name. The tab beside it is
// unaffected either way — what the record changes is where the tabs opened after
// it start.
q("btn-browser-full").addEventListener("click", () => {
  const tab = browserTab();
  if (browserIsFull(tab)) {
    // Forgotten before the step down, since the navigation it makes reads the
    // record back and would put the tab straight up again.
    browserRememberStream(browserUrlIn(tab), false);
    browserSetFull(tab, false);
    return;
  }
  // Refused rather than attempted where the computer has no browser to stream
  // from: the key is hidden there, and a press that arrived anyway has nothing
  // to open.
  if (!hasCapStrict("browser_full")) return;
  browserRememberStream(browserUrlIn(tab), true);
  browserSetFull(tab, true);
});
// This tab, on the computer's own network: the same address in the same frame,
// fetched this time as a page in its own right. An app written to be the top
// window then works — top is the page itself, the views it writes are its own
// to reach, and the storage and cookies a real visit would have are there —
// none of which is true of a tab in the ordinary mode. A second press puts the
// tab back, and the tab beside it is unaffected either way.
q("btn-browser-tab").addEventListener("click", async () => {
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
q("btn-browser-newtab").addEventListener("click", () => browserAddTab());
q("btn-browser-expand").addEventListener("click", () => {
  browserSetExpanded(!browserExpanded);
  refit(0);
});
q("btn-browser-close").addEventListener("click", () => closeDockedBrowser());
q("btn-browser-term").addEventListener("click", () => history.back());

q("browser-url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    const v = q("browser-url").value.trim();
    if (v) browserNavigate(browserTyped(v));
    // Blurred either way: on a phone the address bar is what the keyboard is
    // up for, and the page underneath is what the tap was about. A new tab's
    // recovery goes with it — this blur is the user's own, and taking the field
    // back from the page they just asked for would be the opposite of the help
    // it was armed to give.
    browserCancelGrab();
    q("browser-url").blur();
    return;
  }
  if (e.key === "Escape") {
    e.preventDefault();
    // Not the pane's Escape — this one only puts the field back.
    e.stopPropagation();
    browserSetField(browserCurrentUrl());
    browserCancelGrab();
    q("browser-url").blur();
  }
});

// ---- leaving ---------------------------------------------------------------

// A hardware keyboard's Escape does what the close button does, the docked
// explorer's rule and for its reasons: on capture so the field's own handler
// below it keeps its own Escape, and, docked, only for a press aimed inside
// the pane — the terminal beside it is live and Escape is one of its keys.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!root.classList.contains("active")) return;
  if (e.target === q("browser-url")) return;
  // A tab in full mode has the keyboard, and Escape is one of the keys the page
  // is owed — a dialog to dismiss, a menu to close, vi in a web terminal. The
  // pane does not close under somebody who is typing in it (43-full-browser.js).
  if (fullBrowserHasFocus()) return;
  if (browserDocked && !root.contains(e.target)) return;
  // Ahead of the pane's own Escape: an open panel is the top thing to dismiss,
  // exactly as the file bar's dropdowns are.
  if (q("browser-zoom-wrap").classList.contains("open")) {
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
  // The docked pane pushed nothing, so no pop is ever its own — and a copy of
  // this pane is only ever docked, so no pop is ever a copy's.
  if (browserDocked) return;
  if (!root.classList.contains("active")) return;
  closeFullBrowser();
});

// The left-edge back gesture the other full-screen views carry
// (attachEdgeSwipe, 28-file-explorer.js). Docked it never fires: the pane is
// beside the terminal, not over it, and the gesture belongs to the screen.
attachEdgeSwipe(root, () => history.back());

// ---- per-session and per-computer state ------------------------------------

// The pane goes down with the session it belongs to (fileViews,
// 09-image-viewer.js). No history is touched here: the caller is replacing
// every screen at once and spends the entries itself.
// The addresses, not the pages: the frames go with the pane and its streamed
// tabs are closed on the computer (browserTeardown below), so the session that
// comes back gets its tabs, its stacks and its names, and the tab it is left on
// loads. Keeping them alive instead would keep every session's pages in the
// document — and every session's tabs open in the computer's browser — at once,
// for a switch that costs one page load.
function browserStash() {
  return {
    docked: browserDocked, active: browserActive,
    tabs: browserTabs.map((t) => ({
      stack: t.stack.slice(), idx: t.idx, title: t.title, titleFor: t.titleFor,
      lan: t.lan, full: browserIsFull(t), fid: t.fid, targetId: t.target,
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
  browserDropPages();
  browserDropLink();
  showBrowserZoomMenu(false);
  root.classList.remove("docked");
  root.classList.remove("active");
  if (wasDocked) { syncBrowserExpand(); sideDrop(id); }
}

// Only the docked shape comes back: it is the only one a rail switch can
// happen under. The full-screen pane covers the list the switch is made from,
// so a session is never left with one up.
function browserRestore(s) {
  const recs = Array.isArray(s.tabs) ? s.tabs : [];
  browserDropPages();
  // Through the normaliser rather than as they were kept: a stash written
  // before a restart can hold a proxied address, and restoring one as a page
  // address is what the pane would then navigate to.
  browserTabs = recs.length ? recs.map((r) => {
    const tab = browserNewTab();
    tab.stack = (r.stack || []).map((u) => browserNormalize(u));
    tab.idx = typeof r.idx === "number" ? r.idx : tab.stack.length - 1;
    tab.title = typeof r.title === "string" ? r.title : "";
    tab.titleFor = typeof r.titleFor === "string" ? r.titleFor : "";
    browserSeedMode(tab, r);
    return tab;
  }) : [browserNewTab()];
  browserActive = Math.min(Math.max(0, s.active | 0), browserTabs.length - 1);
  renderBrowserTabs();
  syncBrowserFull();
  syncBrowserLan();
  if (!s.docked) { syncBrowserNav(); return; }
  openDockedBrowser();
  // The tab that was on screen, and it alone: the rest load when a chip asks
  // for one, for browserShowTab's reason.
  const u = browserCurrentUrl();
  if (u) browserNavigate(u, false);
  else { browserSetField(""); syncBrowserNav(); }
}

// This pane back to nothing, for a switch to another computer
// (browserResetForProfile, under the factory). A page the machine being left was
// serving is not a page on the next one.
function browserReset() {
  if (browserDocked) closeDockedBrowser();
  else browserTeardown();
  // Again on its own account: the docked path above goes through
  // closeDockedBrowser, which is not a teardown, and a mint in flight for the
  // machine being left must not land in the pane the next one gets.
  browserGen++;
  browserCancelGrab();
  // Every tab with it, frames and all: they are pages on the machine being
  // left. A fresh one takes their place, the way a closed pane's does. The socket
  // goes too — the browser on the other end of it is the machine's own.
  browserDropPages();
  browserDropLink();
  browserTabs = [browserNewTab()];
  browserActive = 0;
  renderBrowserTabs();
  syncBrowserFull();
  syncBrowserLan();
  showBrowserZoomMenu(false);
  // Nothing is blanked through a frame's location any more: browserDropPages
  // takes the elements out of the document, which discards their browsing
  // contexts and the joint history entries with them. Assigning src is still
  // the thing never done — that would spend an entry the shell's back needs.
  browserSetField("");
  syncBrowserNav();
}

// Every streamed tab in this pane, closed on the computer. The tabs stay, with
// their addresses and their names — what goes is the page, so that nothing of
// the browsing session is left open for the profile reset to refuse
// (browserClearProfile, under the factory).
function browserClearFulls() {
  for (const tab of browserTabs) {
    if (!tab.full) continue;
    browserDropFull(tab);
    tab.loaded = "";
    tab.navigating = false;
  }
}

// The page this pane was showing, fetched again in a browser that has just
// forgotten every login it had. The tab on screen and no other: the rest load
// when a chip asks for one, browserShowTab's rule.
function browserReloadFull() {
  if (!browserOpen) return;
  const tab = browserTab();
  const url = browserUrlIn(tab);
  if (browserIsFull(tab) && url) browserNavigateIn(tab, url, false);
}

// The keys in this pane's own bar that a capability answer takes away. The
// globe key and the header button are the app's rather than a pane's and are
// dealt with once (syncBrowseCap, under the factory), which is also what calls
// this.
function browserSyncCap() {
  // Its own capability, not the proxy's: a server can store bookmarks without
  // httpx to fetch pages with, and one too old for the route would answer the
  // save with a 404 the user only learns about after tapping the star.
  q("btn-browser-star").hidden = !hasCapStrict("bookmarks");
  // Strictly checked too, and with the computer's own refusal on top of it: a
  // server too old for the mode answers with the pane's own permission, and a
  // tab turned round on that is a tab that cannot do the one thing it was
  // turned round for.
  // Opening another tab is not gated: it is a frame in here, which every
  // computer that can show a page at all can serve.
  //
  // Both of the mode keys are set from the tab on screen as well as from the
  // computer's answers, so both go through their own sync rather than being
  // written here: this key is hidden for a streamed tab, and the one beside it
  // is hidden on a computer with no browser to stream from.
  q("btn-browser-full").hidden = !hasCapStrict("browser_full");
  syncBrowserFull();
  syncBrowserLan();
}

renderBrowserTabs();
syncBrowserNav();
syncBrowserFull();
syncBrowserLan();

// What the column and the rest of the app can ask of this pane. Everything else
// in the body above is the pane's own and stays in the closure.
const api = {
  // Null in every build but one being driven by the column's smoke check, which
  // hands a pane its navigations instead of a computer (browserNavigateIn).
  navigateHook: null,
  isOpen: () => browserOpen,
  isDocked: () => browserDocked,
  open: openBrowser,
  closeDocked: closeDockedBrowser,
  renderMarks: renderBrowserMarks,
  syncCap: browserSyncCap,
  stash: browserStash,
  restore: browserRestore,
  teardown: browserTeardown,
  reset: browserReset,
  clearFulls: browserClearFulls,
  reloadFull: browserReloadFull,
};
browserPanes[id] = api;
return api;

}

// ------------------------------------------------------------
// The panes, and the app's way in to them
// ------------------------------------------------------------

// The pane the markup ships, which is the one every existing way in opens and
// the only one that is ever the whole window.
makeBrowserPane("browser", $("screen-browser"));

// The second one, made out of the first: the copy is the whole shape — the tab
// strip with its window controls, the address row, the bookmarks bar and the
// wrap the frames go in — with the ids inside it duplicated, which is why the
// factory reaches them through its own root rather than through the document.
// What does not come over is what belonged to the pane it was copied from: its
// chips, its frames, its bookmarks, the address in its field and the classes the
// column had given it. The frame template does, because that is markup and not
// state — every frame either pane cuts has to carry the same sandbox list. It
// goes in right after the original, so it is a following sibling of #screen-term
// exactly as the original is, which is what the stylesheet's rules are written
// against.
function browserMakeAt(id) {
  if (id !== "browser#2" || browserPanes[id]) return null;
  const src = $("screen-browser");
  const clone = src.cloneNode(true);
  clone.id = "screen-browser-2";
  clone.classList.remove("active", "docked",
                         "side-top", "side-bot", "side-hidden");
  clone.querySelector("#browser-tab-row").textContent = "";
  const wrap = clone.querySelector("#browser-wrap");
  const tpl = wrap.querySelector("#browser-frame-tpl");
  wrap.textContent = "";
  wrap.appendChild(tpl);
  const marks = clone.querySelector("#browser-bookmarks");
  marks.textContent = "";
  marks.hidden = true;
  clone.querySelector("#browser-url").value = "";
  clone.querySelector("#browser-zoom-wrap").classList.remove("open");
  clone.querySelector("#browser-menu-scrim").classList.remove("show");
  clone.querySelector("#btn-browser-zoom").setAttribute("aria-expanded", "false");
  clone.querySelector("#btn-browser-term").style.display = "none";
  const split = clone.querySelector(".dock-split-wrap");
  split.classList.remove("open");
  split.querySelector(".dock-split").setAttribute("aria-expanded", "false");
  split.querySelector(".dock-split-menu").textContent = "";
  src.after(clone);
  sideWireBar(clone);
  const pane = makeBrowserPane(id, clone);
  // The capability answer landed before this pane existed, so its own two keys
  // are told now rather than waiting on a computer that has already spoken — and
  // its bookmarks bar with them, since the list is the computer's and this is
  // the one bar that has never drawn it.
  pane.syncCap();
  pane.renderMarks();
  return pane;
}

// The pane a row id names, made the first time that row is asked for and kept
// from then on — a copy that has been closed is a copy that can be opened again,
// and making a fresh one would throw away the tabs it was holding.
function browserPaneAt(id) {
  return browserPanes[id] || browserMakeAt(id);
}

// How the column opens a second one when the split menu asks for it.
sideMakers.browser = (id) => !!browserPaneAt(id);

// Which of the column's browser rows a call is about. The split menu and a
// restored record name one; everything else — the globe key, a private URL
// tapped in the terminal — means the pane a press last named, which with one
// open is the only answer there is (sideFocusedOf, 26-side-pane.js). The id
// names a slot rather than a pane that must already exist: opening the second
// one is what makes it.
function openBrowser(url, tabs, at, id, opts) {
  let want = id || sideFocusedOf("browser") || "browser";
  // The copy is only ever a row of the column. Off a wide layout, or with no
  // terminal behind it, the only shape left is the full-screen one, and that one
  // is the markup's own pane.
  if (want !== "browser" && !browserDockable()) want = "browser";
  const pane = browserPaneAt(want);
  return pane ? pane.open(url, tabs, at, opts) : undefined;
}

// One row of the column, or every one of them: the narrow-layout fallback and
// the profile switch mean all, the registry and the split menu's eviction mean
// one. A close for a pane that was never made is nothing to do.
function closeDockedBrowser(id) {
  if (id) {
    const pane = browserPanes[id];
    if (pane) pane.closeDocked();
    return;
  }
  for (const pane of Object.values(browserPanes)) pane.closeDocked();
}

// The globe key's own way in and out, the folder key's toggle (openFilesAtCwd,
// 28-file-explorer.js) mirrored: the pane the key put up is the pane it puts
// away, and with two browser rows open that is the one last pressed in. Full
// screen there is nothing to toggle — back is how that one leaves — so
// openBrowser stays the way in for everything else, and a tapped URL still lands
// in a pane that is already open.
function toggleBrowserPane() {
  const id = sideFocusedOf("browser");
  if (id) { closeDockedBrowser(id); return; }
  openBrowser();
}

// ---- the rail's stash (see fileViews in 09-image-viewer.js) -----------------
// Each strip under its own name: two rows of pages, and a restore that did not
// know which was which would put one back in the other's row.

function browserStash(id) {
  const pane = browserPanes[id];
  return pane && pane.isOpen() ? pane.stash() : null;
}

function browserRestore(a, b) {
  if (a) browserPanes.browser.restore(a);
  if (b) {
    const pane = browserPaneAt("browser#2");
    if (pane) pane.restore(b);
  }
}

function browserTeardown() {
  for (const pane of Object.values(browserPanes)) pane.teardown();
}

// Everything the panes hold about the computer being left, for a switch to
// another one (switchProfile, 40-profiles.js). Each pane's own half first, then
// the half that was never a pane's: the tokens are that machine's process, and
// the bookmarks are its list.
function browserResetForProfile() {
  for (const pane of Object.values(browserPanes)) pane.reset();
  browserToken = null;
  // The next computer answers for itself, both on the flavour and on whether it
  // is the one that served this shell.
  browserTabToken = null;
  browserTabBlocked = false;
  browserMarks = [];
  browserMarksAsked = false;
  browserMarkTitles = {};
  browserRenderMarks();
}

// ---- the browsing session on the computer ------------------------------------

// "Clear browsing data" in Settings. Every cookie, login and byte of site data
// the streamed browser ever collected is in one directory on the computer, and
// POST api/browser/reset stops the browser and deletes it — which is the whole
// of "sign me out of everything" (app.py's api_browser_reset).
//
// The route refuses while a tab is open there, and rightly: the tabs are
// somebody's screen and a reset that pulled the page out from under them would
// be indistinguishable from a crash. So the panes close theirs first, and the
// page each of them was showing is fetched again afterwards, in a browser that
// has forgotten it.
async function browserClearProfile() {
  for (const pane of Object.values(browserPanes)) pane.clearFulls();
  let said = "";
  // The closes went out on the panes' own sockets and this goes out on its own
  // connection, so the computer may not have got to them yet: a 409 here is that
  // race rather than a refusal, and it is worth waiting out.
  for (let i = 0; i < 4; i++) {
    let r;
    try {
      r = await fetch(apiURL("api/browser/reset"),
                      { method: "POST", cache: "no-store", headers: authHeaders() });
    } catch (e) {
      dbg("browser reset: ask failed", e);
      toast("Couldn't reach the computer");
      return false;
    }
    if (r.ok) {
      toast("Browser profile cleared");
      for (const pane of Object.values(browserPanes)) pane.reloadFull();
      return true;
    }
    let d = null;
    try { d = await r.json(); } catch (e) {}
    said = (d && d.error) ? String(d.error) : "HTTP " + r.status;
    if (r.status !== 409) break;
    await new Promise((done) => setTimeout(done, 250));
  }
  toast(said || "Couldn't clear the browsing data");
  return false;
}

// Whether the computer on the other end can proxy pages at all. Strictly
// checked: a server too old for api/browse 404s the mint, and the pill's globe
// key stays hidden rather than being left to fail. The header button is not a
// control any more (the pane is a two-pane feature) — it is what buildKeybar
// reads the answer off on a rebuild, which is why its hidden state is still
// kept, and this is the one place either of them is set. The keys inside a pane
// are that pane's own, and every pane hears the answer.
function syncBrowseCap() {
  const on = hasCapStrict("browse");
  const btn = $("btn-browser");
  if (btn) btn.hidden = !on;
  const key = $("keybar").querySelector(".k-browser");
  if (key) key.classList.toggle("show", on);
  for (const pane of Object.values(browserPanes)) pane.syncCap();
}
