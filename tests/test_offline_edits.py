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
globalThis.apiURL = (p) => SERVER + "/" + p;
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

// IndexedDB: one store, keyPath "key", indexes "owner" and "profile".
const rows = new Map();
function req(run) {
  const r = { onsuccess: null, result: undefined };
  r._run = () => { r.result = run(); if (r.onsuccess) r.onsuccess(); };
  return r;
}
function makeTx() {
  const q = [];
  let done = false;
  const tx = { oncomplete: null, onerror: null, onabort: null };
  const push = (r) => { q.push(r); return r; };
  const byIndex = (name, v) => [...rows.values()].filter((x) => x[name] === v);
  tx.objectStore = () => ({
    get: (k) => push(req(() => (rows.has(k) ? structuredClone(rows.get(k)) : undefined))),
    put: (v) => push(req(() => { rows.set(v.key, structuredClone(v)); })),
    delete: (k) => push(req(() => { rows.delete(k); })),
    index: (name) => ({
      getAll: (v) => push(req(() => byIndex(name, v))),
      openCursor: (range) => {
        const list = byIndex(name, range.v);
        let i = 0;
        const r = req(() => null);
        r._run = () => {
          const step = () => {
            r.result = i < list.length ? {
              delete() { rows.delete(list[i].key); },
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
  // Requests queued by an onsuccess run in the same transaction; it
  // completes once a turn passes with none left.
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
  open() {
    const r = { result: null, onsuccess: null, onupgradeneeded: null, onerror: null };
    setTimeout(() => {
      r.result = { transaction: () => makeTx(), close() {} };
      if (r.onupgradeneeded) r.onupgradeneeded = null;
      r.onsuccess();
    }, 0);
    return r;
  },
};

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
               json.dumps(_fragment(doc)))) + HARNESS
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
  unified: draftUnified("a\nb\nc\n", "a\nB\nc\n").text,
  copies: [draftCopyPath("/d/f.txt", 1), draftCopyPath("/d/f.txt", 3), draftCopyPath("/d/.bashrc", 1),
           draftCopyPath("/d/Makefile", 2)],
}));
"""


def test_the_line_merge_takes_apart_edits_and_refuses_touching_ones(doc, tmp_path):
    frag = _fragment(doc)
    src = re.search(r"^var DRAFT_DIFF_MAX_ROWS = .*;$", frag, re.M).group(0) + "\n"
    src += "\n".join(_js_chunk(frag, h) for h in
                     ["function draftMerge3(", "function draftUnified(", "function draftCopyPath("])
    code = ("const PURE = %s;\nconst MERGE_SRC = %s;\n" % (json.dumps(_pure(doc)), json.dumps(src))) + MERGE
    out = _node(tmp_path, "merge.cjs", code)
    base = "".join("l%d\n" % i for i in range(1, 11))
    assert out["apart"] == base.replace("l2\n", "M2\n").replace("l8\n", "T8\n")
    assert out["same"] == base.replace("l5\n", "X\n")
    assert out["overlap"] is None
    assert out["touching"] is None
    assert out["bothInsertAtEnd"] is None
    assert out["noEol"] == "A\nb\nC"
    assert out["unified"] == "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    assert out["copies"] == ["/d/f (mine).txt", "/d/f (mine 3).txt", "/d/.bashrc (mine)",
                             "/d/Makefile (mine 2)"]
