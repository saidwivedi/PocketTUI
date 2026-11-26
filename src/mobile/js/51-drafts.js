// ============================================================
// Offline-safe editing: drafts, saves that wait, conflicts
// ============================================================
// Three layers over the editor's one rule, that a write carries the hash of
// the version it is based on (edViewSave, 29-editor.js; app.py checks it under
// its write lock):
//
//   - A draft: an unsaved buffer kept on this device in IndexedDB, one record
//     per (computer, path), with the version it was loaded from. Opening the
//     file again on any surface puts it back, still based on that version, so
//     the save after a reload goes through the same hash check as any other.
//   - A save that cannot reach the computer waits (a mark on the file, in
//     memory only) and is sent again, one file at a time, when the computer
//     answers. Never as an overwrite.
//   - A save the computer refuses because the file moved on is a conflict:
//     marked, never overwritten unless the user says so.
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
var DRAFT_PROBE_MS = 5000;
var DRAFT_RETRY_MS = 10000;

var draftDbP = null;          // Promise<IDBDatabase|null>, opened on first use
var draftSaidBroken = false;  // the "not kept" line, once per page load
var draftPending = new Set(); // recs with a debounced write not yet made
var draftFlags = new Map();   // draft key -> "queued" | "conflict"
var draftInflight = new Map();// save key -> the promise of the write in flight
var draftFlushing = false;
var draftRetryTimer = 0;
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

// One write per file at a time, whichever view of it asks: keyed by the draft
// key, or by the rec itself where there is none (the demo).
function edSaveKey(rec) { return draftKeyOf(rec) || rec; }

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
  for (const key of [...draftFlags.keys()]) {
    if (key.startsWith(id + "|")) draftFlags.delete(key);
  }
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
  rec.conflict = null;
  rec.restored = null;
  const key = draftKeyOf(rec);
  if (key) draftFlags.delete(key);
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

// ---- writing -----------------------------------------------------------------

// One POST, answered as what it means here: ok (with the new hash), conflict
// (with the hash the computer has), auth, offline (no answer, or a proxy with
// nothing behind it) or error.
async function edWrite(path, hash, text) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), SAVE_TIMEOUT_MS);
  try {
    const r = await fetch(apiURL("api/fs/write"), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ path: path, content: text, hash: hash }),
      signal: ctl.signal,
    });
    const data = await r.json().catch(() => null);
    if (r.status === 401) return { kind: "auth" };
    if (r.status === 409) {
      return { kind: "conflict", hash: data && typeof data.hash === "string" ? data.hash : "" };
    }
    if (r.status === 502 || r.status === 503 || r.status === 504) return { kind: "offline" };
    if (!r.ok || !data || typeof data.hash !== "string") return { kind: "error", status: r.status };
    return { kind: "ok", hash: data.hash };
  } catch (e) {
    return { kind: "offline" };
  } finally {
    clearTimeout(timer);
  }
}

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

// A write this device sent and never heard back about. If one of these is
// what the computer has when the next write is refused, it was ours, and the
// buffer is simply based on it (edSaveRun).
function edNoteUnanswered(rec, text) {
  rec.unanswered = (rec.unanswered || []).concat([text]).slice(-2);
}

