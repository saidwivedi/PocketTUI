// ============================================================
// Offline-safe editing: drafts, saves that wait, conflicts
// ============================================================
// Three layers over the editor's one rule, that a write carries the hash of
// the version it is based on (edViewSave, 29-editor.js; app.py checks it under
// its write lock):
//
//   - A draft: an unsaved buffer kept on this device in IndexedDB, with the
//     version it was loaded from. One record per (computer, path, editing
//     line): a page writes only a record whose latest text it wrote or
//     restored itself, and one that finds its record rewritten by another
//     page of the app starts a record of its own (draftWrite), so two pages
//     with different text keep both. Opening the file puts the newest back,
//     still based on its version, so the save after a reload goes through the
//     same hash check as any other.
//   - A save that cannot reach the computer waits (a mark on the draft, in
//     memory only, for the life of the page) and is sent again, one file at a
//     time, when the computer answers, whether or not the file is still open.
//     Never as an overwrite.
//   - Every request for a buffer goes to the computer the buffer came from,
//     and an operation that finds another computer in force after any wait
//     stops where it is, changing nothing (draftAway).
//   - A save the computer refuses because the file moved on is a conflict:
//     marked, and resolved in a dialog that shows both sides and has exactly
//     one button that drops this device's text.
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
// The diff the conflict dialog draws is capped: past this many rows it says
// so rather than building a page nobody scrolls through.
var DRAFT_DIFF_MAX_ROWS = 4000;

var draftDbP = null;          // Promise<IDBDatabase|null>, opened on first use
var draftSaidBroken = false;  // the "not kept" line, once per page load
var draftPending = new Set(); // recs with a debounced write not yet made
var draftFlags = new Map();   // draft record key -> "queued" | "conflict"
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

function draftRandom() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 10); }

// A record's key: the computer, the path, and the editing line it belongs to.
// A record from before lines existed is keyed by the first two alone and is
// read, adopted and written under the key it has.
function draftNewKey(owner, path) { return owner + "\n" + path + "\n" + draftRandom(); }
function draftKeyOf(rec) {
  if (!rec.owner) return null;
  if (!rec.draftKey) rec.draftKey = draftNewKey(rec.owner, rec.path);
  return rec.draftKey;
}
function draftProfileOf(owner) { return owner.slice(0, owner.indexOf("|")); }

// A rec's own claim on its record: the stamp of the text it last wrote there
// or restored from there. Read when the store answers, not when asked.
function draftOwnCond(rec) { return { stamp: () => edCurrent(rec).draftStamp }; }

// The computer in force is no longer the one `owner` names: whatever was
// under way for a buffer of `owner` stops and leaves it as it is.
function draftAway(owner) { return !!owner && owner !== draftOwner(); }

// One write per file at a time, whichever view of it asks: keyed by computer
// and path, or by the rec itself where there is none (the demo).
function edSaveKey(rec) { return rec.owner ? rec.owner + "\n" + rec.path : rec; }

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

