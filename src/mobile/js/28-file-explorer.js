// ============================================================
// File explorer
// ============================================================
// A phone-first view over /api/fs/*: reached from the session list's folder
// button (opens at $HOME) and from the terminal key bar's folder key (opens at
// the pane's cwd). One history entry per visit, plus one more per folder
// navigated into from there — crumbs, dir taps and the address bar all push,
// so back walks folder history one step at a time; only back at the entry
// folder closes the whole view.
//
// Beside a terminal on a wide layout it is that same view docked instead:
// a pane in the slot the diff pane also wants (26-side-pane.js), with the
// terminal live beside it rather than swapped out under it. Everything below
// the topbar is unchanged — same listing, same taps, same sheets, same
// editor and reader over the top. What the docked shape drops is the browser
// history: it pushes nothing, and its own back arrow walks filesStack, so a
// back press stays the terminal's to answer.

let filesPath = "";        // the directory currently listed
let filesHome = "";        // $HOME as the backend reports it, for ~ crumbs
let filesOrigin = null;    // the screen to restore on close
// The folders listed since the explorer opened, entry folder first — the app's
// own record of where back lands, and what back at [0] closes. It has to be
// ours rather than the state objects stored with the history entries: iOS
// clobbers those (seen after the download flow points the top frame at an
// attachment URL — downloadViaLink's anchor click — which iOS aborts into its
// download UI, nulling history.state on the way), after which every pop arrives
// stateless and a state-reading handler reads each one as the pop past the
// entry and closes the whole view from any depth.
let filesStack = [];
let filesSelected = null;  // the entry the action sheet is about
// The entries currently drawn, so switching list/grid redraws them in place
// rather than re-listing the folder.
let filesEntries = [];
// Whether the explorer is the terminal's right-hand pane rather than a screen
// over it, and whether that pane is filling the main area. Wide layouts only —
// nothing narrower has room for the slot.
let filesDocked = false;
let filesExpanded = cfg.filesExpanded;
// The folder the terminal's own cwd last put the docked pane at — what "the
// pane is still where the terminal left it" is measured against, and "" while
// nothing has ever synced it. Only the docked shape has a terminal to follow.
let filesSyncedCwd = "";
// Set while btn-files-term's multi-entry history.go is in flight, so the one
// popstate it lands as closes the view instead of climbing one folder.
let filesClosing = false;
// A finished long-press ends in a click the browser synthesizes on the same
// row; stamping the moment lets that click be ignored (the viewer's
// viewerOpenedAt idea).
let filesPressedAt = 0;

// ---- reading at a git ref ---------------------------------------------------
// The listing can come from a branch instead of from the disk: `ref` and `root`
// on /api/fs/list and /api/fs/read answer out of `git ls-tree` and `git show`
// at that ref. It is a reading mode and nothing else — every write the explorer
// offers goes away while it is on, the editor opens read-only, and the working
// tree the terminal beside it is sitting in never moves, which is the point of
// looking at a branch this way rather than checking it out.
//
// Deliberately not remembered across loads, unlike the layout and sort above:
// those are how this user reads every folder, and this is a place they went to
// look at something. A reload lands back on the disk.

let filesRef = "";        // the branch being read, "" for the working tree
let filesRefRoot = "";    // the repo top level filesRef's paths are relative to
// The /api/git/branches answer about the folder on screen — {root, current,
// branches}, root null outside a repo — or null before it has been asked.
let filesRepo = null;
// Which folder that answer was about. Inside a known root the answer cannot
// have changed, and outside one there is no root to test the next folder
// against, so this is what says whether the question needs asking again.
let filesRepoAsked = "";

// Mirrors app.py's MEDIA_TYPES allowlist: these open in the existing viewer
// over /api/file rather than in the editor.
const FILES_MEDIA_RE = /\.(?:png|jpe?g|gif|webp|svg|bmp|mp4|webm|mov)$/i;

// Markdown opens rendered, in the reader (33-md-reader.js), rather than in the
// editor — Edit there is one tap away.
const FILES_MD_RE = /\.(?:md|markdown)$/i;

// A page opens as a page, in a tab of its own — seeing the source of a report
// is not what anyone taps it for. Editing is the long-press sheet's Edit.
const FILES_HTML_RE = /\.(?:html?|xhtml)$/i;

function joinPath(dir, name) {
  return (dir === "/" ? "" : dir) + "/" + name;
}
function baseName(p) {
  const parts = p.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || "/";
}
function fmtSize(n) {
  if (n < 1024) return n + " B";
  let v = n;
  for (const u of ["KB", "MB", "GB"]) {
    v /= 1024;
    if (v < 1024) return (v < 10 ? v.toFixed(1) : Math.round(v)) + " " + u;
  }
  return Math.round(v / 1024) + " TB";
}

function openExplorer(path) {
  // Nothing to browse yet — prompt instead of failing against the static host.
  if (needsSetup()) { openSettings(true); return; }
  // Beside a terminal there is room for both, so the explorer docks rather than
  // taking the screen. Every caller lands here — the folder key, a tapped path,
  // the editor's parent folder — so the two shapes are one entry point.
  if (isWideLayout() && $("screen-term").classList.contains("active")) {
    return openDockedFiles(path);
  }
  if (!$("screen-files").classList.contains("active")) {
    filesOrigin = $("screen-term").classList.contains("active")
      ? "screen-term" : "screen-list";
    $(filesOrigin).classList.remove("active");
    // Only the terminal is worth a one-tap way back to — the button says
    // terminal. Set here and nowhere else: filesOrigin outlives a trip through
    // the editor, which returns to this screen without coming back through
    // openExplorer.
    $("btn-files-term").style.display = filesOrigin === "screen-term" ? "" : "none";
    $("screen-files").classList.add("active");
    syncChrome();
    filesStack = [];         // seeded once loadDir below resolves the real path
    history.pushState({ files: true }, "", location.href);
  }
  // The listing is handed back rather than left running: openPathInExplorer
  // opens a file on top of this folder, and the folder is only really the
  // screen underneath once its listing has landed and seeded filesStack.
  return loadDir(path);
}

// The state object for the history entry matching wherever navigation
// currently sits: bare at the entry folder (same as the very first push in
// openExplorer), path-carrying one level in. Nothing reads it back — popstate
// goes by filesStack — but the address field's pushState-back trick below has
// to re-push *something*, and keeping the shape uniform with the other pushes
// costs a line.
function filesEntryState() {
  return filesPath === filesStack[0] ? { files: true } : { files: true, path: filesPath };
}

function closeExplorer() {
  // Before the address-field branch below, which returns without closing the
  // screen: either way the bar's menus are going, scrim and all.
  closeFilesMenus();
  // Back (popstate or the edge swipe) while the address field is open closes
  // just the field, same as Escape — same pushState-back trick editorPopped()
  // uses for a dirty buffer, since the pop has already happened by the time
  // either gets a say.
  if ($("files-path-wrap").classList.contains("editing")) {
    closePathEdit();
    history.pushState(filesEntryState(), "", location.href);
    return;
  }
  $("screen-files").classList.remove("active");
  const back = filesOrigin || "screen-list";
  filesOrigin = null;
  filesStack = [];
  clearRefState();
  $(back).classList.add("active");
  syncChrome();
  // The terminal kept its socket while we were away; it only needs its size
  // re-checked, not a reconnect.
  if (back === "screen-term") refit(0);
}

// ---- docked beside the terminal (wide layouts) -----------------------------
// The same screen, seated in the slot on the terminal's right (26-side-pane.js)
// instead of over it. #screen-term keeps .active throughout — the terminal
// renders and takes keys the whole time the pane is up — so everything that
// used to read "the explorer is up" off that class has to say which of the two
// shapes it means; the flag is what says it.