// The answer to a write whose body was `sent`, landed with `hash`. Clean only
// if the buffer still says what was sent: text typed while the write was out
// stays an edit, now based on the version that landed.
function edLanded(rec, sent, hash) {
  rec = edCurrent(rec);
  rec.hash = hash;
  rec.baseText = sent;
  rec.loaded = { content: sent, hash: hash };
  rec.unanswered = [];
  rec.conflict = null;
  rec.restored = null;
  const key = draftKeyOf(rec);
  if (key) draftFlags.delete(key);
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

function edQueue(rec, manual) {
  rec = edCurrent(rec);
  // Closed while the write was out: its draft was kept at the close, and there
  // is no view left to send from.
  if (rec.closed && !rec.parked) return;
  const key = draftKeyOf(rec);
  const was = key && draftFlags.get(key) === "queued";
  if (key) draftFlags.set(key, "queued");
  draftWrite(rec);
  edPaintState(rec);
  draftsPaintSoon();
  if (!key) toast("Couldn't save");
  else if (manual || !was) {
    toast(was ? "Still can't reach your computer; " + baseName(rec.path) + " will save when it is back"
              : "Can't reach your computer; " + baseName(rec.path) + " will save when it is back", 3500);
  }
  draftRetrySoon();
}

// One save of the rec's buffer against `hash` (its own base unless a "Keep
// mine" names the computer's), serialised per file. Returns the outcome's
// kind. `mode` is "manual" (the user pressed Save: a conflict opens its
// dialog) or "flush" (a waiting save going out: a conflict is only marked).
async function edSaveOnce(rec, mode, hash) {
  const skey = edSaveKey(rec);
  while (draftInflight.has(skey)) await draftInflight.get(skey);
  rec = edCurrent(rec);
  // A waiting save that went out meanwhile may have come back refused: Save
  // then means "show me", not a second refused write.
  if (mode === "manual" && hash === undefined && rec.conflict) {
    draftResolve(rec);
    return "conflict";
  }
  let done;
  draftInflight.set(skey, new Promise((r) => { done = r; }));
  try {
    return await edSaveRun(rec, mode, hash, false);
  } finally {
    draftInflight.delete(skey);
    done();
  }
}

async function edSaveRun(rec, mode, hash, retried) {
  if (rec.readOnly) return "error";
  if (rec.owner && rec.owner !== draftOwner()) {
    toast(baseName(rec.path) + " belongs to another computer");
    return "error";
  }
  const name = baseName(rec.path);
  const text = edText(rec);
  const base = hash === undefined ? rec.hash : hash;
  if (mode === "manual" && netOffline() && rec.owner) { edQueue(rec, true); return "offline"; }
  const res = await edWrite(rec.path, base, text);
  rec = edCurrent(rec);
  if (res.kind === "ok") {
    edLanded(rec, text, res.hash);
    toast(mode === "manual" ? "Saved" : "Saved " + name);
    return "ok";
  }
  if (res.kind === "offline") {
    edNoteUnanswered(rec, text);
    if (!rec.owner) { toast("Couldn't save"); return "error"; }
    edQueue(rec, mode === "manual");
    return "offline";
  }
  if (res.kind === "auth") {
    // A waiting save keeps waiting; the pairing code is asked for again.
    rejectToken();
    return "auth";
  }
  if (res.kind === "error") {
    const key = draftKeyOf(rec);
    if (key && draftFlags.get(key) === "queued") draftFlags.delete(key);
    edPaintState(rec);
    draftsPaintSoon();
    toast("Couldn't save " + name);
    return "error";
  }
  // 409: what is there now decides what the refusal means.
  const cur = await edReadCurrent(rec.path);
  rec = edCurrent(rec);
  if (!cur) {
    if (!rec.owner) { toast("Couldn't save"); return "error"; }
    edQueue(rec, mode === "manual");
    return "offline";
  }
  if (!cur.unreadable && !cur.missing && cur.content === text) {
    // The write landed before, and its answer was lost.
    edLanded(rec, text, cur.hash);
    toast(mode === "manual" ? "Saved" : "Saved " + name);
    return "ok";
  }
  if (!retried && !cur.unreadable && !cur.missing
      && (rec.unanswered || []).includes(cur.content)) {
    // An earlier write of this buffer's landed unanswered: the computer's
    // version is ours, and the buffer is a later edit of it.
    rec.hash = cur.hash;
    rec.baseText = cur.content;
    rec.loaded = { content: cur.content, hash: cur.hash };
    rec.unanswered = [];
    draftWrite(rec);
    return edSaveRun(rec, mode, undefined, true);
  }
  if (rec.closed && !rec.parked) return "conflict";
  rec.conflict = {
    hash: cur.unreadable ? res.hash : cur.hash,
    text: cur.unreadable ? null : cur.content,
    missing: !!cur.missing,
  };
  const key = draftKeyOf(rec);
  if (key) draftFlags.set(key, "conflict");
  draftWrite(rec);
  edPaintState(rec);
  draftsPaintSoon();
  if (mode === "manual") draftResolve(rec);
  else toast(name + " changed on your computer, so it was not saved", 4000);
  return "conflict";
}

// ---- waiting saves -----------------------------------------------------------

function draftQueued() {
  const out = [];
  for (const rec of edLive) {
    const key = draftKeyOf(rec);
    if (key && draftFlags.get(key) === "queued") out.push(rec);
  }
  return out;
}

function draftRetrySoon() {
  clearTimeout(draftRetryTimer);
  draftRetryTimer = 0;
  if (document.hidden || !draftQueued().length) return;
  draftRetryTimer = setTimeout(draftFlush, DRAFT_RETRY_MS);
}

// Whether the computer answers at all: the version route, which needs no body
// and is answered by every server this build talks to.
async function draftProbe() {
  if (netOffline()) return false;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), DRAFT_PROBE_MS);
  try {
    const r = await fetch(apiURL("api/version"),
                          { cache: "no-store", headers: authHeaders(), signal: ctl.signal });
    return r.ok;
  } catch (e) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

