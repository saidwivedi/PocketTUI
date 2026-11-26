// ============================================================
// Offline-safe editing: drafts
// ============================================================
// A draft is an unsaved buffer kept on this device in IndexedDB, one record
// per (computer, path), with the version it was loaded from. Opening the file
// again on any surface puts it back, still based on that version, so the save
// after a reload goes through the editor's one rule like any other: a write
// carries the hash of the version it is based on (edViewSave, 29-editor.js;
// app.py checks it under its write lock).
//
// Module state is `var`, not `let`: removeProfile (02-debug-log.js) and the
// sheet code in earlier fragments call into here, and a `let` read before this
// fragment has run is a temporal-dead-zone throw that stops the whole shell.

var DRAFT_DB = "ptui-drafts";
var DRAFT_STORE = "drafts";
// app.py's MAX_TEXT_BYTES, the editor's own open limit: a buffer past it is
// one the editor could not have opened from the computer either.
var DRAFT_MAX_BYTES = 2 * 1024 * 1024;
var DRAFT_DEBOUNCE_MS = 300;
var SAVE_TIMEOUT_MS = 15000;

var draftDbP = null;          // Promise<IDBDatabase|null>, opened on first use
var draftSaidBroken = false;  // the "not kept" line, once per page load
var draftPending = new Set(); // recs with a debounced write not yet made
var draftPaintTimer = 0;
var edParked = new Set();     // editor-screen recs stashed with their session

// ---- whose and which -------------------------------------------------------

// The computer a draft belongs to: the profile and the address it is reached
// at, so a profile pointed at another machine never picks up the first one's
// drafts. Null in the demo, which keeps nothing.
function draftOwner() {
  if (demoApiOn()) return null;
  const addr = cfg.backend ? cfg.backend.replace(/\/$/, "") : location.origin + BASE;
  return (activeProfileId() || "-") + "|" + addr;
}

function draftKey(owner, path) { return owner + "\n" + path; }
function draftKeyOf(rec) { return rec.owner ? draftKey(rec.owner, rec.path) : null; }
function draftProfileOf(owner) { return owner.slice(0, owner.indexOf("|")); }

// A rec handed on to a new view (edRestore) is followed to it, so a write that
// lands after a session switch updates the buffer that is there now.
function edCurrent(rec) {
  while (rec.next) rec = rec.next;
  return rec;
}

function edText(rec) { return rec.view.state.doc.toString(); }

// ---- the store ---------------------------------------------------------------

function draftFail(e) {
  if (e) dbg("drafts:", e && e.message ? e.message : e);
  if (draftSaidBroken) return;
  draftSaidBroken = true;
  toast("Unsaved edits are not being kept on this device", 4500);
}

function draftDb() {
  if (draftDbP) return draftDbP;
  draftDbP = new Promise((resolve) => {
    let req;
    try {
      req = indexedDB.open(DRAFT_DB, 1);
    } catch (e) { resolve(null); return; }
    req.onupgradeneeded = () => {
      const st = req.result.createObjectStore(DRAFT_STORE, { keyPath: "key" });
      st.createIndex("owner", "owner");
      st.createIndex("profile", "profile");
    };
    req.onsuccess = () => {
      const db = req.result;
      // Another tab of a newer build upgrading the store: let it.
      db.onversionchange = () => { db.close(); draftDbP = null; };
      resolve(db);
    };
    req.onerror = () => resolve(null);
    req.onblocked = () => resolve(null);
  });
  return draftDbP;
}

// One transaction; `body(store, set)` queues its requests and hands the
// answer to `set`. Rejects when there is no store or the transaction fails.
async function draftTx(mode, body) {
  const db = await draftDb();
  if (!db) throw new Error("IndexedDB unavailable");
  return new Promise((resolve, reject) => {
    let tx, out;
    try { tx = db.transaction(DRAFT_STORE, mode); } catch (e) { reject(e); return; }
    try { body(tx.objectStore(DRAFT_STORE), (v) => { out = v; }); } catch (e) { reject(e); return; }
    tx.oncomplete = () => resolve(out);
    tx.onerror = tx.onabort = () => reject(tx.error || new Error("transaction aborted"));
  });
}