// Expanded is the pane's own state, but the class lives on #screen-term: the
// seam it hides and the width it overrides are both read from there.
function syncFilesExpand() {
  const on = filesDocked && filesExpanded;
  $("screen-term").classList.toggle("side-full", on);
  const btn = $("btn-files-expand");
  btn.querySelector("use").setAttribute("href", on ? "#i-collapse" : "#i-expand");
  btn.setAttribute("aria-label", on ? "Shrink the file pane" : "Expand the file pane");
}

// Opening the pane a second time is a navigation within it, not a fresh entry:
// a path tapped in the terminal lands in the pane already open, and the crumb
// stack it walks back through is worth keeping.
function openDockedFiles(path) {
  const already = filesDocked;
  filesDocked = true;
  filesOrigin = "screen-term";
  // Redundant beside a live terminal — it is right there — and the pane has
  // its own cross for leaving.
  $("btn-files-term").style.display = "none";
  $("screen-files").classList.add("docked");
  $("screen-files").classList.add("active");
  syncFilesExpand();
  sideClaim("files");
  if (already) return navigateDir(path);
  filesStack = [];           // seeded once loadDir below resolves the real path
  return loadDir(path);
}

function closeDockedFiles() {
  if (!filesDocked) return;
  closeFilesMenus();
  closePathEdit();
  filesDocked = false;
  filesOrigin = null;
  filesStack = [];
  clearRefState();
  $("screen-files").classList.remove("docked");
  $("screen-files").classList.remove("active");
  syncFilesExpand();         // takes .side-full off the terminal with it
  sideDrop("files");
}

// The docked pane owns no history entries, so its back arrow has to do what a
// pop does for the full-screen explorer: climb filesStack, and close at [0].
function filesBack() {
  if (!filesDocked) { history.back(); return; }
  if ($("files-path-wrap").classList.contains("editing")) { closePathEdit(); return; }
  if (filesStack.length > 1) {
    filesStack.pop();
    const path = filesStack[filesStack.length - 1];
    const hit = cachedListing(path);
    if (hit) applyListing(hit);
    else loadDir(path);
    return;
  }
  closeDockedFiles();
}

$("btn-files-expand").addEventListener("click", () => {
  filesExpanded = !filesExpanded;
  cfg.filesExpanded = filesExpanded;
  syncFilesExpand();
  refit(0);
});
$("btn-files-close").addEventListener("click", () => closeDockedFiles());

// The pane after a rail switch that left it holding the slot with nothing in
// it: this session stashed no folder, so it opens at the session's own cwd.
// The demo has no files to open at all, so it gives the slot back instead of
// leaving the terminal narrowed against an empty pane.
function filesFollowSession() {
  if (!isWideLayout() || sideOwner !== "files" || filesDocked) return;
  if (demoMode) { sideDrop("files"); return; }
  openFilesAtCwd();
}

// ---- putting the whole view away (see fileViews in 09-image-viewer.js) ------
// A rail switch stashes the reader or the editor for the session it is leaving,
// and the browsing underneath goes with it: coming back to a file view that
// landed on the session list would say the switch had lost the folder.

// History entries the explorer owns: openExplorer's push plus one per level
// navigated into — filesStack.length, and still one if the entry folder never
// resolved and the stack stayed empty. The same count jumpToTerminal spends.
// The docked pane pushes none at all, so it owns none.
function filesEntryCount() { return filesDocked ? 0 : (filesStack.length || 1); }

function filesStash() {
  return {
    stack: filesStack.slice(), path: filesPath,
    origin: filesOrigin, docked: filesDocked, syncedCwd: filesSyncedCwd,
    // A branch being read is part of what the session was looking at, so it
    // comes back with the folder rather than the pane returning to the disk.
    ref: filesRef, refRoot: filesRefRoot, repo: filesRepo, repoAsked: filesRepoAsked,
  };
}

// Drops the view without putting anything back: the caller is replacing every
// screen at once, so closeExplorer's return-to-origin (and its refit, which
// would fit a terminal that is about to change session) is not what it wants.
function filesTeardown() {
  $("screen-files").classList.remove("active");
  filesOrigin = null;
  filesStack = [];
  clearRefState();
  // Docked, the slot stays claimed — the pane is the terminal's and the
  // terminal is only changing hands — but nothing is in it until it is filled
  // again, by a restore or by filesFollowSession().
  filesDocked = false;
}

// The mirror of openExplorer's screen work, minus the history push — the
// caller re-pushes every entry the stashed view owned, in one place.
function filesRestore(s) {
  filesStack = s.stack.slice();
  filesPath = s.path;
  filesOrigin = s.origin;
  filesDocked = !!s.docked;
  // A pane that was following its session's terminal when the rail left it is
  // still following when the rail comes back.
  filesSyncedCwd = s.syncedCwd || "";
  filesRef = s.ref || "";
  filesRefRoot = s.refRoot || "";
  filesRepo = s.repo || null;
  filesRepoAsked = s.repoAsked || "";
  syncRefBar();
  syncRefActions();
  $("btn-files-term").style.display =
    !filesDocked && filesOrigin === "screen-term" ? "" : "none";
  $("screen-files").classList.toggle("docked", filesDocked);
  // Docked, the terminal underneath is the pane's neighbour rather than the
  // screen it covers, and it stays on view.
  if (!filesDocked) $(filesOrigin || "screen-list").classList.remove("active");
  $("screen-files").classList.add("active");
  if (filesDocked) { sideClaim("files"); syncFilesExpand(); }
  syncChrome();
  // The rows on screen are whichever folder the session we were away in left
  // there; the cache spares a round trip for a folder already listed.
  const hit = cachedListing(filesPath);
  if (hit) applyListing(hit);
  else loadDir(filesPath);
}

// Where the pane this device is looking at currently sits, or "" when tmux
// cannot say. null instead once an expired token has been answered: there is no
// folder to act on, and rejectToken has already asked for the pairing code.
// Shared by the folder key below and by the docked pane's following, so the two
// ask the same question of the same session.
async function fetchPaneCwd() {
  try {
    const r = await fetch(apiURL("api/session_cwd?session="
        + encodeURIComponent(currentSession || "")
        + "&dev=" + encodeURIComponent(cfg.devname)),
      { cache: "no-store", headers: authHeaders() });
    if (r.status === 401) { rejectToken(); return null; }
    const data = await r.json().catch(() => null);
    if (r.ok && data) return data.cwd || "";
  } catch (e) {}
  return "";
}

// The terminal entry point. The pane's cwd is asked for at tap time — it moves
// with every cd — and $HOME quietly stands in when tmux cannot say.
async function openFilesAtCwd() {
  if (demoMode) { toast("No files in the demo"); return; }
  // Docked, the folder key is a toggle: the pane it opened is the pane it puts
  // away. Full screen there is nothing to toggle — back is how that one leaves.
  if (filesDocked) { closeDockedFiles(); return; }
  const cwd = await fetchPaneCwd();
  if (cwd === null) return;
  const ok = await openExplorer(cwd);
  // The folder that actually resolved, not the string asked for: a cwd tmux
  // could not give lands at $HOME, and that is where the pane is. Docked only —
  // full screen there is no terminal beside it to keep up with.
  if (ok && filesDocked) filesSyncedCwd = filesPath;
}

// Docked, the pane keeps up with the terminal it sits beside: cd in the shell
// and the listing moves with it. Only while the pane is still showing what the
// terminal put there — its own entry folder, and that folder the synced one —
// so browsing anywhere, by crumb, by tap or by a path link out of the terminal,
// hands the pane to the user and the following stops until it is closed and
// opened again. A file view or the open address field is mid-use in the same
// way: the folder under them is not ours to swap out.
function filesFollowsCwd() {
  return filesDocked
      // Reading a branch is the user having taken the pane somewhere; a cd in
      // the terminal cannot pull it back to the working tree from under them.
      && !filesRef
      && filesStack.length <= 1 && filesPath === filesSyncedCwd
      && !$("screen-editor").classList.contains("active")
      && !$("screen-reader").classList.contains("active")
      && !$("files-path-wrap").classList.contains("editing");
}

