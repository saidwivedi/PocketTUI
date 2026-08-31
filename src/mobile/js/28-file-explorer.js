// ============================================================
// File explorer
// ============================================================
// A phone-first view over /api/fs/*: reached from the session list's folder
// button (opens at $HOME) and from the terminal key bar's folder key (opens at
// the pane's cwd). One history entry per visit, plus one more per folder
// navigated into from there — crumbs, dir taps and the address bar all push,
// so back walks folder history one step at a time; only back at the entry
// folder closes the whole view.

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
// Set while btn-files-term's multi-entry history.go is in flight, so the one
// popstate it lands as closes the view instead of climbing one folder.
let filesClosing = false;
// A finished long-press ends in a click the browser synthesizes on the same
// row; stamping the moment lets that click be ignored (the viewer's
// viewerOpenedAt idea).
let filesPressedAt = 0;

// Mirrors app.py's MEDIA_TYPES allowlist: these open in the existing viewer
// over /api/file rather than in the editor.
const FILES_MEDIA_RE = /\.(?:png|jpe?g|gif|webp|svg|bmp|mp4|webm|mov)$/i;

// Markdown opens rendered, in the reader (33-md-reader.js), rather than in the
// editor — Edit there is one tap away.
const FILES_MD_RE = /\.(?:md|markdown)$/i;

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
  loadDir(path);
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
  // screen: either way the layout menu is going, scrim and all.
  showViewMenu(false);
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
  $(back).classList.add("active");
  syncChrome();
  // The terminal kept its socket while we were away; it only needs its size
  // re-checked, not a reconnect.
  if (back === "screen-term") refit(0);
}

// ---- putting the whole view away (see fileViews in 09-image-viewer.js) ------
// A rail switch stashes the reader or the editor for the session it is leaving,
// and the browsing underneath goes with it: coming back to a file view that
// landed on the session list would say the switch had lost the folder.

// History entries the explorer owns: openExplorer's push plus one per level
// navigated into — filesStack.length, and still one if the entry folder never
// resolved and the stack stayed empty. The same count jumpToTerminal spends.
function filesEntryCount() { return filesStack.length || 1; }

function filesStash() {
  return { stack: filesStack.slice(), path: filesPath, origin: filesOrigin };
}

// Drops the view without putting anything back: the caller is replacing every
// screen at once, so closeExplorer's return-to-origin (and its refit, which
// would fit a terminal that is about to change session) is not what it wants.
function filesTeardown() {
  $("screen-files").classList.remove("active");
  filesOrigin = null;
  filesStack = [];
}

// The mirror of openExplorer's screen work, minus the history push — the
// caller re-pushes every entry the stashed view owned, in one place.
function filesRestore(s) {
  filesStack = s.stack.slice();
  filesPath = s.path;
  filesOrigin = s.origin;
  $("btn-files-term").style.display = filesOrigin === "screen-term" ? "" : "none";
  $(filesOrigin || "screen-list").classList.remove("active");
  $("screen-files").classList.add("active");
  syncChrome();
  // The rows on screen are whichever folder the session we were away in left
  // there; the cache spares a round trip for a folder already listed.
  if (filesListCache.has(filesPath)) applyListing(filesListCache.get(filesPath));
  else loadDir(filesPath);
}

// The terminal entry point. The pane's cwd is asked for at tap time — it moves
// with every cd — and $HOME quietly stands in when tmux cannot say.
async function openFilesAtCwd() {
  if (demoMode) { toast("No files in the demo"); return; }
  let cwd = "";
  try {
    const r = await fetch(apiURL("api/session_cwd?session="
        + encodeURIComponent(currentSession || "")
        + "&dev=" + encodeURIComponent(cfg.devname)),
      { cache: "no-store", headers: authHeaders() });
    if (r.status === 401) { rejectToken(); return; }
    const data = await r.json().catch(() => null);
    if (r.ok && data) cwd = data.cwd || "";
  } catch (e) {}
  openExplorer(cwd);
}

// Keyed by the server's own normalized path (what data.path comes back as,
// not necessarily what was requested — ~ resolves, PATH_REWRITES can retarget
// a mount). The address field's suggestions read this before fetching, so
// retyping or backspacing within a directory already listed costs nothing.
const filesListCache = new Map();

