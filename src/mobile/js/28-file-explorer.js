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
//
// Two of these can be open at once, side by side in the column (26-side-pane.js)
// — a second place to look while the first stays where it is. So the listing
// below is a factory and each pane is one call of it, and what is shared is what
// is genuinely shared: the tables and helpers here that only read their
// arguments, the cache of folders already listed, the sheets, and the three file
// views, which are one each however many listings there are and belong to
// whichever listing opened them.

// ------------------------------------------------------------
// Shared, whichever pane is asking
// ------------------------------------------------------------

// Mirrors app.py's MEDIA_TYPES allowlist: these open in the existing viewer
// over /api/file rather than in the editor.
const FILES_MEDIA_RE = /\.(?:png|jpe?g|gif|webp|svg|bmp|mp4|webm|mov)$/i;

// A PDF is servable media too (it is in the same allowlist), but it opens its
// own way rather than in the viewer's image path — see openPdf below.
const FILES_PDF_RE = /\.pdf$/i;

// Markdown opens rendered, in the reader (33-md-reader.js), rather than in the
// editor — Edit there is one tap away.
const FILES_MD_RE = /\.(?:md|markdown)$/i;

// What kind of thing a name says it is, and so which icon the listing draws
// for it. One table, read by rows and tiles alike — a .py is the same file in
// either layout. Everything unlisted is the plain page i-file, which is what
// the explorer drew for every file before this.
const FILE_KIND_EXT = {
  image:   "png jpg jpeg gif webp bmp svg heic avif",
  video:   "mp4 webm mov mkv m4v",
  audio:   "mp3 wav m4a ogg flac",
  code:    "py js ts jsx tsx mjs cjs sh bash zsh c h cpp hpp rs go java kt swift"
           + " rb php lua sql html css scss json yaml yml toml xml ipynb",
  text:    "md txt rst log csv tsv",
  pdf:     "pdf",
  archive: "zip tar gz tgz bz2 xz 7z rar",
};
const FILE_KIND_BY_EXT = (() => {
  const m = new Map();
  for (const [kind, exts] of Object.entries(FILE_KIND_EXT))
    for (const ext of exts.split(" ")) m.set(ext, kind);
  return m;
})();
// The names a project gives meaning to without an extension. All of them are
// something to read, so all of them are text.
const FILE_KIND_BY_NAME = new Set(["readme", "license", "licence", "changelog",
                                   "makefile", "dockerfile"]);

// Case-insensitive, and a leading dot is not an extension: .bashrc is a name,
// not a file of type "bashrc".
function fileKind(name) {
  const n = name.toLowerCase();
  const dot = n.lastIndexOf(".");
  if (dot > 0) return FILE_KIND_BY_EXT.get(n.slice(dot + 1)) || "file";
  return FILE_KIND_BY_NAME.has(n) ? "text" : "file";
}

function kindIcon(kind) {
  return kind === "file" ? "i-file" : "i-" + kind;
}

// The tile's extension chip: what the typed icon cannot say on its own, since
// a .py and a .rs are one icon. Never clipped, so what it prints is always a
// real extension: four characters is what fits under 34px, and a longer
// ending (.ipynb, .service) goes unprinted rather than half-printed. A name
// that starts with a dot gets none either — the tail of .dev.vars or of
// .last_voice.orig is not a type, the way the whole of .bashrc is not one.
function fileBadge(name) {
  if (name.startsWith(".")) return "";
  const dot = name.lastIndexOf(".");
  if (dot <= 0) return "";
  const ext = name.slice(dot + 1);
  if (!/^[a-z0-9]{1,4}$/i.test(ext)) return "";
  return ext.toUpperCase();
}

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

// Keyed by the server's own normalized path (what data.path comes back as,
// not necessarily what was requested — ~ resolves, PATH_REWRITES can retarget
// a mount). The address field's suggestions read this before fetching, so
// retyping or backspacing within a directory already listed costs nothing.
const filesListCache = new Map();

// Above this a blob held whole in the page's memory is what makes iOS kill the
// PWA, so anything bigger goes the long way round.
const DOWNLOAD_BLOB_MAX = 30 * 1024 * 1024;

// Two ways down, because neither is good at the other's size. Below the cap the
// page fetches the bytes itself and hands the save sheet a blob — one tap, no
// browser chrome, which is what nearly every download here is. Above it, or
// when the size is unknown, the browser has to do the downloading instead.
function downloadFile(path, name, size) {
  if (typeof size === "number" && size <= DOWNLOAD_BLOB_MAX) {
    return downloadAsBlob(path, name, size);
  }
  return downloadViaLink(path, name);
}

// Through fetch rather than a plain link: /api/* only answers to the token
// header, which a navigation cannot carry. The bytes are read off the stream
// rather than taken whole from .blob(), because on a slow link the line this
// counts into is the only thing on screen for the length of the transfer.
async function downloadAsBlob(path, name, size) {
  if (demoApiOn()) { toast("Not in the demo"); return; }
  const label = name || baseName(path);
  holdToast("Downloading " + label + "…");
  try {
    const r = await fetch(apiURL("api/fs/download?path=" + encodeURIComponent(path)),
                          { headers: authHeaders() });
    // rejectToken() says its own piece, so nothing is toasted over it — but the
    // held line has no clock of its own and has to come down by hand.
    if (r.status === 401) { hideToast(); rejectToken(); return; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    const url = URL.createObjectURL(await readWithProgress(r, label, size));
    const a = document.createElement("a");
    a.href = url;
    a.download = name || baseName(path);
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Not straight away: Safari needs the URL alive until its save sheet is done.
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    toast("Downloaded " + label);
  } catch (e) {
    toast("Couldn't download");
  }
}

// Repainting the line per chunk would spend a fast transfer in layout, so it is
// rewritten at most this often.
const DOWNLOAD_TICK_MS = 100;

// The body chunk by chunk, with the held line saying how far it has got.
async function readWithProgress(r, label, size) {
  // The listing's size where the caller had one, the header's where it did not,
  // and no total at all when neither says — a response can arrive unmeasured.
  const total = typeof size === "number" && size > 0
    ? size : Number(r.headers.get("Content-Length")) || 0;
  // No readable stream (an old browser, a synthesized response) still
  // downloads; it just cannot be counted.
  if (!r.body || !r.body.getReader) return r.blob();
  const reader = r.body.getReader();
  const chunks = [];
  let got = 0, painted = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    got += value.length;
    if (Date.now() - painted < DOWNLOAD_TICK_MS) continue;
    painted = Date.now();
    // A percentage where the size is known, and how much has landed where it is
    // not. 100% is kept back for the save itself — the last chunk read is not
    // the same moment as a file the browser has taken.
    holdToast("Downloading " + label + "… " + (total
      ? Math.min(99, Math.floor(got * 100 / total)) + "%"
      : fmtSize(got)));
  }
  return new Blob(chunks, { type: r.headers.get("Content-Type") || "" });
}