async function draftGet(owner, path) {
  if (!owner) return null;
  try {
    return await draftTx("readonly", (st, set) => {
      const r = st.get(draftKey(owner, path));
      r.onsuccess = () => set(r.result || null);
    });
  } catch (e) { draftFail(e); return null; }
}

async function draftList(owner) {
  if (!owner) return [];
  try {
    return await draftTx("readonly", (st, set) => {
      const r = st.index("owner").getAll(owner);
      r.onsuccess = () => set(r.result || []);
    });
  } catch (e) { draftFail(e); return []; }
}

// Deletes the record, or with `text` only while the record still holds that
// text: a save answered for an older text must not take a newer draft with it.
async function draftDel(owner, path, text) {
  if (!owner) return;
  try {
    await draftTx("readwrite", (st) => {
      const key = draftKey(owner, path);
      if (text === undefined) { st.delete(key); return; }
      const r = st.get(key);
      r.onsuccess = () => { if (r.result && r.result.text === text) st.delete(key); };
    });
  } catch (e) { draftFail(e); }
  draftsPaintSoon();
}

// A forgotten computer's drafts go with it (removeProfile, 02-debug-log.js).
async function draftDropProfile(id) {
  if (!id) return;
  try {
    await draftTx("readwrite", (st) => {
      const r = st.index("profile").openCursor(IDBKeyRange.only(id));
      r.onsuccess = () => {
        const c = r.result;
        if (!c) return;
        c.delete();
        c.continue();
      };
    });
  } catch (e) { dbg("drafts: drop profile failed", e); }
  draftsPaintSoon();
}

// ---- keeping a buffer --------------------------------------------------------

// A doc change in a rec that keeps drafts (29-editor.js's update listener).
function draftTouch(rec) {
  if (!rec.owner || rec.readOnly) return;
  clearTimeout(rec.draftTimer);
  draftPending.add(rec);
  rec.draftTimer = setTimeout(() => draftWrite(rec), DRAFT_DEBOUNCE_MS);
}

function draftBytes(text) {
  // Every UTF-16 unit is at most three UTF-8 bytes, so most texts are settled
  // without encoding them.
  if (text.length * 3 <= DRAFT_MAX_BYTES) return text.length;
  return new TextEncoder().encode(text).length;
}

// The rec's buffer into its record, now. A clean buffer has nothing to keep,
// and a closed or discarded one is not this device's to keep any more.
async function draftWrite(rec) {
  clearTimeout(rec.draftTimer);
  draftPending.delete(rec);
  if (!rec.owner || rec.readOnly || rec.discarded || !rec.dirty) return;
  if (rec.closed && !rec.parked) return;
  const text = edText(rec);
  if (draftBytes(text) > DRAFT_MAX_BYTES) {
    if (!rec.draftBig) {
      rec.draftBig = true;
      toast(baseName(rec.path) + " is too large to keep an unsaved copy on this device", 4000);
    }
    return;
  }
  rec.draftBig = false;
  const record = {
    key: draftKeyOf(rec), owner: rec.owner, profile: draftProfileOf(rec.owner),
    path: rec.path, baseHash: rec.hash, baseText: rec.baseText == null ? null : rec.baseText,
    text: text, updatedAt: Date.now(),
  };
  try {
    await draftTx("readwrite", (st) => { st.put(record); });
  } catch (e) { draftFail(e); return; }
  draftsPaintSoon();
}

// Every buffer with edits, written now. On the way out (hidden, pagehide) the
// debounce may never fire: iOS suspends a backgrounded page and Chrome for
// Android discards one. All of them rather than only the pending ones, because
// another page of this app may have saved the shared record away meanwhile.
function draftFlushAll() {
  for (const rec of edLive) if (rec.dirty) draftWrite(rec);
  for (const rec of edParked) if (rec.dirty) draftWrite(rec);
}