// The waiting saves, one at a time, each against its own base. Only the
// views that are live here: a buffer put away with its session waits until
// that session is opened again (edRestore kicks this), since the record it
// would land in is not a view anyone is looking at.
async function draftFlush() {
  clearTimeout(draftRetryTimer);
  draftRetryTimer = 0;
  if (draftFlushing || !draftQueued().length) return;
  draftFlushing = true;
  try {
    if (!(await draftProbe())) return;
    const tried = new Set();
    for (;;) {
      const owner = draftOwner();
      const rec = draftQueued().find((r) => !tried.has(r) && r.owner === owner);
      if (!rec) break;
      tried.add(rec);
      const kind = await edSaveOnce(rec, "flush");
      if (kind === "offline" || kind === "auth") break;
    }
  } finally {
    draftFlushing = false;
    draftRetrySoon();
  }
}

function draftKick() {
  if (draftQueued().length) draftFlush();
}

// ---- a conflict ----------------------------------------------------------------

// A rec marked in conflict, asked about: overwrite the computer's version with
// this device's text, against the hash it has now, or leave both as they are.
var conflictAsking = false;
async function draftResolve(rec) {
  rec = edCurrent(rec);
  if (!rec.conflict || rec.closed || conflictAsking) return;
  conflictAsking = true;
  let ok;
  try {
    ok = await appConfirm(baseName(rec.path) + " changed on your computer since you "
      + "opened it. Keep mine replaces it with your version; Cancel leaves both as "
      + "they are.", { title: "File changed on your computer", confirmLabel: "Keep mine" });
  } finally {
    conflictAsking = false;
  }
  rec = edCurrent(rec);
  if (!ok || !rec.conflict || rec.closed) return;
  await edSaveOnce(rec, "manual", rec.conflict.hash);
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
  rec.conflict = old.conflict;
  rec.unanswered = old.unanswered;
  rec.restored = old.restored;
  rec.draftBig = old.draftBig;
  if (old.dirty !== rec.dirty) { rec.dirty = old.dirty; rec.onDirty(rec, old.dirty); }
  // Its draft was discarded from the pill while it was away.
  if (old.revert && rec.loaded) edReplaceClean(rec, rec.loaded.content, rec.loaded.hash);
  edPaintState(rec);
  draftKick();
}

// A parked buffer whose session went for good (releaseFileView,
// 09-image-viewer.js): its draft stays in the store, but nothing here will
// send its waiting save any more, so it stops saying it will.
function draftReleaseParked(rec) {
  if (!rec || !edParked.has(rec)) return;
  edParked.delete(rec);
  rec.parked = false;
  rec.closed = true;
  draftUnflag(rec);
}