// One question in flight at a time, and every condition read again on the way
// back: the pane can be closed, browsed into or covered while tmux is being
// asked, and an answer landing then is about a pane that no longer wants it.
let filesCwdBusy = false;
async function followPaneCwd() {
  if (filesCwdBusy || !filesFollowsCwd()) return;
  filesCwdBusy = true;
  let cwd;
  try { cwd = await fetchPaneCwd(); } finally { filesCwdBusy = false; }
  // "" is tmux declining to answer, not a move to $HOME — the pane stays put.
  if (!cwd || cwd === filesSyncedCwd || !filesFollowsCwd()) return;
  filesStack = [];         // a new entry folder, seeded once loadDir resolves it
  if (await loadDir(cwd)) filesSyncedCwd = filesPath;
}

// The wide tick (31-wide-layout.js) is the backstop; these are the moments a cd
// has just happened, so a cd shows in the pane in well under the 15 s the tick
// would take. The listing itself is fast — what was slow was the asking.
//
// Enter is the first moment, seen in send() (09-image-viewer.js), where every
// input source funnels through: any Enter may move the cwd, so the bytes are
// not read for "cd" — the ask is scheduled twice, once for a local prompt and
// once late enough for a shell across ssh to have redrawn.
const CWD_AFTER_ENTER_MS = [400, 1500];
// The second is the terminal's title, which a prompt that sets one rewrites
// per prompt — several writes to a prompt, so this trigger coalesces them.
const CWD_AFTER_TITLE_MS = 300;
let cwdEnterTimers = [];
let cwdTitleTimer = null;

// Everything the tick returns on before it ever reaches followPaneCwd, so a
// keystroke or a title write asks nothing the tick would not have asked. Read
// again when the timer fires: the pane can be closed, or the app backgrounded,
// in the second the ask was waiting out.
function cwdCheckWanted() {
  return !document.hidden && !demoMode && !needsSetup()
      && !$("sheet-scrim").classList.contains("show")
      && filesFollowsCwd();
}

function cwdCheckNow() {
  if (cwdCheckWanted()) followPaneCwd();
}

// One pending set per trigger, re-armed rather than stacked: a run of Enters
// asks about the last one, not about each.
function scheduleCwdAfterEnter() {
  for (const t of cwdEnterTimers) clearTimeout(t);
  cwdEnterTimers = [];
  if (!cwdCheckWanted()) return;
  cwdEnterTimers = CWD_AFTER_ENTER_MS.map((ms) => setTimeout(cwdCheckNow, ms));
}

function scheduleCwdAfterTitle() {
  clearTimeout(cwdTitleTimer);
  cwdTitleTimer = null;
  if (!cwdCheckWanted()) return;
  cwdTitleTimer = setTimeout(cwdCheckNow, CWD_AFTER_TITLE_MS);
}

// The other terminal entry point: a path printed in a pane and tapped
// (activateLink in 08-links.js). A folder is browsed; a file opens the way
// tapping its row would, over the folder that holds it — closeEditor and
// closeReader put #screen-files back rather than returning anywhere of their
// own, so the parent has to be on screen and on the history stack before the
// file's view goes up. Awaiting the listing is what makes that ordering real:
// back from the file then lands on the parent, and back again on the terminal.
async function openPathInExplorer(path) {
  if (demoMode) { toast("No files in the demo"); return; }
  let hit;
  try {
    hit = await resolveFsPath(path);
  } catch (e) {
    dbg("path link failed:", e);
    toast("Couldn't find " + path);
    return;
  }
  if (hit.kind === "dir") { openExplorer(hit.path); return; }
  if (await openExplorer(hit.dir)) openEntry(hit.entry, hit.dir);
}

// Keyed by the server's own normalized path (what data.path comes back as,
// not necessarily what was requested — ~ resolves, PATH_REWRITES can retarget
// a mount). The address field's suggestions read this before fetching, so
// retyping or backspacing within a directory already listed costs nothing.
const filesListCache = new Map();

// Is `path` inside `root`? The one place ~ is expanded client-side, because
// the address bar is the one thing that can type it — everything else already
// holds a path the backend spelled out.
function insideRoot(path, root) {
  if (!root) return false;
  let p = String(path || "");
  if (p.startsWith("~")) p = filesHome + p.slice(1);
  return p === root || p.startsWith(root + "/");
}

// The ref a request for `path` is made at: the branch being read, unless the
// path is outside the repo that branch lives in. A ref has no answer for a
// folder in another tree, so that folder is simply read live — the request is
// not refused, and applyListing below is what then ends the mode.
function refFor(path) {
  return insideRoot(path, filesRefRoot) ? filesRef : "";
}

function refParams(ref) {
  return ref ? ["ref=" + encodeURIComponent(ref),
                "root=" + encodeURIComponent(filesRefRoot)] : [];
}

// The same folder at two refs is two listings, so the ref is part of the key.
function filesCacheKey(path, ref) { return ref ? ref + "\n" + path : path; }

// The cached listing for `path` at whatever ref it would be fetched at. Every
// reader of the cache goes through this rather than through the map, so none
// of them can paint a branch's folder over the disk's or the other way round.
function cachedListing(path) {
  return filesListCache.get(filesCacheKey(path, refFor(path)));
}

// Raw GET against /api/fs/list, cache-populating. Throws on anything but a
// clean 200 so callers keep their own error handling (loadDir's toast-style
// failure, the suggestion dropdown's inline note) rather than sharing one.
async function fsList(path) {
  const ref = refFor(path);
  const parts = path ? ["path=" + encodeURIComponent(path)] : [];
  parts.push(...refParams(ref));
  const q = parts.length ? "?" + parts.join("&") : "";
  const r = await fetch(apiURL("api/fs/list" + q),
                        { cache: "no-store", headers: authHeaders() });
  if (r.status === 401) { rejectToken(); throw new Error("unauthorized"); }
  const data = await r.json().catch(() => null);
  if (!r.ok || !data) {
    const err = new Error((data && data.error) || "HTTP " + r.status);
    err.code = data && data.error;
    throw err;
  }
  filesListCache.set(filesCacheKey(data.path, ref), data);
  return data;
}

// What a path is on the backend, resolved the only way the client can: list
// it. A directory answers with its own listing; anything else that exists is
// found by name in its parent, which is also where its type comes from. Shared
// by the address bar and by a path tapped in the terminal so the two agree on
// what a typed-or-printed string means. The error fsList threw is passed on for
// the caller to phrase — "not_a_directory" reaches the caller only when the
// leaf is not in its parent either, which means nothing is there at all.
async function resolveFsPath(path) {
  let data;
  try {
    data = await fsList(path);
  } catch (e) {
    if (e.code !== "not_a_directory") throw e;
    const [dirPart, name] = splitTyped(path);
    let parent = null;
    try {
      parent = dirPart ? (cachedListing(dirPart) || await fsList(dirPart)) : null;
    } catch (e2) {}
    const entry = parent && (parent.entries || []).find(x => x.name === name);
    if (!entry) throw e;
    return { kind: "entry", entry: entry, dir: parent.path };
  }
  return { kind: "dir", path: data.path, data: data };
}

// /api/fs/read, with the answers both readers of a file — the editor and the
// markdown reader — give: an expired token asks for the pairing code again, a
// file that is not text or is too big offers the download instead, and
// anything else is one toast. Returns the payload, or null once it has already
// said its piece. Shared rather than repeated so the two screens cannot drift
// into telling the same story two ways.
async function fsReadText(path) {
  let r, data;
  try {
    const q = ["path=" + encodeURIComponent(path), ...refParams(refFor(path))];
    r = await fetch(apiURL("api/fs/read?" + q.join("&")),
                    { cache: "no-store", headers: authHeaders() });
    data = await r.json().catch(() => null);
  } catch (e) { toast("Couldn't read the file"); return null; }
  if (r.status === 401) { rejectToken(); return null; }
  if (!r.ok) {
    const err = data && data.error;
    if (err === "binary_file" || err === "too_large") {
      const why = err === "binary_file" ? "isn't a text file"
                                        : "is too big to open here";
      if (confirm(baseName(path) + " " + why + ". Download it instead?")) {
        downloadFile(path, baseName(path));
      }
    } else {
      toast("Couldn't read the file");
    }
    return null;
  }
  return data;
}