// An explicit discard: the record, the marks and any pending write go.
function draftDiscard(rec) {
  rec.discarded = true;
  clearTimeout(rec.draftTimer);
  draftPending.delete(rec);
  rec.restored = null;
  if (rec.owner) draftDel(rec.owner, rec.path);
}

// The record for a file about to be opened, or null. Asked before the view is
// built, so nothing typed into it can race the answer.
async function draftLookup(path) {
  return draftGet(draftOwner(), path);
}

// A freshly built rec gets its file's draft, if there is one: the draft's
// text in the buffer as an edit (undo goes back to the computer's version),
// still based on the version it was made from. `loaded` is what the computer
// sent just now, {content, hash}.
function draftApply(rec, loaded, d) {
  rec.loaded = { content: loaded.content, hash: loaded.hash };
  rec.baseText = loaded.content;
  if (!d || rec.readOnly || !rec.owner) return;
  // The computer already has this exact text: an earlier save landed and its
  // answer was lost. That is the write the draft was waiting for.
  if (d.text === loaded.content) {
    draftDel(rec.owner, rec.path, d.text);
    return;
  }
  rec.hash = d.baseHash || "";
  rec.baseText = d.baseText;
  rec.view.dispatch({ changes: { from: 0, to: rec.view.state.doc.length, insert: d.text } });
  rec.restored = { stale: (d.baseHash || "") !== (loaded.hash || "") };
  edPaintState(rec);
}

// A buffer set to a version of the computer's, clean. The dispatch's own
// update marks it dirty; the flag is put back after it.
function edReplaceClean(rec, text, hash) {
  rec.view.dispatch({ changes: { from: 0, to: rec.view.state.doc.length, insert: text } });
  rec.hash = hash;
  rec.baseText = text;
  rec.loaded = { content: text, hash: hash };
  clearTimeout(rec.draftTimer);
  draftPending.delete(rec);
  rec.dirty = false;
  rec.onDirty(rec, false);
}

// ---- reading and landing -------------------------------------------------------

// The file as the computer has it now: {content, hash}, {missing: true}, or
// {unreadable: true} for one the editor could not show; null when the
// computer did not answer.
async function edReadCurrent(path) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), SAVE_TIMEOUT_MS);
  try {
    const r = await fetch(apiURL("api/fs/read?path=" + encodeURIComponent(path)),
                          { cache: "no-store", headers: authHeaders(), signal: ctl.signal });
    const data = await r.json().catch(() => null);
    if (r.status === 404) return { missing: true, content: "", hash: "" };
    if (r.status === 401 || r.status >= 500) return null;
    if (!r.ok || !data || typeof data.content !== "string") return { unreadable: true };
    return { content: data.content, hash: data.hash };
  } catch (e) {
    return null;
  } finally {
    clearTimeout(timer);
  }
}


// The answer to a write whose body was `sent`, landed with `hash`. Clean only
// if the buffer still says what was sent: text typed while the write was out
// stays an edit, now based on the version that landed.
function edLanded(rec, sent, hash) {
  rec = edCurrent(rec);
  rec.hash = hash;
  rec.baseText = sent;
  rec.loaded = { content: sent, hash: hash };
  rec.restored = null;
  if (edText(rec) === sent) {
    clearTimeout(rec.draftTimer);
    draftPending.delete(rec);
    rec.dirty = false;
    rec.onDirty(rec, false);
    if (rec.owner) draftDel(rec.owner, rec.path, sent);
  } else {
    draftWrite(rec);
  }
  edPaintState(rec);
  draftsPaintSoon();
}

// ---- buffers put away with their session ------------------------------------

// The editor screen's buffer coming back (edRestore): the new rec takes over
// the parked one's save state, and a write still out for the parked one lands
// in the new one (edCurrent).
function draftAdopt(old, rec) {
  edParked.delete(old);
  old.parked = false;
  old.next = rec;
  rec.hash = old.hash;
  rec.baseText = old.baseText;
  rec.loaded = old.loaded;
  rec.restored = old.restored;
  rec.draftBig = old.draftBig;
  if (old.dirty !== rec.dirty) { rec.dirty = old.dirty; rec.onDirty(rec, old.dirty); }
  // Its draft was discarded from the pill while it was away.
  if (old.revert && rec.loaded) edReplaceClean(rec, rec.loaded.content, rec.loaded.hash);
  edPaintState(rec);
}