// A view closed or released: its file's waiting or conflict mark is about that
// view's buffer, and goes unless another view of the same file is still here.
// The draft itself stays in the store.
function draftUnflag(rec) {
  const key = draftKeyOf(rec);
  if (!key || !draftFlags.has(key)) return;
  for (const r of edLive) if (r !== rec && draftKeyOf(r) === key) return;
  for (const r of edParked) if (r !== rec && draftKeyOf(r) === key) return;
  draftFlags.delete(key);
  draftsPaintSoon();
}

// ---- the strip over a buffer ---------------------------------------------------

// One line above the text saying what is true of this buffer beyond "edited":
// in conflict, waiting to save, or restored from this device. Painted
// into the rec's own host, so it rides with the view on every surface.
function edPaintState(rec) {
  rec = edCurrent(rec);
  if (rec.closed && !rec.parked) return;
  const key = draftKeyOf(rec);
  const flag = key ? draftFlags.get(key) : null;
  rec.state = rec.conflict ? "conflict" : flag === "queued" ? "queued" : null;
  if (rec.onMark) rec.onMark(rec);
  if (!rec.view || !rec.view.dom.parentNode) return;
  let strip = rec.strip;
  const host = rec.view.dom.parentNode;
  let msg = "", acts = [];
  if (rec.conflict) {
    msg = rec.conflict.missing
      ? "Not saved: the file is gone from your computer."
      : "Not saved: the file changed on your computer.";
    acts = [["Resolve", "primary", () => draftResolve(rec)]];
  } else if (flag === "queued") {
    msg = "Can't reach your computer. This file will save when it is back.";
  } else if (rec.restored && rec.dirty) {
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
  strip.className = "ed-strip" + (rec.conflict ? " bad" : flag === "queued" ? " warn" : "");
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
  const bad = list.some((d) => draftFlags.get(d.key) === "conflict");
  btn.classList.toggle("drafts-bad", bad);
  $("drafts-count").textContent = list.length + " unsaved";
  btn.setAttribute("aria-label", list.length === 1 ? "1 unsaved file" : list.length + " unsaved files");
  if ($("drafts-wrap").classList.contains("open")) fillDraftsMenu(list);
}

function draftStateWord(d) {
  const f = draftFlags.get(d.key);
  if (f === "conflict") return ["Conflict", "bad"];
  if (f === "queued") return ["Will save when back", "warn"];
  return ["Unsaved", ""];
}

function fillDraftsMenu(list) {
  const menu = $("drafts-menu");
  menu.textContent = "";
  list.sort((a, b) => b.updatedAt - a.updatedAt);
  for (const d of list) {
    const [word, tone] = draftStateWord(d);
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
          el("span", { class: "profile-menu-status", "data-tone": tone }, word))),
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
  draftFlags.delete(d.key);
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
// save a conflict rather than a quiet create.
async function draftOpenFile(d) {
  showDraftsMenu(false);
  const live = draftLiveRecs(d)[0];
  if (live) {
    for (const ws of wsLive) {
      for (const g of ws.groups) {
        for (const t of g.tabs) if (t.rec === live) ws.open(t.path, "editor");
      }
    }
    if (live.conflict) draftResolve(live);
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
  if (edRec && edRec.path === d.path && edRec.conflict) draftResolve(edRec);
}

$("btn-drafts").addEventListener("click", () => {
  showDraftsMenu(!$("drafts-wrap").classList.contains("open"));
});
$("drafts-scrim").addEventListener("click", () => showDraftsMenu(false));

// ---- when to act ---------------------------------------------------------------

document.addEventListener("visibilitychange", () => {
  if (document.hidden) { draftFlushAll(); return; }
  draftKick();
  draftsPaintSoon();
});
window.addEventListener("pagehide", draftFlushAll);
window.addEventListener("online", draftKick);

draftsPaintSoon();