// Paints a successful /api/fs/list response — shared by loadDir and the
// address bar's Enter handler, which needs the same rendering but has to
// inspect the failure first (a "not a directory" error there still might be
// a file to open, where loadDir's callers always mean a directory).
function applyListing(data) {
  // A folder landing here is a navigation, and neither dropdown survives one.
  // Not in renderEntries below: the layout menu's own redraw goes straight
  // there, and closing itself again on the way would read as a loop.
  closeFilesMenus();
  filesPath = data.path;
  filesHome = data.home || "";
  // A folder outside the repo the branch lives in is one the branch has no
  // answer for, and fsList has already read it live — so the mode ends where
  // its repo does rather than following the user out of it.
  if (filesRef && !insideRoot(filesPath, filesRefRoot)) {
    filesRef = "";
    filesRefRoot = "";
    syncRefActions();
  }
  // The first listing after openExplorer's push is the entry folder — back
  // here is what closes the explorer, however the path the caller asked for
  // (~, a cwd, a rewritten mount) actually resolved. Only the first: this also
  // runs for same-folder reloads and for the popstate landings below, neither
  // of which is a new level, so the deeper pushes belong to the navigators.
  if (filesStack.length === 0) filesStack.push(data.path);
  renderCrumbs(data.path);
  renderEntries(data.entries || []);
  $("files-error").style.display = "none";
  // After filesPath, which is the folder the question is about.
  syncRefBar();
  syncRepo();
}
function showListError(e) {
  dbg("fs list failed:", e);
  $("files-list").innerHTML = "";
  $("files-empty").style.display = "none";
  $("files-error").style.display = "block";
}

async function loadDir(path) {
  try {
    applyListing(await fsList(path));
    return true;
  } catch (e) {
    showListError(e);
    return false;
  }
}

// loadDir plus a history entry — for an actual navigation (crumb, dir tap,
// address bar) as opposed to a same-folder reload after rename/delete/mkdir/
// upload, which call loadDir(filesPath) directly and must not grow the stack.
// A failed load leaves filesPath unchanged, so nothing is pushed for it —
// the error just replaces the current folder's view in place. Every
// history.pushState of a folder is mirrored by a filesStack push: history
// reserves the entry back has to consume, the stack is what back reads.
// Answers whether the folder is on screen, the way loadDir does: a tapped path
// link opens the file over the folder that holds it, and openPathInExplorer
// reaches this one when the docked pane is already up.
async function navigateDir(path) {
  const ok = await loadDir(path);
  if (ok) {
    // The docked pane reserves nothing: a back press there belongs to the
    // terminal beside it, and its own arrow reads the stack directly.
    if (!filesDocked) {
      history.pushState({ files: true, path: filesPath }, "", location.href);
    }
    filesStack.push(filesPath);
  }
  return ok;
}

function crumbBtn(label, target, current) {
  return el("button", {
    type: "button",
    class: "crumb" + (current ? " current" : ""),
    onclick: () => { if (!current) navigateDir(target); },
  }, label);
}

function renderCrumbs(path) {
  const wrap = $("files-crumbs");
  wrap.innerHTML = "";
  let parts, prefix, rootLabel;
  if (filesHome && (path === filesHome || path.startsWith(filesHome + "/"))) {
    parts = path.slice(filesHome.length).split("/").filter(Boolean);
    prefix = filesHome; rootLabel = "~";
  } else {
    parts = path.split("/").filter(Boolean);
    prefix = ""; rootLabel = "/";
  }
  wrap.appendChild(crumbBtn(rootLabel, prefix || "/", parts.length === 0));
  parts.forEach((seg, i) => {
    prefix += "/" + seg;
    wrap.appendChild(el("span", { class: "crumb-sep" }, "›"));
    wrap.appendChild(crumbBtn(seg, prefix, i === parts.length - 1));
  });
  // The tail is the current folder — that is the segment to keep in view.
  wrap.scrollLeft = wrap.scrollWidth;
}

// ---- address bar ------------------------------------------------------------
// Tapping btn-files-edit-path swaps files-crumbs for files-path-input, an
// editable field pre-filled with the current path. Typing filters the parent
// directory's listing down to entries whose name starts with the segment
// after the last "/" (auto-roll: completing a directory appends "/" and
// starts filtering the next segment, exactly like tab-completion in a shell).
// Escape, the back button/edge-swipe and tapping away all close it without
// navigating; only a suggestion tap or Enter does.

let suggestGen = 0;   // stamped on every fetch so a slow one can't clobber a later reply
let suggestTimer = null;
const SUGGEST_DEBOUNCE = 180;   // ms of no typing before a list request fires

// Splits an in-progress path into the directory to list and the partial name
// typed after its last "/" — e.g. "/home/sai/pro" -> ["/home/sai", "pro"],
// "/home/sai/" -> ["/home/sai", ""], "/" -> ["/", ""], "~/pro" -> ["~", "pro"].
function splitTyped(v) {
  const i = v.lastIndexOf("/");
  if (i < 0) return [v.startsWith("~") ? "~" : "", v];
  const dir = v.slice(0, i) || "/";
  return [dir, v.slice(i + 1)];
}

function openPathEdit() {
  // The field takes the bar over and the stylesheet hides the two dropdowns
  // with it — so close them here, or the scrim would be left dimming the
  // screen with nothing to tap it away.
  closeFilesMenus();
  $("files-path-wrap").classList.add("editing");
  $("files-suggest-scrim").classList.add("show");
  const input = $("files-path-input");
  input.value = filesPath;
  input.focus();
  // setSelectionRange after focus, not before — iOS otherwise ignores it on a
  // field that was not already focused.
  input.setSelectionRange(input.value.length, input.value.length);
  const [dirPart, segPart] = splitTyped(input.value);
  if (dirPart) loadSuggestions(dirPart, segPart);
}

function closePathEdit() {
  $("files-path-wrap").classList.remove("editing");
  $("files-suggest-scrim").classList.remove("show");
  $("files-path-input").blur();
  hideSuggestions();
  clearTimeout(suggestTimer);
  suggestTimer = null;
  suggestGen++;   // orphans any fetch already in flight
}

function hideSuggestions() {
  $("files-suggest").classList.remove("show");
  $("files-suggest").innerHTML = "";
}

// Deliberately unlike fileRow() in the list below: same icon, same colors,
// same row shape made this dropdown read as a clone of the file list rather
// than a floating panel of its own (reported after the first elevation
// pass). No icon at all here — the typed prefix in bold plus a trailing "/"
// on directories (kept in --umber, the app's one accent, rather than a
// second accent color) carry the same information a folder/file glyph would,
// through typography instead of a repeated icon.
function suggestRow(entry, segPart) {
  const name = entry.name;
  const nameEl = el("span", { class: "name" },
    el("strong", {}, name.slice(0, segPart.length)),
    name.slice(segPart.length),
  );
  const kids = [nameEl];
  if (entry.type === "dir") kids.push(el("span", { class: "suggest-slash" }, "/"));
  return el("button", { type: "button", class: "suggest-row" }, ...kids);
}

function renderSuggestions(dirPart, segPart, entries) {
  const box = $("files-suggest");
  box.innerHTML = "";
  const matches = entries.filter(e => e.name.startsWith(segPart));
  if (!matches.length) {
    box.appendChild(el("div", { class: "suggest-note" },
      segPart ? "No matches" : "Empty folder"));
  } else {
    for (const e of matches) {
      const row = suggestRow(e, segPart);
      row.addEventListener("click", () => pickSuggestion(dirPart, e));
      box.appendChild(row);
    }
  }
  box.classList.add("show");
}