// A parked buffer whose session went for good (releaseFileView,
// 09-image-viewer.js): its draft stays in the store.
function draftReleaseParked(rec) {
  if (!rec || !edParked.has(rec)) return;
  edParked.delete(rec);
  rec.parked = false;
  rec.closed = true;
}


// ---- the strip over a buffer ---------------------------------------------------

// One line above the text saying what is true of this buffer beyond "edited":
// that it was restored from this device. Painted into the rec's own host, so
// it rides with the view on every surface.
function edPaintState(rec) {
  rec = edCurrent(rec);
  if (rec.closed && !rec.parked) return;
  if (!rec.view || !rec.view.dom.parentNode) return;
  let strip = rec.strip;
  const host = rec.view.dom.parentNode;
  let msg = "", acts = [];
  if (rec.restored && rec.dirty) {
    msg = rec.restored.stale
      ? "Restored unsaved changes. The file has changed on your computer since; saving will ask which to keep."
      : "Restored unsaved changes from this device.";
    acts = [["Discard", "", () => edDiscardRestored(rec)],
            ["OK", "", () => { rec.restored = null; edPaintState(rec); }]];
  }
  if (!msg) {
    if (strip) { strip.remove(); rec.strip = null; rec.view.requestMeasure(); }
    return;
  }
  if (!strip) {
    strip = rec.strip = el("div", { class: "ed-strip", role: "status" });
  }
  strip.textContent = "";
  strip.appendChild(el("span", { class: "ed-strip-msg" }, msg));
  for (const [label, cls, run] of acts) {
    strip.appendChild(el("button", { type: "button", class: "ed-strip-btn " + cls, onclick: run }, label));
  }
  if (strip.parentNode !== host || strip.nextSibling !== rec.view.dom) {
    host.insertBefore(strip, rec.view.dom);
    rec.view.requestMeasure();
  }
}

async function edDiscardRestored(rec) {
  rec = edCurrent(rec);
  const ok = await appConfirm("Your edits to " + baseName(rec.path)
    + " that were kept on this device will be lost.",
    { title: "Discard the restored changes?", confirmLabel: "Discard" });
  if (!ok) return;
  rec = edCurrent(rec);
  if (rec.closed) return;
  draftDiscard(rec);
  rec.discarded = false;
  edReplaceClean(rec, rec.loaded.content, rec.loaded.hash);
  edPaintState(rec);
  draftsPaintSoon();
}

// ---- the unsaved-files pill ----------------------------------------------------

function draftsPaintSoon() {
  clearTimeout(draftPaintTimer);
  draftPaintTimer = setTimeout(draftsPaint, 120);
}

async function draftsPaint() {
  const btn = $("btn-drafts");
  const owner = draftOwner();
  const list = await draftList(owner);
  if (owner !== draftOwner()) return;
  btn.hidden = !list.length;
  if (!list.length) { showDraftsMenu(false); return; }
  $("drafts-count").textContent = list.length + " unsaved";
  btn.setAttribute("aria-label", list.length === 1 ? "1 unsaved file" : list.length + " unsaved files");
  if ($("drafts-wrap").classList.contains("open")) fillDraftsMenu(list);
}