// The browser does the downloading, not us. So the token header only buys a
// short-lived signed link (a navigation cannot carry the header, hence the
// signature) and the browser streams the file itself. No "Downloading…" toast:
// the browser puts its own download UI on screen, and ours would only sit on
// top of it and outlive it. Silence unless something goes wrong.
async function downloadViaLink(path, name) {
  if (demoApiOn()) { toast("Not in the demo"); return; }
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

// ---- drag and drop (pointer devices only) ----------------------------------
// Two drags, told apart by what the dataTransfer carries. One from the OS
// carries "Files" and drops onto the listing as an upload into the folder on
// screen — the same uploadFiles() the Upload row picks for. One that started on
// a row carries FILES_DRAG_TYPE and drops onto a folder row, a folder tile or
// an ancestor crumb as a move, which is the rename the action sheet already
// posts with a folder in dst instead of a new name.
//
// None of it is wired on a touch device: a phone has no second pointer to drag
// with, and draggable=true can take a long press away from the row on some
// Android builds. touchOnly() is asked per render and per event rather than
// latched, for the tablet that picks up a trackpad mid-session.

// Ours, not the OS's — the one thing that says the drag started in this app.
const FILES_DRAG_TYPE = "application/x-pockettui-entry";

// What a row drag is carrying: the entry, the folder it was picked up in, and
// which listing that was. Held here because dataTransfer.getData() is unreadable
// until the drop, and every dragover before it has to know what is in flight to
// answer at all. One for the column rather than one per pane: an entry dragged
// out of one listing and dropped on a folder in the other is a move like any
// other, and the two panes have to be reading the same drag for it.
let filesDragEntry = null;

function hasDragType(dt, type) {
  return !!dt && Array.from(dt.types || []).includes(type);
}

// A folder dragged in from the OS has no bytes behind it — getAsFile() hands
// over something that either fails to read or uploads as zero bytes — so it
// comes back as its directory entry instead, for walkDroppedDir() to open on a
// server that takes folders and for the drop to leave out on one that does
// not. webkitGetAsEntry is what tells the two apart; where it is missing, or
// answers null for a file the page itself made, the item counts as a file.
// Synchronous on purpose: the items are only readable during the drop event
// itself, while the entries taken out of them stay good for the walk after.
function droppedEntries(dt) {
  const items = Array.from(dt.items || []).filter((it) => it.kind === "file");
  if (items.length && items[0].webkitGetAsEntry) {
    const files = [], dirs = [];
    for (const it of items) {
      const e = it.webkitGetAsEntry();
      if (e && e.isDirectory) { dirs.push(e); continue; }
      const f = it.getAsFile();
      if (f) files.push(f);
    }
    return { files, dirs };
  }
  return { files: Array.from(dt.files || []), dirs: [] };
}

// Everything in a dropped folder, depth first, as the upload loop's items:
// relPath starts with the folder's own name, so dropping photos/ on /x lands
// its files under /x/photos. A folder with nothing in it at all comes back in
// `empty`, since no file will make it. readEntries() is a page at a time
// (Chrome stops at 100), and only an empty page means the folder is done.
async function walkDroppedDir(dir, prefix, into) {
  const path = prefix + dir.name;
  const reader = dir.createReader();
  const kids = [];
  for (;;) {
    const page = await new Promise((ok, fail) => reader.readEntries(ok, fail));
    if (!page.length) break;
    kids.push(...page);
  }
  if (!kids.length) into.empty.push(path);
  for (const k of kids) {
    if (k.isDirectory) { await walkDroppedDir(k, path + "/", into); continue; }
    if (!k.isFile) continue;
    try {
      const file = await new Promise((ok, fail) => k.file(ok, fail));
      into.items.push({ file, relPath: path + "/" + k.name });
    } catch (e) {
      into.unreadable++;
    }
  }
  return into;
}

// The folder picker's own test. iOS Safari has the webkitdirectory property
// and ignores it — the sheet opens a plain photo or file picker and the pick
// arrives flat — so the property alone would put a row there that does
// nothing its name says; a2hsPlatform() is what counts iPadOS as iOS.
function folderPickerWorks() {
  return "webkitdirectory" in document.createElement("input")
         && a2hsPlatform() !== "ios";
}

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

// ------------------------------------------------------------
// Every pane of this kind
// ------------------------------------------------------------

// The listings the column has, by the id it knows each of them by ("files" for
// the one the markup ships, "files#2" for the copy made beside it). Everything
// below that the rest of the app calls is a dispatcher over this.
const filesPanes = {};

// Which listing the editor, the reader or the media viewer over it was opened
// from, or null while none of them is seated in a pane. There is one of each for
// the two rows, so this is what says which row they are drawn in, whose keys act
// on them, and whose Escape puts them away.
let filesViewOwner = null;

// Which listing the action sheet and the + sheet are about. Both are the app's
// own sheets rather than a pane's — they cover the window — so they are wired
// once and act on the listing that opened them.
let filesSheetOwner = "files";

// The pane a call that names none is about: the one holding the file view if
// there is one, then the one last pressed in (sideFocusedOf, 26-side-pane.js),
// then the markup's own. The press that opened a file is what set that focus, so
// a read or a download asked for by the editor lands on the listing the file
// came from.
function filesActive() {
  return filesPanes[filesViewOwner] || filesPanes[sideFocusedOf("files")]
      || filesPanes.files;
}

function filesSheetPane() { return filesPanes[filesSheetOwner] || filesActive(); }

// The action sheet raised over an entry. The sheet's markup is reached from here
// rather than from inside a pane, for the same reason its rows are wired here:
// there is one of it, over the window, whichever listing raised it.
// `at` is a right-click's point (and the pane it landed in): the same rows then
// open as a popover there instead of the sheet a long press raises.
function filesShowActions(entry, at) {
  // A folder downloads as a zip the server builds while it sends it, so the row
  // is offered over one too — but only where the server on the other end can
  // build it, since an older one answers a folder with a 404.
  const download = !(entry.type === "dir" && !hasCap("zip_dir"));
  // Only where the tap itself no longer reaches the editor. Every other text
  // file already opens there, so an Edit row would say nothing.
  const edit = entry.type === "file" && FILES_HTML_RE.test(entry.name);
  if (at) {
    $("ctx-file-download").style.display = download ? "" : "none";
    $("ctx-file-edit").style.display = edit ? "" : "none";
    filesShowMenu(at);
    return;
  }
  $("file-actions-title").textContent = entry.name;
  $("btn-file-download").style.display = download ? "" : "none";
  $("btn-file-edit").style.display = edit ? "" : "none";
  showSheet(true, "sheet-file-actions");
}

// ---- the right-click menu ----------------------------------------------------
// Where a w-by-h menu opened at the pointer (x, y) goes: its top-left corner at
// the point, flipped to the point's left or above it when it would cross the
// right or bottom edge of `box` (the pane the click was in, already cut to the
// window), then clamped `pad` inside the window, so a menu taller than a short
// pane still shows whole.
function filesMenuPlace(x, y, w, h, box, vw, vh, pad) {
  let left = x + w > box.right - pad ? x - w : x;
  let top = y + h > box.bottom - pad ? y - h : y;
  left = Math.max(pad, Math.min(left, vw - pad - w));
  top = Math.max(pad, Math.min(top, vh - pad - h));
  return { left: Math.round(left), top: Math.round(top) };
}

function filesShowMenu(at) {
  const menu = $("file-ctx-menu");
  menu.hidden = false;
  const vw = window.innerWidth, vh = window.innerHeight;
  const r = at.pane ? at.pane.getBoundingClientRect() : null;
  const box = r ? { right: Math.min(r.right, vw), bottom: Math.min(r.bottom, vh) }
                : { right: vw, bottom: vh };
  const p = filesMenuPlace(at.x, at.y, menu.offsetWidth, menu.offsetHeight, box, vw, vh, 6);
  menu.style.left = p.left + "px";
  menu.style.top = p.top + "px";
}

function filesHideMenu() { $("file-ctx-menu").hidden = true; }
function filesMenuOpen() { return !$("file-ctx-menu").hidden; }

function filesAnyDocked() {
  return Object.values(filesPanes).some((p) => p.isDocked());
}

// Either shape, either pane — what "the explorer is up at all" means now.
function filesAnyOpen() {
  return Object.values(filesPanes).some((p) => p.isOpen());
}

// The explorer over the whole window, which is the markup's own pane and no
// other: a copy exists only as a row of the column beside a terminal.
function filesFullScreen() {
  return filesPanes.files.isOpen() && !filesPanes.files.isDocked();
}

function filesActivePath() {
  const p = filesActive();
  return p ? p.path() : "";
}

// A write lands on one listing and is a fact about a folder: any other pane
// showing that same folder is now out of date and re-reads it. `path` is passed
// where the folder written to is not the writing pane's own — a move empties the
// folder the entry came from as well as filling the one it went to.
function filesRefreshPeers(from, path) {
  const at = path || (filesPanes[from] ? filesPanes[from].path() : "");
  if (!at) return;
  for (const key of Object.keys(filesPanes)) {
    if (key !== from) filesPanes[key].reloadAt(at);
  }
}

// Where the full-screen listing was scrolled to when a file view covered it,
// or null while none is covering it.
let filesCoveredScroll = null;

// ---- a file opened in a pane -----------------------------------------------
// The editor, the reader and the media viewer open inside the docked pane
// rather than over the whole window: same slot, same width, and the listing
// left active underneath them, so the slot stays claimed and closing the pane
// still closes one thing. The class is the whole of it — the geometry is the
// stylesheet's — and a phone, which has no pane, never gets it. The answer is
// handed back because the two shapes differ in more than geometry: the
// full-screen one owns a history entry and the docked one owns none.
//
// There is one of each for two listings, so a view also has an owner: the pane
// it was opened from. That is the row it is drawn in — the registry's els()
// under the factory reaches for these three while their owner is its own — so
// the column is laid out again whenever the owner changes.
function dockFileView(el, pane) {
  const owner = pane || filesActive();
  if (owner && owner.isDocked()) {
    el.classList.add("docked");
    filesSetViewOwner(owner);
    return true;
  }
  el.classList.remove("docked");
  filesSetViewOwner(null);
  // Full screen the view covers the explorer rather than sitting in it, and
  // an active screen under another one would be two screens at once. Only the
  // markup's own pane is ever the whole window. The listing scrolls the page,
  // and the page under the view is the view's, so where the listing was is
  // kept here for undockFileView — only when it is the listing being covered,
  // not the reader handing its screen to the editor.
  if (filesPanes.files.isOpen()) filesCoveredScroll = window.scrollY;
  filesPanes.files.setActive(false);
  return false;
}

// Which row the three of them are in, said in one place: the id, and the layout
// that reads it.
function filesSetViewOwner(pane) {
  filesViewOwner = pane ? pane.id : null;
  sideLayout();
}

// The mirror, asked of the screen itself rather than of the owner: a view opened
// in one shape has to be put away in the one it was opened in.
function undockFileView(el) {
  if (el.classList.contains("docked")) {
    el.classList.remove("docked");
    filesSetViewOwner(null);
    return true;
  }
  filesPanes.files.setActive(true);
  if (filesCoveredScroll !== null) {
    window.scrollTo(0, filesCoveredScroll);
    filesCoveredScroll = null;
  }
  return false;
}

// Which of the three the pane is showing over its listing, or null for none.
function dockedFileView() {
  for (const id of ["screen-editor", "screen-reader", "viewer"]) {
    if ($(id).classList.contains("docked")) return $(id);
  }
  return null;
}

// Put that view away. False is the editor asking about unsaved work and being
// told to stay: the press that got here is spent on the question, and whatever
// was going to close the pane has to stand down with it.
function closeDockedFileView() {
  const el = dockedFileView();
  if (!el) return true;
  if (el.id === "screen-editor") return editorCloseDocked();
  if (el.id === "screen-reader") closeReader();
  else hideImage();
  return true;
}

// ------------------------------------------------------------
// One pane
// ------------------------------------------------------------

// One call of this is one pane. The body below is the module this file used to
// be, wrapped rather than rewritten — it is not indented into the function,
// because every line of it would then have moved and the change would read as a
// rewrite of an explorer that has not changed. What the wrapper buys is that
// each `let` is now that pane's own, and a listing landing from a fetch started
// before the user touched the other pane still draws into the pane it was asked
// for, which no swapped "current pane" pointer can promise across an await.
// `root` is that pane's own screen and `q` is how every id inside it is reached,
// since the copy carries the same ids as the original; `id` is what the column
// calls it, and it is what the body branches on wherever a thing belongs to the
// markup's own pane alone — the whole window, the history entries that shape
// owns, the terminal's cwd, and the size the app remembers being read at.
// What the rest of the app calls is the dispatchers under the factory.
function makeFilesPane(id, root) {

const q = (name) => root.querySelector("#" + name);

// This pane as the rest of the app holds it. Filled in at the end of this body,
// and read only from a listener or a call that happens well after.
const self = () => filesPanes[id];

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
// Only the markup's own pane keeps the size it is read at as the app's
// preference: two panes writing one setting would each be undoing the other, so
// a copy opens at its own height and keeps its expand in memory
// (filesSetExpanded below).
let filesExpanded = id === "files" ? cfg.filesExpanded : false;
// The folder the terminal's own cwd last put the docked pane at — what "the
// pane is still where the terminal left it" is measured against, and "" while
// nothing has ever synced it. Only the docked shape has a terminal to follow.
let filesSyncedCwd = "";
// The folder this session's pane was left at, when it reopened there instead of
// at the terminal's cwd (filesOpenAtCwd), and "" otherwise. While the pane is
// still on it the following is armed, with filesSyncedCwd holding the cwd the
// terminal had at the reopen: only a cd to somewhere new moves the pane.
let filesHeldAt = "";
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

function openExplorer(path, opts) {
  // Unpaired there is no computer to browse, and the demo's tree stands in
  // (17-fake-api.js), the same one its terminal lists.
  // Beside a terminal there is room for both, so the explorer docks rather than
  // taking the screen. Every caller lands here — the folder key, a tapped path,
  // the editor's parent folder — so the two shapes are one entry point. `opts`
  // is nothing to do with the folder: it is the column's, carried through to the
  // claim the docked shape makes (sideClaim, 26-side-pane.js).
  if (isWideLayout() && $("screen-term").classList.contains("active")) {
    return openDockedFiles(path, opts);
  }
  // The whole window is the markup's own pane and no other: a copy is a second
  // row of the column, which only a wide layout beside a terminal has, and it
  // owns none of the history entries this shape unwinds.
  if (id !== "files") return Promise.resolve(false);
  if (!root.classList.contains("active")) {
    filesOrigin = $("screen-term").classList.contains("active")
      ? "screen-term" : "screen-list";
    $(filesOrigin).classList.remove("active");
    // Only the terminal is worth a one-tap way back to — the button says
    // terminal. Set here and nowhere else: filesOrigin outlives a trip through
    // the editor, which returns to this screen without coming back through
    // openExplorer.
    q("btn-files-term").style.display = filesOrigin === "screen-term" ? "" : "none";
    root.classList.add("active");
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

// The rows and crumbs a closed explorer leaves behind. Emptied on the way out,
// because the next open shows the screen before its listing lands, and that
// can take seconds on a network mount: rows left over from the folder the pane
// was closed on read as the explorer having opened there, and then jumping to
// the folder it really opened at.
function filesClearListing() {
  stopThumbs();
  filesEntries = [];
  q("files-list").innerHTML = "";
  q("files-empty").style.display = "none";
  q("files-crumbs").innerHTML = "";
}

function closeExplorer() {
  // Before the address-field branch below, which returns without closing the
  // screen: either way the bar's menus are going, scrim and all.
  closeFilesMenus();
  // Back (popstate or the edge swipe) while the address field is open closes
  // just the field, same as Escape — same pushState-back trick editorPopped()
  // uses for a dirty buffer, since the pop has already happened by the time
  // either gets a say.
  if (q("files-path-wrap").classList.contains("editing")) {
    closePathEdit();
    history.pushState(filesEntryState(), "", location.href);
    return;
  }
  root.classList.remove("active");
  const back = filesOrigin || "screen-list";
  filesOrigin = null;
  filesStack = [];
  clearRefState();
  filesClearListing();
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
// seam it hides and the width it overrides are both read from there, and only
// one row of the column may be expanded at a time (sideSetFull).
function syncFilesExpand() {
  const on = filesDocked && filesExpanded;
  sideSetFull(id, on);
  // Every bar this pane can wear one in: the listing's own, and the editor's,
  // the reader's and the viewer's while a file of its own is open in one.
  for (const box of filesEls()) {
    for (const btn of box.querySelectorAll(".dock-expand")) {
      // The explorer's own bar draws the side panes' glyph set (#i-m-*); the
      // file views seated in its row keep the app's.
      const use = btn.querySelector("use");
      const m = use.getAttribute("href").startsWith("#i-m-") ? "m-" : "";
      use.setAttribute("href", "#i-" + m + (on ? "collapse" : "expand"));
      btn.setAttribute("aria-label", on ? "Shrink the file pane" : "Expand the file pane");
    }
  }
}

// The expand set from outside the pane: only one row of the column may fill the
// main area, so the other row's arrival folds this one away (sideSetFull,
// 26-side-pane.js). The flag, the key it is remembered under and the bars it is
// drawn on all move together, which is what makes this a function rather than
// three lines at the caller.
function filesSetExpanded(v) {
  filesExpanded = v;
  if (id === "files") cfg.filesExpanded = v;
  syncFilesExpand();
}

// Opening the pane a second time is a navigation within it, not a fresh entry:
// a path tapped in the terminal lands in the pane already open, and the crumb
// stack it walks back through is worth keeping.
function openDockedFiles(path, opts) {
  // The column's answer comes first, and a no is final: the row this would take
  // may be a docked editor with unsaved work whose owner was asked and said
  // stay (sideClaim, 26-side-pane.js), and nothing here may have moved by then.
  if (!sideClaim(id, opts)) return Promise.resolve(false);
  const already = filesDocked;
  filesDocked = true;
  filesOrigin = "screen-term";
  // Redundant beside a live terminal — it is right there — and the pane has
  // its own cross for leaving.
  q("btn-files-term").style.display = "none";
  root.classList.add("docked");
  root.classList.add("active");
  syncFilesExpand();
  if (already) return navigateDir(path);
  filesStack = [];           // seeded once loadDir below resolves the real path
  return loadDir(path);
}

// This pane's way out: its own cross, the folder key toggling it away, and the
// column taking its row for another (the registry below).
function closeDockedFiles() {
  if (!filesDocked) return;
  // A file opened in this pane goes with it, and the editor gets the same say
  // about unsaved work that its own back gives it. Only a file of this pane's:
  // one seated over the other listing is that pane's to ask about, and a row
  // evicted from under a listing that owns nothing asks nothing.
  if (filesViewOwner === id && !closeDockedFileView()) return;
  closeFilesMenus();
  closePathEdit();
  filesDocked = false;
  filesOrigin = null;
  filesStack = [];
  filesHeldAt = "";
  clearRefState();
  filesClearListing();
  root.classList.remove("docked");
  root.classList.remove("active");
  syncFilesExpand();         // takes .side-full off the terminal with it
  sideDrop(id);
}

// The docked pane owns no history entries, so its back arrow has to do what a
// pop does for the full-screen explorer: climb filesStack, and close at [0].
function filesBack() {
  if (!filesDocked) { history.back(); return; }
  if (q("files-path-wrap").classList.contains("editing")) { closePathEdit(); return; }
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

// Wired under this pane's own screen rather than over the document: the pane's
// two controls appear in its bar, and a second explorer brings a bar of its own.
// The same pair in the three file views is wired once under the factory — there
// is one editor, one reader and one viewer for both rows — and acts on whichever
// listing owns them.
for (const btn of root.querySelectorAll(".dock-expand")) {
  btn.addEventListener("click", () => {
    filesSetExpanded(!filesExpanded);
    refit(0);
  });
}
for (const btn of root.querySelectorAll(".dock-close")) {
  btn.addEventListener("click", () => closeDockedFiles());
}

// This pane as a row of the column: its own screen, plus the three file views
// while this is the listing they were opened from — a file opened here is seated
// over this listing and belongs to whichever row it is in, and the other
// explorer's row must not take them with it.
function filesEls() {
  return filesViewOwner === id
    ? [root, $("screen-editor"), $("screen-reader"), $("viewer")]
    : [root];
}
sideRegister(id, {
  type: "files",
  els: filesEls,
  close: () => closeDockedFiles(),
  setExpanded: (on) => filesSetExpanded(on),
});

// The pane a session comes back to with no folder to put in it: a reload
// remembered that this session had the explorer docked (cfg.sidePane), and the
// explorer's half of that is a claim on the slot rather than a folder — so it
// opens at the session's own cwd, exactly as the folder key would. The only
// caller is restoreFileView's reload path; a rail switch always has a folder.
// The demo answers the same way, out of its own tree (17-fake-api.js).
function followSession() {
  if (!isWideLayout() || filesDocked) return;
  // Handed back because the claim it makes is a round trip away: the cwd has to
  // answer before the row is in the column, and the restore that called this
  // has an order to put the rows in once it is (sideOrder, 26-side-pane.js).
  return filesOpenAtCwd();
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
    heldAt: filesHeldAt,
    // A branch being read is part of what the session was looking at, so it
    // comes back with the folder rather than the pane returning to the disk.
    ref: filesRef, refRoot: filesRefRoot, repo: filesRepo, repoAsked: filesRepoAsked,
  };
}

// Drops the view without putting anything back: the caller is replacing every
// screen at once, so closeExplorer's return-to-origin (and its refit, which
// would fit a terminal that is about to change session) is not what it wants.
function filesTeardown() {
  root.classList.remove("active");
  filesOrigin = null;
  filesStack = [];
  filesHeldAt = "";
  clearRefState();
  filesClearListing();
  const wasDocked = filesDocked;
  filesDocked = false;
  // Docked, the slot goes back with the folder: the pane was the leaving
  // session's, and the terminal is about to be another session's. Whatever that
  // one had put away claims it again (restoreFileView), and a session that had
  // nothing gets the whole width. Same tail as closeDockedFiles, which is the
  // other way the pane ends.
  if (wasDocked) { syncFilesExpand(); sideDrop(id); }
}

// Everything this pane is holding about the computer being left, for a switch to
// another one (filesResetForProfile, under the factory): whatever shape the view
// is in goes away, since a folder on that machine is not a folder on this one.
// The caller puts the session list back on screen, so nothing is restored here.
function filesReset() {
  closeFilesMenus();
  closePathEdit();
  if (filesDocked) closeDockedFiles();
  else filesTeardown();
  filesPath = "";
  filesHome = "";
  filesSyncedCwd = "";
  filesHeldAt = "";
  filesEntries = [];
  filesSelected = null;
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
  filesHeldAt = s.heldAt || "";
  filesRef = s.ref || "";
  filesRefRoot = s.refRoot || "";
  filesRepo = s.repo || null;
  filesRepoAsked = s.repoAsked || "";
  syncRefBar();
  syncRefActions();
  q("btn-files-term").style.display =
    !filesDocked && filesOrigin === "screen-term" ? "" : "none";
  root.classList.toggle("docked", filesDocked);
  // Docked, the terminal underneath is the pane's neighbour rather than the
  // screen it covers, and it stays on view.
  if (!filesDocked) $(filesOrigin || "screen-list").classList.remove("active");
  root.classList.add("active");
  if (filesDocked) { sideClaim(id); syncFilesExpand(); }
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
// with every cd — and $HOME quietly stands in when tmux cannot say. `opts` is
// the column's (sideClaim's `keep`).
async function filesOpenAtCwd(opts) {
  // A copy is the user asking for a second look at where they already are, so it
  // opens on the folder the first listing is showing. With no listing up there
  // is no "here" to look at twice, and it falls to the session's own cwd, which
  // is where the folder key opens.
  if (id !== "files") {
    const here = filesPanes.files ? filesPanes.files.path() : "";
    if (here) return openExplorer(here, opts);
  }
  const from = currentSession;
  filesHeldAt = "";
  const cwd = await fetchPaneCwd();
  if (cwd === null) return;
  // The rail can move on while tmux is being asked, and the answer is about the
  // session it was asked of: opening now would put one session's folder in
  // another session's slot, which is the one thing the pane must never do.
  if (currentSession !== from) return;
  // A session the explorer was open in before goes back to where it was left.
  // Asked of the disk first, so a folder that has gone since falls through to
  // the terminal's cwd as a new session's would, with no error on the way.
  const held = id === "files" ? filesLastDir(from) : "";
  if (held && held !== cwd) {
    let there = false, gone = false;
    try {
      there = !!(await fsList(held));
    } catch (e) {
      // An expired token has already been answered (rejectToken); any other
      // failure that is not the folder being gone leaves the memory alone.
      if (e.message === "unauthorized") return;
      gone = !!e.code;
    }
    if (currentSession !== from) return;
    if (there) {
      const ok = await openExplorer(held, opts);
      // Following waits for the terminal to move: the cwd it has now is the
      // baseline, and only a cd to somewhere else takes the pane there.
      if (ok && filesDocked) { filesSyncedCwd = cwd; filesHeldAt = filesPath; }
      return ok;
    }
    if (gone) forgetFilesDir(from);
  }
  const ok = await openExplorer(cwd, opts);
  // The folder that actually resolved, not the string asked for: a cwd tmux
  // could not give lands at $HOME, and that is where the pane is. Docked only —
  // full screen there is no terminal beside it to keep up with — and the
  // markup's own pane only: a copy is a second place to look and never follows
  // a cd (filesFollowsCwd below).
  if (ok && filesDocked && id === "files") filesSyncedCwd = filesPath;
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
      && filesStack.length <= 1
      && (filesPath === filesSyncedCwd || (!!filesHeldAt && filesPath === filesHeldAt))
      && !$("screen-editor").classList.contains("active")
      && !$("screen-reader").classList.contains("active")
      && !q("files-path-wrap").classList.contains("editing");
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
  if (await loadDir(cwd)) { filesSyncedCwd = filesPath; filesHeldAt = ""; }
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
  // The demo's terminal has a cwd too, answered in memory (17-fake-api.js).
  return !document.hidden && (demoMode || !needsSetup())
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
async function openPath(path) {
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
  const qs = parts.length ? "?" + parts.join("&") : "";
  const r = await fetch(apiURL("api/fs/list" + qs),
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
    const qs = ["path=" + encodeURIComponent(path), ...refParams(refFor(path))];
    r = await fetch(apiURL("api/fs/read?" + qs.join("&")),
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
  q("files-error").style.display = "none";
  // Where this session's explorer is, for the next time it opens
  // (filesLastDir, 06-session-list.js). The markup's own pane only — the copy
  // is a second look at a folder, not where the session went — and only over a
  // terminal, whose session this is, and only on the working tree: a folder
  // read at a ref is the branch's.
  if (id === "files" && filesOrigin === "screen-term" && !filesRef && currentSession
      && root.classList.contains("active")) {
    rememberFilesDir(currentSession, data.path);
  }
  // After filesPath, which is the folder the question is about.
  syncRefBar();
  syncRepo();
}
function showListError(e) {
  dbg("fs list failed:", e);
  q("files-list").innerHTML = "";
  q("files-empty").style.display = "none";
  q("files-error").style.display = "block";
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
  const btn = el("button", {
    type: "button",
    class: "crumb" + (current ? " current" : ""),
    onclick: () => { if (!current) navigateDir(target); },
  }, label);
  // An ancestor crumb is how a dragged entry moves up out of here. The current
  // folder's own crumb is where the entry already is, so it takes nothing.
  if (!current && !touchOnly()) wireMoveTarget(btn, target);
  return btn;
}

function renderCrumbs(path) {
  const wrap = q("files-crumbs");
  wrap.innerHTML = "";
  let parts, prefix, rootLabel;
  if (filesHome && (path === filesHome || path.startsWith(filesHome + "/"))) {
    parts = path.slice(filesHome.length).split("/").filter(Boolean);
    prefix = filesHome; rootLabel = "~";
  } else {
    parts = path.split("/").filter(Boolean);
    prefix = ""; rootLabel = "/";
  }
  const home = crumbBtn(rootLabel === "~" ? svgIcon("i-m-home") : rootLabel,
                        prefix || "/", parts.length === 0);
  if (rootLabel === "~") home.setAttribute("aria-label", "Home");
  wrap.appendChild(home);
  parts.forEach((seg, i) => {
    prefix += "/" + seg;
    wrap.appendChild(el("span", { class: "crumb-sep" }, svgIcon("i-m-chev-right")));
    wrap.appendChild(crumbBtn(seg, prefix, i === parts.length - 1));
  });
  // The tool row's title is the folder the crumbs end on.
  root.querySelector(".files-title-name").textContent =
    parts.length ? parts[parts.length - 1] : rootLabel;
  // The tail is the current folder — that is the segment to keep in view.
  wrap.scrollLeft = wrap.scrollWidth;
}

// ---- address bar ------------------------------------------------------------
// Tapping btn-files-edit-path (or the path bar's empty stretch) swaps
// files-crumbs for files-path-input, an editable field pre-filled with the
// current path. Typing filters the parent directory's listing down to entries
// whose name starts with the segment after the last "/" (auto-roll: completing
// a directory appends "/" and starts filtering the next segment, exactly like
// tab-completion in a shell).
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
  q("files-path-wrap").classList.add("editing");
  q("files-suggest-scrim").classList.add("show");
  const input = q("files-path-input");
  input.value = filesPath;
  input.focus();
  // setSelectionRange after focus, not before — iOS otherwise ignores it on a
  // field that was not already focused.
  input.setSelectionRange(input.value.length, input.value.length);
  const [dirPart, segPart] = splitTyped(input.value);
  if (dirPart) loadSuggestions(dirPart, segPart);
}

function closePathEdit() {
  q("files-path-wrap").classList.remove("editing");
  q("files-suggest-scrim").classList.remove("show");
  q("files-path-input").blur();
  hideSuggestions();
  clearTimeout(suggestTimer);
  suggestTimer = null;
  suggestGen++;   // orphans any fetch already in flight
}

function hideSuggestions() {
  q("files-suggest").classList.remove("show");
  q("files-suggest").innerHTML = "";
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
  const box = q("files-suggest");
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
  const input = q("files-path-input");
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
    const box = q("files-suggest");
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
  const typed = q("files-path-input").value.trim();
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

q("btn-files-edit-path").addEventListener("click", () => {
  if (q("files-path-wrap").classList.contains("editing")) closePathEdit();
  else openPathEdit();
});
// The bar is the field too: a click on its empty stretch (past the last crumb,
// between two, on a separator) opens it the way the pencil does. A crumb, the
// branch pop-up and the read-only tag keep their own jobs, and a click inside
// the open field is typing.
root.querySelector(".files-pathbar").addEventListener("click", (e) => {
  if (e.target.closest(".crumb, #files-ref-wrap, #files-ref-ro, #btn-files-edit-path")) return;
  if (q("files-path-wrap").classList.contains("editing")) return;
  openPathEdit();
});
// Same as Escape: close without navigating, whether the tap landed on the
// dimmed file list or on empty space below a short one.
q("files-suggest-scrim").addEventListener("click", () => closePathEdit());
q("files-path-input").addEventListener("input", (e) => fetchSuggestions(e.target.value));
q("files-path-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); submitPathEdit(); }
  else if (e.key === "Escape") { e.preventDefault(); closePathEdit(); }
});
// Losing focus to anything but a suggestion row (which itself closes the
// field on tap) means the user tapped away — close without navigating, same
// as Escape. Deferred, same idea as composeBlurred() in 17-key-bar.js
// (blur fires before the click that caused it), but longer than its tick-0:
// a touch's click can lag its own blur by more than one turn on mobile, and
// firing early here would drop the suggestion tap on the floor.
q("files-path-input").addEventListener("blur", () => {
  setTimeout(() => {
    if (document.activeElement !== q("files-path-input")
        && !q("files-suggest").contains(document.activeElement)) {
      closePathEdit();
    }
  }, 150);
});

// ---- the listing ------------------------------------------------------------
// Two layouts over one set of entries: rows, or the icon tiles a phone's own
// file manager shows. Only the drawing differs — both go through wireRow(), so
// tap, long press and everything they open behave identically either way.

function renderEntries(entries) {
  const list = q("files-list");
  const grid = cfg.filesView === "grid";
  filesEntries = entries;
  stopThumbs();
  thumbsDrawn = grid && thumbsOn();
  list.classList.toggle("files-grid", grid);
  list.innerHTML = "";
  q("files-empty").style.display = entries.length ? "none" : "block";
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
    el("span", { class: "file-ic " + e.type }, svgIcon(entryIcon(e))),
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
  const slot = el("div", { class: "file-slot" },
    el("span", { class: "file-ic " + e.type }, svgIcon(entryIcon(e))));
  if (wantsThumb(e)) {
    // The icon stays put and the picture arrives over it, so a tile is never
    // an empty box waiting for a server.
    slot.classList.add("has-thumb");
    slot.setAttribute("data-thumb", joinPath(filesPath, e.name));
    // A first frame is a photo until something says otherwise, so the tile
    // that got one says so — once it has one, in loadThumb below.
    if (fileKind(e.name) === "video") slot.setAttribute("data-video", "1");
    thumbWatch(slot);
  } else if (e.type !== "dir") {
    tileBadge(slot, e.name);
  }
  const tile = el("div", { class: "file-tile" },
    slot,
    el("div", { class: "file-name" }, e.name),
  );
  wireRow(tile, e);
  return tile;
}

// One icon per entry, whichever layout is asking. A symlink is drawn as what
// it is named after: the listing cannot say what it points at, and a name is
// the only evidence either way.
function entryIcon(e) {
  return e.type === "dir" ? "i-folder" : kindIcon(fileKind(e.name));
}

function tileBadge(slot, name) {
  const ext = fileBadge(name);
  if (ext) slot.appendChild(el("span", { class: "file-badge" }, ext));
}

// ---- grid thumbnails --------------------------------------------------------
// A folder of photos is a wall of identical icons, so in the grid the picture
// tiles show the picture: /api/fs/thumb renders a <=256px JPEG of an image or a
// video's first frame. Fetched one tile at a time rather than through
// api/file_link, which would cost a signing round-trip per tile, and only as
// tiles come into view — a folder of four hundred photos must not be four
// hundred requests the moment it opens.
//
// Everything here belongs to one render. A folder change disconnects the
// observer and aborts the fetches in flight, so nothing a listing left behind
// can paint itself into the tiles of the listing that replaced it.

const THUMB_MAX = 6;       // requests allowed in flight at once
let thumbRun = null;       // the current render's {obs, ctrl, queue, active}
// Whether the tiles on screen were drawn expecting thumbnails, so the arrival
// of the capability map can be told from a redraw that already knew.
let thumbsDrawn = false;

// Whether this server renders thumbnails for the folder on screen. A listing
// read at a ref is a commit and the endpoint reads the working tree, so that
// mode simply keeps its icons. Strict, not hasCap(): a server that predates
// the endpoint answers 404 for every tile.
function thumbsOn() {
  return hasCapStrict("thumbs") && !refFor(filesPath);
}

// Which tiles ask for one: pictures and videos, by name. An .svg is a picture
// the backend refuses (it rasterises nothing), so it keeps the image icon and
// takes a badge like any other typed file.
function wantsThumb(e) {
  if (e.type === "dir" || !thumbsOn()) return false;
  if (/\.svg$/i.test(e.name)) return false;
  const kind = fileKind(e.name);
  // A PDF's tile is its first page, rasterised by a tool ffmpeg's presence
  // says nothing about — a machine can have ffmpeg and no PDF renderer — so it
  // has a capability of its own, and without it the tile keeps the pdf icon.
  if (kind === "pdf") return hasCapStrict("pdf_thumbs");
  return kind === "image" || kind === "video";
}

function stopThumbs() {
  if (!thumbRun) return;
  thumbRun.obs.disconnect();
  thumbRun.ctrl.abort();
  thumbRun = null;
}

function thumbWatch(slot) {
  if (!thumbRun) {
    // Docked, the pane is what scrolls; full screen, the page is. Asked of the
    // live style rather than of the layout, since the same markup is both.
    const wrap = q("files-list").closest(".files-wrap");
    const root = wrap && getComputedStyle(wrap).overflowY !== "visible" ? wrap : null;
    const run = { obs: null, ctrl: new AbortController(), queue: [], active: 0 };
    // A screen's worth of lead time, so a tile is usually painted by the time
    // a scroll brings it up.
    run.obs = new IntersectionObserver((recs) => {
      if (run !== thumbRun) return;
      for (const r of recs) {
        if (!r.isIntersecting) continue;
        run.obs.unobserve(r.target);
        run.queue.push(r.target);
      }
      pumpThumbs(run);
    }, { root: root, rootMargin: "300px" });
    thumbRun = run;
  }
  thumbRun.obs.observe(slot);
}

function pumpThumbs(run) {
  while (run === thumbRun && run.active < THUMB_MAX && run.queue.length) {
    run.active++;
    // Both arms, so a rejection nobody expected cannot wedge the slot shut.
    const done = () => {
      if (run !== thumbRun) return;
      run.active--;
      pumpThumbs(run);
    };
    loadThumb(run.queue.shift(), run).then(done, done);
  }
}

// One tile. Every failure ends the same way — the typed icon that is already
// there stays — so a server that cannot render this particular file costs the
// tile nothing. No rejectToken() on a 401 either: the listing behind these
// tiles asks the same server with the same header, and it is the one that
// should be telling the user their code expired.
async function loadThumb(slot, run) {
  const path = slot.getAttribute("data-thumb");
  try {
    const r = await fetch(apiURL("api/fs/thumb?path=" + encodeURIComponent(path)),
                          { headers: authHeaders(), signal: run.ctrl.signal });
    if (run !== thumbRun) return;
    if (!r.ok) {
      // Unsupported is a fact about the file rather than a failure, so the
      // tile says what it is instead of pretending it is still loading.
      if (r.status === 415) tileBadge(slot, baseName(path));
      return;
    }
    const blob = await r.blob();
    if (run !== thumbRun) return;
    const url = URL.createObjectURL(blob);
    const img = el("img", { class: "file-thumb", alt: "", draggable: "false" });
    // Revoked the moment it has been decoded: the bitmap is the browser's from
    // then on, and a folder of photos must not leave its blobs behind.
    const drop = () => URL.revokeObjectURL(url);
    img.addEventListener("load", () => {
      img.classList.add("on");
      if (slot.getAttribute("data-video"))
        slot.appendChild(el("span", { class: "file-play" }, svgIcon("i-play")));
      drop();
    });
    img.addEventListener("error", drop);
    img.src = url;
    slot.appendChild(img);
  } catch (e) {
    return;   // aborted by the next render, or the network said no
  }
}

// The capability map can land after a folder is already on screen (36-server-
// version.js calls this where it calls the diff pane's and the ref bar's own
// gates). A grid drawn before the answer arrived is redrawn once, rather than
// keeping its icons until the user navigates.
function syncThumbsCap() {
  if (cfg.filesView !== "grid" || !filesEntries.length) return;
  if (thumbsDrawn === thumbsOn()) return;
  if (q("files-error").style.display === "block") return;
  renderEntries(filesEntries);
}

// The switch itself, a button and a menu rather than a <select> — see the
// markup for why. Open and closed are one class on the wrap; the scrim rides
// along, so tapping anywhere off the menu closes it and nothing underneath
// takes that tap as a row press.
function showViewMenu(on) {
  q("files-view-wrap").classList.toggle("open", on);
  q("files-view-scrim").classList.toggle("show", on);
  q("btn-files-view").setAttribute("aria-expanded", on ? "true" : "false");
}

// Only ever one open, and the scrim above belongs to whichever it is.
q("btn-files-view").addEventListener("click", () => showRefMenu(false));

// Both groups tick from cfg — so this is also what a fresh load calls to catch
// up with what was remembered.
function syncViewMenu() {
  const v = cfg.filesView, s = cfg.filesSort;
  for (const row of q("files-view-menu").querySelectorAll(".view-row")) {
    const on = row.dataset.view ? row.dataset.view === v : row.dataset.sort === s;
    row.classList.toggle("on", on);
    row.setAttribute("aria-checked", on ? "true" : "false");
  }
}
syncViewMenu();

q("btn-files-view").addEventListener("click", () => {
  showViewMenu(!q("files-view-wrap").classList.contains("open"));
});
q("files-view-scrim").addEventListener("click", closeFilesMenus);
for (const row of q("files-view-menu").querySelectorAll(".view-row")) {
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
    if (q("files-error").style.display !== "block") renderEntries(filesEntries);
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
  q("files-ref-wrap").classList.toggle("open", on);
  q("files-view-scrim").classList.toggle("show", on);
  q("btn-files-ref").setAttribute("aria-expanded", on ? "true" : "false");
}

function closeFilesMenus() { showViewMenu(false); showRefMenu(false); filesHideMenu(); }

// Which repo the folder on screen is in, and what it has to offer. Asked per
// repo rather than per folder: inside a root already known the answer cannot
// have changed, and outside every root there is nothing to re-ask until the
// folder itself moves.
async function syncRepo() {
  if (demoApiOn() || !hasCap("git_ref")) { filesRepo = null; syncRefBar(); return; }
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
  q("files-ref-wrap").hidden = !repo;
  q("files-ref-ro").hidden = !filesRef;
  if (!repo) { showRefMenu(false); return; }
  const live = repo.current || "HEAD";
  const btn = q("btn-files-ref");
  btn.textContent = "";
  btn.append(svgIcon("i-m-branch"), el("span", { class: "ref-label" }, filesRef || live),
             svgIcon("i-m-chev-down"));
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
  const menu = q("files-ref-menu");
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
  q("btn-files-add").style.display = filesRef ? "none" : "";
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

q("btn-files-ref").addEventListener("click", () => {
  showRefMenu(!q("files-ref-wrap").classList.contains("open"));
});

// Tap opens; a long press (or a desktop right-click) opens the action sheet.
function wireRow(row, entry) {
  let timer = null, sx = 0, sy = 0, pressedByTouch = false;
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
    // A finger's long press can arrive as a contextmenu too (Android Chrome);
    // that one keeps the sheet the long-press timer above raises. Read off the
    // press itself rather than the device, so an iPad's trackpad gets the menu.
    if (pressedByTouch) { openFileActions(entry); return; }
    openFileActions(entry, { x: ev.clientX, y: ev.clientY, pane: root });
  });
  row.addEventListener("pointerdown", (ev) => {
    pressedByTouch = ev.pointerType === "touch";
  }, { passive: true });
  row.addEventListener("click", () => {
    if (Date.now() - filesPressedAt < 500) return;  // the long-press's own click
    openEntry(entry);
  });
  // The pointer device's second way to move a file, wired nowhere near the
  // listeners above — see the drag-and-drop section for why a phone gets none
  // of it. A folder is both something to drag and somewhere to drop.
  if (!touchOnly()) {
    wireEntryDrag(row, entry);
    if (entry.type === "dir") wireMoveTarget(row, joinPath(filesPath, entry.name));
  }
}

function openEntry(e, dir=filesPath) {
  // There is one editor, one reader and one viewer for the two listings, so a
  // file already open over the other one is put away first — with the same say
  // about unsaved work its own back gives it, and a stay stops this open here.
  if (!filesTakeView()) return;
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
    else showImage(full, self());
    return;
  }
  if (FILES_PDF_RE.test(e.name)) {
    if (filesRef) toast("Can't preview PDFs from " + filesRef);
    else openPdf(full);
    return;
  }
  if (FILES_MD_RE.test(e.name)) { openReader(full, self()); return; }
  if (FILES_HTML_RE.test(e.name) && !filesRef) { openRendered(full); return; }
  openEditor(full, { pane: self() });
}

// Whether this pane may take the three views over. A stay is the editor's, and
// it stops whatever was about to open a file here.
function filesTakeView() {
  if (!filesViewOwner || filesViewOwner === id) return true;
  return closeDockedFileView();
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
  if (demoApiOn()) { toast("Not in the demo"); return; }
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

// A PDF opens two ways, because a phone browser will not draw one in a frame:
// iOS Safari paints page one of an <iframe>'d PDF and stops there, and Android
// Chrome paints nothing at all. So a two-pane layout gets the viewer, where an
// image of the same tap lands, and a phone gets a tab — both of those browsers
// have a reader, they just will not lend it to a frame. The mint and the
// anchor are openRendered's above, over the viewer's signed link rather than
// the page one's, and the mint costs the click its synchrony for the same
// reason. Nothing is capped on the way: the file is streamed, by Range, from
// whichever of the two is showing it.
async function openPdf(path) {
  if (demoApiOn()) { toast("Not in the demo"); return; }
  if (isWideLayout()) { showImage(path, self()); return; }
  let url;
  try {
    const r = await fetch(apiURL("api/file_link?path=" + encodeURIComponent(path)),
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

function openFileActions(entry, at) {
  // Every row in this sheet either writes the working tree or downloads it off
  // the disk, and at a ref the listing on screen is neither.
  if (filesRef) { toast("Read-only on " + filesRef); return; }
  // The sheet is the app's own rather than this pane's, so this is what says
  // which listing the rows it is about belong to.
  filesSheetOwner = id;
  filesSelected = entry;
  filesShowActions(entry, at);
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

// The sheet's four rows, and the + sheet's two that write. The rows themselves
// are wired once under the factory — one sheet over the window, whichever
// listing raised it — and each of them lands here, in the pane that did.

function sheetEdit() {
  const e = filesSelected;
  if (!e) return;
  showSheet(false);
  if (!filesTakeView()) return;
  openEditor(joinPath(filesPath, e.name), { pane: self() });
}

async function sheetRename() {
  const e = filesSelected;
  if (!e) return;
  showSheet(false);
  const next = await appPrompt("Rename " + e.name,
                               { value: e.name, confirmLabel: "Rename" });
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
  filesRefreshPeers(id);
}

function sheetDownload() {
  const e = filesSelected;
  if (!e) return;
  showSheet(false);
  // A folder has no size to weigh the two ways down against — the archive is
  // built as it is sent and nobody knows how big it is until it is over — so it
  // always goes the browser's way round rather than through a blob this page
  // would have to hold whole.
  if (e.type === "dir") {
    downloadViaLink(joinPath(filesPath, e.name), e.name + ".zip");
    return;
  }
  downloadFile(joinPath(filesPath, e.name), e.name, e.size);
}

async function sheetDelete() {
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
  filesRefreshPeers(id);
}

// Whether the header download in a file view has anything to offer — the action
// sheet's rule, asked per file: /api/fs/download only ever reads the working
// tree, and a file being read at a ref is not what is on the disk.
function canDownload(path) { return !refFor(path); }

// ---- the + sheet -----------------------------------------------------------

q("btn-files-add").addEventListener("click", () => {
  filesSheetOwner = id;
  // Strict: an older server 404s every file of a tree whose folders it will
  // not make.
  $("btn-files-upload-dir").hidden = !(hasCapStrict("upload_dirs") && folderPickerWorks());
  showSheet(true, "sheet-files-add");
});

async function addNewFile() {
  showSheet(false);
  const name = await appPrompt("New file name", { confirmLabel: "Create" });
  if (name === null || !name.trim()) return;
  if (name.includes("/")) { toast("Names can't contain /"); return; }
  if (!filesTakeView()) return;
  // Nothing is written yet: the editor opens empty and the first Save (hash "")
  // is what creates the file, so an abandoned name leaves no husk behind.
  openEditor(joinPath(filesPath, name.trim()), { create: true, pane: self() });
}

async function addNewFolder() {
  showSheet(false);
  const name = await appPrompt("New folder name", { confirmLabel: "Create" });
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
  filesRefreshPeers(id);
}

// One file at a time, never in parallel: a 409 asks its own Replace? question,
// and a pile of confirms racing each other is unanswerable. Items are
// {file, relPath}, relPath being the file's name for a plain pick or drop and
// its path inside the folder for a folder's — any "/" in one, or a folder
// with nothing in it, makes the batch a tree and uploadTree() its loop.
async function uploadFiles(items, empty = [], unreadable = 0) {
  if (demoApiOn()) { toast("Not in the demo"); return; }
  if (refFor(filesPath)) { toast("Read-only on " + refFor(filesPath)); return; }
  if (empty.length || items.some((it) => it.relPath.includes("/"))) {
    return uploadTree(items, empty, unreadable);
  }
  let done = 0, failed = 0;
  for (const it of items) {
    const err = await uploadFile(it, false);
    if (err === null) { done++; continue; }
    // "" is a declined replace or an expired token — both already said their
    // piece, or deliberately say nothing.
    if (err) { failed++; if (items.length === 1) toast(err); }
  }
  if (items.length === 1) { if (done) toast("Uploaded " + items[0].file.name); }
  else if (done) toast("Uploaded " + done + (done === 1 ? " file" : " files")
                       + (failed ? ", " + failed + " failed" : ""));
  else if (failed) toast("Couldn't upload " + failed + " files");
  if (done) { loadDir(filesPath); filesRefreshPeers(id); }
}

// A folder's worth, which can be hundreds of files: one line of progress held
// in place rather than a toast per file, one Replace? for the whole batch —
// asked at the first file already there and applied to every one after it —
// and one summary at the end. Into the folder that was on screen when it
// started, whatever the listing moves on to while it runs. `unreadable` is the
// files the walk could not open, which count as failed from the start.
async function uploadTree(items, empty, unreadable = 0) {
  const dir = filesPath;
  let done = 0, skipped = 0, failed = unreadable, made = 0, replace = null;
  for (let i = 0; i < items.length; i++) {
    holdToast("Uploading " + (i + 1) + "/" + items.length);
    let got = await uploadTreeFile(dir, items[i], replace === true);
    if (got === "exists") {
      if (replace === null) {
        replace = await appConfirm(
          items[i].relPath + " is the first. Replace every file that is "
          + "already here, or keep them all and skip those?",
          { title: "Some of these files already exist", confirmLabel: "Replace all" });
      }
      got = replace ? await uploadTreeFile(dir, items[i], true) : "skipped";
    }
    if (got === "auth") { hideToast(); return; }
    if (got === "ok") done++;
    else if (got === "skipped") skipped++;
    else failed++;
  }
  // A folder with no file in it has nothing to upload, so it is made on its
  // own, one level at a time: mkdir makes only the last one, and the levels
  // above an empty folder can be as empty as it is. A level already there
  // answers 409, which here is the same as having made it.
  const have = new Set();
  for (const rel of empty) {
    let ok = true;
    const parts = rel.split("/");
    for (let n = 1; n <= parts.length && ok; n++) {
      const at = parts.slice(0, n).join("/");
      if (have.has(at)) continue;
      const res = await fsPost("api/fs/mkdir", { path: joinPath(dir, at) });
      if (!res) { hideToast(); return; }
      ok = res.ok || res.status === 409;
      if (ok) have.add(at);
    }
    if (ok) made++; else failed++;
  }
  if (!items.length && !failed) {
    toast("Created " + made + (made === 1 ? " empty folder" : " empty folders"));
  } else {
    const parts = ["Uploaded " + done + (done === 1 ? " file" : " files")];
    if (skipped) parts.push(skipped + " skipped");
    if (failed) parts.push(failed + " failed");
    toast(parts.join(", "), 4000);
  }
  if (done || made) { loadDir(filesPath); filesRefreshPeers(id); }
}

// One file of a tree: "ok", "exists" for the batch to decide on, "auth" when
// the token is gone and the batch has to stop, otherwise "failed". mkdirs=1 is
// what has the server make the file's folders on the way; a file sitting where
// one of them has to go comes back as a 409 of its own, which is a failure
// rather than a file to replace.
async function uploadTreeFile(dir, it, overwrite) {
  const qs = "?path=" + encodeURIComponent(joinPath(dir, it.relPath))
             + "&mkdirs=1" + (overwrite ? "&overwrite=1" : "");
  try {
    const r = await fetch(apiURL("api/fs/upload" + qs),
                          { method: "POST", headers: authHeaders(), body: it.file });
    if (r.status === 401) { rejectToken(); return "auth"; }
    if (r.ok) return "ok";
    if (r.status === 409 && !overwrite) {
      const data = await r.json().catch(() => null);
      if (data && data.error === "exists") return "exists";
    }
    return "failed";
  } catch (e) {
    return "failed";
  }
}

// Returns null when the file landed, "" when nothing more should be said, and
// otherwise the message for whatever went wrong — the batch decides whether
// that surfaces per file or as one summary.
async function uploadFile(it, overwrite) {
  const f = it.file;
  const target = joinPath(filesPath, it.relPath);
  try {
    const qs = "?path=" + encodeURIComponent(target) + (overwrite ? "&overwrite=1" : "");
    // The body is the file itself — the raw-body shape /api/fs/upload shares
    // with /api/transcribe.
    const r = await fetch(apiURL("api/fs/upload" + qs),
                          { method: "POST", headers: authHeaders(), body: f });
    if (r.status === 401) { rejectToken(); return ""; }
    if (r.status === 409 && !overwrite) {
      if (confirm(f.name + " already exists here. Replace it?")) return uploadFile(it, true);
      return "";
    }
    if (r.status === 413) return "Too large to upload (50 MB max)";
    if (!r.ok) throw new Error("HTTP " + r.status);
    return null;
  } catch (e) {
    return "Couldn't upload";
  }
}

// dragenter/dragleave fire once per element crossed, children included, so the
// listing's highlight rides a depth count rather than the last event seen.
let filesDropDepth = 0;

// Clears both highlights at once — what dragend and every drop end on, so a
// drag that ends anywhere at all leaves nothing lit behind it.
function clearDropHints() {
  filesDropDepth = 0;
  filesDropZone.classList.remove("drop-files");
  for (const n of document.querySelectorAll(".drop-into")) n.classList.remove("drop-into");
}

// The source half: every row and tile, wired by wireRow.
function wireEntryDrag(node, entry) {
  node.draggable = true;
  node.addEventListener("dragstart", (ev) => {
    // A listing read at a ref is a commit, not the disk. Nothing in it moves,
    // so the drag never starts.
    if (refFor(filesPath)) { ev.preventDefault(); return; }
    filesDragEntry = { name: entry.name, type: entry.type, dir: filesPath,
                       from: id };
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData(FILES_DRAG_TYPE, entry.name);
    ev.dataTransfer.setData("text/plain", joinPath(filesPath, entry.name));
  });
  node.addEventListener("dragend", () => {
    filesDragEntry = null;
    clearDropHints();
    // A drag the browser cancels can still be followed by a click on the row,
    // and that click is not a tap. Same stamp the long press sets, read by the
    // same check in wireRow.
    filesPressedAt = Date.now();
  });
}

// Whether the entry in flight can land in `dir`. Everything that would be a
// no-op or a nonsense move is refused here, and this same answer is what
// decides whether the target lights up — nothing offers a drop it will refuse.
function canMoveInto(dir, dt) {
  const d = filesDragEntry;
  if (!d || !dir || !hasDragType(dt, FILES_DRAG_TYPE)) return false;
  // Read-only at a ref — this listing is where the entry would land.
  if (refFor(filesPath)) return false;
  // And the listing it was picked up in has to still be that folder, whichever
  // of the two panes it is: a drag crossing to the other listing is a move like
  // any other, and one whose own listing navigated under it is not.
  const src = filesPanes[d.from];
  if (!src || src.path() !== d.dir) return false;
  const from = joinPath(d.dir, d.name);
  // Where it already is (the current folder's crumb), itself, or its own
  // subtree — a folder cannot be moved inside itself.
  return dir !== d.dir && dir !== from && !dir.startsWith(from + "/");
}

// The destination half: a folder row, a folder tile, an ancestor crumb.
function wireMoveTarget(node, dir) {
  node.addEventListener("dragover", (ev) => {
    if (!canMoveInto(dir, ev.dataTransfer)) return;
    ev.preventDefault();          // the one thing that makes this a drop target
    ev.dataTransfer.dropEffect = "move";
    node.classList.add("drop-into");
  });
  node.addEventListener("dragleave", () => node.classList.remove("drop-into"));
  node.addEventListener("drop", (ev) => {
    node.classList.remove("drop-into");
    if (!canMoveInto(dir, ev.dataTransfer)) return;
    ev.preventDefault();
    ev.stopPropagation();         // not also an upload into the folder on screen
    moveEntry(filesDragEntry, dir);   // read now: dragend is about to clear it
  });
}

// One rename, so the backend's refusal to clobber arrives as the same 409 the
// rename sheet already reads.
async function moveEntry(d, dir) {
  if (!d) return;
  const res = await fsPost("api/fs/rename", {
    src: joinPath(d.dir, d.name),
    dst: joinPath(dir, d.name),
  });
  if (!res) return;
  if (!res.ok) {
    toast(res.status === 409 ? "Something with that name already exists there"
                             : "Couldn't move");
    return;
  }
  toast("Moved " + d.name + " to " + baseName(dir));
  loadDir(filesPath);
  filesRefreshPeers(id);
  // Dropped from the other listing, the folder it came from is a folder short an
  // entry, wherever it is on screen.
  if (d.dir !== filesPath) filesRefreshPeers(id, d.dir);
}

// The whole scroll area rather than the rows, so an empty folder takes a drop
// on its "Empty folder" line as readily as a full one takes it on a row. One
// element for both shapes: the docked pane renders into this same listing.
const filesDropZone = root.querySelector(".files-wrap");

// An upload drag, as opposed to a row's own drag passing over the listing on
// its way to a folder, or a ref browse where there is nothing to write to.
function isUploadDrag(ev) {
  return !touchOnly() && !refFor(filesPath)
         && !hasDragType(ev.dataTransfer, FILES_DRAG_TYPE)
         && hasDragType(ev.dataTransfer, "Files");
}

filesDropZone.addEventListener("dragenter", (ev) => {
  if (!isUploadDrag(ev)) return;
  ev.preventDefault();
  filesDropDepth++;
  filesDropZone.classList.add("drop-files");
});
filesDropZone.addEventListener("dragover", (ev) => {
  if (!isUploadDrag(ev)) return;
  ev.preventDefault();
  ev.dataTransfer.dropEffect = "copy";
  filesDropZone.classList.add("drop-files");   // a dragenter the browser skipped
});
filesDropZone.addEventListener("dragleave", () => {
  if (--filesDropDepth <= 0) {
    filesDropDepth = 0;
    filesDropZone.classList.remove("drop-files");
  }
});
filesDropZone.addEventListener("drop", (ev) => {
  clearDropHints();
  if (!isUploadDrag(ev)) return;
  ev.preventDefault();
  if (demoApiOn()) { toast("Not in the demo"); return; }
  const { files, dirs } = droppedEntries(ev.dataTransfer);
  const items = files.map((f) => ({ file: f, relPath: f.name }));
  if (dirs.length && hasCapStrict("upload_dirs")) { uploadDropped(items, dirs); return; }
  // An older server cannot make a tree's folders, so its folders are left out
  // as they always were — and said so when they were all the drop had.
  if (items.length) uploadFiles(items);
  else toast(dirs.length ? "Update the server to upload folders"
                         : "Folders can't be dropped");
});

async function uploadDropped(items, dirs) {
  holdToast("Reading " + (dirs.length === 1 ? dirs[0].name : dirs.length + " folders"));
  const into = { items, empty: [], unreadable: 0 };
  try {
    for (const d of dirs) await walkDroppedDir(d, "", into);
  } catch (e) {
    toast("Couldn't read the folder");
    return;
  }
  hideToast();
  if (!into.items.length && !into.empty.length) {
    toast(into.unreadable ? "Couldn't read the folder" : "Nothing to upload");
    return;
  }
  uploadFiles(into.items, into.empty, into.unreadable);
}

// ---- navigation chrome -----------------------------------------------------

q("btn-files-back").addEventListener("click", filesBack);

// Straight back to the terminal, however deep the browsing went. Not
// closeExplorer() directly: the explorer's history entries would stay on the
// stack behind the terminal, and the terminal's own back would then spend them
// one by one going nowhere. Unwinding through history instead leaves the stack
// exactly where pressing back at every level would have.
function jumpToTerminal() {
  // The field's own back is spent on closing it; take it out of the way first
  // so the pops below all count as folders.
  if (q("files-path-wrap").classList.contains("editing")) closePathEdit();
  // Every entry the explorer owns: openExplorer's push plus one per level
  // navigated into, which is filesStack.length — one at the entry folder, and
  // still one if the entry folder never resolved and the stack stayed empty.
  filesClosing = true;
  history.go(-(filesStack.length || 1));
}
q("btn-files-term").addEventListener("click", jumpToTerminal);

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
  if (!root.classList.contains("active")) return;
  // A file open in the pane is the top thing to dismiss, and it goes back to
  // the listing rather than closing the pane — the in-pane back arrow's job.
  // Scoped to a press aimed inside it for the reason below. The viewer answers
  // its own Escape first (09-image-viewer.js) and stops the key there, so this
  // reaches it only where that handler stood aside.
  const view = dockedFileView();
  if (view) {
    // One of the three, and it belongs to one listing: only that listing's copy
    // of this handler may put it away, or two of them would answer one press.
    if (filesViewOwner !== id) return;
    if (!view.contains(e.target)) return;
    // Not out of the buffer: inside CodeMirror the key is the document's own —
    // vim's way out of insert mode, and the only way out there is.
    if (view.id === "screen-editor" && $("editor-host").contains(e.target)) return;
    e.preventDefault();
    closeDockedFileView();
    return;
  }
  // Docked, the terminal beside the pane is live and Escape is one of its
  // keys — so only a press aimed inside the pane is the pane's to answer.
  if (filesDocked && !root.contains(e.target)) return;
  // Ahead of the origin check: an open dropdown is the top thing to dismiss,
  // and it is there to dismiss whether or not a terminal is behind.
  if (q("files-view-wrap").classList.contains("open")
      || q("files-ref-wrap").classList.contains("open")) {
    e.preventDefault();
    closeFilesMenus();
    return;
  }
  if (filesOrigin !== "screen-term") return;          // nothing to jump back to
  if (q("files-path-wrap").classList.contains("editing")) return;
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
  // Only the markup's own pane is ever the whole window, and only that shape
  // pushes anything: a copy is a row of the column, which owns no entry, so no
  // pop is ever a copy's to answer.
  if (id !== "files") return;
  // Docked, these two pushed nothing either (dockFileView above), so a pop is
  // never theirs: it is the terminal's, and the terminal's own handler takes
  // it — the pane and the file in it go down together with the session.
  if (!dockedFileView()) {
    if ($("screen-editor").classList.contains("active")) { editorPopped(); return; }
    if ($("screen-reader").classList.contains("active")) { closeReader(); return; }
  }
  // The docked pane pushed nothing, so no pop is ever its own: this one is the
  // terminal's, and closeTerminal() above has already spent it.
  if (filesDocked) return;
  if (!root.classList.contains("active")) return;
  // A go(-n) past several entries arrives as one popstate, not n of them, so
  // the per-level unwind below would land on the folder one up while history
  // already sits behind the whole view. The flag is the button saying this pop
  // is the explorer leaving, whatever depth it left from — and closing here
  // rather than before the go() keeps #screen-term inactive until the pop is
  // spent, so the terminal's own popstate handler stays out of it.
  if (filesClosing) { filesClosing = false; closeExplorer(); return; }
  // The media viewer over the explorer pushes no entry of its own (it is an
  // overlay), so the back that meant "close this picture" has spent the
  // folder's: put the picture away and push that entry back, the dirty
  // editor's trick (editorPopped), rather than climbing a folder or closing
  // the explorer with the picture still on screen.
  if ($("viewer").classList.contains("show")) {
    hideImage();
    history.pushState(filesEntryState(), "", location.href);
    return;
  }
  // An open address field takes the back first — closeExplorer turns that one
  // into closing just the field, and re-pushes the entry the pop consumed.
  if (q("files-path-wrap").classList.contains("editing")) { closeExplorer(); return; }
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

attachEdgeSwipe(root, filesBack);

// Pull-down to refresh — the session list's own pattern.
(function() {
  let startY = null;
  const scr = root;
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

// The same reload as a key on the tool row, since a pointer cannot pull. It
// spins for as long as the listing takes, and never shorter than a glance, so
// a folder that answers at once still shows it was asked. The listing's
// scroll is put back after, on whichever of the page or the pane scrolls it.
q("btn-files-refresh").addEventListener("click", async () => {
  const btn = q("btn-files-refresh");
  if (!filesPath || btn.classList.contains("spin")) return;
  const pageY = window.scrollY, paneY = filesDropZone.scrollTop;
  btn.classList.add("spin");
  const [ok] = await Promise.all([loadDir(filesPath),
                                  new Promise((r) => setTimeout(r, 400))]);
  btn.classList.remove("spin");
  if (!ok) return;
  if (!filesDocked) window.scrollTo(0, pageY);
  filesDropZone.scrollTop = paneY;
});

// What the column and the rest of the app can ask of this pane. Everything else
// in the body above is the pane's own and stays in the closure.
const api = {
  id: id,
  isDocked: () => filesDocked,
  isOpen: () => root.classList.contains("active"),
  path: () => filesPath,
  origin: () => filesOrigin,
  setActive: (on) => root.classList.toggle("active", on),
  open: openExplorer,
  openPath: openPath,
  openAtCwd: filesOpenAtCwd,
  followSession: followSession,
  followCwd: followPaneCwd,
  afterEnter: scheduleCwdAfterEnter,
  afterTitle: scheduleCwdAfterTitle,
  closeDocked: closeDockedFiles,
  closeFull: closeExplorer,
  closePathEdit: closePathEdit,
  entryCount: filesEntryCount,
  reloadAt: (path) => { if (filesPath && filesPath === path) loadDir(filesPath); },
  load: loadDir,
  readText: fsReadText,
  canDownload: canDownload,
  toggleExpanded: () => filesSetExpanded(!filesExpanded),
  stash: filesStash,
  restore: filesRestore,
  teardown: filesTeardown,
  reset: filesReset,
  syncRefCap: syncRefCap,
  syncThumbsCap: syncThumbsCap,
  sheet: { edit: sheetEdit, rename: sheetRename,
           download: sheetDownload, remove: sheetDelete },
  add: { file: addNewFile, folder: addNewFolder },
  uploadPicked: uploadFiles,
};
filesPanes[id] = api;
return api;

}

// ------------------------------------------------------------
// The panes, and the app's way in to them
// ------------------------------------------------------------

// The listing the markup ships, which is the one every existing way in opens and
// the only one that is ever the whole window.
makeFilesPane("files", $("screen-files"));

// The second one, made out of the first: the copy is the whole shape — the bar,
// the crumbs, both dropdowns and their scrim, and the list — with the ids inside
// it duplicated, which is why the factory reaches them through its own root
// rather than through the document. What does not come over is what belonged to
// the pane it was copied from: the folder it had listed, the crumbs over it, its
// suggestions and the classes the column had given it. It goes in right after
// the original, so it is a following sibling of #screen-term exactly as the
// original is, which is what the stylesheet's rules are written against.
function filesMakeAt(id) {
  if (id !== "files#2" || filesPanes[id]) return null;
  const src = $("screen-files");
  const clone = src.cloneNode(true);
  clone.id = "screen-files-2";
  clone.classList.remove("active", "docked",
                         "side-top", "side-bot", "side-hidden");
  for (const name of ["files-list", "files-crumbs", "files-suggest"]) {
    clone.querySelector("#" + name).innerHTML = "";
  }
  clone.querySelector("#files-list").classList.remove("files-grid");
  clone.querySelector("#files-path-wrap").classList.remove("editing");
  clone.querySelector("#files-suggest").classList.remove("show");
  clone.querySelector("#files-suggest-scrim").classList.remove("show");
  clone.querySelector("#files-view-scrim").classList.remove("show");
  clone.querySelector("#files-view-wrap").classList.remove("open");
  clone.querySelector("#files-ref-wrap").classList.remove("open");
  clone.querySelector("#files-ref-wrap").hidden = true;
  clone.querySelector("#files-ref-ro").hidden = true;
  clone.querySelector("#btn-files-term").style.display = "none";
  clone.querySelector("#files-empty").style.display = "none";
  clone.querySelector("#files-error").style.display = "none";
  const wrap = clone.querySelector(".dock-split-wrap");
  wrap.classList.remove("open");
  wrap.querySelector(".dock-split").setAttribute("aria-expanded", "false");
  wrap.querySelector(".dock-split-menu").textContent = "";
  src.after(clone);
  sideWireBar(clone);
  return makeFilesPane(id, clone);
}

// The pane a row id names, made the first time that row is asked for and kept
// from then on — a copy that has been closed is a copy that can be opened again,
// and making a fresh one would throw away the folder it was reading.
function filesPaneAt(id) {
  return filesPanes[id] || filesMakeAt(id);
}

// How the column opens a second one when the split menu asks for it.
sideMakers.files = (id) => !!filesPaneAt(id);

// ---- the ways in -----------------------------------------------------------

// A folder asked for by name, in the listing a press last named: the folder
// button on the session list, a path tapped in the terminal, the editor's parent
// folder. With nothing pressed in it is the markup's own pane.
function openExplorer(path, opts) {
  const pane = filesPanes[sideFocusedOf("files")] || filesPanes.files;
  return pane.open(path, opts);
}

function openPathInExplorer(path) {
  const pane = filesPanes[sideFocusedOf("files")] || filesPanes.files;
  return pane.openPath(path);
}

// The folder key's own way in and out. Docked, it is a toggle: the pane it
// opened is the pane it puts away, and which pane that is is the column's answer
// rather than this module's — with two explorer rows it is the one last pressed
// in (sideFocusedOf, 26-side-pane.js). Full screen there is nothing to toggle —
// back is how that one leaves — so the opener below stays the way in for
// everything else, including the split menu, which never toggles.
function openFilesAtCwd() {
  const id = sideFocusedOf("files");
  if (id) { closeDockedFiles(id); return; }
  return filesOpenAtCwd("files");
}

// Which of the column's explorer rows a call is about. The id names a slot
// rather than a pane that must already exist: the split menu asks for the second
// one, and opening it is what makes it.
function filesOpenAtCwd(id, opts) {
  const pane = filesPaneAt(id || "files");
  return pane ? pane.openAtCwd(opts) : undefined;
}

// A row a remembered column names with no folder to put in it, made if the id
// names the copy: the explorer's half of that record is a claim on the slot
// rather than a folder, and the pane opens at the session's own cwd
// (followSession, in the factory).
function filesFollowSession(id) {
  const pane = filesPaneAt(id || "files");
  return pane ? pane.followSession() : undefined;
}

// One row of the column, or every one of them: the narrow-layout fallback and
// the profile switch mean all, the registry and the split menu's eviction mean
// one. A close for a pane that was never made is nothing to do.
function closeDockedFiles(id) {
  if (id) {
    const pane = filesPanes[id];
    if (pane) pane.closeDocked();
    return;
  }
  for (const pane of Object.values(filesPanes)) pane.closeDocked();
}

// The shapes and the state only the markup's own pane can be in: the whole
// window, and the history entries that shape owns.
function closeExplorer() { filesPanes.files.closeFull(); }
function filesEntryCount() { return filesPanes.files.entryCount(); }
function filesFullOrigin() { return filesPanes.files.origin(); }

// Both of them: an address field open in either listing is one the switch away
// from this session closes.
function closePathEdit() {
  for (const pane of Object.values(filesPanes)) pane.closePathEdit();
}

// The terminal's cwd is one terminal's, and one pane follows it: the markup's
// own, which is the one the folder key, the boot restore and a tapped path open.
// A copy is the user's second place to look, and two listings jumping on every
// cd is the thing that would make the split not worth having.
function followPaneCwd() { return filesPanes.files.followCwd(); }
function scheduleCwdAfterEnter() { filesPanes.files.afterEnter(); }
function scheduleCwdAfterTitle() { filesPanes.files.afterTitle(); }

// The server's answers, which are about the computer rather than about a pane,
// so every listing hears them.
function syncRefCap() {
  for (const pane of Object.values(filesPanes)) pane.syncRefCap();
}
function syncThumbsCap() {
  for (const pane of Object.values(filesPanes)) pane.syncThumbsCap();
}

// A switch to another computer: every listing goes, and the folders they cached
// go with them — a folder on that machine is not a folder on this one.
function filesResetForProfile() {
  for (const pane of Object.values(filesPanes)) pane.reset();
  filesListCache.clear();
}

// ---- reading and writing, in the listing the caller came from ---------------
// The editor, the reader and the viewer ask these of "the explorer" and mean the
// listing their file was opened from, which is what filesActive() answers: the
// press that opened the file is what set the focus these read.

function fsReadText(path) { return filesActive().readText(path); }
function canDownload(path) { return filesActive().canDownload(path); }
function loadDir(path) { return filesActive().load(path); }

// ---- the rail's stash (see fileViews in 09-image-viewer.js) -----------------
// Both listings go into the record, each under its own name, and the owner of
// whatever file view was over them goes in with them: two rows and one editor,
// so a restore that did not know whose it was would put it back in the wrong one.

function filesStash(id) {
  const pane = filesPanes[id];
  return pane && pane.isOpen() ? pane.stash() : null;
}

function filesRestore(a, b, owner) {
  if (a) filesPanes.files.restore(a);
  if (b) {
    const pane = filesPaneAt("files#2");
    if (pane) pane.restore(b);
  }
  filesSetViewOwner(filesPanes[owner]);
}

function filesTeardown() {
  for (const pane of Object.values(filesPanes)) pane.teardown();
  filesSetViewOwner(null);
}

// ---- the keys and sheets that are the app's rather than a pane's ------------

// The session list's folder button, which opens at $HOME.
$("btn-files").addEventListener("click", () => openExplorer(""));

// The two sheets: one set of rows over the window however many listings there
// are, so each row acts on the listing that raised the sheet (filesSheetOwner).
$("btn-file-edit").addEventListener("click", () => filesSheetPane().sheet.edit());
$("btn-file-rename").addEventListener("click", () => filesSheetPane().sheet.rename());
$("btn-file-download").addEventListener("click", () => filesSheetPane().sheet.download());
$("btn-file-delete").addEventListener("click", () => filesSheetPane().sheet.remove());
$("btn-files-newfile").addEventListener("click", () => filesSheetPane().add.file());
// The right-click menu's rows are the sheet's, on the listing that raised it.
$("ctx-file-edit").addEventListener("click", () => { filesHideMenu(); filesSheetPane().sheet.edit(); });
$("ctx-file-rename").addEventListener("click", () => { filesHideMenu(); filesSheetPane().sheet.rename(); });
$("ctx-file-download").addEventListener("click", () => { filesHideMenu(); filesSheetPane().sheet.download(); });
$("ctx-file-delete").addEventListener("click", () => { filesHideMenu(); filesSheetPane().sheet.remove(); });
// Anything that is not a press inside the menu puts it away: a press elsewhere
// (which still does what it does, so a right-click on another row moves the
// menu there), a scroll or wheel, a resize, the window losing focus. On the
// window, in capture, so it runs ahead of every document-level handler — and
// for Escape that matters: the explorer's own Escape would otherwise read the
// press as "back to the terminal".
window.addEventListener("pointerdown", (e) => {
  if (filesMenuOpen() && !$("file-ctx-menu").contains(e.target)) filesHideMenu();
}, true);
window.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !filesMenuOpen()) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  filesHideMenu();
}, true);
for (const type of ["scroll", "wheel", "resize", "blur"]) {
  window.addEventListener(type, () => { if (filesMenuOpen()) filesHideMenu(); },
                          { capture: true, passive: true });
}
$("btn-files-newfolder").addEventListener("click", () => filesSheetPane().add.folder());
$("btn-files-upload").addEventListener("click", () => {
  showSheet(false);
  $("files-upload-input").click();
});
$("files-upload-input").addEventListener("change", (ev) => {
  // The FileList is live and empties with the input, so copy it out before the
  // reset that lets the same pick fire change again.
  const files = Array.from(ev.target.files || []);
  ev.target.value = "";
  if (files.length) {
    filesSheetPane().uploadPicked(files.map((f) => ({ file: f, relPath: f.name })));
  }
});
$("btn-files-upload-dir").addEventListener("click", () => {
  showSheet(false);
  $("files-upload-dir-input").click();
});
// webkitRelativePath already starts with the picked folder's own name, the
// shape a drop's walk builds. A picker that ignored webkitdirectory (a browser
// the row's test did not catch) hands over plain files with no relative path,
// and those upload flat, as the Upload files row would have.
$("files-upload-dir-input").addEventListener("change", (ev) => {
  const files = Array.from(ev.target.files || []);
  ev.target.value = "";
  if (!files.length) { toast("Nothing to upload"); return; }
  filesSheetPane().uploadPicked(
    files.map((f) => ({ file: f, relPath: f.webkitRelativePath || f.name })));
});

// The pane's two controls in the bar of each file view. One editor, one reader
// and one viewer for the two rows, so these are wired once and act on the
// listing the file in them was opened from.
for (const name of ["screen-editor", "screen-reader", "viewer"]) {
  for (const btn of $(name).querySelectorAll(".dock-expand")) {
    btn.addEventListener("click", () => {
      const pane = filesPanes[filesViewOwner];
      if (!pane) return;
      pane.toggleExpanded();
      refit(0);
    });
  }
  // The cross over a file closes the file, not the pane: the listing it was
  // opened from is right there underneath, and the listing's own cross is the
  // way out of the pane. The same close as the back arrow and Escape, so an
  // unsaved buffer gets its question either way.
  for (const btn of $(name).querySelectorAll(".dock-close")) {
    btn.addEventListener("click", () => closeDockedFileView());
  }
}

// A file dropped anywhere but a listing would otherwise navigate the window to
// it, throwing the session away. While an explorer is up that default is
// swallowed document-wide — and only that: nothing is read off the drop, nothing
// lights up, and no other handler is stopped.
for (const type of ["dragover", "drop"]) {
  document.addEventListener(type, (ev) => {
    if (!filesAnyOpen()) return;
    if (hasDragType(ev.dataTransfer, "Files")) ev.preventDefault();
  });
}

// The two full-screen file views' own edge swipe. Theirs rather than a pane's:
// seated in a pane they push no entry and there is nothing for a swipe to unwind.
attachEdgeSwipe($("screen-editor"), () => history.back());
attachEdgeSwipe($("screen-reader"), () => history.back());
