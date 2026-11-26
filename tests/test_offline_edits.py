"""Offline-safe editing (src/mobile/js/51-drafts.js).

The client's save logic is run in node, as shipped in the assembled shell,
against the server's own /api/fs/read and /api/fs/write handlers (app.py),
served over HTTP by uvicorn on a loopback port: every 200, 409 and hash the
client reacts to is the real server's. The browser around it (IndexedDB, the
DOM) is the smallest stand-in the fragment needs; the drafts store is an
in-memory IndexedDB with the semantics the fragment relies on (transactions
complete after their requests, a get-then-delete in one transaction).

Pinned here: a write never goes out without the hash it is based on, a draft
is only dropped for a write of its own text, text typed during a save stays
an edit, a lost answer is not a conflict, a moved file is a conflict and is
never overwritten by a waiting save, and the line merge refuses touching edits.
"""

import importlib.util
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import app as A  # noqa: E402


def _load_build_mobile():
    spec = importlib.util.spec_from_file_location("build_mobile", REPO / "build_mobile.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _js_chunk(src, head):
    """One top-level declaration out of `src` (test_build.py's rule): a
    function brace to brace, a const to its semicolon."""
    at = src.index("\n" + head) + 1
    if head.startswith("const"):
        return src[at:src.index(";\n", at) + 1]
    return src[at:src.index("\n}\n", at) + 3]


@pytest.fixture(scope="module")
def doc():
    return _load_build_mobile().assemble()


@pytest.fixture(scope="module")
def server():
    """The real fs handlers, alone on a loopback port: no auth middleware, no
    lifespan (which would start the pane watcher), nothing but the routes the
    editor's save path calls."""
    import uvicorn
    from fastapi import FastAPI

    mini = FastAPI()
    mini.get("/api/fs/read")(A.api_fs_read)
    mini.post("/api/fs/write")(A.api_fs_write)
    mini.get("/api/version")(A.api_version)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(mini, host="127.0.0.1", port=port,
                                        log_level="error", lifespan="off"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.02)
    assert srv.started
    yield "http://127.0.0.1:%d" % port
    srv.should_exit = True
    t.join(timeout=5)


IDB_MOCK = r"""
// IndexedDB, in memory: a store exists only once the page's onupgradeneeded
// has created it (a transaction on a store never created throws, as the real
// one does), its indexes are the ones the upgrade declared, and a transaction
// completes once a turn passes with no request left (requests queued by an
// onsuccess run in the same transaction).
const rows = new Map();
const idb = { version: 0, stores: {} };
function req(run) {
  const r = { onsuccess: null, result: undefined };
  r._run = () => { r.result = run(); if (r.onsuccess) r.onsuccess(); };
  return r;
}
function makeTx(name) {
  const def = idb.stores[name];
  if (!def) throw new Error("NotFoundError: no object store " + name);
  const q = [];
  let done = false;
  const tx = { oncomplete: null, onerror: null, onabort: null };
  const push = (r) => { q.push(r); return r; };
  const byIndex = (iname, v) => {
    const kp = def.indexes[iname];
    if (!kp) throw new Error("NotFoundError: no index " + iname);
    return [...rows.values()].filter((x) => x[kp] === v).map((x) => structuredClone(x));
  };
  tx.objectStore = () => ({
    get: (k) => push(req(() => (rows.has(k) ? structuredClone(rows.get(k)) : undefined))),
    put: (v) => push(req(() => { rows.set(v[def.keyPath], structuredClone(v)); })),
    delete: (k) => push(req(() => { rows.delete(k); })),
    index: (iname) => ({
      getAll: (v) => push(req(() => byIndex(iname, v))),
      openCursor: (range) => {
        const list = byIndex(iname, range.v);
        let i = 0;
        const r = req(() => null);
        r._run = () => {
          const step = () => {
            r.result = i < list.length ? {
              delete() { rows.delete(list[i][def.keyPath]); },
              continue() { i++; q.push({ _run: step }); },
            } : null;
            if (r.onsuccess) r.onsuccess();
          };
          step();
        };
        return push(r);
      },
    }),
  });
  const loop = () => setTimeout(() => {
    if (!q.length) { if (!done) { done = true; if (tx.oncomplete) tx.oncomplete(); } return; }
    while (q.length) q.shift()._run();
    loop();
  }, 0);
  loop();
  return tx;
}
globalThis.IDBKeyRange = { only: (v) => ({ v }) };
globalThis.indexedDB = {
  open(name, version) {
    const r = { result: null, onsuccess: null, onupgradeneeded: null, onerror: null, onblocked: null };
    setTimeout(() => {
      const db = {
        createObjectStore(sname, o) {
          idb.stores[sname] = { keyPath: o.keyPath, indexes: {} };
          return { createIndex(iname, kp) { idb.stores[sname].indexes[iname] = kp; } };
        },
        transaction: (sname) => makeTx(sname),
        close() {},
      };
      r.result = db;
      if ((version || 1) > idb.version) {
        const old = idb.version;
        idb.version = version || 1;
        if (r.onupgradeneeded) r.onupgradeneeded({ oldVersion: old, newVersion: idb.version });
      }
      r.onsuccess();
    }, 0);
    return r;
  },
};
"""


HARNESS = r"""
// ---- the browser, as little of it as the fragment touches -------------------
const els = new Map();
const fakeEl = () => ({
  addEventListener() {}, setAttribute() {}, focus() {}, appendChild() {},
  querySelectorAll() { return []; }, querySelector() { return null; },
  classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
  dataset: {}, textContent: "", hidden: false,
});
globalThis.$ = (id) => { if (!els.has(id)) els.set(id, fakeEl()); return els.get(id); };
globalThis.document = { addEventListener() {}, hidden: false };
globalThis.window = { addEventListener() {} };
globalThis.location = { origin: "http://example.net" };
globalThis.BASE = "/";
globalThis.cfg = { backend: SERVER };
globalThis.demoApiOn = () => false;
globalThis.activeProfileId = () => "p1";
globalThis.apiURL = (p) => cfg.backend + "/" + p;
globalThis.authHeaders = (x) => Object.assign({}, x || {});
globalThis.netOffline = () => false;
globalThis.baseName = (p) => p.split("/").pop();
globalThis.dbg = () => {};
const toasts = [];
globalThis.toast = (m) => toasts.push(m);
globalThis.hideToast = () => {};
let rejected = 0;
globalThis.rejectToken = () => { rejected++; };
let dialogs = 0;
globalThis.showSheet = () => {};
globalThis.diffRender = () => {};
globalThis.diffNote = () => fakeEl();
globalThis.el = () => fakeEl();
globalThis.svgIcon = () => fakeEl();
globalThis.edLive = new Set();
globalThis.edRec = null;
globalThis.wsLive = new Set();

IDB_MOCK

// ---- the shipped code ----------------------------------------------------------
(0, eval)(PURE);
(0, eval)(FRAGMENT);
draftResolve = () => { dialogs++; };

// A view: the buffer, and typing into it the way the editor's update
// listener reacts to a doc change (29-editor.js).
function mkRec(path, text, hash) {
  let doc = text;
  const rec = {
    path, hash, dirty: false, readOnly: false, owner: draftOwner(),
    baseText: text, loaded: { content: text, hash }, state: null,
    onDirty(r, on) {}, onMark: null,
  };
  rec.view = {
    get state() { return { doc: { toString: () => doc, get length() { return doc.length; } } }; },
    dispatch(tr) {
      const c = tr.changes;
      doc = doc.slice(0, c.from) + c.insert + doc.slice(c.to === undefined ? c.from : c.to);
      if (!rec.dirty) rec.dirty = true;
      draftTouch(rec);
    },
    dom: { parentNode: null }, requestMeasure() {},
  };
  edLive.add(rec);
  return rec;
}
const type = (rec, s) => rec.view.dispatch({ changes: { from: rec.view.state.doc.length, insert: s } });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const draft = (rec) => rows.get(draftKeyOf(rec)) || null;

// Every write body the client sends, to check what it was based on.
const realFetch = globalThis.fetch;
const sent = [];
let lose = 0, delay = 0;
globalThis.fetch = async (url, opts) => {
  const isWrite = String(url).includes("api/fs/write");
  if (isWrite) sent.push(JSON.parse(opts.body));
  if (isWrite && delay) await sleep(delay);
  const r = await realFetch(url, opts);
  if (isWrite && lose) { lose--; throw new TypeError("answer lost"); }
  return r;
};

async function read(path) {
  const r = await realFetch(SERVER + "/api/fs/read?path=" + encodeURIComponent(path));
  return r.json();
}
async function writeDisk(path, text) {
  // Another client's save, through the same server.
  const cur = await read(path);
  const r = await realFetch(SERVER + "/api/fs/write", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content: text, hash: cur.hash || "" }) });
  return r.status;
}

const out = {};
const fresh = () => { for (const r of edLive) clearTimeout(r.draftTimer); edLive.clear(); draftFlags.clear(); };
(async () => {
  fresh();
  // 1. A plain save: clean, draft gone, based on the loaded hash.
  {
    const p = DIR + "/one.txt";
    const cur = await read(p);
    const rec = mkRec(p, cur.content, cur.hash);
    type(rec, "edit");
    await sleep(400);
    const before = draft(rec);
    sent.length = 0;
    const kind = await edSaveOnce(rec, "manual");
    await sleep(50);
    out.plain = { kind, dirty: rec.dirty, draftBefore: before && before.text, draftAfter: draft(rec),
                  baseSent: sent[0].hash === cur.hash, disk: (await read(p)).content };
  }
  fresh();
  // 2. Typing while the save is out: stays an edit, draft keeps the newer text.
  {
    const p = DIR + "/two.txt";
    const cur = await read(p);
    const rec = mkRec(p, cur.content, cur.hash);
    type(rec, "A");
    await sleep(400);
    delay = 300;
    const saving = edSaveOnce(rec, "manual");
    await sleep(100);
    type(rec, "B");
    await saving;
    delay = 0;
    await sleep(400);
    const landed = await read(p);
    out.typing = { dirty: rec.dirty, disk: landed.content, draft: draft(rec) && draft(rec).text,
                   draftBase: draft(rec) && draft(rec).baseHash === landed.hash, hash: rec.hash === landed.hash };
  }
  fresh();
  // 3. The answer is lost: queued, then the flush finds its own text there.
  {
    const p = DIR + "/three.txt";
    const cur = await read(p);
    const rec = mkRec(p, cur.content, cur.hash);
    type(rec, "landed");
    await sleep(400);
    lose = 1;
    const k1 = await edSaveOnce(rec, "manual");
    const queued = draftFlags.get(draftKeyOf(rec));
    const diskAfterLost = (await read(p)).content;
    sent.length = 0;
    dialogs = 0;
    await draftFlush();
    await sleep(50);
    out.lost = { k1, queued, diskAfterLost, dirty: rec.dirty, flag: draftFlags.get(draftKeyOf(rec)) || null,
                 conflict: !!rec.conflict, draft: draft(rec), dialogs,
                 allBased: sent.every((b) => b.hash === cur.hash) };
  }
  fresh();
  // 4. Lost answer, more typing, then the flush: rebased on its own write.
  {
    const p = DIR + "/four.txt";
    const cur = await read(p);
    const rec = mkRec(p, cur.content, cur.hash);
    type(rec, "first");
    lose = 1;
    await edSaveOnce(rec, "manual");
    type(rec, " second");
    await draftFlush();
    await sleep(450);
    out.rebase = { conflict: !!rec.conflict, dirty: rec.dirty, disk: (await read(p)).content, draft: draft(rec) };
  }
  fresh();
  // 5. Moved on the computer while the save waited: conflict, no overwrite,
  //    no dialog; then "Keep mine" against the hash it showed.
  {
    const p = DIR + "/five.txt";
    const cur = await read(p);
    const rec = mkRec(p, cur.content, cur.hash);
    type(rec, "mine");
    await sleep(400);
    lose = 1;            // the save lands unanswered...
    await edSaveOnce(rec, "manual");
    await writeDisk(p, "theirs\n");   // ...and someone else saves after it
    sent.length = 0;
    dialogs = 0;
    await draftFlush();
    await sleep(50);
    const disk = (await read(p)).content;
    out.conflict = { conflict: !!rec.conflict, flag: draftFlags.get(draftKeyOf(rec)) || null, disk, dialogs,
                     theirs: rec.conflict && rec.conflict.text, draft: draft(rec) && draft(rec).text,
                     sentHashes: sent.map((b) => b.hash), base: cur.hash, dirty: rec.dirty };
    const k = await edSaveOnce(rec, "manual", rec.conflict.hash);
    await sleep(50);
    out.keepMine = { k, disk: (await read(p)).content, dirty: rec.dirty, draft: draft(rec) };
  }
  fresh();
  // 6. Create-new: the copy's write never replaces an existing file.
  {
    const p = DIR + "/six (mine).txt";
    out.create = [(await edWrite(p, "", "x")).kind, (await edWrite(p, "", "y")).kind, (await read(p)).content];
  }
  fresh();
  // 7. A 401 keeps the save waiting and asks for the code.
  {
    const p = DIR + "/one.txt";
    const cur = await read(p);
    const rec = mkRec(p, cur.content, cur.hash);
    type(rec, "z");
    const keep = globalThis.fetch;
    globalThis.fetch = async (u, o) => (String(u).includes("api/fs/write")
      ? new Response('{"error":"bad token"}', { status: 401 }) : keep(u, o));
    draftFlags.set(draftKeyOf(rec), "queued");
    rejected = 0;
    await draftFlush();
    globalThis.fetch = keep;
    await sleep(450);
    out.auth = { flag: draftFlags.get(draftKeyOf(rec)) || null, rejected, dirty: rec.dirty, draft: !!draft(rec) };
    edLive.delete(rec);
  }
  fresh();
  // 8. Profile forgotten: its drafts go, another profile's stay.
  {
    rows.set("p2|x\n/a", { key: "p2|x\n/a", owner: "p2|x", profile: "p2", path: "/a", text: "t" });
    const before = [...rows.values()].filter((r) => r.profile === "p1").length;
    await draftDropProfile("p1");
    out.forget = { p1Before: before, p1After: [...rows.values()].filter((r) => r.profile === "p1").length,
                   p2: rows.has("p2|x\n/a") };
  }
  fresh();
  // 9. A draft for another computer is not looked up for this one.
  {
    rows.set("p9|http://example.net\n" + DIR + "/one.txt", { key: "p9|http://example.net\n" + DIR + "/one.txt",
      owner: "p9|http://example.net", profile: "p9", path: DIR + "/one.txt", text: "other", baseHash: "" });
    out.otherOwner = await draftLookup(DIR + "/one.txt");
  }
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _pure(doc):
    names = ["const WS_DIFF_MAX_LINES", "const WS_DIFF_MAX_EDITS", "const WS_PATCH_CONTEXT",
             "function wsSplitLines(", "function wsLineKeys(", "function wsLineDiff("]
    # As vars: an indirect eval keeps a const to itself, and the fragment
    # evaluated after it has to see them.
    return "\n".join(re.sub(r"^const ", "var ", _js_chunk(doc, n)) for n in names)


def _fragment(doc):
    src = (REPO / "src" / "mobile" / "js" / "51-drafts.js").read_text(encoding="utf-8")
    assert src in doc
    return src


def _node(tmp_path, name, code):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    f = tmp_path / name
    f.write_text(code, encoding="utf-8")
    r = subprocess.run([node, str(f)], capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode()
    return json.loads(r.stdout.decode().strip().splitlines()[-1])


@pytest.fixture(scope="module")
def run(doc, server, tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("drafts")
    for n, text in [("one", "one\n"), ("two", "two\n"), ("three", "three\n"),
                    ("four", "four\n"), ("five", "five\n")]:
        (tmp_path / (n + ".txt")).write_text(text)
    code = ("const SERVER = %s;\nconst DIR = %s;\nconst PURE = %s;\nconst FRAGMENT = %s;\n"
            % (json.dumps(server), json.dumps(str(tmp_path)), json.dumps(_pure(doc)),
               json.dumps(_fragment(doc)))) + HARNESS.replace("IDB_MOCK", IDB_MOCK)
    return _node(tmp_path, "drafts.cjs", code)


def test_a_save_is_based_on_its_hash_and_drops_its_own_draft(run):
    p = run["plain"]
    assert p["kind"] == "ok" and p["dirty"] is False
    assert p["draftBefore"] == "one\nedit"
    assert p["draftAfter"] is None
    assert p["baseSent"] is True
    assert p["disk"] == "one\nedit"


def test_text_typed_during_a_save_stays_an_edit_with_its_draft(run):
    t = run["typing"]
    assert t["disk"] == "two\nA"
    assert t["dirty"] is True
    assert t["draft"] == "two\nAB"
    # The newer text is now based on the version that landed.
    assert t["draftBase"] is True and t["hash"] is True


def test_a_lost_answer_is_not_a_conflict(run):
    lost = run["lost"]
    assert lost["k1"] == "offline" and lost["queued"] == "queued"
    assert lost["diskAfterLost"] == "three\nlanded"
    assert lost["dirty"] is False and lost["flag"] is None and lost["conflict"] is False
    assert lost["draft"] is None and lost["dialogs"] == 0
    # The flush went out against the base it had, never an overwrite.
    assert lost["allBased"] is True


def test_a_lost_answer_then_more_typing_rebases_on_its_own_write(run):
    r = run["rebase"]
    assert r["conflict"] is False and r["dirty"] is False
    assert r["disk"] == "four\nfirst second"
    assert r["draft"] is None


def test_a_waiting_save_never_overwrites_a_moved_file(run):
    c = run["conflict"]
    assert c["conflict"] is True and c["flag"] == "conflict"
    assert c["disk"] == "theirs\n"
    assert c["dialogs"] == 0
    assert c["theirs"] == "theirs\n"
    assert c["draft"] == "five\nmine" and c["dirty"] is True
    assert c["sentHashes"] and all(h == c["base"] for h in c["sentHashes"])
    k = run["keepMine"]
    assert k["k"] == "ok" and k["disk"] == "five\nmine" and k["dirty"] is False and k["draft"] is None


def test_a_copy_is_written_create_new(run):
    assert run["create"] == ["ok", "conflict", "x"]


def test_a_401_keeps_the_save_waiting(run):
    a = run["auth"]
    assert a["flag"] == "queued" and a["rejected"] == 1 and a["dirty"] is True and a["draft"] is True


def test_forgetting_a_computer_drops_only_its_drafts(run):
    f = run["forget"]
    assert f["p1Before"] > 0 and f["p1After"] == 0 and f["p2"] is True


def test_another_computers_draft_is_not_applied(run):
    assert run["otherOwner"] is None


MERGE = r"""
(0, eval)(PURE);
(0, eval)(MERGE_SRC);
const T = (n, f) => Array.from({ length: n }, (_, i) => f(i + 1)).join("\n") + "\n";
const base = T(10, (i) => "l" + i);
const edit = (t, n, s) => t.replace("l" + n + "\n", s + "\n");
console.log(JSON.stringify({
  apart: draftMerge3(base, edit(base, 2, "M2"), edit(base, 8, "T8")),
  same: draftMerge3(base, edit(base, 5, "X"), edit(base, 5, "X")),
  overlap: draftMerge3(base, edit(base, 5, "M"), edit(base, 5, "T")),
  touching: draftMerge3(base, edit(base, 5, "M"), edit(base, 6, "T")),
  bothInsertAtEnd: draftMerge3(base, base + "m\n", base + "t\n"),
  noEol: draftMerge3("a\nb\nc", "A\nb\nc", "a\nb\nC"),
  // Repeated lines: deleting one "x" could be any of them, so it touches the
  // edit of the first whichever one the diff picked.
  slideDelete: draftMerge3("x\nx\nx\n", "x\nx\n", "y\nx\nx\n"),
  slideInsert: draftMerge3("a\nx\nb\n", "a\nx\nx\nb\n", "a\nX\nb\n"),
  repeatsApart: draftMerge3("x\nx\na\nb\nc\nx\n", "x\na\nb\nc\nx\n", "x\nx\na\nb\nc\ny\n"),
  // Both sides deleted "an a", maybe not the same one: the equal hunks are
  // not taken as one edit.
  sameDeleteOfRepeats: draftMerge3("a\na\nx\ny\nz\n", "a\nx\ny\nz\n", "a\nx\ny\nZ\n"),
  unified: draftUnified("a\nb\nc\n", "a\nB\nc\n").text,
  copies: [draftCopyPath("/d/f.txt", 1), draftCopyPath("/d/f.txt", 3), draftCopyPath("/d/.bashrc", 1),
           draftCopyPath("/d/Makefile", 2)],
}));
"""


def test_the_line_merge_takes_apart_edits_and_refuses_touching_ones(doc, tmp_path):
    frag = _fragment(doc)
    src = re.search(r"^var DRAFT_DIFF_MAX_ROWS = .*;$", frag, re.M).group(0) + "\n"
    src += "\n".join(_js_chunk(frag, h) for h in
                     ["function draftSlide(", "function draftMerge3(", "function draftUnified(",
                      "function draftCopyPath("])
    code = ("const PURE = %s;\nconst MERGE_SRC = %s;\n" % (json.dumps(_pure(doc)), json.dumps(src))) + MERGE
    out = _node(tmp_path, "merge.cjs", code)
    base = "".join("l%d\n" % i for i in range(1, 11))
    assert out["apart"] == base.replace("l2\n", "M2\n").replace("l8\n", "T8\n")
    # The same edit on both sides touches itself: refused, like any overlap.
    assert out["same"] is None
    assert out["overlap"] is None
    assert out["touching"] is None
    assert out["bothInsertAtEnd"] is None
    assert out["noEol"] == "A\nb\nC"
    assert out["slideDelete"] is None
    assert out["slideInsert"] is None
    assert out["repeatsApart"] == "x\na\nb\nc\ny\n"
    assert out["sameDeleteOfRepeats"] is None
    assert out["unified"] == "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    assert out["copies"] == ["/d/f (mine).txt", "/d/f (mine 3).txt", "/d/.bashrc (mine)",
                             "/d/Makefile (mine 2)"]


# ---- the review fixes, through the shipped handlers ---------------------------
#
# Each page of the app is its own node `vm` context running the fragment (and
# the editor's own close and open code where a finding is about them), with
# its own module state and its own fake DOM, over one shared IndexedDB and the
# real server handlers. Two backends are two servers on two ports serving the
# same disk, so a request built from the wrong profile shows up as a request
# to the wrong port. Nothing here replaces a handler: the dialog's buttons are
# the functions the buttons call, the flush is draftFlush.

PAGE = r"""
var page = { toasts: [], rejected: 0, confirms: 0, confirmAnswer: false, profile: "p1", built: 0,
             onEnsure: null, appConfirms: 0, appConfirmAnswer: true };
var els = new Map();
function fakeEl(id) {
  const cls = new Set();
  const e = {
    id, children: [], _text: "", dataset: {}, style: {}, hidden: false, disabled: false,
    addEventListener() {}, setAttribute() {}, focus() {}, remove() {},
    appendChild(c) { e.children.push(c); return c; },
    querySelectorAll() { return []; }, querySelector() { return null; },
    classList: {
      toggle(c, on) { if (on === undefined) on = !cls.has(c); if (on) cls.add(c); else cls.delete(c); },
      add(c) { cls.add(c); }, remove(c) { cls.delete(c); }, contains(c) { return cls.has(c); },
    },
  };
  Object.defineProperty(e, "textContent", {
    get() { return [e._text].concat(e.children.map((c) => c.textContent || "")).filter(Boolean).join(" "); },
    set(v) { e._text = v; e.children = []; },
  });
  return e;
}
function $(id) { if (!els.has(id)) els.set(id, fakeEl(id)); return els.get(id); }
function el(tag, attrs, ...kids) {
  const e = fakeEl();
  e.tag = tag; e.attrs = attrs || {};
  for (const k of kids) if (k != null) e.appendChild(typeof k === "string" ? { textContent: k } : k);
  return e;
}
var document = { addEventListener() {}, removeEventListener() {}, hidden: false };
var window = { addEventListener() {} };
var location = { origin: "http://example.net" };
var BASE = "/";
var cfg = { backend: SERVER_A };
function demoApiOn() { return false; }
function activeProfileId() { return page.profile; }
function apiURL(p) { return cfg.backend + "/" + p; }
function authHeaders(x) { return Object.assign({}, x || {}); }
function netOffline() { return false; }
function baseName(p) { return p.split("/").pop(); }
function dbg() {}
function toast(m) { page.toasts.push(m); }
function hideToast() {}
function rejectToken() { page.rejected++; }
// One sheet at a time, as the real one: showing one hides the rest.
function showSheet(on, id) {
  $("sheet-conflict").classList.remove("show");
  if (on) $(id).classList.add("show");
}
function diffRender() {}
function diffNote(t) { return el("div", {}, t); }
function svgIcon() { return fakeEl(); }
var edLive = new Set();
var edRec = null, edView = null, edDirty = false, edPath = "", edReadOnly = false, edReturn = null;
var wsLive = new Set();
function confirm() { page.confirms++; return page.confirmAnswer; }
var history = { pushState() {} };
function filesActivePath() { return ""; }
function loadDir() {}
function isWideLayout() { return true; }
function syncChrome() {}
async function appConfirm() { page.appConfirms++; return page.appConfirmAnswer; }
function edShortRef(r) { return r; }
// What openEditor needs around it (29-editor.js).
function usageCount() {}
async function fsReadText(path) {
  const r = await fetch(apiURL("api/fs/read?path=" + encodeURIComponent(path)));
  return r.ok ? r.json() : null;
}
// A read at a git ref; page.badRef answers it as a ref gone from the repo,
// after running page.onRef (a switch of computer while it was out).
async function fsReadTextAt(path, ref) {
  if (ref && page.badRef) { if (page.onRef) await page.onRef(); return { error: "bad_ref" }; }
  return fsReadText(path);
}
async function ensureCM() { if (page.onEnsure) await page.onEnsure(); }
function canDownload() { return true; }
function edSetDirty(on) { edDirty = on; }
function edSyncWrapButton() {}
function edSyncVimButton() {}
function dockFileView() { return false; }
function edShowLine() {}
function edBuild(content, path) {
  page.built++;
  edRec = mkRec(path, content, "");
  edRec.onDirty = (r, on) => edSetDirty(on);
}
function closeEditor() {
  if (edRec) { edDropView(edRec); edRec = null; }
  edSetDirty(false);
}
// What switchProfile needs around it (40-profiles.js): the teardown is the
// test's own (page.onTeardown), the rest records that the switch happened.
var profileGen = 0, profileSwitchSeq = 0, currentSession = null, activeVerdict = null;
var profileChecks = new Map();
function readProfiles() { return [{ id: "p1" }, { id: "p2" }]; }
function showProfileMenu() {}
function closeTerminal() {}
function dropAllFileViews() { if (page.onTeardown) page.onTeardown(); }
function filesResetForProfile() {}
function diffResetForProfile() {}
function browserResetForProfile() {}
function sessionsResetForProfile() {}
function versionResetForProfile() {}
function pairResetForProfile() {}
function setActiveProfile(id) { page.switched = (page.switched || 0) + 1; page.profile = id; cfg.backend = SERVER_B; }
function syncProfileUI() {}
function fetchServerVersion() {}
function loadSessions() {}
function loadSessionsAfterProbe() {}
function pushResetForProfile() {}
// What hcOpenEditor needs around it (49-human-code.js): a brief full screen.
var hcGen = 0, hcView = null, hcOwner = null, hcEdRow = null, filesPanes = {};
function hcShowAgain() {}
async function hcOtherRow() { return null; }
"""

HELPERS = r"""
// A view: the buffer, and typing into it the way the editor's update
// listener reacts to a doc change (29-editor.js).
function mkRec(path, text, hash) {
  let doc = text;
  const rec = {
    path, hash, dirty: false, readOnly: false, owner: draftOwner(),
    baseText: text, loaded: { content: text, hash }, state: null,
    onDirty(r, on) {}, onMark: null,
  };
  rec.view = {
    get state() { return { doc: { toString: () => doc, get length() { return doc.length; } } }; },
    dispatch(tr) {
      const c = tr.changes;
      doc = doc.slice(0, c.from) + c.insert + doc.slice(c.to === undefined ? c.from : c.to);
      if (!rec.dirty) { rec.dirty = true; rec.onDirty(rec, true); }
      draftTouch(rec);
    },
    dom: { parentNode: null }, requestMeasure() {}, destroy() {},
  };
  edLive.add(rec);
  return rec;
}
function type(rec, s) { rec.view.dispatch({ changes: { from: rec.view.state.doc.length, insert: s } }); }
// The unsaved list's rows as text: name, folder, state word, time.
async function listRows() {
  fillDraftsMenu(await draftList(draftOwner()));
  return $("drafts-menu").children.map((r) => r.textContent);
}
"""

FIXES = r"""
const vm = require("vm");
IDB_MOCK

const realFetch = globalThis.fetch;
const log = [];
let hook = null, offline = false;
globalThis.fetch = async (url, opts) => {
  url = String(url);
  const isWrite = url.includes("api/fs/write");
  log.push({ url, isWrite, body: isWrite ? JSON.parse(opts.body) : null });
  if (offline) throw new TypeError("Failed to fetch");
  if (hook && hook.before) await hook.before(url, opts);
  const r = await realFetch(url, opts);
  if (hook && hook.after) await hook.after(url, r);
  return r;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const toB = () => log.filter((x) => x.url.startsWith(SERVER_B)).length;

// Web Locks, one lock manager for every page of the test: a page holds its
// lock until killPage, which is the page going away (a closed tab, an evicted
// one, the old page of a reload).
const heldLocks = new Set();
const locksApi = {
  request(name, cb) { heldLocks.add(name); return Promise.resolve().then(() => cb({ name })); },
  async query() { return { held: [...heldLocks].map((name) => ({ name, mode: "exclusive" })), pending: [] }; },
};
function killPage(P) {
  for (const r of P.edLive) clearTimeout(r.draftTimer);
  heldLocks.delete("ptui-draft-page-" + P.draftPageId);
}

function makePage(opts) {
  const ctx = { console, setTimeout, clearTimeout, AbortController, TextEncoder, structuredClone,
                indexedDB, IDBKeyRange, fetch: (u, o) => globalThis.fetch(u, o) };
  if (!(opts && opts.noLocks)) ctx.navigator = { locks: locksApi };
  vm.createContext(ctx);
  vm.runInContext("var SERVER_A = " + JSON.stringify(SERVER_A) + ", SERVER_B = " + JSON.stringify(SERVER_B)
                 + ";\n" + PAGE_SRC, ctx);
  vm.runInContext(PURE, ctx);
  vm.runInContext(FRAGMENT, ctx);
  vm.runInContext(EDITOR, ctx);
  vm.runInContext(HELPERS_SRC, ctx);
  vm.runInContext(WS_SRC, ctx);
  return ctx;
}
// A switch to the second computer: another profile at another address.
const switchToB = (P) => { P.page.profile = "p2"; P.cfg.backend = SERVER_B; };

async function read(path) {
  const r = await realFetch(SERVER_A + "/api/fs/read?path=" + encodeURIComponent(path));
  return r.status === 404 ? null : r.json();
}
async function writeDisk(path, text) {
  const cur = await read(path);
  await realFetch(SERVER_A + "/api/fs/write", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content: text, hash: (cur && cur.hash) || "" }) });
}
const recordsFor = (path) => [...rows.values()].filter((r) => r.path === path);
const draftOf = (P, rec) => rows.get(P.draftKeyOf(rec)) || null;