function fillDraftsMenu(list) {
  const menu = $("drafts-menu");
  menu.textContent = "";
  list.sort((a, b) => b.updatedAt - a.updatedAt);
  for (const d of list) {
    let dir = d.path.slice(0, d.path.lastIndexOf("/") + 1) || "/";
    const home = typeof filesHome === "string" && filesHome ? filesHome.replace(/\/$/, "") + "/" : "";
    if (home && dir.startsWith(home)) dir = "~/" + dir.slice(home.length);
    // Isolated left-to-right inside the right-to-left box that elides it from
    // its start (styles.css), so the slashes stay where they are.
    dir = "\u2066" + dir + "\u2069";
    const row = el("div", { class: "view-row profile-row draft-row", role: "menuitem", tabindex: "0" },
      el("span", { class: "profile-menu-text" },
        el("span", { class: "profile-menu-name" }, baseName(d.path)),
        el("span", { class: "profile-menu-sub" },
          el("span", { class: "profile-menu-addr" }, dir),
          el("span", { class: "profile-menu-status" }, "Unsaved"))),
      el("button", {
        type: "button", class: "icon-btn profile-menu-trash draft-trash",
        "aria-label": "Discard unsaved changes to " + baseName(d.path),
        onclick: (e) => { e.stopPropagation(); draftDiscardFromMenu(d); },
      }, svgIcon("i-trash")));
    row.addEventListener("click", () => draftOpenFile(d));
    row.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      draftOpenFile(d);
    });
    menu.appendChild(row);
  }
}

function showDraftsMenu(on) {
  $("drafts-wrap").classList.toggle("open", on);
  $("drafts-scrim").classList.toggle("show", on);
  $("btn-drafts").setAttribute("aria-expanded", on ? "true" : "false");
  if (on) draftList(draftOwner()).then((list) => {
    if ($("drafts-wrap").classList.contains("open")) fillDraftsMenu(list);
  });
}

// The live views of a draft's file: the editor screen's, the workspace tabs'.
function draftLiveRecs(d) {
  return [...edLive].filter((r) => draftKeyOf(r) === d.key);
}

async function draftDiscardFromMenu(d) {
  showDraftsMenu(false);
  const ok = await appConfirm("Your edits to " + baseName(d.path)
    + " that were kept on this device will be lost.",
    { title: "Discard unsaved changes?", confirmLabel: "Discard" });
  if (!ok) return;
  await draftDel(d.owner, d.path);
  // A view of it that is open goes back to the computer's version it last
  // knew; one put away with its session does so when it comes back.
  for (const rec of draftLiveRecs(d)) {
    draftDiscard(rec);
    rec.discarded = false;
    if (rec.loaded) edReplaceClean(rec, rec.loaded.content, rec.loaded.hash);
    edPaintState(rec);
  }
  for (const rec of edParked) if (draftKeyOf(rec) === d.key) rec.revert = true;
  draftsPaintSoon();
}

// Opens a draft's file where its edits are: the view that already has it, else
// the editor, which finds the draft by itself (openEditor). A file gone from
// the computer opens as a new one, and the draft's base hash then makes its
// save a 409 rather than a quiet create.
async function draftOpenFile(d) {
  showDraftsMenu(false);
  const live = draftLiveRecs(d)[0];
  if (live) {
    for (const ws of wsLive) {
      for (const g of ws.groups) {
        for (const t of g.tabs) if (t.rec === live) ws.open(t.path, "editor");
      }
    }
    return;
  }
  const cur = await edReadCurrent(d.path);
  const opts = cur && cur.missing ? { create: true } : {};
  // On a phone the pill is on the session list, which is a screen of its
  // own: the editor takes its place and gives it back on the way out.
  if (!isWideLayout() && $("screen-list").classList.contains("active")) {
    opts.returnTo = { show: () => { $("screen-list").classList.add("active"); syncChrome(); } };
  }
  await openEditor(d.path, opts);
  if (opts.returnTo && $("screen-editor").classList.contains("active")
      && !$("screen-editor").classList.contains("docked")) {
    $("screen-list").classList.remove("active");
    syncChrome();
  }
}

$("btn-drafts").addEventListener("click", () => {
  showDraftsMenu(!$("drafts-wrap").classList.contains("open"));
});
$("drafts-scrim").addEventListener("click", () => showDraftsMenu(false));

// ---- when to act ---------------------------------------------------------------

document.addEventListener("visibilitychange", () => {
  if (document.hidden) { draftFlushAll(); return; }
  draftsPaintSoon();
});
window.addEventListener("pagehide", draftFlushAll);

draftsPaintSoon();