// Directory: complete the segment, append "/", keep editing and immediately
// list the next (as yet empty) segment underneath it — the auto-roll. File:
// navigate there like a tap in the list would, and close the field.
function pickSuggestion(dirPart, entry) {
  const input = $("files-path-input");
  const full = joinPath(dirPart, entry.name);
  if (entry.type === "dir") {
    input.value = full + "/";
    input.focus();
    clearTimeout(suggestTimer);
    loadSuggestions(full, "");   // immediate — no debounce for a deliberate tap
    return;
  }
  closePathEdit();
  openEntry(entry, dirPart);
}

// The actual list-and-render step, shared by the debounced typing path below
// and the auto-roll after a directory tap, which needs it to run at once
// rather than wait out the debounce meant for a still-moving finger.
async function loadSuggestions(dirPart, segPart) {
  const gen = ++suggestGen;
  const cached = cachedListing(dirPart);
  try {
    const data = cached || await fsList(dirPart);
    if (gen !== suggestGen) return;   // the field moved on while this was in flight
    renderSuggestions(dirPart, segPart, data.entries || []);
  } catch (e) {
    if (gen !== suggestGen) return;
    const box = $("files-suggest");
    box.innerHTML = "";
    // Same wording the explorer's own error state uses for not_readable;
    // not_found and bad_path are both just "nothing to suggest yet" here —
    // the user may still be mid-directory-name.
    box.appendChild(el("div", { class: "suggest-note" },
      e.code === "not_readable" ? "Can't read this folder" : "No matches"));
    box.classList.add("show");
  }
}

function fetchSuggestions(typed) {
  clearTimeout(suggestTimer);
  const [dirPart, segPart] = splitTyped(typed);
  if (!dirPart) { hideSuggestions(); return; }
  suggestTimer = setTimeout(() => loadSuggestions(dirPart, segPart), SUGGEST_DEBOUNCE);
}

// Enter, or the on-screen keyboard's own go/search key (both fire "submit"
// on a text input's enclosing form — there isn't one here, so this is wired
// to the input's keydown instead). Resolves the typed path exactly: a
// directory is opened like a crumb tap, a file like a list tap; neither
// existing surfaces the explorer's own error state rather than a dead end.
async function submitPathEdit() {
  const typed = $("files-path-input").value.trim();
  if (!typed) { closePathEdit(); return; }
  clearTimeout(suggestTimer);
  suggestGen++;
  let hit;
  try {
    hit = await resolveFsPath(typed);
  } catch (e) {
    // A path that is not there at all leaves the field open to be fixed; a
    // folder that exists and cannot be listed is the explorer's own error.
    if (e.code === "not_a_directory") { toast("Couldn't find that path"); return; }
    closePathEdit();
    showListError(e);
    return;
  }
  closePathEdit();
  if (hit.kind !== "dir") { openEntry(hit.entry, hit.dir); return; }
  applyListing(hit.data);
  if (!filesDocked) {
    history.pushState({ files: true, path: filesPath }, "", location.href);
  }
  filesStack.push(filesPath);
}

$("btn-files-edit-path").addEventListener("click", () => {
  if ($("files-path-wrap").classList.contains("editing")) closePathEdit();
  else openPathEdit();
});
// Same as Escape: close without navigating, whether the tap landed on the
// dimmed file list or on empty space below a short one.
$("files-suggest-scrim").addEventListener("click", () => closePathEdit());
$("files-path-input").addEventListener("input", (e) => fetchSuggestions(e.target.value));
$("files-path-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); submitPathEdit(); }
  else if (e.key === "Escape") { e.preventDefault(); closePathEdit(); }
});
// Losing focus to anything but a suggestion row (which itself closes the
// field on tap) means the user tapped away — close without navigating, same
// as Escape. Deferred, same idea as composeBlurred() in 17-key-bar.js
// (blur fires before the click that caused it), but longer than its tick-0:
// a touch's click can lag its own blur by more than one turn on mobile, and
// firing early here would drop the suggestion tap on the floor.
$("files-path-input").addEventListener("blur", () => {
  setTimeout(() => {
    if (document.activeElement !== $("files-path-input")
        && !$("files-suggest").contains(document.activeElement)) {
      closePathEdit();
    }
  }, 150);
});

// ---- the listing ------------------------------------------------------------
// Two layouts over one set of entries: rows, or the icon tiles a phone's own
// file manager shows. Only the drawing differs — both go through wireRow(), so
// tap, long press and everything they open behave identically either way.

function renderEntries(entries) {
  const list = $("files-list");
  const grid = cfg.filesView === "grid";
  filesEntries = entries;
  list.classList.toggle("files-grid", grid);
  list.innerHTML = "";
  $("files-empty").style.display = entries.length ? "none" : "block";
  for (const e of sortedEntries()) list.appendChild(grid ? fileTile(e) : fileRow(e));
}

// The order the rows are drawn in, over the arrival order filesEntries keeps.
// Directories stay above files in every mode — an ordering is about reading a
// folder's contents, not about mixing its shelves into them. "name" is the
// backend's own order, so it is the array untouched.
function sortedEntries() {
  const mode = cfg.filesSort;
  if (mode === "name") return filesEntries;
  // Ranked high-to-low below, so oldest-first is newest-first on a flipped key.
  const key = mode === "size" ? (e) => e.size
            : mode === "oldest" ? (e) => -e.mtime
            : (e) => e.mtime;
  // sort() is stable, so whatever the key ties — every directory under "size",
  // files written in the same second — falls back to the server's name order.
  return filesEntries.slice().sort((a, b) =>
    (a.type === "dir" ? 0 : 1) - (b.type === "dir" ? 0 : 1) || key(b) - key(a));
}

function fileRow(e) {
  // No mtime is what a listing read at a ref has to say — a commit records
  // when the tree was written, not when each file in it was — so the row drops
  // the age rather than dating everything to the epoch.
  const meta = e.type === "file"
                 ? fmtSize(e.size) + (e.mtime ? " · " + relTime(e.mtime) : "")
             : e.type === "link" ? "link" : "";
  const row = el("div", { class: "file-row" },
    el("span", { class: "file-ic " + e.type },
       svgIcon(e.type === "dir" ? "i-folder" : "i-file")),
    el("div", { class: "file-name" }, e.name),
    el("div", { class: "file-meta" }, meta),
  );
  wireRow(row, e);
  return row;
}

// The tile the grid draws instead: the same icon at tile size with the name
// under it. No meta line — a tile has no room for one, and size and age were
// never why anyone switched to icons.
function fileTile(e) {
  const tile = el("div", { class: "file-tile" },
    el("span", { class: "file-ic " + e.type },
       svgIcon(e.type === "dir" ? "i-folder" : "i-file")),
    el("div", { class: "file-name" }, e.name),
  );
  wireRow(tile, e);
  return tile;
}

// The switch itself, a button and a menu rather than a <select> — see the
// markup for why. Open and closed are one class on the wrap; the scrim rides
// along, so tapping anywhere off the menu closes it and nothing underneath
// takes that tap as a row press.
function showViewMenu(on) {
  $("files-view-wrap").classList.toggle("open", on);
  $("files-view-scrim").classList.toggle("show", on);
  $("btn-files-view").setAttribute("aria-expanded", on ? "true" : "false");
}

// Only ever one open, and the scrim above belongs to whichever it is.
$("btn-files-view").addEventListener("click", () => showRefMenu(false));

// Both groups tick from cfg — so this is also what a fresh load calls to catch
// up with what was remembered.
function syncViewMenu() {
  const v = cfg.filesView, s = cfg.filesSort;
  for (const row of $("files-view-menu").querySelectorAll(".view-row")) {
    const on = row.dataset.view ? row.dataset.view === v : row.dataset.sort === s;
    row.classList.toggle("on", on);
    row.setAttribute("aria-checked", on ? "true" : "false");
  }
}
syncViewMenu();