// A rec on `path` with `mine` typed, refused because the computer has `theirs`.
async function inConflict(P, path, mine, theirs, mode) {
  const cur = await read(path);
  const rec = P.mkRec(path, cur.content, cur.hash);
  P.type(rec, mine);
  await sleep(400);
  await writeDisk(path, theirs);
  const kind = await P.edSaveOnce(rec, mode);
  return { rec, kind, base: cur.content };
}

const out = {};
(async () => {
  // F1. "Save mine as a copy", dismissed while the copy is out, typed into,
  //     and another file's conflict opened: the copy's completion touches
  //     neither buffer and leaves the other file's dialog up.
  try {
    const P = makePage();
    const b = await inConflict(P, DIR + "/f1b.txt", "B", "theirsB\n", "flush");
    const a = await inConflict(P, DIR + "/f1a.txt", "A", "theirsA\n", "manual");
    const shownForA = P.$("sheet-conflict").classList.contains("show") && P.conflictRec === a.rec;
    hook = { before: async (url) => { if (url.includes("api/fs/write")) await sleep(300); } };
    const copying = P.conflictSaveCopy();
    await sleep(100);
    P.showSheet(false);                 // Escape / the scrim
    P.type(a.rec, " more");
    P.draftResolve(b.rec);              // another file's conflict, opened
    await copying;
    hook = null;
    await sleep(450);
    out.f1 = {
      shownForA, copy: (await read(DIR + "/f1a (mine).txt")).content,
      aText: P.edText(a.rec), aDirty: a.rec.dirty, aConflict: !!a.rec.conflict,
      aDraft: draftOf(P, a.rec) && draftOf(P, a.rec).text,
      bText: P.edText(b.rec), bDirty: b.rec.dirty, bConflict: !!b.rec.conflict,
      bDialog: P.$("sheet-conflict").classList.contains("show") && P.conflictRec === b.rec,
    };
    // Dismissed and typed into, with no other dialog opened since.
    P.showSheet(false);
    const t = await inConflict(P, DIR + "/f1d.txt", "T", "theirsT\n", "manual");
    hook = { before: async (url) => { if (url.includes("api/fs/write")) await sleep(300); } };
    const copying2 = P.conflictSaveCopy();
    await sleep(100);
    P.showSheet(false);
    P.type(t.rec, " more");
    await copying2;
    hook = null;
    await sleep(450);
    out.f1typed = { text: P.edText(t.rec), dirty: t.rec.dirty, conflict: !!t.rec.conflict,
                    draft: draftOf(P, t.rec) && draftOf(P, t.rec).text,
                    copy: (await read(DIR + "/f1d (mine).txt")).content };
    // Undisturbed, the same button finishes the way it always did: the copy
    // written, the tab on the computer's version, clean, its draft gone.
    P.showSheet(false);
    const c = await inConflict(P, DIR + "/f1c.txt", "C", "theirsC\n", "manual");
    await P.conflictSaveCopy();
    await sleep(450);
    out.f1plain = { copy: (await read(DIR + "/f1c (mine).txt")).content, text: P.edText(c.rec),
                    dirty: c.rec.dirty, conflict: !!c.rec.conflict, draft: recordsFor(DIR + "/f1c.txt").length,
                    dialog: P.$("sheet-conflict").classList.contains("show") };
  } catch (e) { out.errors = (out.errors || []).concat(String(e && e.stack || e).slice(0, 400)); }
  // F2. Two pages, one file, different unsaved text: two records, both kept.
  try {
    const P = makePage(), Q = makePage();
    const p = DIR + "/f2.txt";
    const cur = await read(p);
    const rp = P.mkRec(p, cur.content, cur.hash);
    P.type(rp, "P1");
    await sleep(400);
    // Q opens the file: it restores the newest record and adopts it.
    const d = await Q.draftLookup(p);
    const rq = Q.mkRec(p, cur.content, cur.hash);
    Q.draftApply(rq, { content: cur.content, hash: cur.hash }, d);
    Q.type(rq, "Q");
    await sleep(400);
    P.type(rp, "P2");
    await sleep(400);
    const texts = recordsFor(p).map((r) => r.text).sort();
    const newest = await P.draftLookup(p);
    const rows2 = await P.listRows();
    // A third page opening the file gets the newest, and is told of the other.
    const R = makePage();
    const rr = R.mkRec(p, cur.content, cur.hash);
    R.draftApply(rr, { content: cur.content, hash: cur.hash }, await R.draftLookup(p));
    // P saves its text: only P's record goes.
    const k = await P.edSaveOnce(rp, "manual");
    await sleep(100);
    // P's view closed and discarded never takes Q's record either.
    P.draftDiscard(rp);
    await sleep(100);
    // R restored while P was open, so it keeps a record of its own.
    out.f2 = { texts, newest: newest.text, others: newest.others, rows: rows2,
               restored: R.edText(rr), restoredOthers: rr.restored && rr.restored.others,
               k, after: recordsFor(p).filter((r) => r.key !== R.draftKeyOf(rr)).map((r) => r.text) };
  } catch (e) { out.errors = (out.errors || []).concat(String(e && e.stack || e).slice(0, 400)); }
  // F3. A 409 from computer A, then a switch to B before the follow-up read:
  //     nothing goes to B, the rec and its draft are as they were.
  try {
    const P = makePage();
    const p = DIR + "/f3.txt";
    const cur = await read(p);
    const rec = P.mkRec(p, cur.content, cur.hash);
    P.type(rec, "mine");
    await sleep(400);
    await writeDisk(p, "theirs\n");
    log.length = 0;
    hook = { after: async (url, r) => { if (url.includes("api/fs/write") && r.status === 409) switchToB(P); } };
    const kind = await P.edSaveOnce(rec, "manual");
    hook = null;
    await sleep(100);
    out.f3 = { kind, toB: toB(), dirty: rec.dirty, conflict: !!rec.conflict,
               flag: P.draftFlags.get(P.draftKeyOf(rec)) || null, draft: draftOf(P, rec) && draftOf(P, rec).text };
    // The copy's name retry after a switch: no file is made on B.
    const Q = makePage();
    const c = await inConflict(Q, DIR + "/f3c.txt", "mine", "theirs\n", "manual");
    log.length = 0;
    hook = { after: async (url, r) => { if (url.includes("api/fs/write") && r.status === 409) switchToB(Q); } };
    await Q.conflictSaveCopy();
    hook = null;
    out.f3copy = { toB: toB(), second: await read(DIR + "/f3c (mine 2).txt"), dirty: c.rec.dirty,
                   conflict: !!c.rec.conflict, text: Q.edText(c.rec) };
  } catch (e) { out.errors = (out.errors || []).concat(String(e && e.stack || e).slice(0, 400)); }
  // F4. A switch while an open waits: no buffer is built, and a draft is
  //     never applied to another computer's rec.
  try {
    const P = makePage();
    const p = DIR + "/f4.txt";
    const cur = await read(p);
    const r0 = P.mkRec(p, cur.content, cur.hash);
    P.type(r0, "on A");
    await sleep(400);
    P.edLive.delete(r0);
    P.page.onEnsure = async () => switchToB(P);
    await P.openEditor(p, {});
    P.page.onEnsure = null;
    const built = P.page.built;
    // The draft handed to a rec of B's directly.
    const d = await makePage().draftLookup(p);
    const rb = P.mkRec(p, cur.content, cur.hash);   // P is on B now
    P.draftApply(rb, { content: cur.content, hash: cur.hash }, d);
    out.f4 = { built, ownerA: d.owner.startsWith("p1|"), recB: rb.owner.startsWith("p2|"),
               applied: P.edText(rb), dirty: rb.dirty };
  } catch (e) { out.errors = (out.errors || []).concat(String(e && e.stack || e).slice(0, 400)); }
  // F5. A computer whose file is not valid UTF-8: its lossy text is never
  //     "my text is there", never resent over, never offered to merge.
  try {
    const P = makePage();
    const p = DIR + "/f5.txt";            // bytes "ab\xff\n" on disk
    const lossy = (await read(p)).content;
    const rec = P.mkRec(p, "old\n", "0000");
    P.type(rec, "");
    rec.view.dispatch({ changes: { from: 0, to: 4, insert: lossy } });
    const k1 = await P.edSaveOnce(rec, "manual");
    const merge = P.$("btn-conflict-merge").hidden, theirs = P.$("btn-conflict-theirs").hidden;
    const c1 = rec.conflict && { text: rec.conflict.text, lossy: rec.conflict.lossy };
    P.showSheet(false);
    // An earlier write "landed unanswered" whose text equals the lossy one.
    const rec2 = P.mkRec(p, "old\n", "0000");
    rec2.unanswered = [lossy];
    P.type(rec2, "");
    rec2.view.dispatch({ changes: { from: 0, to: 4, insert: lossy + "more" } });
    log.length = 0;
    const k2 = await P.edSaveOnce(rec2, "flush");
    out.f5 = { k1, dirty: rec.dirty, c1, merge, theirs, k2, writes2: log.filter((x) => x.isWrite).length,
               draft: !!draftOf(P, rec), conflict2: !!rec2.conflict };
  } catch (e) { out.errors = (out.errors || []).concat(String(e && e.stack || e).slice(0, 400)); }
  // F6. "Merge both" puts the merge in the buffer as an unsaved edit and
  //     writes nothing.
  try {
    const P = makePage();
    const p = DIR + "/f6.txt";            // "a\nb\nc\nd\ne\n"
    const cur = await read(p);
    const rec = P.mkRec(p, cur.content, cur.hash);
    rec.view.dispatch({ changes: { from: 0, to: 1, insert: "A" } });
    await sleep(400);
    await writeDisk(p, "a\nb\nc\nd\nE\n");
    await P.edSaveOnce(rec, "manual");
    const offered = !P.$("btn-conflict-merge").hidden;
    log.length = 0;
    P.conflictMerge();
    await sleep(400);
    out.f6 = { offered, writes: log.filter((x) => x.isWrite).length, text: P.edText(rec), dirty: rec.dirty,
               merged: !!rec.merged, disk: (await read(p)).content, draft: draftOf(P, rec) && draftOf(P, rec).text };
  } catch (e) { out.errors = (out.errors || []).concat(String(e && e.stack || e).slice(0, 400)); }
  // F7. The waiting save belongs to the draft: leaving the phone editor with
  //     it asks nothing, and the flush sends it with no view open; a moved
  //     file is marked in the list and not resent; a dirty file that is not
  //     waiting still asks.
  try {
    const P = makePage();
    const p = DIR + "/f7.txt", p2 = DIR + "/f7b.txt", p3 = DIR + "/f7c.txt", p4 = DIR + "/f7d.txt";
    const c1 = await read(p), c2 = await read(p2), c3 = await read(p3), c4 = await read(p4);
    offline = true;
    // The phone editor's buffer, saved offline, then left with back.
    P.edRec = P.mkRec(p, c1.content, c1.hash);
    P.edRec.onDirty = (r, on) => P.edSetDirty(on);
    P.type(P.edRec, "queued");
    const k1 = await P.edSaveOnce(P.edRec, "manual");
    P.page.confirms = 0;
    P.page.toasts.length = 0;
    P.editorPopped();
    await sleep(50);
    const leftConfirms = P.page.confirms, leftToasts = P.page.toasts.slice(), edRecAfter = P.edRec;
    // A workspace tab of another file, queued and closed.
    const t2 = P.mkRec(p2, c2.content, c2.hash);
    P.type(t2, "tab");
    await P.edSaveOnce(t2, "manual");
    P.edDropView(t2);
    // A buffer parked with its session, queued.
    const t3 = P.mkRec(p3, c3.content, c3.hash);
    P.type(t3, "parked");
    await P.edSaveOnce(t3, "manual");
    P.edDropView(t3, true);
    // A dirty buffer that is not waiting: back still asks.
    P.edRec = P.mkRec(p4, c4.content, c4.hash);
    P.edRec.onDirty = (r, on) => P.edSetDirty(on);
    P.type(P.edRec, "plain");
    P.page.confirms = 0;
    P.page.confirmAnswer = false;
    P.editorPopped();
    const plainConfirms = P.page.confirms;
    P.edLive.delete(P.edRec);
    P.edRec = null;
    await sleep(400);
    const rowsQueued = await P.listRows();
    // Someone saves the tab's file meanwhile; then the computer is back.
    offline = false;
    await writeDisk(p2, "theirs\n");
    log.length = 0;
    P.page.toasts.length = 0;
    await P.draftFlush();
    await sleep(400);
    const writes = log.filter((x) => x.isWrite).map((x) => x.body.path);
    const rowsAfter = await P.listRows();
    out.f7 = {
      k1, leftConfirms, leftToasts, closed: edRecAfter === null, plainConfirms,
      rowsQueued, rowsAfter, writes, toasts: P.page.toasts,
      disk1: (await read(p)).content, disk2: (await read(p2)).content, disk3: (await read(p3)).content,
      left: [p, p2, p3].map((x) => recordsFor(x).map((r) => r.text)),
      flag2: [...P.draftFlags.values()], t3dirty: t3.dirty,
      dialog: P.$("sheet-conflict").classList.contains("show"),
    };
    // Flushed again: the refused one is not resent.
    log.length = 0;
    await P.draftFlush();
    out.f7.again = log.filter((x) => x.isWrite).length;
  } catch (e) { out.errors = (out.errors || []).concat(String(e && e.stack || e).slice(0, 400)); }
  // G1. A page takes over a record in place only once the page that wrote it
  //     is gone (Web Locks); while it is open, the opener keeps its own.
  try {
    const p = DIR + "/g1.txt";
    const cur = await read(p);
    const P = makePage(), Q = makePage();
    const rp = P.mkRec(p, cur.content, cur.hash);
    P.type(rp, "A");
    await sleep(400);
    // Q opens the file while P is open, and types; P never writes again.
    const rq = Q.mkRec(p, cur.content, cur.hash);
    Q.draftApply(rq, { content: cur.content, hash: cur.hash }, await Q.draftLookup(p));
    Q.type(rq, "B");
    await sleep(400);
    killPage(P);                       // evicted, without writing again
    const alive = recordsFor(p).map((r) => r.text).sort();
    // The reload of one page: the old page is gone, the new one restores,
    // types, and there is still one record.
    const r1 = DIR + "/g1r.txt";
    const c1 = await read(r1);
    const S1 = makePage();
    const s1 = S1.mkRec(r1, c1.content, c1.hash);
    S1.type(s1, "X");
    await sleep(400);
    killPage(S1);
    const S2 = makePage();
    const s2 = S2.mkRec(r1, c1.content, c1.hash);
    S2.draftApply(s2, { content: c1.content, hash: c1.hash }, await S2.draftLookup(r1));
    const restored = S2.edText(s2);
    S2.type(s2, "Y");
    await sleep(400);
    const reload = recordsFor(r1).map((r) => r.text);
    // No Web Locks (an http LAN address): nothing throws at load, and the
    // record is taken over in place, as before.
    const r2 = DIR + "/g1n.txt";
    const c2 = await read(r2);
    const N1 = makePage({ noLocks: true }), N2 = makePage({ noLocks: true });
    const n1 = N1.mkRec(r2, c2.content, c2.hash);
    N1.type(n1, "M");
    await sleep(400);
    const n2 = N2.mkRec(r2, c2.content, c2.hash);
    N2.draftApply(n2, { content: c2.content, hash: c2.hash }, await N2.draftLookup(r2));
    N2.type(n2, "N");
    await sleep(400);
    out.g1 = { alive, restored, reload, noLocks: recordsFor(r2).map((r) => r.text) };
    for (const X of [Q, S2, N1, N2]) killPage(X);
  } catch (e) { out.errors = (out.errors || []).concat("g1 " + String(e && e.stack || e).slice(0, 400)); }
  // G2. Leaving a waiting buffer skips the question only once the store holds
  //     its current text: text it refuses (too large) gets the question.
  try {
    const P = makePage();
    const p = DIR + "/g2.txt", p2 = DIR + "/g2b.txt", p3 = DIR + "/g2c.txt";
    const c = await read(p), c2 = await read(p2), c3 = await read(p3);
    const big = "x".repeat(200);
    offline = true;
    // The editor screen, left with back.
    P.edRec = P.mkRec(p, c.content, c.hash);
    P.edRec.onDirty = (r, on) => P.edSetDirty(on);
    P.type(P.edRec, "A");
    await P.edSaveOnce(P.edRec, "manual");
    await sleep(400);
    const ed = P.edRec;
    P.DRAFT_MAX_BYTES = 100;
    P.type(ed, big);                   // within the debounce, and too large
    P.page.confirms = 0;
    P.page.confirmAnswer = false;
    P.editorPopped();
    await sleep(100);
    const edOut = { confirms: P.page.confirms, open: P.edRec === ed, text: P.edText(ed).length };
    P.DRAFT_MAX_BYTES = 2 * 1024 * 1024;
    // Small text typed just before back: written first, no question, closed.
    P.type(ed, "C");
    P.page.confirms = 0;
    P.editorPopped();
    await sleep(100);
    edOut.smallConfirms = P.page.confirms;
    edOut.smallClosed = P.edRec === null;
    edOut.smallStored = (recordsFor(p)[0] || {}).text === c.content + "A" + big + "C";
    // A workspace tab, closed with its own cross.
    const ws = P.makeWsTabs();
    const r2 = P.mkRec(p2, c2.content, c2.hash);
    const t2 = ws.addTab(r2, "g2b.txt");
    P.type(r2, "A");
    await P.edSaveOnce(r2, "manual");
    await sleep(400);
    P.DRAFT_MAX_BYTES = 100;
    P.type(r2, big);
    P.page.appConfirms = 0;
    P.page.appConfirmAnswer = false;
    await ws.close(t2);
    const wsOut = { asked: P.page.appConfirms, closed: t2.closed, text: P.edText(r2).length };
    // The pane's close: the same tab counted and asked about.
    P.page.appConfirms = 0;
    wsOut.count = ws.dirtyCount();
    wsOut.paneOk = await ws.askDiscardAll();
    wsOut.paneAsked = P.page.appConfirms;
    P.DRAFT_MAX_BYTES = 2 * 1024 * 1024;
    // Small text just before the close: kept, no question.
    const r3 = P.mkRec(p3, c3.content, c3.hash);
    const t3 = ws.addTab(r3, "g2c.txt");
    P.type(r3, "A");
    await P.edSaveOnce(r3, "manual");
    await sleep(400);
    P.type(r3, "D");
    P.page.appConfirms = 0;
    wsOut.paneCountSmall = ws.dirtyCount() >= 1;
    await ws.close(t3);
    wsOut.smallAsked = P.page.appConfirms;
    wsOut.smallClosed = t3.closed;
    wsOut.smallStored = (recordsFor(p3)[0] || {}).text === c3.content + "AD";
    P.page.appConfirmAnswer = true;
    offline = false;
    for (const r of P.edLive) clearTimeout(r.draftTimer);
    P.draftFlags.clear();
    out.g2 = { ed: edOut, ws: wsOut };
    killPage(P);
  } catch (e) { offline = false; out.errors = (out.errors || []).concat("g2 " + String(e && e.stack || e).slice(0, 400)); }
  // G3. A discard from the unsaved list takes only the text the row showed.
  try {
    const p = DIR + "/g3.txt";
    const cur = await read(p);
    const P = makePage(), L = makePage();
    const rp = P.mkRec(p, cur.content, cur.hash);
    P.type(rp, "A");
    await sleep(400);
    const row = (await L.draftList(L.draftOwner())).find((d) => d.path === p);
    killPage(P);
    // Another page takes the record over (P is gone) and rewrites it.
    const Q = makePage();
    const rq = Q.mkRec(p, cur.content, cur.hash);
    Q.draftApply(rq, { content: cur.content, hash: cur.hash }, await Q.draftLookup(p));
    Q.type(rq, "B");
    await sleep(400);
    const sameKey = Q.draftKeyOf(rq) === row.key;
    await L.draftDiscardFromMenu(row);
    const afterStale = recordsFor(p).map((r) => r.text);
    const told = L.page.toasts.some((t) => t.includes("changed since"));
    out.g3 = { sameKey, afterStale, told };
    // The view holding the record has text not yet written: not reset.
    const row2 = (await Q.draftList(Q.draftOwner())).find((d) => d.path === p);
    Q.type(rq, "C");
    await Q.draftDiscardFromMenu(row2);
    const viewKept = Q.edText(rq);
    await sleep(400);
    // Unchanged since drawn: discarded, and the view goes back.
    const row3 = (await Q.draftList(Q.draftOwner())).find((d) => d.path === p);
    await Q.draftDiscardFromMenu(row3);
    await sleep(50);
    Object.assign(out.g3, { viewKept, afterFresh: recordsFor(p).length, viewReset: Q.edText(rq), dirty: rq.dirty });
    killPage(L); killPage(Q);
  } catch (e) { out.errors = (out.errors || []).concat("g3 " + String(e && e.stack || e).slice(0, 400)); }
  // G4. A 409 ends automatic sending even when the read after it fails: the
  //     next reconnect only reads.
  try {
    let failRead = false;
    const refusedThenDark = {
      before: async (url) => { if (failRead && url.includes("api/fs/read")) { failRead = false; throw new TypeError("gone"); } },
      after: async (url, r) => { if (url.includes("api/fs/write") && r.status === 409) failRead = true; },
    };
    const writes = () => log.filter((x) => x.isWrite).length;
    const P = makePage();
    // Live view, waiting save, file moved, read after the 409 fails.
    const p = DIR + "/g4.txt";
    const cur = await read(p);
    const rec = P.mkRec(p, cur.content, cur.hash);
    P.type(rec, "mine");
    offline = true;
    await P.edSaveOnce(rec, "manual");
    offline = false;
    await writeDisk(p, "theirs\n");
    hook = refusedThenDark;
    await P.draftFlush();
    hook = null;
    const flagLive = P.draftFlags.get(P.draftKeyOf(rec)) || null;
    // The file goes back to the version the buffer was based on: a resent
    // write would now land.
    await writeDisk(p, cur.content);
    log.length = 0;
    await P.draftFlush();
    await sleep(50);
    const live = { flag: flagLive, writes: writes(), after: P.draftFlags.get(P.draftKeyOf(rec)) || null,
                   conflict: !!rec.conflict, disk: (await read(p)).content, dirty: rec.dirty };
    // Stored draft (view closed), same story.
    const q = DIR + "/g4b.txt";
    const cq = await read(q);
    const rq = P.mkRec(q, cq.content, cq.hash);
    P.type(rq, "mine");
    offline = true;
    await P.edSaveOnce(rq, "manual");
    await sleep(400);
    P.edDropView(rq);
    offline = false;
    await writeDisk(q, "theirs\n");
    hook = refusedThenDark;
    log.length = 0;
    await P.draftFlush();
    hook = null;
    const kq = P.draftKeyOf(rq);
    const flagStored = P.draftFlags.get(kq) || null;
    const firstWrites = writes();
    await writeDisk(q, cq.content);
    log.length = 0;
    await P.draftFlush();
    await sleep(50);
    const stored = { flag: flagStored, firstWrites, writes: writes(), after: P.draftFlags.get(kq) || null,
                     disk: (await read(q)).content, kept: recordsFor(q).map((r) => r.text) };
    // Refused because an earlier write of the same text landed unanswered:
    // the reconnect's read finds it there, saved, and writes nothing.
    const w = DIR + "/g4c.txt";
    const cw = await read(w);
    const rw = P.mkRec(w, cw.content, cw.hash);
    P.type(rw, "T");
    hook = { after: async (url) => { if (url.includes("api/fs/write")) { hook = null; throw new TypeError("lost"); } } };
    await P.edSaveOnce(rw, "manual");      // lands, answer lost: queued
    hook = refusedThenDark;
    await P.draftFlush();                 // resent: 409, read fails
    hook = null;
    const flagLanded = P.draftFlags.get(P.draftKeyOf(rw)) || null;
    log.length = 0;
    await P.draftFlush();
    await sleep(50);
    const landed = { flag: flagLanded, writes: writes(), dirty: rw.dirty, after: P.draftFlags.get(P.draftKeyOf(rw)) || null };
    // The kept exception: X landed unanswered, Y typed, Y refused against the
    // old base and the read fails; at the reconnect the read finds X, this
    // buffer's own, and Y goes once against it.
    const x = DIR + "/g4d.txt";
    const cx = await read(x);
    const rx = P.mkRec(x, cx.content, cx.hash);
    P.type(rx, "X");
    hook = { after: async (url) => { if (url.includes("api/fs/write")) { hook = null; throw new TypeError("lost"); } } };
    await P.edSaveOnce(rx, "manual");
    P.type(rx, "Y");
    hook = refusedThenDark;
    await P.draftFlush();
    hook = null;
    const flagOwn = P.draftFlags.get(P.draftKeyOf(rx)) || null;
    log.length = 0;
    await P.draftFlush();
    await sleep(450);
    const own = { flag: flagOwn, writes: writes(), disk: (await read(x)).content, dirty: rx.dirty,
                  conflict: !!rx.conflict };
    out.g4 = { live, stored, landed, own };
    killPage(P);
  } catch (e) { hook = null; offline = false; out.errors = (out.errors || []).concat("g4 " + String(e && e.stack || e).slice(0, 400)); }
  // G5. A git-ref open whose ref is gone, answered after a switch to B: no
  //     fallback read to B, no buffer, no tab state changed.
  try {
    const P = makePage();
    const p = DIR + "/g5.txt";
    P.page.badRef = true;
    P.page.onRef = async () => switchToB(P);
    log.length = 0;
    await P.openEditor(p, { ref: "abc123" });
    const editor = { toB: toB(), built: P.page.built };
    const Q = makePage();
    Q.page.badRef = true;
    Q.page.onRef = async () => switchToB(Q);
    const ws = Q.makeWsTabs();
    const tab = { path: p, kind: "editor", ref: "abc123", key: p + "@abc123", title: "g5.txt @abc123", closed: false };
    ws.byPath.set(tab.key, tab);
    log.length = 0;
    const filled = await ws.fillTab(tab, {});
    out.g5 = { editor, ws: { filled, toB: toB(), ref: tab.ref, key: tab.key, mapped: [...ws.byPath.keys()] } };
    killPage(P); killPage(Q);
  } catch (e) { out.errors = (out.errors || []).concat("g5 " + String(e && e.stack || e).slice(0, 400)); }
  // H1. An adopted record is claimed at once: a reloaded page that restored
  //     text and typed nothing owns it, and a page opening the file after it
  //     starts its own; two pages opening at the same moment never both take it.
  try {
    const p = DIR + "/h1.txt", q = DIR + "/h1b.txt";
    const cur = await read(p), cq = await read(q);
    const open = async (X, path, c) => {
      const r = X.mkRec(path, c.content, c.hash);
      X.draftApply(r, { content: c.content, hash: c.hash }, await X.draftLookup(path));
      return r;
    };
    const replace = (X, r, t) => r.view.dispatch({ changes: { from: 0, to: r.view.state.doc.length, insert: t } });
    const S1 = makePage();
    const s1 = S1.mkRec(p, cur.content, cur.hash);
    S1.type(s1, "X");
    await sleep(400);
    killPage(S1);                         // the reload: the old page goes
    const S2 = makePage();
    const s2 = await open(S2, p, cur);    // restored, nothing typed
    await sleep(400);
    const Q = makePage();
    const rq = await open(Q, p, cur);
    replace(Q, rq, "Q only\n");
    await sleep(400);
    const reload = { restored: S2.edText(s2), forked: Q.draftKeyOf(rq) !== S2.draftKeyOf(s2),
                     texts: recordsFor(p).map((r) => r.text).sort() };
    // Two pages opening the file at the same moment.
    const T1 = makePage();
    const t1 = T1.mkRec(q, cq.content, cq.hash);
    T1.type(t1, "X");
    await sleep(400);
    killPage(T1);
    const A1 = makePage(), B1 = makePage();
    const [da, db] = await Promise.all([A1.draftLookup(q), B1.draftLookup(q)]);
    const ra = A1.mkRec(q, cq.content, cq.hash), rb = B1.mkRec(q, cq.content, cq.hash);
    A1.draftApply(ra, { content: cq.content, hash: cq.hash }, da);
    B1.draftApply(rb, { content: cq.content, hash: cq.hash }, db);
    replace(B1, rb, "B only\n");         // A types nothing
    await sleep(400);
    const both = { shared: [da.shared, db.shared].sort(), keys: A1.draftKeyOf(ra) !== B1.draftKeyOf(rb),
                   texts: recordsFor(q).map((r) => r.text).sort() };
    out.h1 = { reload, both };
    for (const X of [S2, Q, A1, B1]) killPage(X);
  } catch (e) { out.errors = (out.errors || []).concat("h1 " + String(e && e.stack || e).slice(0, 400)); }
  // H2. A buffer torn down with no question whose text the store will not
  //     hold: kept in memory and listed, said so, and still sent when the
  //     computer is back; a switch of computer asks first.
  try {
    const P = makePage();
    const p = DIR + "/h2a.txt", p2 = DIR + "/h2b.txt", p3 = DIR + "/h2c.txt";
    const c = await read(p), c2 = await read(p2), c3 = await read(p3);
    const big = "y".repeat(200);
    // A workspace tab, queued, grown past the limit, its session gone.
    const ws = P.makeWsTabs();
    const r = P.mkRec(p, c.content, c.hash);
    ws.addTab(r, "h2a.txt");
    P.type(r, "A");
    offline = true;
    await P.edSaveOnce(r, "manual");
    await sleep(400);
    P.DRAFT_MAX_BYTES = 100;
    P.type(r, big);
    await sleep(400);
    P.page.toasts.length = 0;
    ws.destroy();
    // A parked editor buffer, grown past the limit, its session gone.
    const r2 = P.mkRec(p2, c2.content, c2.hash);
    P.type(r2, "B" + big);
    await sleep(400);
    P.edDropView(r2, true);
    P.draftReleaseParked(r2);
    await sleep(400);
    const rows = await P.listRows();
    const said = P.page.toasts.filter((t) => t.includes("could not be kept")).length;
    const listed = (await P.draftList(P.draftOwner())).filter((d) => d.path === p || d.path === p2)
      .map((d) => d.text.length).sort((a, b) => a - b);
    // The computer is back: the waiting one is sent from memory, its newest text.
    offline = false;
    await P.draftFlush();
    await sleep(100);
    const disk = (await read(p)).content;
    const after = (await P.draftList(P.draftOwner())).filter((d) => d.path === p).length;
    P.DRAFT_MAX_BYTES = 2 * 1024 * 1024;
    // A switch of computer with a tab the store will not hold: asked; a stay
    // switches nothing and keeps the text, a yes switches.
    const ws3 = P.makeWsTabs();
    const r3 = P.mkRec(p3, c3.content, c3.hash);
    ws3.addTab(r3, "h2c.txt");
    P.DRAFT_MAX_BYTES = 100;
    P.type(r3, big);
    await sleep(400);
    P.page.onTeardown = () => ws3.destroy();
    P.page.appConfirms = 0;
    P.page.appConfirmAnswer = false;
    P.switchProfile("p2");
    await sleep(400);
    const stay = { asked: P.page.appConfirms, switched: P.page.switched || 0, live: P.edLive.has(r3),
                   text: P.edText(r3).length, dirty: r3.dirty };
    P.page.appConfirmAnswer = true;
    P.switchProfile("p2");
    await sleep(400);
    const go = { asked: P.page.appConfirms, switched: P.page.switched || 0, discarded: !!r3.discarded };
    P.DRAFT_MAX_BYTES = 2 * 1024 * 1024;
    out.h2 = { said, listed, rows: rows.filter((x) => x.includes("this page only")).length, disk, after, stay, go };
    killPage(P);
  } catch (e) { offline = false; out.errors = (out.errors || []).concat("h2 " + String(e && e.stack || e).slice(0, 400)); }
  // H3. A discard from the list leaves a parked buffer holding newer text it
  //     could not store: only the record the row showed goes.
  try {
    const P = makePage();
    const p = DIR + "/h3.txt";
    const c = await read(p);
    const r = P.mkRec(p, c.content, c.hash);
    P.type(r, "X");
    await sleep(400);
    P.DRAFT_MAX_BYTES = 100;
    P.type(r, "y".repeat(200));
    await sleep(400);
    P.edDropView(r, true);
    const row = (await P.draftList(P.draftOwner())).find((d) => d.path === p);
    P.page.appConfirmAnswer = true;
    await P.draftDiscardFromMenu(row);
    await sleep(100);
    // The session comes back: the buffer is restored as it was left.
    const r2 = P.mkRec(p, P.edText(r), c.hash);
    P.draftAdopt(r, r2);
    out.h3 = { row: row.text, records: recordsFor(p).length, revert: !!r.revert, text: P.edText(r2).length,
               dirty: r2.dirty };
    P.DRAFT_MAX_BYTES = 2 * 1024 * 1024;
    killPage(P);
  } catch (e) { out.errors = (out.errors || []).concat("h3 " + String(e && e.stack || e).slice(0, 400)); }
  // H4. A tap made on computer A while the queued editor's text goes into the
  //     store, and a switch to B during that wait: nothing is resumed on B.
  try {
    const P = makePage();
    const p = DIR + "/h4.txt", q = DIR + "/h4b.txt";
    const c = await read(p);
    P.edRec = P.mkRec(p, c.content, c.hash);
    P.edRec.onDirty = (r, on) => P.edSetDirty(on);
    P.type(P.edRec, "A");
    offline = true;
    await P.edSaveOnce(P.edRec, "manual");
    offline = false;
    P.$("screen-editor").classList.add("active");
    P.type(P.edRec, "more");               // not in the store yet
    log.length = 0;
    const opening = P.hcOpenEditor(q, {});
    switchToB(P);
    await Promise.race([opening, sleep(500)]);
    const brief = { toB: toB(), built: P.page.built };
    // The same wait under any other caller (a session switch's re-run).
    P.page.profile = "p1"; P.cfg.backend = SERVER_A;
    P.type(P.edRec, "again");
    let resumed = 0;
    const now = P.edMayLeave(() => { resumed++; });
    switchToB(P);
    await sleep(500);
    out.h4 = { brief, deferred: now === undefined, resumed };
    for (const r of P.edLive) clearTimeout(r.draftTimer);
    P.draftFlags.clear();
    killPage(P);
  } catch (e) { offline = false; out.errors = (out.errors || []).concat("h4 " + String(e && e.stack || e).slice(0, 400)); }
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


WS = r"""
// The workspace's tab close, its pane-close question and its file open
// (47-workspace.js), lifted out of makeWorkspace with the little of its
// closure they reach.
function makeWsTabs() {
  const groups = [{ tabs: [], active: null }];
  const byPath = new Map();
  const ctx = { readText: (p) => fsReadText(p) };
  function activate() {}
  function syncEmpty() {}
  function unsplit() {}
  function paintChip() {}
  function endDrag() {}
  let scp = null, attached = false;
  function detach() {}
  const wsEl = { remove() {} };
  function onVisibility() {}
  const api = {};
CHUNKS
  function addTab(rec, title) {
    const tab = { path: rec.path, key: rec.path, title, rec, dirty: rec.dirty, closed: false,
                  group: groups[0], chip: fakeEl(), body: fakeEl() };
    rec.onDirty = (r, on) => { tab.dirty = on; };
    groups[0].tabs.push(tab);
    byPath.set(tab.key, tab);
    return tab;
  }
  return { close, drop, destroy, dirtyCount, askDiscardAll, fileTabs, fillTab, addTab, byPath };
}
"""


def _ws_src():
    src = (REPO / "src" / "mobile" / "js" / "47-workspace.js").read_text(encoding="utf-8")
    out = []
    for head in ["async function close(", "function drop(", "function fileTabs(",
                 "function dirtyCount(", "async function askDiscardAll(", "function askDiscardAll(",
                 "async function fillTab(", "function destroy("]:
        at = src.find("\n  " + head)
        if at < 0:
            continue
        at += 1
        line_end = src.index("\n", at)
        # A one-line function, or one closed by its own two-space brace.
        if src[at:line_end].rstrip().endswith("}"):
            out.append(src[at:line_end + 1])
        else:
            out.append(src[at:src.index("\n  }\n", at) + 5])
    return WS.replace("CHUNKS", "".join(out))


@pytest.fixture(scope="module")
def server_b(server):
    """A second computer: the same handlers on another port."""
    import uvicorn
    from fastapi import FastAPI

    mini = FastAPI()
    mini.get("/api/fs/read")(A.api_fs_read)
    mini.post("/api/fs/write")(A.api_fs_write)
    mini.get("/api/version")(A.api_version)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(mini, host="127.0.0.1", port=port,
                                        log_level="error", lifespan="off"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.02)
    assert srv.started
    yield "http://127.0.0.1:%d" % port
    srv.should_exit = True
    t.join(timeout=5)


@pytest.fixture(scope="module")
def fixes(doc, server, server_b, tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("fixes")
    for n in ["f1a", "f1b", "f1c", "f1d", "f2", "f3", "f3c", "f4", "f7", "f7b", "f7c", "f7d",
              "g1", "g1r", "g1n", "g2", "g2b", "g2c", "g3", "g4", "g4b", "g4c", "g4d", "g5",
              "h1", "h1b", "h2a", "h2b", "h2c", "h3", "h4", "h4b"]:
        (tmp_path / (n + ".txt")).write_text(n + "\n")
    (tmp_path / "f3c (mine).txt").write_text("taken\n")
    (tmp_path / "f5.txt").write_bytes(b"ab\xff\n")
    (tmp_path / "f6.txt").write_text("a\nb\nc\nd\ne\n")
    editor = "\n".join(_js_chunk(doc, h) for h in
                       ["function edDropView(", "function edAskDiscard(", "function edMayLeave(",
                        "function editorPopped(", "function editorCloseDocked(", "async function openEditor(",
                        "function switchProfile(", "async function hcOpenEditor("]
                       if "\n" + h in doc)
    code = ("const SERVER_A = %s;\nconst SERVER_B = %s;\nconst DIR = %s;\nconst PURE = %s;\n"
            "const FRAGMENT = %s;\nconst EDITOR = %s;\nconst PAGE_SRC = %s;\nconst HELPERS_SRC = %s;\n"
            % (json.dumps(server), json.dumps(server_b), json.dumps(str(tmp_path)),
               json.dumps(_pure(doc)), json.dumps(_fragment(doc)), json.dumps(editor),
               json.dumps(PAGE), json.dumps(HELPERS))) + FIXES.replace("IDB_MOCK", IDB_MOCK)
    code = "const WS_SRC = %s;\n" % json.dumps(_ws_src()) + code
    return _node(tmp_path, "fixes.cjs", code)


def test_every_scenario_ran(fixes):
    assert fixes.get("errors") is None


def test_a_copy_finishing_late_touches_only_its_own_unchanged_buffer(fixes):
    f = fixes["f1"]
    assert f["shownForA"] is True
    assert f["copy"] == "f1a\nA"
    # Typed into after the copy went out: still an edit, still in conflict,
    # its draft holding the newer text.
    assert f["aText"] == "f1a\nA more" and f["aDirty"] is True and f["aConflict"] is True
    assert f["aDraft"] == "f1a\nA more"
    # The other file's buffer and its dialog are as they were.
    assert f["bText"] == "f1b\nB" and f["bDirty"] is True and f["bConflict"] is True
    assert f["bDialog"] is True
    t = fixes["f1typed"]
    assert t["copy"] == "f1d\nT"
    assert t["text"] == "f1d\nT more" and t["dirty"] is True and t["conflict"] is True
    assert t["draft"] == "f1d\nT more"
    p = fixes["f1plain"]
    assert p["copy"] == "f1c\nC" and p["text"] == "theirsC\n"
    assert p["dirty"] is False and p["conflict"] is False and p["draft"] == 0 and p["dialog"] is False


def test_two_pages_with_different_text_keep_two_drafts(fixes):
    f = fixes["f2"]
    assert f["texts"] == ["f2\nP1P2", "f2\nP1Q"]
    assert f["newest"] == "f2\nP1P2" and f["others"] == 1
    rows = [r for r in f["rows"] if r.startswith("f2.txt ")]
    assert len(rows) == 2
    assert "newest" in rows[0] and "older" in rows[1]
    assert f["restored"] == "f2\nP1P2" and f["restoredOthers"] == 1
    # P's save and P's discard take only P's record.
    assert f["k"] == "ok"
    assert f["after"] == ["f2\nP1Q"]


def test_a_profile_switch_stops_a_save_and_a_copy_where_they_are(fixes):
    f = fixes["f3"]
    assert f["kind"] == "away" and f["toB"] == 0
    assert f["dirty"] is True and f["conflict"] is False and f["flag"] is None
    assert f["draft"] == "f3\nmine"
    c = fixes["f3copy"]
    assert c["toB"] == 0 and c["second"] is None
    assert c["dirty"] is True and c["conflict"] is True and c["text"] == "f3c\nmine"


def test_an_open_interrupted_by_a_switch_builds_nothing_and_drafts_stay_home(fixes):
    f = fixes["f4"]
    assert f["built"] == 0
    assert f["ownerA"] is True and f["recB"] is True
    assert f["applied"] == "f4\n" and f["dirty"] is False


def test_a_lossy_server_text_is_never_taken_as_mine(fixes):
    f = fixes["f5"]
    assert f["k1"] == "conflict" and f["dirty"] is True and f["draft"] is True
    assert f["c1"] == {"text": None, "lossy": True}
    assert f["merge"] is True and f["theirs"] is True
    assert f["k2"] == "conflict" and f["writes2"] == 1 and f["conflict2"] is True


def test_merge_both_only_edits_the_buffer(fixes):
    f = fixes["f6"]
    assert f["offered"] is True and f["writes"] == 0
    assert f["text"] == "A\nb\nc\nd\nE\n" and f["dirty"] is True and f["merged"] is True
    assert f["disk"] == "a\nb\nc\nd\nE\n"
    assert f["draft"] == "A\nb\nc\nd\nE\n"


def test_a_waiting_save_belongs_to_the_draft(fixes, tmp_path_factory):
    f = fixes["f7"]
    assert f["k1"] == "offline"
    # Back from a waiting buffer: no question, closed, one toast saying so.
    assert f["leftConfirms"] == 0 and f["closed"] is True
    assert "f7.txt will save when the connection is back" in f["leftToasts"]
    # A dirty buffer that is not waiting still asks.
    assert f["plainConfirms"] == 1
    rows = [r for r in f["rowsQueued"] if r.startswith("f7")]
    assert sorted(r.split(" ")[0] + " " + ("waits" if "Will save when back" in r else
                                           "unsaved" if " Unsaved " in r else "?") for r in rows) == [
        "f7.txt waits", "f7b.txt waits", "f7c.txt waits", "f7d.txt unsaved"]
    # The flush, with no view of the first two open and the third parked.
    assert f["disk1"] == "f7\nqueued" and f["disk3"] == "f7c\nparked" and f["t3dirty"] is False
    assert f["disk2"] == "theirs\n"
    assert f["left"] == [[], ["f7b\ntab"], []]
    assert f["flag2"] == ["conflict"] and f["dialog"] is False
    assert any("Changed on your computer" in r and "f7b.txt" in r for r in f["rowsAfter"])
    assert f["again"] == 0


def test_a_record_another_open_page_wrote_is_never_taken_over(fixes):
    f = fixes["g1"]
    # P's text survives P going away without writing again; Q kept its own.
    assert f["alive"] == ["g1\nA", "g1\nAB"]
    # A reload: restored, typed into, one record.
    assert f["restored"] == "g1r\nX" and f["reload"] == ["g1r\nXY"]
    # Without Web Locks the record is taken over in place, as before.
    assert f["noLocks"] == ["g1n\nMN"]


def test_a_waiting_buffer_is_closed_unasked_only_once_its_text_is_stored(fixes):
    e = fixes["g2"]["ed"]
    assert e["confirms"] == 1 and e["open"] is True and e["text"] == len("g2\nA") + 200
    assert e["smallConfirms"] == 0 and e["smallClosed"] is True and e["smallStored"] is True
    w = fixes["g2"]["ws"]
    assert w["asked"] == 1 and w["closed"] is False and w["text"] == len("g2b\nA") + 200
    assert w["count"] == 1 and w["paneOk"] is False and w["paneAsked"] == 1
    assert w["paneCountSmall"] is True
    assert w["smallAsked"] == 0 and w["smallClosed"] is True and w["smallStored"] is True


def test_a_discard_from_the_list_takes_only_the_text_it_showed(fixes):
    f = fixes["g3"]
    assert f["sameKey"] is True
    assert f["afterStale"] == ["g3\nAB"] and f["told"] is True
    assert f["viewKept"] == "g3\nABC"
    assert f["afterFresh"] == 0 and f["viewReset"] == "g3\n" and f["dirty"] is False


def test_a_refusal_ends_automatic_sending_even_when_its_read_fails(fixes):
    g = fixes["g4"]
    live = g["live"]
    assert live["flag"] == "recheck" and live["writes"] == 0
    assert live["after"] == "conflict" and live["conflict"] is True
    assert live["disk"] == "g4\n" and live["dirty"] is True
    st = g["stored"]
    assert st["flag"] == "recheck" and st["firstWrites"] == 1 and st["writes"] == 0
    assert st["after"] == "conflict" and st["disk"] == "g4b\n" and st["kept"] == ["g4b\nmine"]
    ld = g["landed"]
    assert ld["flag"] == "recheck" and ld["writes"] == 0 and ld["dirty"] is False and ld["after"] is None
    own = g["own"]
    assert own["flag"] == "recheck" and own["writes"] == 1
    assert own["disk"] == "g4d\nXY" and own["dirty"] is False and own["conflict"] is False


def test_a_missing_ref_fallback_never_reads_from_another_computer(fixes):
    f = fixes["g5"]
    assert f["editor"] == {"toB": 0, "built": 0}
    w = f["ws"]
    assert w["filled"] is False and w["toB"] == 0
    assert w["ref"] == "abc123" and w["key"] == w["mapped"][0] and len(w["mapped"]) == 1


def test_an_adopted_record_is_claimed_at_once(fixes):
    f = fixes["h1"]
    r = f["reload"]
    # The reloaded page restored X and typed nothing: it owns the record, so
    # the page opening the file after it starts its own, and X is still kept.
    assert r["restored"] == "h1\nX" and r["forked"] is True
    assert r["texts"] == ["Q only\n", "h1\nX"]
    b = f["both"]
    # Two openers at once: one claims, the other is refused and forks.
    assert b["shared"] == [False, True] and b["keys"] is True
    assert b["texts"] == ["B only\n", "h1b\nX"]


def test_text_the_store_will_not_hold_is_never_dropped_unasked(fixes):
    f = fixes["h2"]
    # Session gone: a workspace tab and a parked buffer, both kept in memory
    # with their newest text, listed as this page only, and said so.
    assert f["said"] == 2 and f["rows"] == 2
    assert f["listed"] == [len("h2a\nA") + 200, len("h2b\nB") + 200]
    # The waiting one is sent from memory when the computer is back.
    assert f["disk"] == "h2a\nA" + "y" * 200 and f["after"] == 0
    # A switch of computer asks; a stay switches nothing and loses nothing.
    s = f["stay"]
    assert s["asked"] == 1 and s["switched"] == 0 and s["live"] is True and s["dirty"] is True
    assert s["text"] == len("h2c\n") + 200
    g = f["go"]
    assert g["asked"] == 2 and g["switched"] == 1 and g["discarded"] is True


def test_a_list_discard_leaves_a_parked_buffer_with_other_text(fixes):
    f = fixes["h3"]
    assert f["row"] == "h3\nX" and f["records"] == 0
    assert f["revert"] is False and f["dirty"] is True and f["text"] == len("h3\nX") + 200


def test_a_continuation_never_resumes_on_another_computer(fixes):
    f = fixes["h4"]
    assert f["brief"] == {"toB": 0, "built": 0}
    assert f["deferred"] is True and f["resumed"] == 0