// Raw GET against /api/fs/list, cache-populating. Throws on anything but a
// clean 200 so callers keep their own error handling (loadDir's toast-style
// failure, the suggestion dropdown's inline note) rather than sharing one.
async function fsList(path) {
  const q = path ? "?path=" + encodeURIComponent(path) : "";
  const r = await fetch(apiURL("api/fs/list" + q),
                        { cache: "no-store", headers: authHeaders() });
  if (r.status === 401) { rejectToken(); throw new Error("unauthorized"); }
  const data = await r.json().catch(() => null);
  if (!r.ok || !data) {
    const err = new Error((data && data.error) || "HTTP " + r.status);
    err.code = data && data.error;
    throw err;
  }
  filesListCache.set(data.path, data);
  return data;
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
    r = await fetch(apiURL("api/fs/read?path=" + encodeURIComponent(path)),
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
  // A folder landing here is a navigation, and the layout menu does not survive
  // one. Not in renderEntries below: the menu's own redraw goes straight there,
  // and closing itself again on the way would read as a loop.
  showViewMenu(false);
  filesPath = data.path;
  filesHome = data.home || "";
  // The first listing after openExplorer's push is the entry folder — back
  // here is what closes the explorer, however the path the caller asked for
  // (~, a cwd, a rewritten mount) actually resolved. Only the first: this also
  // runs for same-folder reloads and for the popstate landings below, neither
  // of which is a new level, so the deeper pushes belong to the navigators.
  if (filesStack.length === 0) filesStack.push(data.path);
  renderCrumbs(data.path);
  renderEntries(data.entries || []);
  $("files-error").style.display = "none";
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
async function navigateDir(path) {
  if (await loadDir(path)) {
    history.pushState({ files: true, path: filesPath }, "", location.href);
    filesStack.push(filesPath);
  }
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
  // The field takes the bar over and the stylesheet hides the layout button
  // with it — so close the menu here, or its scrim would be left dimming the
  // screen with nothing to tap it away.
  showViewMenu(false);
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
  const cached = filesListCache.get(dirPart);
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
  try {
    const data = await fsList(typed);
    closePathEdit();
    applyListing(data);
    history.pushState({ files: true, path: filesPath }, "", location.href);
    filesStack.push(filesPath);
    return;
  } catch (e) {
    if (e.code !== "not_a_directory") {
      closePathEdit();
      showListError(e);
      return;
    }
  }
  // Not a directory: it may still be a file. Split off the parent and confirm
  // the leaf is really there rather than trusting the typed name outright.
  const [dirPart, name] = splitTyped(typed);
  try {
    const data = dirPart ? (filesListCache.get(dirPart) || await fsList(dirPart)) : null;
    const entry = data && (data.entries || []).find(e => e.name === name);
    if (entry) { closePathEdit(); openEntry(entry, dirPart); return; }
  } catch (e2) {}
  toast("Couldn't find that path");
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
  for (const e of entries) list.appendChild(grid ? fileTile(e) : fileRow(e));
}

function fileRow(e) {
  const meta = e.type === "file" ? fmtSize(e.size) + " · " + relTime(e.mtime)
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

// The button wears the current view and the menu ticks it, both from cfg — so
// this is also what a fresh load calls to catch up with what was remembered.
function syncViewMenu() {
  const v = cfg.filesView;
  $("files-view-label").textContent = v === "grid" ? "Grid" : "List";
  for (const row of $("files-view-menu").querySelectorAll(".view-row")) {
    const on = row.dataset.view === v;
    row.classList.toggle("on", on);
    row.setAttribute("aria-checked", on ? "true" : "false");
  }
}
syncViewMenu();

$("btn-files-view").addEventListener("click", () => {
  showViewMenu(!$("files-view-wrap").classList.contains("open"));
});
$("files-view-scrim").addEventListener("click", () => showViewMenu(false));
for (const row of $("files-view-menu").querySelectorAll(".view-row")) {
  // The choice is global, so nothing is re-listed: the entries already on
  // screen are simply drawn the other way, and every later listing — including
  // the ones a rail switch restores from the cache — follows cfg.
  row.addEventListener("click", () => {
    showViewMenu(false);
    cfg.filesView = row.dataset.view;
    syncViewMenu();
    // An unreadable folder is showing its error, not a listing; leave it alone
    // rather than repainting the entries it replaced.
    if ($("files-error").style.display !== "block") renderEntries(filesEntries);
  });
}

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
  if (FILES_MEDIA_RE.test(e.name)) { showImage(full); return; }
  if (FILES_MD_RE.test(e.name)) { openReader(full); return; }
  openEditor(full);
}

function openFileActions(entry) {
  filesSelected = entry;
  $("file-actions-title").textContent = entry.name;
  $("btn-file-download").style.display = entry.type === "dir" ? "none" : "";
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
$("btn-files-back").addEventListener("click", () => history.back());

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
  // Ahead of the origin check: an open layout menu is the top thing to
  // dismiss, and it is there to dismiss whether or not a terminal is behind.
  if ($("files-view-wrap").classList.contains("open")) {
    e.preventDefault();
    showViewMenu(false);
    return;
  }
  if (filesOrigin !== "screen-term") return;          // nothing to jump back to
  if ($("files-path-wrap").classList.contains("editing")) return;
  if ($("sheet-scrim").classList.contains("show")) return;
  if ($("viewer").classList.contains("show")) return;
  e.preventDefault();
  jumpToTerminal();
}, true);

// Explorer and editor sit on the history stack the way the terminal does, so
// back unwinds them one screen at a time. The terminal's own popstate handler
// ignores these pops — #screen-term is not active while either screen is up.
window.addEventListener("popstate", () => {
  if ($("screen-editor").classList.contains("active")) { editorPopped(); return; }
  if ($("screen-reader").classList.contains("active")) { closeReader(); return; }
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
    if (filesListCache.has(path)) applyListing(filesListCache.get(path));
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
attachEdgeSwipe($("screen-files"), () => history.back());
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