async function draftGet(key) {
  if (!key) return null;
  try {
    return await draftTx("readonly", (st, set) => {
      const r = st.get(key);
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

// Deletes the record. With no `cond` that is an explicit discard of it; else
// only while the record still carries `cond.stamp` (this page's own latest
// text, so another page's text is never taken) and, given `cond.text`, still
// holds that text (a save answered for an older text must not take a newer
// draft with it).
async function draftDrop(key, cond) {
  if (!key) return;
  try {
    await draftTx("readwrite", (st) => {
      if (!cond) { st.delete(key); return; }
      const r = st.get(key);
      r.onsuccess = () => {
        const cur = r.result;
        if (!cur) return;
        if ("stamp" in cond) {
          const stamp = typeof cond.stamp === "function" ? cond.stamp() : cond.stamp;
          if (cur.stamp !== stamp) return;
        }
        if (cond.text !== undefined && cur.text !== cond.text) return;
        st.delete(key);
      };
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
  const stamp = draftRandom();
  const record = {
    key: "", owner: rec.owner, profile: draftProfileOf(rec.owner),
    path: rec.path, baseHash: rec.hash, baseText: rec.baseText == null ? null : rec.baseText,
    text: text, updatedAt: Date.now(), stamp: stamp,
  };
  try {
    await draftTx("readwrite", (st) => {
      const key = draftKeyOf(rec);
      const r = st.get(key);
      r.onsuccess = () => {
        const cur = r.result;
        // Rewritten by another page since this one last wrote or restored
        // it: that text is the other page's, and this buffer starts a record
        // of its own rather than replacing it.
        if (cur && cur.stamp !== rec.draftStamp) draftFork(rec, key);
        // Already there as this page left it (a hide or pagehide writing
        // every dirty buffer): rewriting would only make it look newer.
        else if (cur && cur.text === text && cur.baseHash === record.baseHash) return;
        record.key = rec.draftKey;
        st.put(record);
        rec.draftStamp = stamp;
      };
    });
  } catch (e) { draftFail(e); return; }
  draftsPaintSoon();
}

// A rec moved to a record of its own. Its waiting or conflict mark goes with
// it, unless another view here still holds the old record.
function draftFork(rec, old) {
  rec.draftKey = draftNewKey(rec.owner, rec.path);
  const flag = draftFlags.get(old);
  if (!flag) return;
  draftFlags.set(rec.draftKey, flag);
  for (const r of edLive) if (r !== rec && r.draftKey === old) return;
  for (const r of edParked) if (r !== rec && r.draftKey === old) return;
  draftFlags.delete(old);
}

// Every buffer with edits, written now. On the way out (hidden, pagehide) the
// debounce may never fire: iOS suspends a backgrounded page and Chrome for
// Android discards one. All of them rather than only the pending ones, because
// another page of this app may have saved the shared record away meanwhile.
function draftFlushAll() {
  for (const rec of edLive) if (rec.dirty) draftWrite(rec);
  for (const rec of edParked) if (rec.dirty) draftWrite(rec);
}

// An explicit discard of this buffer: its record (while it still holds this
// page's text), the marks and any pending write go.
function draftDiscard(rec) {
  rec.discarded = true;
  clearTimeout(rec.draftTimer);
  draftPending.delete(rec);
  rec.conflict = null;
  rec.restored = null;
  const key = draftKeyOf(rec);
  if (key) {
    draftFlags.delete(key);
    draftDrop(key, draftOwnCond(rec));
  }
}

// The record for a file about to be opened, or null: the newest of the
// file's records on `owner` (the computer in force when the open started), or
// the one keyed `key`. Asked before the view is built, so nothing typed into
// it can race the answer. `others` on it counts the file's other records.
async function draftLookup(path, owner, key) {
  if (owner === undefined) owner = draftOwner();
  const all = (await draftList(owner)).filter((d) => d.path === path);
  if (!all.length) return null;
  all.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  const d = (key && all.find((x) => x.key === key)) || all[0];
  d.others = all.length - 1;
  return d;
}

// A freshly built rec gets its file's draft, if there is one: the draft's
// text in the buffer as an edit (undo goes back to the computer's version),
// still based on the version it was made from. `loaded` is what the computer
// sent just now, {content, hash}. The rec adopts the record: its later
// writes go there for as long as no other page rewrites it.
function draftApply(rec, loaded, d) {
  rec.loaded = { content: loaded.content, hash: loaded.hash };
  rec.baseText = loaded.content;
  if (!d || rec.readOnly || !rec.owner) return;
  // Another computer's draft is never put into this one's file.
  if (d.owner !== rec.owner) return;
  // The computer already has this exact text: an earlier save landed and its
  // answer was lost. That is the write the draft was waiting for.
  if (d.text === loaded.content) {
    draftDrop(d.key, { stamp: d.stamp, text: d.text });
    return;
  }
  rec.draftKey = d.key;
  rec.draftStamp = d.stamp;
  rec.hash = d.baseHash || "";
  rec.baseText = d.baseText;
  rec.view.dispatch({ changes: { from: 0, to: rec.view.state.doc.length, insert: d.text } });
  rec.restored = { stale: (d.baseHash || "") !== (loaded.hash || ""), others: d.others || 0 };
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

// One POST to `owner`'s computer, answered as what it means here: ok (with
// the new hash), conflict (with the hash the computer has), auth, offline (no
// answer, or a proxy with nothing behind it), error, or away (`owner` is not
// the computer in force, before the request or once it is answered: the
// address and the code in force are another computer's).
async function edWrite(path, hash, text, owner) {
  if (draftAway(owner)) return { kind: "away" };
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
    if (draftAway(owner)) return { kind: "away" };
    if (r.status === 401) return { kind: "auth" };
    if (r.status === 409) {
      return { kind: "conflict", hash: data && typeof data.hash === "string" ? data.hash : "" };
    }
    if (r.status === 502 || r.status === 503 || r.status === 504) return { kind: "offline" };
    if (!r.ok || !data || typeof data.hash !== "string") return { kind: "error", status: r.status };
    return { kind: "ok", hash: data.hash };
  } catch (e) {
    return draftAway(owner) ? { kind: "away" } : { kind: "offline" };
  } finally {
    clearTimeout(timer);
  }
}

// The file as `owner`'s computer has it now: {content, hash}, {missing: true},
// or {unreadable: true} for one the editor could not show; null when the
// computer did not answer; {away: true} as edWrite's away. `lossy` says the
// content is the computer's bytes with the invalid UTF-8 replaced: shown, never
// taken as equal to any text of this device's.
async function edReadCurrent(path, owner) {
  if (draftAway(owner)) return { away: true };
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), SAVE_TIMEOUT_MS);
  try {
    const r = await fetch(apiURL("api/fs/read?path=" + encodeURIComponent(path)),
                          { cache: "no-store", headers: authHeaders(), signal: ctl.signal });
    const data = await r.json().catch(() => null);
    if (draftAway(owner)) return { away: true };
    if (r.status === 404) return { missing: true, content: "", hash: "" };
    if (r.status === 401 || r.status >= 500) return null;
    if (!r.ok || !data || typeof data.content !== "string") return { unreadable: true };
    return { content: data.content, hash: data.hash, lossy: !!data.lossy };
  } catch (e) {
    return draftAway(owner) ? { away: true } : null;
  } finally {
    clearTimeout(timer);
  }
}

// What the computer has is exactly `text`: read whole, valid UTF-8, equal.
function draftHolds(cur, text) {
  return !!cur && !cur.away && !cur.unreadable && !cur.missing && !cur.lossy && cur.content === text;
}

// The conflict mark for a refused write, from what the computer has now. A
// version that can't be shown, or only lossily, is no text to load or merge.
function draftConflictOf(cur, refusedHash) {
  const shown = !cur.unreadable && !cur.lossy;
  return {
    hash: cur.unreadable ? refusedHash : cur.hash,
    text: shown ? cur.content : null,
    missing: !!cur.missing,
    lossy: !!cur.lossy,
  };
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
  rec.merged = false;
  const key = draftKeyOf(rec);
  if (key) draftFlags.delete(key);
  if (edText(rec) === sent) {
    clearTimeout(rec.draftTimer);
    draftPending.delete(rec);
    rec.dirty = false;
    rec.onDirty(rec, false);
    // Whatever this buffer last wrote to its record is on the computer now
    // or was undone before the save; another page's later text stays.
    if (key) draftDrop(key, draftOwnCond(rec));
  } else {
    draftWrite(rec);
  }
  edPaintState(rec);
  draftsPaintSoon();
}

function edQueue(rec, manual) {
  rec = edCurrent(rec);
  // Closed while the write was out: its draft was kept at the close, and the
  // waiting save is the draft's now (draftFlush sends it from the store).
  if (rec.closed && !rec.parked) {
    if (rec.discarded || !rec.owner) return;
    draftFlags.set(draftKeyOf(rec), "queued");
    draftsPaintSoon();
    draftRetrySoon();
    return;
  }
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
// "away" is a save stopped because another computer came into force: the rec,
// its draft and its marks are as they were, for when its computer is back.
async function edSaveOnce(rec, mode, hash) {
  const skey = edSaveKey(rec);
  while (draftInflight.has(skey)) await draftInflight.get(skey);
  rec = edCurrent(rec);
  if (draftAway(rec.owner)) {
    if (mode === "manual") toast(baseName(rec.path) + " belongs to another computer");
    return "away";
  }
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
  const owner = rec.owner;
  const name = baseName(rec.path);
  const text = edText(rec);
  const base = hash === undefined ? rec.hash : hash;
  if (mode === "manual" && netOffline() && rec.owner) { edQueue(rec, true); return "offline"; }
  const res = await edWrite(rec.path, base, text, owner);
  rec = edCurrent(rec);
  if (res.kind === "away") return "away";
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
  const cur = await edReadCurrent(rec.path, owner);
  rec = edCurrent(rec);
  if (cur && cur.away) return "away";
  if (!cur) {
    if (!rec.owner) { toast("Couldn't save"); return "error"; }
    edQueue(rec, mode === "manual");
    return "offline";
  }
  if (draftHolds(cur, text)) {
    // The write landed before, and its answer was lost.
    edLanded(rec, text, cur.hash);
    toast(mode === "manual" ? "Saved" : "Saved " + name);
    return "ok";
  }
  if (!retried && !cur.unreadable && !cur.missing && !cur.lossy
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
  const key = draftKeyOf(rec);
  if (rec.closed && !rec.parked) {
    // Closed while the write was out: the mark is the draft's.
    if (key && !rec.discarded) {
      draftFlags.set(key, "conflict");
      draftsPaintSoon();
      toast(name + " changed on your computer, so it was not saved", 4000);
    }
    return "conflict";
  }
  rec.conflict = draftConflictOf(cur, res.hash);
  if (key) draftFlags.set(key, "conflict");
  draftWrite(rec);
  edPaintState(rec);
  draftsPaintSoon();
  if (mode === "manual") draftResolve(rec);
  else toast(name + " changed on your computer, so it was not saved", 4000);
  return "conflict";
}

// ---- waiting saves -----------------------------------------------------------

// The keys of the drafts whose save waits, on the computer in force.
function draftQueued() {
  const owner = draftOwner();
  if (!owner) return [];
  const out = [];
  for (const [key, flag] of draftFlags) {
    if (flag === "queued" && key.startsWith(owner + "\n")) out.push(key);
  }
  return out;
}

// The view here that holds a draft record, open or put away with its session.
function draftRecOf(key) {
  for (const r of edLive) if (r.owner && draftKeyOf(r) === key) return r;
  for (const r of edParked) if (r.owner && draftKeyOf(r) === key) return r;
  return null;
}

// Whether leaving this buffer can skip the discard question: its save waits
// for the computer, and its text is in the store to be sent from.
function draftIsQueued(rec) {
  if (!rec || !rec.owner || draftSaidBroken || rec.draftBig) return false;
  return draftFlags.get(draftKeyOf(rec)) === "queued";
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

// The waiting saves on the computer in force, one at a time, each against its
// own base, whether or not the file is still open: a view that holds the
// draft (open or put away with its session) sends its buffer as it is now,
// and a draft with no view here is sent from the store.
async function draftFlush() {
  clearTimeout(draftRetryTimer);
  draftRetryTimer = 0;
  if (draftFlushing || !draftQueued().length) return;
  draftFlushing = true;
  try {
    if (!(await draftProbe())) return;
    const tried = new Set();
    for (;;) {
      const key = draftQueued().find((k) => !tried.has(k));
      if (!key) break;
      tried.add(key);
      const rec = draftRecOf(key);
      const kind = rec ? await edSaveOnce(rec, "flush") : await draftSendStored(key);
      if (kind === "offline" || kind === "auth" || kind === "away") break;
    }
  } finally {
    draftFlushing = false;
    draftRetrySoon();
  }
}

// A waiting draft with no view here: its stored text against its stored base.
// The outcomes are edSaveRun's, without a buffer to update unless one has
// adopted the record meanwhile, and a refusal is only marked.
async function draftSendStored(key) {
  const d = await draftGet(key);
  if (!d) {
    draftFlags.delete(key);
    draftsPaintSoon();
    return "gone";
  }
  const owner = d.owner;
  if (draftAway(owner)) return "away";
  const skey = owner + "\n" + d.path;
  while (draftInflight.has(skey)) await draftInflight.get(skey);
  // Opened meanwhile: the view sends its own buffer.
  const rec = draftRecOf(key);
  if (rec) return edSaveOnce(rec, "flush");
  if (draftFlags.get(key) !== "queued") return "gone";
  let done;
  draftInflight.set(skey, new Promise((r) => { done = r; }));
  try {
    return await draftSendRecord(key, d);
  } finally {
    draftInflight.delete(skey);
    done();
  }
}

async function draftSendRecord(key, d) {
  const owner = d.owner, name = baseName(d.path);
  const landed = (hash) => {
    draftFlags.delete(key);
    draftDrop(key, { stamp: d.stamp, text: d.text });
    // A view that adopted the record while the write was out, still based
    // on the version the record was: the text it sent is its base now.
    const rec = draftRecOf(key);
    if (rec && (edText(rec) === d.text || rec.hash === (d.baseHash || ""))) edLanded(rec, d.text, hash);
    draftsPaintSoon();
    toast("Saved " + name);
    return "ok";
  };
  const res = await edWrite(d.path, d.baseHash || "", d.text, owner);
  if (res.kind === "away") return "away";
  if (res.kind === "ok") return landed(res.hash);
  if (res.kind === "offline") return "offline";
  if (res.kind === "auth") { rejectToken(); return "auth"; }
  if (res.kind === "error") {
    draftFlags.delete(key);
    draftsPaintSoon();
    toast("Couldn't save " + name);
    return "error";
  }
  const cur = await edReadCurrent(d.path, owner);
  if (cur && cur.away) return "away";
  if (!cur) return "offline";
  if (draftHolds(cur, d.text)) return landed(cur.hash);
  if (draftFlags.get(key) !== "queued") return "conflict";
  draftFlags.set(key, "conflict");
  const rec = draftRecOf(key);
  if (rec && !rec.conflict) {
    rec.conflict = draftConflictOf(cur, res.hash);
    edPaintState(rec);
  }
  draftsPaintSoon();
  toast(name + " changed on your computer, so it was not saved", 4000);
  return "conflict";
}

function draftKick() {
  if (draftQueued().length) draftFlush();
}

// ---- the conflict dialog -----------------------------------------------------

// The base lines an edit could equally be placed over. Among repeated lines
// the diff picks one of several places for the same change (deleting one of
// three equal lines is any of them), so an edit is held to cover every place
// it could slide to: a removed block while the line beyond one end equals the
// line at the other, an inserted block likewise against its own lines.
function draftSlide(B, e) {
  let lo = e.a, hi = e.a + e.al;
  if (e.al) {
    let l = e.a, r = e.a + e.al;
    while (l > 0 && B[l - 1] === B[r - 1]) { l--; r--; }
    lo = Math.min(lo, l);
    l = e.a; r = e.a + e.al;
    while (r < B.length && B[l] === B[r]) { l++; r++; }
    hi = Math.max(hi, r);
  }
  if (e.lines.length) {
    let rot = e.lines.slice(), p = e.a;
    while (p > 0 && B[p - 1] === rot[rot.length - 1]) { rot.unshift(rot.pop()); p--; }
    lo = Math.min(lo, p);
    rot = e.lines.slice(); p = e.a + e.al;
    while (p < B.length && B[p] === rot[0]) { rot.push(rot.shift()); p++; }
    hi = Math.max(hi, p);
  }
  return [lo, hi];
}

// A three-way merge of whole lines: the edits from base to mine and from base
// to theirs, applied together, or null when an edit of one side touches one
// of the other (overlap, abut, or insert at the same place, anywhere either
// could slide to) unless they are the same edit. Refusing is always safe;
// a merge that guessed where an edit went is not.
function draftMerge3(base, mine, theirs) {
  if (base == null || mine == null || theirs == null) return null;
  const B = wsLineKeys(wsSplitLines(base));
  const M = wsLineKeys(wsSplitLines(mine));
  const T = wsLineKeys(wsSplitLines(theirs));
  const side = (X, s) => wsLineDiff(B, X).map((h) => {
    const e = { a: h.a, al: h.al, lines: X.slice(h.b, h.b + h.bl), s };
    [e.lo, e.hi] = draftSlide(B, e);
    return e;
  });
  const mineEdits = side(M, 0), theirEdits = side(T, 1);
  const same = (x, y) => x.a === y.a && x.al === y.al
    && x.lines.length === y.lines.length && x.lines.every((l, i) => l === y.lines[i]);
  const dup = new Set();
  for (const x of mineEdits) {
    for (const y of theirEdits) {
      if (x.lo > y.hi || y.lo > x.hi) continue;
      if (!same(x, y)) return null;
      dup.add(y);
    }
  }
  const keep = mineEdits.concat(theirEdits.filter((y) => !dup.has(y)));
  keep.sort((x, y) => x.a - y.a || x.al - y.al);
  const out = [];
  let i = 0;
  for (const e of keep) {
    for (; i < e.a; i++) out.push(B[i]);
    for (const l of e.lines) out.push(l);
    i = e.a + e.al;
  }
  for (; i < B.length; i++) out.push(B[i]);
  // The no-newline mark wsLineKeys puts on a last line may only end the text.
  for (let k = 0; k < out.length - 1; k++) if (out[k].endsWith("\u0000")) return null;
  if (!out.length) return "";
  const last = out[out.length - 1];
  if (last.endsWith("\u0000")) {
    out[out.length - 1] = last.slice(0, -1);
    return out.join("\n");
  }
  return out.join("\n") + "\n";
}

// The computer's text against this device's as a unified diff the diff pane
// draws (diffRender, 37-git-diff.js): theirs as the old side, mine as the new.
function draftUnified(theirs, mine) {
  const A = wsSplitLines(theirs), B = wsSplitLines(mine);
  const hunks = wsLineDiff(wsLineKeys(A), wsLineKeys(B));
  const ctx = WS_PATCH_CONTEXT;
  const groups = [];
  for (const h of hunks) {
    const g = groups[groups.length - 1];
    if (g && h.a - (g.a + g.al) <= 2 * ctx) {
      g.al = h.a + h.al - g.a;
      g.bl = h.b + h.bl - g.b;
      g.hunks.push(h);
    } else {
      groups.push({ a: h.a, al: h.al, b: h.b, bl: h.bl, hunks: [h] });
    }
  }
  const out = [];
  let rows = 0;
  for (const g of groups) {
    const pre = Math.min(ctx, g.a);
    const post = Math.min(ctx, A.lines.length - (g.a + g.al));
    const lines = [];
    let i = g.a - pre;
    for (const h of g.hunks) {
      for (; i < h.a; i++) lines.push(" " + A.lines[i]);
      for (let k = h.a; k < h.a + h.al; k++) lines.push("-" + A.lines[k]);
      for (let k = h.b; k < h.b + h.bl; k++) lines.push("+" + B.lines[k]);
      i = h.a + h.al;
    }
    for (; i < g.a + g.al + post; i++) lines.push(" " + A.lines[i]);
    const olds = pre + g.al + post, news = pre + g.bl + post;
    const at = (start, n) => (n ? start + 1 : start) + "," + n;
    out.push("@@ -" + at(g.a - pre, olds) + " +" + at(g.b - pre, news) + " @@");
    out.push(...lines);
    rows += lines.length + 1;
    if (rows > DRAFT_DIFF_MAX_ROWS) return { text: out.join("\n") + "\n", cut: true };
  }
  return { text: out.length ? out.join("\n") + "\n" : "", cut: false };
}

// The sibling a copy of this device's text is written to: "name (mine).ext",
// then "(mine 2)" and on while the name is taken.
function draftCopyPath(path, n) {
  const slash = path.lastIndexOf("/");
  const dir = path.slice(0, slash + 1), name = path.slice(slash + 1);
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  return dir + stem + (n > 1 ? " (mine " + n + ")" : " (mine)") + ext;
}

var conflictRec = null;

// The dialog for a rec marked in conflict. One at a time: asked again while it
// is up, it stays as it is.
function draftResolve(rec) {
  rec = edCurrent(rec);
  if (!rec.conflict || rec.closed) return;
  if ($("sheet-conflict").classList.contains("show")) return;
  conflictRec = rec;
  // A toast left over from the flush would sit over the dialog's buttons.
  hideToast();
  const c = rec.conflict;
  const mine = edText(rec);
  const name = baseName(rec.path);
  $("conflict-title").textContent = name + " changed on your computer";
  $("conflict-msg").textContent = c.missing
    ? "The file was deleted or moved on your computer after you started editing. Your text is still here."
    : "Someone or something saved a different version while you were editing. Nothing has been overwritten.";
  const body = $("conflict-diff");
  body.textContent = "";
  if (c.text == null) {
    body.appendChild(diffNote(c.lossy
      ? "The computer's version is not valid UTF-8, so it can't be shown or merged here."
      : "The computer's version can't be shown here."));
  } else {
    const d = draftUnified(c.text, mine);
    if (d.text) diffRender(body, { diff: d.text }, "conflict", rec.path, null);
    else body.appendChild(diffNote("The two versions are the same."));
    if (d.cut) body.appendChild(el("div", { class: "diff-notice" }, "Diff cut short; the rest is in the file."));
  }
  const merged = c.missing ? null : draftMerge3(rec.baseText, mine, c.text);
  $("btn-conflict-merge").hidden = merged == null;
  // Nothing to load when the computer's version could not be read.
  $("btn-conflict-theirs").hidden = c.text == null;
  $("btn-conflict-theirs").textContent = c.missing ? "Discard mine" : "Keep the computer's (discard mine)";
  for (const b of $("sheet-conflict").querySelectorAll(".actions button")) b.disabled = false;
  showSheet(true, "sheet-conflict");
  $("btn-conflict-cancel").focus();
}

function conflictBusy(on) {
  for (const b of $("sheet-conflict").querySelectorAll(".actions button")) b.disabled = on;
}

// The rec the dialog was opened for, still in conflict; null otherwise, and
// the sheet put away. Read once, at the press: whatever the action awaits
// afterwards, it finishes on this rec and no other (the dialog may have been
// dismissed and opened for another file meanwhile).
function conflictTarget() {
  const rec = conflictRec && edCurrent(conflictRec);
  if (!rec || !rec.conflict || rec.closed) { showSheet(false); return null; }
  return rec;
}

// The dialog is still up for `rec`: an action finishing late closes or frees
// only its own dialog, never one opened since for another file.
function conflictShowing(rec) {
  return $("sheet-conflict").classList.contains("show")
    && !!conflictRec && edCurrent(conflictRec) === edCurrent(rec);
}

async function conflictKeepMine() {
  const rec = conflictTarget();
  if (!rec) return;
  conflictBusy(true);
  showSheet(false);
  // The overwrite, against the version the dialog showed: a file that moved
  // again since is refused once more and comes back here. The save lands on
  // this rec, and marks it clean only if it still holds the text sent
  // (edLanded).
  const hash = rec.conflict.hash;
  await edSaveOnce(rec, "manual", hash);
}

function conflictKeepTheirs() {
  const rec = conflictTarget();
  if (!rec || rec.conflict.text == null) return;
  const c = rec.conflict;
  showSheet(false);
  draftDiscard(rec);
  rec.discarded = false;
  edReplaceClean(rec, c.text, c.hash);
  edPaintState(rec);
  draftsPaintSoon();
  toast(c.missing ? "Discarded your edits" : "Loaded the computer's version");
}

async function conflictSaveCopy() {
  const rec = conflictTarget();
  if (!rec) return;
  conflictBusy(true);
  const mine = edText(rec);
  const owner = rec.owner;
  let landed = null, away = false;
  for (let n = 1; n <= 50 && !landed; n++) {
    const path = draftCopyPath(rec.path, n);
    const res = await edWrite(path, "", mine, owner);
    if (res.kind === "ok") landed = path;
    else if (res.kind === "away") { away = true; break; }
    else if (res.kind === "auth") {
      if (conflictShowing(rec)) { conflictBusy(false); showSheet(false); }
      rejectToken();
      return;
    }
    else if (res.kind !== "conflict") break;
  }
  if (!landed) {
    if (conflictShowing(rec)) conflictBusy(false);
    toast(away ? "Didn't save a copy: another computer is open now" : "Couldn't save a copy");
    return;
  }
  if (conflictShowing(rec)) showSheet(false);
  toast("Saved yours as " + baseName(landed), 3500);
  const now = edCurrent(rec);
  // The text is on the computer under the new name. The buffer takes the
  // computer's version of its own file only if it is still open here, on its
  // computer, in conflict, and still holds exactly the text that was copied;
  // anything typed since, or a closed view, keeps its draft and its mark.
  if (!draftAway(owner) && !(now.closed && !now.parked) && now.conflict && edText(now) === mine) {
    const c = now.conflict;
    const key = draftKeyOf(now);
    if (key) {
      draftFlags.delete(key);
      draftDrop(key, draftOwnCond(now));
    }
    now.conflict = null;
    now.restored = null;
    if (c.missing) {
      edReplaceClean(now, "", "");
    } else if (c.text != null) {
      edReplaceClean(now, c.text, c.hash);
    }
    edPaintState(now);
    draftsPaintSoon();
  }
  if (draftAway(owner)) return;
  const dir = typeof filesActivePath === "function" ? filesActivePath() : "";
  if (dir) loadDir(dir);
}

function conflictMerge() {
  const rec = conflictTarget();
  if (!rec) return;
  const c = rec.conflict;
  const merged = draftMerge3(rec.baseText, edText(rec), c.text);
  if (merged == null) { $("btn-conflict-merge").hidden = true; return; }
  showSheet(false);
  // An edit like any other, based on the computer's version now: nothing is
  // written until the user has read it and pressed Save.
  rec.hash = c.hash;
  rec.baseText = c.text;
  rec.loaded = { content: c.text, hash: c.hash };
  rec.conflict = null;
  rec.restored = null;
  rec.merged = true;
  const key = draftKeyOf(rec);
  if (key) draftFlags.delete(key);
  rec.view.dispatch({ changes: { from: 0, to: rec.view.state.doc.length, insert: merged } });
  if (!rec.dirty) { rec.dirty = true; rec.onDirty(rec, true); }
  draftWrite(rec);
  edPaintState(rec);
  draftsPaintSoon();
}

$("btn-conflict-cancel").addEventListener("click", () => showSheet(false));
$("btn-conflict-mine").addEventListener("click", conflictKeepMine);
$("btn-conflict-theirs").addEventListener("click", conflictKeepTheirs);
$("btn-conflict-copy").addEventListener("click", conflictSaveCopy);
$("btn-conflict-merge").addEventListener("click", conflictMerge);

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
  rec.merged = old.merged;
  rec.draftBig = old.draftBig;
  rec.draftKey = old.draftKey;
  rec.draftStamp = old.draftStamp;
  if (old.dirty !== rec.dirty) { rec.dirty = old.dirty; rec.onDirty(rec, old.dirty); }
  // Its draft was discarded from the pill while it was away.
  if (old.revert && rec.loaded) edReplaceClean(rec, rec.loaded.content, rec.loaded.hash);
  edPaintState(rec);
  draftKick();
}

// A parked buffer whose session went for good (releaseFileView,
// 09-image-viewer.js): its draft stays in the store, and a waiting save or a
// conflict mark stays with the draft (draftFlush sends it from the store).
function draftReleaseParked(rec) {
  if (!rec || !edParked.has(rec)) return;
  edParked.delete(rec);
  rec.parked = false;
  rec.closed = true;
}

// A view closed with its save waiting (edDropView): nothing was asked, so
// this says where the text went.
function draftClosedNote(rec) {
  if (rec.dirty && !rec.discarded && draftIsQueued(rec)) {
    toast(baseName(rec.path) + " will save when the connection is back", 3500);
  }
}

// ---- the strip over a buffer ---------------------------------------------------

// One line above the text saying what is true of this buffer beyond "edited":
// in conflict, waiting to save, restored from this device, or merged. Painted
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
  } else if (rec.merged) {
    msg = "Merged with your computer's version. Read it over, then save.";
    acts = [["OK", "", () => { rec.merged = false; edPaintState(rec); }]];
  } else if (rec.restored && rec.dirty) {
    msg = rec.restored.stale
      ? "Restored unsaved changes. The file has changed on your computer since; saving will ask which to keep."
      : "Restored unsaved changes from this device.";
    // Another page of the app had different unsaved text for this file; the
    // newest is here, the rest are in the unsaved list.
    if (rec.restored.others) {
      msg += rec.restored.others === 1 ? " Another unsaved copy is in the unsaved list."
        : " " + rec.restored.others + " other unsaved copies are in the unsaved list.";
    }
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
  if (f === "conflict") return ["Changed on your computer", "bad"];
  if (f === "queued") return ["Will save when back", "warn"];
  return ["Unsaved", ""];
}

// When a draft was last written: the time today, the date before that.
function draftWhen(t) {
  const d = new Date(t || 0), now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === now.toDateString() ? hm
    : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + hm;
}

function fillDraftsMenu(list) {
  const menu = $("drafts-menu");
  menu.textContent = "";
  list.sort((a, b) => b.updatedAt - a.updatedAt);
  // A file with more than one unsaved copy (two pages of the app with
  // different text): each row says which is newer.
  const seen = new Map();
  for (const d of list) seen.set(d.path, (seen.get(d.path) || 0) + 1);
  const first = new Set();
  for (const d of list) {
    const [word, tone] = draftStateWord(d);
    let when = draftWhen(d.updatedAt);
    if (seen.get(d.path) > 1) {
      when += first.has(d.path) ? " \u00b7 older" : " \u00b7 newest";
      first.add(d.path);
    }
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
          el("span", { class: "profile-menu-status", "data-tone": tone }, word),
          el("span", { class: "draft-when" }, when))),
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
  await draftDrop(d.key);
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
  const cur = await edReadCurrent(d.path, d.owner);
  if (cur && cur.away) return;
  const opts = cur && cur.missing ? { create: true } : {};
  // This row's copy, which need not be the file's newest.
  opts.draftKey = d.key;
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