$("btn-files-view").addEventListener("click", () => {
  showViewMenu(!$("files-view-wrap").classList.contains("open"));
});
$("files-view-scrim").addEventListener("click", closeFilesMenus);
for (const row of $("files-view-menu").querySelectorAll(".view-row")) {
  // Either choice is global, so nothing is re-listed: the entries already on
  // screen are simply drawn the other way, and every later listing — including
  // the ones a rail switch restores from the cache — follows cfg.
  row.addEventListener("click", () => {
    showViewMenu(false);
    if (row.dataset.view) cfg.filesView = row.dataset.view;
    else cfg.filesSort = row.dataset.sort;
    syncViewMenu();
    // An unreadable folder is showing its error, not a listing; leave it alone
    // rather than repainting the entries it replaced.
    if ($("files-error").style.display !== "block") renderEntries(filesEntries);
  });
}

// ---- the branch picker ------------------------------------------------------
// Same button-and-menu as the layout switch above, over one more question: which
// tree the folder on screen is read from. Both dropdowns share #files-view-scrim
// — there is one floating layer over this bar, not two — so opening either
// closes the other, and the moments that mean "close whatever is open" close
// both.

function showRefMenu(on) {
  if (on) showViewMenu(false);
  $("files-ref-wrap").classList.toggle("open", on);
  $("files-view-scrim").classList.toggle("show", on);
  $("btn-files-ref").setAttribute("aria-expanded", on ? "true" : "false");
}

function closeFilesMenus() { showViewMenu(false); showRefMenu(false); }

// Which repo the folder on screen is in, and what it has to offer. Asked per
// repo rather than per folder: inside a root already known the answer cannot
// have changed, and outside every root there is nothing to re-ask until the
// folder itself moves.
async function syncRepo() {
  if (demoMode || !hasCap("git_ref")) { filesRepo = null; syncRefBar(); return; }
  const path = filesPath;
  const fresh = filesRepo && (filesRepo.root ? insideRoot(path, filesRepo.root)
                                             : path === filesRepoAsked);
  if (fresh) { syncRefBar(); return; }
  let d = null;
  try {
    const r = await fetch(apiURL("api/git/branches?path=" + encodeURIComponent(path)),
                          { cache: "no-store", headers: authHeaders() });
    if (r.status === 401) { rejectToken(); return; }
    if (r.ok) d = await r.json();
  } catch (e) {}
  // The explorer can have moved on while the question was in flight, and a late
  // answer would name a repo it has already left.
  if (path !== filesPath) return;
  // A server that could not answer — an older one with no such route — is a
  // folder with no branches, not a folder to keep asking about.
  filesRepo = d && typeof d === "object" ? d : { root: null, current: null, branches: [] };
  filesRepoAsked = path;
  syncRefBar();
}

// Everything the picker shows: whether there is a repo to offer at all, which
// ref is being read, and whether the bar is saying read-only.
function syncRefBar() {
  const repo = filesRepo && filesRepo.root ? filesRepo : null;
  $("files-ref-wrap").hidden = !repo;
  $("files-ref-ro").hidden = !filesRef;
  if (!repo) { showRefMenu(false); return; }
  const live = repo.current || "HEAD";
  const btn = $("btn-files-ref");
  btn.textContent = filesRef || live;
  btn.classList.toggle("on", !!filesRef);
  btn.setAttribute("aria-label", filesRef
    ? "Reading " + filesRef + ", read-only. Change branch"
    : "Branch to browse");
  syncRefMenu(repo, live);
}

// The working tree first and labelled as such, then every other branch. The row
// that ends the mode is the one row always present, so it is the one that never
// moves; the rest are the server's order, locals before remotes.
function syncRefMenu(repo, live) {
  const menu = $("files-ref-menu");
  menu.innerHTML = "";
  for (const name of [live, ...(repo.branches || []).filter(b => b !== live)]) {
    const on = name === (filesRef || live);
    const row = el("button", {
      type: "button", class: "view-row" + (on ? " on" : ""),
      role: "menuitemradio", "aria-checked": on ? "true" : "false",
    },
      el("span", { class: "view-check", "aria-hidden": "true" }, "✓"),
      el("span", { class: "ref-name" }, name),
      name === live ? el("span", { class: "ref-live" }, "working tree") : null,
    );
    row.addEventListener("click", () => pickRef(name === live ? "" : name));
    menu.appendChild(row);
  }
}

// "" is the working tree — the row labelled so, and every path out of the mode.
function pickRef(ref) {
  showRefMenu(false);
  if (ref === filesRef) return;
  filesRef = ref;
  filesRefRoot = ref && filesRepo ? filesRepo.root || "" : "";
  syncRefBar();
  syncRefActions();
  // The same folder, read from somewhere else. The cache is keyed by ref, so
  // this is a fetch rather than a repaint of what is already on screen.
  loadDir(filesPath);
}

// The bar's one control that writes. mkdir, upload and the new-file editor all
// address the working tree, and offering them over a listing that is not the
// working tree would be offering to change a different file than the one shown.
// The action sheet's rows are the same story, refused in openFileActions.
function syncRefActions() {
  $("btn-files-add").style.display = filesRef ? "none" : "";
}

// Leaving the explorer leaves the branch behind, the way it leaves the folder
// behind: this is a place the user went to look at something, not a setting.
function clearRefState() {
  filesRef = "";
  filesRefRoot = "";
  filesRepo = null;
  filesRepoAsked = "";
  syncRefBar();
  syncRefActions();
}

// The explorer's one server-gated control (36-server-version.js calls this the
// way it calls the diff pane's own gate): a server too old to answer
// /api/git/branches must not be left with a bar offering a mode it cannot serve.
function syncRefCap() {
  if (hasCap("git_ref")) { if (filesPath) syncRepo(); return; }
  filesRepo = null;
  filesRepoAsked = "";
  if (filesRef) pickRef("");
  else syncRefBar();
}

$("btn-files-ref").addEventListener("click", () => {
  showRefMenu(!$("files-ref-wrap").classList.contains("open"));
});

// Tap opens; a long press (or a desktop right-click) opens the action sheet.
function wireRow(row, entry) {
  let timer = null, sx = 0, sy = 0;
  const cancel = () => { clearTimeout(timer); timer = null; };
  row.addEventListener("touchstart", (ev) => {
    cancel();
    if (ev.touches.length !== 1) return;
    sx = ev.touches[0].clientX; sy = ev.touches[0].clientY;
    timer = setTimeout(() => {
      timer = null;
      filesPressedAt = Date.now();
      openFileActions(entry);
    }, 500);
  }, { passive: true });
  row.addEventListener("touchmove", (ev) => {
    if (timer && Math.hypot(ev.touches[0].clientX - sx,
                            ev.touches[0].clientY - sy) > 10) cancel();
  }, { passive: true });
  row.addEventListener("touchend", cancel, { passive: true });
  row.addEventListener("touchcancel", cancel, { passive: true });
  row.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    filesPressedAt = Date.now();
    openFileActions(entry);
  });
  row.addEventListener("click", () => {
    if (Date.now() - filesPressedAt < 500) return;  // the long-press's own click
    openEntry(entry);
  });
}

function openEntry(e, dir=filesPath) {
  const full = joinPath(dir, e.name);
  if (e.type === "dir") { navigateDir(full); return; }
  // A broken link or a special file: nothing to enter or edit, so the sheet
  // (whose Download is the only sensible offer) is the whole answer.
  if (e.type === "link") { openFileActions(e); return; }
  // The viewer and the sandboxed page both fetch the file straight off the
  // disk, and neither route takes a ref — so at a ref an image says so, and a
  // page opens as the source it is, in the editor that already opens read-only.
  if (FILES_MEDIA_RE.test(e.name)) {
    if (filesRef) toast("Can't preview images from " + filesRef);
    else showImage(full);
    return;
  }
  if (FILES_MD_RE.test(e.name)) { openReader(full); return; }
  if (FILES_HTML_RE.test(e.name) && !filesRef) { openRendered(full); return; }
  openEditor(full);
}

