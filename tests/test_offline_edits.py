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
    assert out["same"] == base.replace("l5\n", "X\n")
    assert out["overlap"] is None
    assert out["touching"] is None
    assert out["bothInsertAtEnd"] is None
    assert out["noEol"] == "A\nb\nC"
    assert out["slideDelete"] is None
    assert out["slideInsert"] is None
    assert out["repeatsApart"] == "x\na\nb\nc\ny\n"
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
             onEnsure: null };
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
var document = { addEventListener() {}, hidden: false };
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
async function appConfirm() { return true; }
// What openEditor needs around it (29-editor.js).
function usageCount() {}
async function fsReadText(path) {
  const r = await fetch(apiURL("api/fs/read?path=" + encodeURIComponent(path)));
  return r.ok ? r.json() : null;
}
async function fsReadTextAt() { return null; }
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

function makePage() {
  const ctx = { console, setTimeout, clearTimeout, AbortController, TextEncoder, structuredClone,
                indexedDB, IDBKeyRange, fetch: (u, o) => globalThis.fetch(u, o) };
  vm.createContext(ctx);
  vm.runInContext("var SERVER_A = " + JSON.stringify(SERVER_A) + ";\n" + PAGE_SRC, ctx);
  vm.runInContext(PURE, ctx);
  vm.runInContext(FRAGMENT, ctx);
  vm.runInContext(EDITOR, ctx);
  vm.runInContext(HELPERS_SRC, ctx);
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
    out.f2 = { texts, newest: newest.text, others: newest.others, rows: rows2,
               restored: R.edText(rr), restoredOthers: rr.restored && rr.restored.others,
               k, after: recordsFor(p).map((r) => r.text) };
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
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


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
    for n in ["f1a", "f1b", "f1c", "f1d", "f2", "f3", "f3c", "f4", "f7", "f7b", "f7c", "f7d"]:
        (tmp_path / (n + ".txt")).write_text(n + "\n")
    (tmp_path / "f3c (mine).txt").write_text("taken\n")
    (tmp_path / "f5.txt").write_bytes(b"ab\xff\n")
    (tmp_path / "f6.txt").write_text("a\nb\nc\nd\ne\n")
    editor = "\n".join(_js_chunk(doc, h) for h in
                       ["function edDropView(", "function editorPopped(", "async function openEditor("])
    code = ("const SERVER_A = %s;\nconst SERVER_B = %s;\nconst DIR = %s;\nconst PURE = %s;\n"
            "const FRAGMENT = %s;\nconst EDITOR = %s;\nconst PAGE_SRC = %s;\nconst HELPERS_SRC = %s;\n"
            % (json.dumps(server), json.dumps(server_b), json.dumps(str(tmp_path)),
               json.dumps(_pure(doc)), json.dumps(_fragment(doc)), json.dumps(editor),
               json.dumps(PAGE), json.dumps(HELPERS))) + FIXES.replace("IDB_MOCK", IDB_MOCK)
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