// A page renders in a tab of the browser's, not in ours: it is served
// sandboxed into an origin of its own (see app.py's api_fs_site), which is
// what keeps a report's scripts away from the token this page holds — and an
// <iframe> here would put it back inside our document instead.
//
// Same shape as downloadViaLink: the token header buys a short-lived signed
// link, and the browser fetches the page and everything it references with
// that. The mint has to finish before there is a URL to open, so on iOS the
// click is no longer synchronous with the tap that started it and Safari can
// hold the tab back — the anchor is still the best of the options, window.open
// there returns null outright.
async function openRendered(path) {
  let url;
  try {
    const r = await fetch(apiURL("api/fs/render_link?path=" + encodeURIComponent(path)),
                          { cache: "no-store", headers: authHeaders() });
    if (r.status === 401) { rejectToken(); return; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    url = apiURL((await r.json()).url);
  } catch (e) {
    toast("Couldn't open " + baseName(path));
    return;
  }
  const a = document.createElement("a");
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function openFileActions(entry) {
  // Every row in this sheet either writes the working tree or downloads it off
  // the disk, and at a ref the listing on screen is neither.
  if (filesRef) { toast("Read-only on " + filesRef); return; }
  filesSelected = entry;
  $("file-actions-title").textContent = entry.name;
  $("btn-file-download").style.display = entry.type === "dir" ? "none" : "";
  // Only where the tap itself no longer reaches the editor. Every other text
  // file already opens there, so an Edit row would say nothing.
  $("btn-file-edit").style.display =
    entry.type === "file" && FILES_HTML_RE.test(entry.name) ? "" : "none";
  showSheet(true, "sheet-file-actions");
}

// ---- the action sheet ------------------------------------------------------

async function fsPost(route, body) {
  const r = await fetch(apiURL(route), {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  if (r.status === 401) { rejectToken(); return null; }
  const data = await r.json().catch(() => null);
  return { ok: r.ok, status: r.status, data };
}

$("btn-file-edit").addEventListener("click", () => {
  const e = filesSelected;
  if (!e) return;
  showSheet(false);
  openEditor(joinPath(filesPath, e.name));
});

$("btn-file-rename").addEventListener("click", async () => {
  const e = filesSelected;
  if (!e) return;
  showSheet(false);
  const next = prompt("Rename " + e.name, e.name);
  if (next === null || !next.trim() || next.trim() === e.name) return;
  if (next.includes("/")) { toast("Names can't contain /"); return; }
  const res = await fsPost("api/fs/rename", {
    src: joinPath(filesPath, e.name),
    dst: joinPath(filesPath, next.trim()),
  });
  if (!res) return;
  if (!res.ok) {
    toast(res.status === 409 ? "Something with that name already exists"
                             : "Couldn't rename");
    return;
  }
  loadDir(filesPath);
});

$("btn-file-download").addEventListener("click", () => {
  const e = filesSelected;
  if (!e) return;
  showSheet(false);
  downloadFile(joinPath(filesPath, e.name), e.name, e.size);
});

$("btn-file-delete").addEventListener("click", async () => {
  const e = filesSelected;
  if (!e) return;
  showSheet(false);
  const what = e.type === "dir" ? "folder " + e.name : e.name;
  if (!confirm("Delete " + what + "?")) return;
  const res = await fsPost("api/fs/delete", { path: joinPath(filesPath, e.name) });
  if (!res) return;
  if (!res.ok) {
    // The backend never deletes a non-empty folder — the shell is a tap away
    // for anyone who really means rm -r.
    toast(res.data && res.data.error === "not_empty"
      ? "That folder isn't empty" : "Couldn't delete");
    return;
  }
  toast("Deleted");
  loadDir(filesPath);
});

// Above this a blob held whole in the page's memory is what makes iOS kill the
// PWA, so anything bigger goes the long way round.
const DOWNLOAD_BLOB_MAX = 30 * 1024 * 1024;

// Two ways down, because neither is good at the other's size. Below the cap the
// page fetches the bytes itself and hands the save sheet a blob — one tap, no
// browser chrome, which is what nearly every download here is. Above it, or
// when the size is unknown, the browser has to do the downloading instead.
function downloadFile(path, name, size) {
  if (typeof size === "number" && size <= DOWNLOAD_BLOB_MAX) {
    return downloadAsBlob(path, name);
  }
  return downloadViaLink(path, name);
}

// Through fetch rather than a plain link: /api/* only answers to the token
// header, which a navigation cannot carry.
async function downloadAsBlob(path, name) {
  toast("Downloading…");
  try {
    const r = await fetch(apiURL("api/fs/download?path=" + encodeURIComponent(path)),
                          { headers: authHeaders() });
    if (r.status === 401) { rejectToken(); return; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = name || baseName(path);
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Not straight away: Safari needs the URL alive until its save sheet is done.
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (e) {
    toast("Couldn't download");
  }
}

// The browser does the downloading, not us. So the token header only buys a
// short-lived signed link (a navigation cannot carry the header, hence the
// signature) and the browser streams the file itself. No "Downloading…" toast:
// the browser puts its own download UI on screen, and ours would only sit on
// top of it and outlive it. Silence unless something goes wrong.
async function downloadViaLink(path, name) {
  let url;
  try {
    const r = await fetch(apiURL("api/fs/download_link?path=" + encodeURIComponent(path)),
                          { cache: "no-store", headers: authHeaders() });
    if (r.status === 401) { rejectToken(); return; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    url = apiURL((await r.json()).url);
  } catch (e) {
    toast("Couldn't download");
    return;
  }
  // A synchronous anchor click, for openUrl()'s reason: window.open lands on a
  // blank tab in Safari. No target — the browser keeps the page and saves the
  // file, here or in the tab it hands the cross-origin backend.
  const a = document.createElement("a");
  a.href = url;
  // Honoured same-origin only; against the public deploy's cross-origin backend
  // it is ignored and the server's Content-Disposition is what saves the file.
  a.download = name || baseName(path);
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ---- the + sheet -----------------------------------------------------------

$("btn-files-add").addEventListener("click", () => showSheet(true, "sheet-files-add"));

$("btn-files-newfile").addEventListener("click", () => {
  showSheet(false);
  const name = prompt("New file name");
  if (name === null || !name.trim()) return;
  if (name.includes("/")) { toast("Names can't contain /"); return; }
  // Nothing is written yet: the editor opens empty and the first Save (hash "")
  // is what creates the file, so an abandoned name leaves no husk behind.
  openEditor(joinPath(filesPath, name.trim()), { create: true });
});

$("btn-files-newfolder").addEventListener("click", async () => {
  showSheet(false);
  const name = prompt("New folder name");
  if (name === null || !name.trim()) return;
  if (name.includes("/")) { toast("Names can't contain /"); return; }
  const res = await fsPost("api/fs/mkdir", { path: joinPath(filesPath, name.trim()) });
  if (!res) return;
  if (!res.ok) {
    toast(res.status === 409 ? "Something with that name already exists"
                             : "Couldn't create the folder");
    return;
  }
  loadDir(filesPath);
});

$("btn-files-upload").addEventListener("click", () => {
  showSheet(false);
  $("files-upload-input").click();
});

$("files-upload-input").addEventListener("change", (ev) => {
  // The FileList is live and empties with the input, so copy it out before the
  // reset that lets the same pick fire change again.
  const files = Array.from(ev.target.files || []);
  ev.target.value = "";
  if (files.length) uploadFiles(files);
});

// One file at a time, never in parallel: a 409 asks its own Replace? question,
// and a pile of confirms racing each other is unanswerable.
async function uploadFiles(files) {
  let done = 0, failed = 0;
  for (const f of files) {
    const err = await uploadFile(f, false);
    if (err === null) { done++; continue; }
    // "" is a declined replace or an expired token — both already said their
    // piece, or deliberately say nothing.
    if (err) { failed++; if (files.length === 1) toast(err); }
  }
  if (files.length === 1) { if (done) toast("Uploaded " + files[0].name); }
  else if (done) toast("Uploaded " + done + (done === 1 ? " file" : " files")
                       + (failed ? ", " + failed + " failed" : ""));
  else if (failed) toast("Couldn't upload " + failed + " files");
  if (done) loadDir(filesPath);
}

// Returns null when the file landed, "" when nothing more should be said, and
// otherwise the message for whatever went wrong — the batch decides whether
// that surfaces per file or as one summary.
async function uploadFile(f, overwrite) {
  const target = joinPath(filesPath, f.name);
  try {
    const q = "?path=" + encodeURIComponent(target) + (overwrite ? "&overwrite=1" : "");
    // The body is the file itself — the raw-body shape /api/fs/upload shares
    // with /api/transcribe.
    const r = await fetch(apiURL("api/fs/upload" + q),
                          { method: "POST", headers: authHeaders(), body: f });
    if (r.status === 401) { rejectToken(); return ""; }
    if (r.status === 409 && !overwrite) {
      if (confirm(f.name + " already exists here. Replace it?")) return uploadFile(f, true);
      return "";
    }
    if (r.status === 413) return "Too large to upload (50 MB max)";
    if (!r.ok) throw new Error("HTTP " + r.status);
    return null;
  } catch (e) {
    return "Couldn't upload";
  }
}

// ---- navigation chrome -----------------------------------------------------

$("btn-files").addEventListener("click", () => openExplorer(""));
$("btn-files-back").addEventListener("click", filesBack);

// Straight back to the terminal, however deep the browsing went. Not
// closeExplorer() directly: the explorer's history entries would stay on the
// stack behind the terminal, and the terminal's own back would then spend them
// one by one going nowhere. Unwinding through history instead leaves the stack
// exactly where pressing back at every level would have.
function jumpToTerminal() {
  // The field's own back is spent on closing it; take it out of the way first
  // so the pops below all count as folders.
  if ($("files-path-wrap").classList.contains("editing")) closePathEdit();
  // Every entry the explorer owns: openExplorer's push plus one per level
  // navigated into, which is filesStack.length — one at the entry folder, and
  // still one if the entry folder never resolved and the stack stayed empty.
  filesClosing = true;
  history.go(-(filesStack.length || 1));
}
$("btn-files-term").addEventListener("click", jumpToTerminal);

// A hardware keyboard's Escape does what the button does. No media query and no
// pointer-type gate: a phone keyboard never sends Escape at all, while an iPad
// with one is exactly who this is for. Everything else Escape can mean here
// wins first — the address field's own keydown closes just the field, a sheet
// or the media viewer is the top thing to dismiss, and the editor and the
// reader take #screen-files' active class with them while they are up, so the
// screen check below is what keeps their Escape theirs.
// On capture, and bailing out without touching the event: bubbling would put
// this after the field's own handler, which has closed the field by then, and
// one press would both close the field and jump to the terminal.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("screen-files").classList.contains("active")) return;
  // Docked, the terminal beside the pane is live and Escape is one of its
  // keys — so only a press aimed inside the pane is the pane's to answer.
  if (filesDocked && !$("screen-files").contains(e.target)) return;
  // Ahead of the origin check: an open dropdown is the top thing to dismiss,
  // and it is there to dismiss whether or not a terminal is behind.
  if ($("files-view-wrap").classList.contains("open")
      || $("files-ref-wrap").classList.contains("open")) {
    e.preventDefault();
    closeFilesMenus();
    return;
  }
  if (filesOrigin !== "screen-term") return;          // nothing to jump back to
  if ($("files-path-wrap").classList.contains("editing")) return;
  if ($("sheet-scrim").classList.contains("show")) return;
  if ($("viewer").classList.contains("show")) return;
  e.preventDefault();
  if (filesDocked) closeDockedFiles();
  else jumpToTerminal();
}, true);

// Explorer and editor sit on the history stack the way the terminal does, so
// back unwinds them one screen at a time. The terminal's own popstate handler
// ignores these pops — #screen-term is not active while either screen is up.
window.addEventListener("popstate", () => {
  if ($("screen-editor").classList.contains("active")) { editorPopped(); return; }
  if ($("screen-reader").classList.contains("active")) { closeReader(); return; }
  // The docked pane pushed nothing, so no pop is ever its own: this one is the
  // terminal's, and closeTerminal() above has already spent it.
  if (filesDocked) return;
  if (!$("screen-files").classList.contains("active")) return;
  // A go(-n) past several entries arrives as one popstate, not n of them, so
  // the per-level unwind below would land on the folder one up while history
  // already sits behind the whole view. The flag is the button saying this pop
  // is the explorer leaving, whatever depth it left from — and closing here
  // rather than before the go() keeps #screen-term inactive until the pop is
  // spent, so the terminal's own popstate handler stays out of it.
  if (filesClosing) { filesClosing = false; closeExplorer(); return; }
  // An open address field takes the back first — closeExplorer turns that one
  // into closing just the field, and re-pushes the entry the pop consumed.
  if ($("files-path-wrap").classList.contains("editing")) { closeExplorer(); return; }
  // The popped entry's own state is deliberately not consulted: iOS wipes the
  // stored state objects out from under us (see filesStack above), and a
  // handler that trusted them read every later pop as the pop past the entry
  // and closed the explorer from any depth. The stack is ours, so pop it and
  // land on the folder underneath — in place, with no further push: the pop
  // already put history where that folder belongs. Anything below the entry
  // folder — including an entry that never resolved — closes.
  if (filesStack.length > 1) {
    filesStack.pop();
    const path = filesStack[filesStack.length - 1];
    const hit = cachedListing(path);
    if (hit) applyListing(hit);
    else loadDir(path);
    return;
  }
  closeExplorer();
});

// The terminal's left-edge back gesture (20-edge-swipe.js), re-armed for the
// screens this feature adds. A copy rather than a share: the terminal's
// binding also feeds the global edgeSwipe flag its scroll code reads, and
// entangling that is a worse trade than repeating two dozen lines.
// The preventDefault in touchmove is load-bearing, not cosmetic: iOS 18+ home
// screen web apps run their own system edge-swipe-back that pops history, so a
// passive listener leaves the swipe firing both that and onBack() — one gesture,
// two backs. Cancelling the move once the drag is armed and horizontal-rightward
// suppresses the system gesture and leaves onBack() the only navigation.
function attachEdgeSwipe(scr, onBack) {
  let sx = 0, sy = 0, armed = false, committed = false;
  scr.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) { armed = false; return; }
    const t = e.touches[0];
    armed = t.clientX <= EDGE_ZONE;
    sx = t.clientX; sy = t.clientY;
    committed = false;
  }, { passive: true, capture: true });
  scr.addEventListener("touchmove", (e) => {
    if (!armed || e.touches.length !== 1) return;
    const dx = e.touches[0].clientX - sx, dy = e.touches[0].clientY - sy;
    if (dx < 0 || Math.abs(dy) > Math.abs(dx)) {
      if (Math.abs(dy) > 12) armed = false;
      return;
    }
    e.preventDefault();
    committed = dx >= EDGE_TRIGGER;
  }, { passive: false, capture: true });
  const finish = () => {
    const go = armed && committed;
    armed = false; committed = false;
    if (go) onBack();
  };
  scr.addEventListener("touchend", finish, { passive: true });
  scr.addEventListener("touchcancel", finish, { passive: true });
}
attachEdgeSwipe($("screen-files"), filesBack);
attachEdgeSwipe($("screen-editor"), () => history.back());
attachEdgeSwipe($("screen-reader"), () => history.back());

// Pull-down to refresh — the session list's own pattern.
(function() {
  let startY = null;
  const scr = $("screen-files");
  scr.addEventListener("touchstart", (e) => {
    startY = window.scrollY <= 0 ? e.touches[0].clientY : null;
  }, { passive: true });
  scr.addEventListener("touchend", (e) => {
    if (startY !== null && e.changedTouches[0].clientY - startY > 70 && filesPath) {
      loadDir(filesPath);
    }
    startY = null;
  }, { passive: true });
})();
