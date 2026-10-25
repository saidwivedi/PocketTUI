// ============================================================
// Explorer workspace: the expanded pane as a folder tree and file tabs
// ============================================================
// When the docked explorer fills the main area (filesSetExpanded,
// 28-file-explorer.js) it lays itself out the way a code editor does: the
// folder tree on the left at a width the user drags, and the files on the right
// as tabs. The tree carries the working tree's git status as the diff pane
// draws it, and an editor tab can mark its file's changes in the text (the
// inline change marks below). The folder listing is not one of the tabs: it
// stays alive out of view, so the bar above keeps driving it. Nothing here
// exists until the first expand: a phone, the pane at its docked width and the
// full-screen explorer are the listing they always were.
//
// One workspace per explorer pane, made by that pane (workspace() in
// makeFilesPane) and handed the pane's own surface as `ctx`, so a tree and its
// tabs always act on the listing they sit in. The pane loads before this
// fragment, so it reaches makeWorkspace only from a press, never while it loads.
//
// The tabs are in groups, at most four in a 2x2 grid: one or two columns, each
// one group or two stacked, made by dragging a tab to the right of a group or
// below it, VS Code's split beside and split down. A group whose last tab
// leaves goes, and a column with none left goes, unless that group is the only
// one: then it stays, empty, and says how to open a file.

// The tree's width, in pixels, before the user has dragged it, and the bounds
// the drag is held to: wide enough for a name, never more than most of the pane.
const WS_TREE_W = 220;
const WS_TREE_MIN = 140;
const WS_TREE_MAX = 0.6;

// The least height (or width) either side of a seam between groups is held
// to: a band of tabs and a few lines of what they show.
const WS_GROUP_MIN = 120;

// A tab's press becomes a drag after this much travel, the session list's
// mouse slop, so a click still picks a tab and its cross still closes it; a
// finger has to hold still this long first, since the band also scrolls.
const WS_DRAG_SLOP = 6;
const WS_TOUCH_SLOP = 8;
const WS_LIFT_MS = 250;

// The badges' order of importance when one folder holds several kinds of
// change: an edit outranks a deletion, which outranks a rename, which outranks
// a new file. A folder shows the strongest thing inside it.
const WS_RANK = { new: 1, ren: 2, del: 3, mod: 4 };

// Every live workspace, for the one Ctrl+S listener below to find the editor a
// press was made in.
const wsLive = new Set();

// /api/git/status's rows as badges by absolute path: each file its own letter
// and kind (the unstaged side first, a change that is only staged falling back
// to the staged letter, diffBadge's reading of both), and each folder on the way
// up to the repository's root the strongest kind beneath it. A row ending in "/"
// is a folder git has never seen, which is new as a whole; `whole` says so, so
// the files inside it, which status does not list, can be marked new too.
function wsAggregate(files, root) {
  const base = String(root || "").replace(/\/+$/, "");
  const marks = new Map();
  const bump = (dir, kind, whole) => {
    const had = marks.get(dir);
    if (!had || WS_RANK[kind] > WS_RANK[had.kind]) {
      marks.set(dir, { text: "", kind, dir: true, whole: !!(whole || (had && had.whole)) });
    } else if (whole) had.whole = true;
  };
  for (const f of files || []) {
    const rel = String(f.path || "");
    if (!rel) continue;
    const b = diffBadge(f.unstaged === false ? "staged" : "unstaged", f.status || "");
    // A conflict has no kind of its own in the diff pane; it is an edit here.
    const kind = b.kind || "mod";
    const isDir = rel.endsWith("/");
    const abs = base + "/" + rel.replace(/\/+$/, "");
    if (isDir) bump(abs, kind, true);
    else marks.set(abs, { text: b.text, kind, dir: false, whole: false });
    for (let d = abs; ;) {
      const i = d.lastIndexOf("/");
      if (i < 0) break;
      d = d.slice(0, i);
      if (d.length < base.length) break;
      bump(d || "/", kind, false);
      if (d === base) break;
    }
  }
  return marks;
}

// The badge for one path out of wsAggregate's map, or null: its own mark, or
// for anything inside a folder git has never seen, that folder's "new".
function wsMarkFor(marks, path, isDir) {
  const own = marks.get(path);
  if (own) return own;
  for (let d = path; ;) {
    const i = d.lastIndexOf("/");
    if (i <= 0) return null;
    d = d.slice(0, i);
    const m = marks.get(d);
    if (m && m.whole) return isDir ? { text: "", kind: "new", dir: true }
                                   : { text: "U", kind: "new", dir: false };
  }
}

// A folder's children in the tree: folders first, then the backend's own name
// order. The listing's sort (cfg.filesSort) is how one folder is read; a tree
// sorted by date would reshuffle under the user every time a file was saved.
function wsTreeOrder(entries) {
  const dirs = [], rest = [];
  for (const e of entries || []) (e.type === "dir" ? dirs : rest).push(e);
  return dirs.concat(rest);
}

// What a tab of `path` shows, by the explorer's own rules for a tap (openEntry,
// 28-file-explorer.js). A mesh is the 3D viewer's, where that viewer is in the
// bundle; a .humancode file is the brief (49-human-code.js); anything
// else that is not a picture, a PDF, Markdown or a page is text for the editor.
function wsKindOf(path) {
  if (typeof MESH_EXT_RE !== "undefined" && MESH_EXT_RE.test(path)
      && typeof meshMount === "function") return "mesh";
  if (FILES_MEDIA_RE.test(path)) return "media";
  if (FILES_PDF_RE.test(path)) return "pdf";
  if (FILES_MD_RE.test(path)) return "reader";
  if (FILES_HTML_RE.test(path)) return "html";
  if (HC_BRIEF_RE.test(path)) return "humancode";
  return "editor";
}

// Which group a tap on a line of a brief opens its file in: the group the last
// tap used while it is still there and is not the brief's own, else the first
// other group in reading order, else null, for a group made beside the brief.
function wsBriefGroup(groups, own, last) {
  if (last && last !== own && groups.includes(last)) return last;
  return groups.find((g) => g !== own) || null;
}

// The key a tab is found by: its path, or for a file read at a commit (a
// brief's Pinned), the path at that commit, which is a tab of its own beside
// the file on the disk.
function wsTabKey(path, ref) {
  return ref ? path + "@" + ref : path;
}

// The folder a tree row is, when it is a human_code/ folder holding a brief (a
// .humancode file): the brief's path, which the row opens instead of the
// folder. With more than one, the first by name. A brief in any other folder
// leaves the folder as it is; its own file row opens it.
function wsBriefIn(path, entries) {
  if (!/(?:^|\/)human_code$/.test(path)) return "";
  const names = (entries || []).filter((e) => e.type === "file" && HC_BRIEF_RE.test(e.name))
    .map((e) => e.name).sort();
  return names.length ? joinPath(path, names[0]) : "";
}

// Where a dragged tab would land, worked out from plain numbers so it reads
// without a page: `pt` is the pointer, `geo.cols` how many columns there are,
// and each of `geo.groups` its tab band's box (`row`), its body's box (`body`),
// its column's box (`col`), how many groups that column holds (`stack`) and
// the middles of its chips in strip order (`mids`). Over a band, the slot
// before the first chip whose middle is right of the pointer. Over a body, the
// right third of its column is a new column beside it while there is only one
// column; the rest of the column's lower half is a new group under it while
// the column holds only that group; and what is left that group, the tab
// added at the end. The zones are the column's, not the body's, so a lone
// group's are the files' as a whole. Anywhere else, nothing.
function wsDropTarget(pt, geo) {
  const inside = (r) => !!r && pt.x >= r.left && pt.x < r.right
                        && pt.y >= r.top && pt.y < r.bottom;
  for (let g = 0; g < geo.groups.length; g++) {
    const G = geo.groups[g];
    if (inside(G.row)) {
      let index = 0;
      while (index < G.mids.length && G.mids[index] < pt.x) index++;
      return { kind: "insert", group: g, index };
    }
    if (inside(G.body)) {
      const c = G.col;
      if (geo.cols === 1 && pt.x >= c.left + (c.right - c.left) * 2 / 3) {
        return { kind: "split", dir: "side", group: g };
      }
      if (G.stack === 1 && pt.y >= c.top + (c.bottom - c.top) / 2) {
        return { kind: "split", dir: "down", group: g };
      }
      return { kind: "append", group: g };
    }
  }
  return null;
}

// A tab's move in the model alone: out of its group's list, into `to`'s at
// `index` (a slot counted with the tab still where it was, as the drop target
// counts it; null for the end). Says where it went and, when the move left the
// group it came from with no tabs, that group, for the caller to take away.
function wsMoveTab(tab, to, index) {
  const from = tab.group;
  const at = from.tabs.indexOf(tab);
  from.tabs.splice(at, 1);
  if (index == null) index = to.tabs.length;
  else if (from === to && index > at) index--;
  index = Math.max(0, Math.min(index, to.tabs.length));
  to.tabs.splice(index, 0, tab);
  tab.group = to;
  return { index, emptied: from !== to && !from.tabs.length ? from : null };
}

// What a drop does, in the model alone, for either thing the drag carries: a
// tab (`{ kind: "tab", tab }`) or a file from the tree (`{ kind: "path",
// path }`), with `have` the tab that file already has, if any. `t` is
// wsDropTarget's answer and `groups` the groups it counts. A file that is
// open is its tab, so it moves, as a tab drag would, and one path keeps one
// tab; one that is not opens at the target ("open"). A tab dropped on its
// own group's body stays put (null), and a tree file dropped there is only
// brought to the front ("focus"). A split of a group with no tabs, which is
// the files' only group, would leave an empty group beside the new one, so a
// file dropped on such a group's split zone opens in it instead. Says the op,
// the tab or path, the group, `split` (the direction, or null) and `index`
// (a band slot, or null for the end).
function wsDropPlan(payload, t, groups, have) {
  if (!t) return null;
  const tab = payload.kind === "tab" ? payload.tab : have || null;
  const g = groups[t.group];
  if (t.kind === "append" && tab && tab.group === g) {
    return payload.kind === "tab" ? null
      : { op: "focus", tab, path: tab.path, group: g, split: null, index: null };
  }
  const split = t.kind === "split" && (tab || g.tabs.length) ? t.dir : null;
  return { op: tab ? "move" : "open", tab, path: tab ? tab.path : payload.path,
           group: g, split, index: t.kind === "insert" ? t.index : null };
}

// A group with no tabs out of the grid in the model alone: `cols` the columns,
// each with its `groups`, and each group knowing its `col`. The only group in
// the grid stays, and a group still holding tabs is never taken, so no tab is
// lost. Says what went: "group" (its column keeps the other), "col" (the
// column went with it, and the other column is the files' whole width), or
// null for nothing.
function wsDropGroup(cols, g) {
  const total = cols.reduce((n, c) => n + c.groups.length, 0);
  if (total < 2 || g.tabs.length) return null;
  const c = g.col;
  c.groups.splice(c.groups.indexOf(g), 1);
  if (c.groups.length) return "group";
  cols.splice(cols.indexOf(c), 1);
  return "col";
}

// The name a folder goes by in the tree's head: ~ for home, as the crumbs say.
function wsFolderLabel(path, home) {
  if (home && path === home) return "~";
  return baseName(path) || "/";
}

// ---- inline change marks -----------------------------------------------------
// A changed file's editor tab shows its git changes inside the text, VS Code's
// way: added lines tinted, removed lines as a block where they were, a bar in
// the gutter, and a tick and a cross on each change to stage or revert it.
// The base is the index's copy of the file (/api/fs/read with ref ":"), which
// is what `git diff` compares the working tree with, so a staged change drops
// out of the marks; the other side is the buffer, so the marks follow typing.
// The functions here are plain arithmetic on lines, for node to test.

// Past this many lines on either side the marks are off: the diff below is
// Myers', whose cost grows with the product of the length and the changes.
const WS_DIFF_MAX_LINES = 20000;
// Edits past which Myers' trace (its memory is the square of this) is not
// worth keeping; the changed middle is then one change, which is still true.
const WS_DIFF_MAX_EDITS = 1000;
// The unchanged lines a staged change carries on each side, as `git diff`.
const WS_PATCH_CONTEXT = 3;

// A text as its lines and whether it ends in a newline. "" is no lines at all,
// and "a\n" one line, as git counts them.
function wsSplitLines(text) {
  const lines = String(text).split("\n");
  const eol = lines[lines.length - 1] === "";
  if (eol) lines.pop();
  return { lines, eol };
}

// What the diff compares: the lines, the last one marked when the file has no
// newline after it, so that adding or losing that newline is a change of the
// last line rather than nothing at all (git's "\ No newline at end of file").
function wsLineKeys(t) {
  if (t.eol || !t.lines.length) return t.lines;
  const keys = t.lines.slice();
  keys[keys.length - 1] += "\u0000";
  return keys;
}

// The changes that turn line list `a` (the base) into `b` (the buffer), as
// hunks {a, al, b, bl}: `al` lines of `a` from index `a` replaced by `bl`
// lines of `b` from index `b`. A run of removed lines next to a run of added
// ones is one hunk, a modified line; al 0 is a pure addition and bl 0 a pure
// deletion. The common head and tail are trimmed before Myers' O(ND) walk.
function wsLineDiff(a, b) {
  let s = 0;
  while (s < a.length && s < b.length && a[s] === b[s]) s++;
  let ea = a.length, eb = b.length;
  while (ea > s && eb > s && a[ea - 1] === b[eb - 1]) { ea--; eb--; }
  const A = a.slice(s, ea), B = b.slice(s, eb);
  const N = A.length, M = B.length;
  if (!N && !M) return [];
  if (!N || !M) return [{ a: s, al: N, b: s, bl: M }];
  // Each step's furthest x on every diagonal, kept from -d to d for the walk
  // back; a diff with more edits than the cap is the one change it trims to.
  const max = N + M, off = max;
  const v = new Int32Array(2 * max + 2);
  const trace = [];
  let found = -1;
  for (let d = 0; d <= Math.min(max, WS_DIFF_MAX_EDITS) && found < 0; d++) {
    trace.push(v.slice(off - d, off + d + 2));
    for (let k = -d; k <= d; k += 2) {
      let x = k === -d || (k !== d && v[off + k - 1] < v[off + k + 1])
        ? v[off + k + 1] : v[off + k - 1] + 1;
      let y = x - k;
      while (x < N && y < M && A[x] === B[y]) { x++; y++; }
      v[off + k] = x;
      if (x >= N && y >= M) { found = d; break; }
    }
  }
  if (found < 0) return [{ a: s, al: N, b: s, bl: M }];
  // Back from the end: each step was one deletion or one insertion after a
  // run of equal lines; collected in reverse, then read forwards as hunks.
  const ops = [];
  let x = N, y = M;
  for (let d = found; d > 0; d--) {
    const V = trace[d], at = (k) => V[k + d];   // V holds diagonals -d..d+1
    const k = x - y;
    const down = k === -d || (k !== d && at(k - 1) < at(k + 1));
    const pk = down ? k + 1 : k - 1;
    const px = at(pk), py = px - pk;
    while (x > (down ? px : px + 1) && y > (down ? py + 1 : py)) { ops.push("="); x--; y--; }
    if (down) { ops.push("+"); y--; } else { ops.push("-"); x--; }
  }
  while (x > 0 && y > 0) { ops.push("="); x--; y--; }
  ops.reverse();
  const hunks = [];
  let i = s, j = s, h = null;
  for (const op of ops) {
    if (op === "=") { h = null; i++; j++; continue; }
    if (!h) { h = { a: i, al: 0, b: j, bl: 0 }; hunks.push(h); }
    if (op === "-") { h.al++; i++; } else { h.bl++; j++; }
  }
  return hunks;
}

// One hunk as the patch /api/git/apply stages with `git apply --cached`:
// the base's lines with WS_PATCH_CONTEXT of context, in diffBlockPatch's
// header and layout (37-git-diff.js). It applies to the base alone, which is
// the index, so its new side starts where its old side does.
function wsHunkPatch(path, base, buf, h) {
  const pre = Math.max(0, h.a - WS_PATCH_CONTEXT);
  const post = Math.min(base.lines.length, h.a + h.al + WS_PATCH_CONTEXT);
  const lastA = base.lines.length - 1, lastB = buf.lines.length - 1;
  const noEol = "\\ No newline at end of file";
  const out = [];
  const ctx = (i) => {
    out.push(" " + base.lines[i]);
    if (i === lastA && !base.eol) out.push(noEol);
  };
  for (let i = pre; i < h.a; i++) ctx(i);
  for (let i = h.a; i < h.a + h.al; i++) {
    out.push("-" + base.lines[i]);
    if (i === lastA && !base.eol) out.push(noEol);
  }
  for (let i = h.b; i < h.b + h.bl; i++) {
    out.push("+" + buf.lines[i]);
    if (i === lastB && !buf.eol) out.push(noEol);
  }
  for (let i = h.a + h.al; i < post; i++) ctx(i);
  const olds = (h.a - pre) + h.al + (post - h.a - h.al);
  const news = (h.a - pre) + h.bl + (post - h.a - h.al);
  // A side with no lines names the line before it, 0 for an empty file.
  const at = (n) => (n ? pre + 1 : pre) + "," + n;
  return ["diff --git a/" + path + " b/" + path, "--- a/" + path, "+++ b/" + path,
          "@@ -" + at(olds) + " +" + at(news) + " @@"].concat(out).join("\n") + "\n";
}

// The buffer edit that puts one hunk back the way the base has it, as
// CodeMirror's change {from, to, insert} in offsets of the buffer's text.
function wsRevertEdit(base, buf, h) {
  const starts = [0];
  for (const line of buf.lines) starts.push(starts[starts.length - 1] + line.length + 1);
  const end = buf.lines.length ? starts[buf.lines.length] - (buf.eol ? 0 : 1) : 0;
  const pos = (i) => (i >= buf.lines.length ? end : starts[i]);
  let insert = "";
  for (let i = h.a; i < h.a + h.al; i++) {
    insert += base.lines[i];
    if (i < base.lines.length - 1 || base.eol) insert += "\n";
  }
  return { from: pos(h.b), to: pos(h.b + h.bl), insert };
}

// The tiny key on an editor tab's chip that shows and hides its marks. Shown
// for a file with a mark of its own (an edit, a new file, a rename; a deleted
// file has no text to mark) or with changes in the buffer the badges have not
// caught up with yet, on a computer that can answer both the index read and
// the apply route; lit in the accent while the marks are on.
function wsMarksKeyState(tab, mark, git, hunks) {
  // A file read at a commit has no changes of its own to mark.
  const editor = !!tab && tab.kind === "editor" && !tab.ref;
  const marked = !!mark && !mark.dir && WS_RANK[mark.kind] !== undefined
                 && mark.kind !== "del";
  const show = editor && !!git && (marked || hunks > 0);
  const on = show && !!tab.marksOn;
  return { show, pressed: on, title: on ? "Hide the changes" : "Show the changes" };
}

// The CodeMirror half of the marks, made once on first use: the editor bundle
// loads lazily (ensureCM, 29-editor.js), and these classes extend its own.
// `set` replaces a view's marks; between two counts the field moves them with
// the edits, so they stay on their lines while the next count is pending.
let wsMarksKitMemo = null;
function wsMarksKit() {
  if (wsMarksKitMemo) return wsMarksKitMemo;
  const CM = window.CM6;
  const set = CM.StateEffect.define();
  const none = () => ({ deco: CM.Decoration.none, bars: new CM.RangeSetBuilder().finish() });
  const field = CM.StateField.define({
    create: none,
    update(v, tr) {
      for (const e of tr.effects) if (e.is(set)) return e.value;
      return tr.docChanged ? { deco: v.deco.map(tr.changes), bars: v.bars.map(tr.changes) } : v;
    },
    provide: (f) => CM.EditorView.decorations.from(f, (v) => v.deco),
  });
  // The gutter's bar: an added line, a modified one, or where lines went.
  class Bar extends CM.GutterMarker {
    constructor(kind) { super(); this.kind = kind; }
    eq(o) { return o.kind === this.kind; }
    toDOM() { return el("span", { class: "cm-ws-bar " + this.kind }); }
  }
  const bars = { add: new Bar("add"), mod: new Bar("mod"), del: new Bar("del") };
  // Removed lines, as a block where they were: read, not edited. `acts` makes
  // the hunk's tick and cross, or is null on a read-only view.
  class Gone extends CM.WidgetType {
    constructor(lines, acts) { super(); this.lines = lines; this.acts = acts; }
    eq(o) {
      return o.acts === this.acts && o.lines.length === this.lines.length
             && o.lines.every((l, i) => l === this.lines[i]);
    }
    toDOM() {
      const box = el("div", { class: "cm-ws-del" });
      for (const l of this.lines) box.appendChild(el("div", { class: "cm-ws-del-line" }, l || " "));
      if (this.acts) box.appendChild(this.acts());
      return box;
    }
    ignoreEvent() { return true; }
  }
  // The tick and cross alone, for a hunk that only adds: on its first line.
  class Acts extends CM.WidgetType {
    constructor(acts) { super(); this.acts = acts; }
    eq(o) { return o.acts === this.acts; }
    toDOM() { return this.acts(); }
    ignoreEvent() { return true; }
  }
  // A view's marks for `hunks` of `base` against its text: the added lines'
  // tint, the removed lines' block, the bars, and `acts` on each hunk's first
  // line, which is the removed block when there is one.
  function build(doc, hunks, base, acts) {
    const ranges = [];
    const gb = new CM.RangeSetBuilder();
    for (const h of hunks) {
      if (h.al) {
        // Above the line that now follows the removed ones; past the last line
        // of a file with no newline after it, under that line.
        const above = h.b < doc.lines;
        ranges.push(CM.Decoration.widget({
          widget: new Gone(base.lines.slice(h.a, h.a + h.al), acts),
          block: true, side: above ? -1 : 1,
        }).range(above ? doc.line(h.b + 1).from : doc.length));
      }
      const kind = h.al && h.bl ? "mod" : h.bl ? "add" : "del";
      for (let i = h.b; i < h.b + h.bl; i++) {
        const from = doc.line(i + 1).from;
        const head = i === h.b && !h.al && !!acts;
        ranges.push(CM.Decoration.line({ class: "cm-ws-add" + (head ? " cm-ws-head" : "") })
                      .range(from));
        if (head) ranges.push(CM.Decoration.widget({ widget: new Acts(acts), side: 1 }).range(from));
        gb.add(from, from, bars[kind]);
      }
      if (!h.bl) {
        const from = doc.line(Math.min(h.b + 1, doc.lines)).from;
        gb.add(from, from, bars.del);
      }
    }
    return { deco: CM.Decoration.set(ranges, true), bars: gb.finish() };
  }
  // A view's extension: the field, its gutter, and `onDoc` told of each edit.
  const ext = (onDoc) => [
    field,
    CM.gutter({ class: "cm-ws-gutter", markers: (view) => view.state.field(field).bars }),
    CM.EditorView.updateListener.of((u) => { if (u.docChanged) onDoc(); }),
  ];
  wsMarksKitMemo = { set, build, ext };
  return wsMarksKitMemo;
}

// The badges' poll: one timer, and one question in flight at a time. `ask`
// puts one status question and answers { ms, data } (ms: how long the backend
// took), false for "ask nothing more until kicked", or null for an answer with
// nothing in it; `paint` draws an answer's data. The next question waits
// clamp(4 x ms, 2 s, 60 s), the diff pane's pace. A kick (a save, an upload, a
// rename, a delete, a new folder, a move, a new root) asks at once in place of
// the timer, and kicks that land while a question is out fold into one more
// question after it, however many there were. A kick made while an answer is
// being painted is that answer's own consequence and asks nothing: it would
// otherwise turn every answer into the next question. `clock` is the timer
// pair, for a test to stand in.
function wsPoller(wanted, ask, paint, clock) {
  const set = clock ? clock.set : (f, ms) => setTimeout(f, ms);
  const clear = clock ? clock.clear : (t) => clearTimeout(t);
  let timer = null, busy = false, again = false, painting = false, every = DIFF_POLL_MS;
  function stop() {
    if (timer !== null) clear(timer);
    timer = null;
  }
  async function run() {
    stop();
    if (!wanted()) return;
    if (busy) { again = true; return; }
    busy = true;
    let r = null;
    try { r = await ask(); } catch (e) { r = null; }
    if (r) {
      every = Math.min(DIFF_POLL_MAX_MS,
                       Math.max(DIFF_POLL_MS, Math.round((Number(r.ms) || 0) * DIFF_POLL_FACTOR)));
      painting = true;
      try { paint(r.data); } catch (e) { dbg("ws git status paint failed:", e); }
      painting = false;
    }
    busy = false;
    if (r === false) { again = false; return; }
    if (again) { again = false; run(); return; }
    stop();
    if (wanted()) timer = set(run, every);
  }
  function kick() {
    if (!painting) run();
  }
  return { kick, stop, get every() { return every; } };
}

function makeWorkspace(ctx) {
  const root = ctx.root;
  const wrap = root.querySelector(".files-wrap");

  const treeHead = el("div", { class: "ws-tree-head" });
  const treeBody = el("div", { class: "ws-tree-body", role: "tree" });
  const seam = el("div", { class: "ws-seam ws-tree-seam", role: "separator",
                           "aria-orientation": "vertical",
                           "aria-label": "Resize the folder tree" });
  const groupsEl = el("div", { class: "ws-groups" });
  // Focusable as a whole, so a click on a tree row or the listing puts the
  // keyboard inside the pane and its Escape reaches the pane's handler.
  // Where the listing waits while the workspace is on: the same element, kept
  // in the document out of view, so the bar's crumbs, path field, view menu
  // and actions, and the listing's own listeners, keep working on it.
  const hold = el("div", { class: "ws-hold" });
  hold.hidden = true;
  const wsEl = el("div", { class: "ws", tabindex: "-1" },
    el("aside", { class: "ws-tree" }, treeHead, treeBody), seam, groupsEl, hold);
  root.appendChild(wsEl);

  let on = false;
  let attached = true;
  let treeW = 0;

  // ---- the tree ----------------------------------------------------------
  // One node per path the tree has drawn, so a listing landing for any folder
  // can find its row and a refresh can keep what was open.
  const nodes = new Map();
  let treeRoot = null;       // the node the tree is drawn from
  let selected = null;
  // A folder closed by a click on its name while the listing goes there: the
  // listing's answer (onListing -> reveal) would otherwise open it again.
  // Kept while the listing stays on it, for its partial answers on a slow
  // mount, and dropped once it moves elsewhere or the folder is opened.
  let keepShut = null;

  function makeNode(path, name, type, depth, entry) {
    const node = { path, name, type, depth, entry, expanded: false, loaded: false,
                   children: [], el: null, kidsEl: null, badgeEl: null };
    if (depth < 0) {
      // The root draws no row of its own: its name is the head, and its
      // children are the body.
      node.kidsEl = treeBody;
      nodes.set(path, node);
      return node;
    }
    const dir = type === "dir";
    node.badgeEl = el("span", { class: "ws-badge" });
    node.el = el("div", { class: "ws-node " + type, "data-path": path, title: path,
                          role: "treeitem", style: "--depth:" + depth },
      el("span", { class: "ws-twisty" }, dir ? svgIcon("i-m-chev-right") : null),
      el("span", { class: "file-ic " + type },
         svgIcon(dir ? "i-folder" : kindIcon(fileKind(name)))),
      el("span", { class: "ws-name" }, name),
      node.badgeEl);
    if (dir) {
      node.el.setAttribute("aria-expanded", "false");
      node.kidsEl = el("div", { class: "ws-kids", role: "group" });
      node.kidsEl.hidden = true;
      // A listing row dragged onto a folder of the tree moves into it, as it
      // does onto a folder row of the listing.
      ctx.wireMoveTarget(node.el, path);
      if (name === "human_code") findBrief(node);
    }
    nodes.set(path, node);
    paintNode(node);
    return node;
  }

  // A human_code/ folder is looked into as soon as its row is drawn, out of
  // the explorer's cache when it can be: holding a brief, the row is named
  // "Human code" with a page for its icon, and its name opens the brief as a
  // tab. The chevron still opens the folder, and its right-click menu is the
  // folder's, as any row's is.
  async function findBrief(node) {
    let data = ctx.cachedListing(node.path);
    if (!data) {
      try { data = await ctx.fsList(node.path); } catch (e) { return; }
    }
    if (data && nodes.get(node.path) === node) markBrief(node, data.entries);
  }

  function markBrief(node, entries) {
    node.brief = wsBriefIn(node.path, entries);
    node.el.classList.toggle("ws-brief", !!node.brief);
    node.el.querySelector(".ws-name").textContent = node.brief ? "Human code" : node.name;
    node.el.querySelector(".file-ic").replaceChildren(svgIcon(node.brief ? "i-text" : "i-folder"));
  }

  // A node and everything under it leave the map, so a folder deleted and made
  // again starts closed rather than as a row that remembers a dead subtree.
  function forget(node) {
    for (const p of node.children) {
      const kid = nodes.get(p);
      if (kid) forget(kid);
    }
    nodes.delete(node.path);
    if (selected === node) selected = null;
  }

  function reRoot(path) {
    for (const n of nodes.values()) if (n.el) n.el.remove();
    nodes.clear();
    treeBody.textContent = "";
    selected = null;
    treeRoot = makeNode(path, baseName(path) || "/", "dir", -1, null);
    treeRoot.expanded = true;
    treeHead.textContent = wsFolderLabel(path, ctx.home());
    treeHead.title = path;
    kickStatus();
  }

  // A folder's listing into its node: rows matched by name keep their node,
  // and with it whether they are open and what they hold; rows that are gone
  // go; new ones are made. Then every row is put in order, which moves the ones
  // already there rather than redrawing them.
  function fill(node, data) {
    const was = new Map(node.children.map((p) => [p, nodes.get(p)]));
    const kids = [];
    for (const e of wsTreeOrder(data.entries)) {
      const path = joinPath(node.path, e.name);
      let kid = was.get(path);
      if (kid && kid.type !== e.type) { forget(kid); kid.el.remove(); if (kid.kidsEl) kid.kidsEl.remove(); kid = null; }
      if (kid) { kid.entry = e; was.delete(path); }
      else kid = makeNode(path, e.name, e.type, node.depth + 1, e);
      kids.push(kid);
    }
    for (const gone of was.values()) {
      if (!gone) continue;
      forget(gone);
      gone.el.remove();
      if (gone.kidsEl) gone.kidsEl.remove();
    }
    node.children = kids.map((k) => k.path);
    node.loaded = true;
    if (node.el && node.name === "human_code") markBrief(node, data.entries);
    for (const kid of kids) {
      node.kidsEl.appendChild(kid.el);
      if (kid.kidsEl) node.kidsEl.appendChild(kid.kidsEl);
    }
  }

  function setExpanded(node, open) {
    node.expanded = open;
    if (!node.el) return;
    node.el.classList.toggle("open", open);
    node.el.setAttribute("aria-expanded", open ? "true" : "false");
    node.kidsEl.hidden = !open;
  }

  // A folder's listing, out of the explorer's cache when it has one and asked
  // for otherwise; the row breathes while it is on its way. Null on a failure,
  // which leaves the row closed rather than saying anything: the tree is a
  // view of folders the listing can still open and explain.
  async function list(node) {
    const hit = ctx.cachedListing(node.path);
    if (hit) return hit;
    if (node.el) node.el.classList.add("pending");
    try {
      return await ctx.fsList(node.path);
    } catch (e) {
      dbg("ws tree list failed:", e);
      return null;
    } finally {
      if (node.el) node.el.classList.remove("pending");
    }
  }

  async function expand(node) {
    if (!node.loaded) {
      const data = await list(node);
      if (!data || !nodes.has(node.path)) return false;
      fill(node, data);
    }
    setExpanded(node, true);
    return true;
  }

  function select(path) {
    const node = nodes.get(path) || null;
    if (selected && selected.el) selected.el.classList.remove("sel");
    selected = node;
    if (node && node.el) {
      node.el.classList.add("sel");
      node.el.scrollIntoView({ block: "nearest" });
    }
  }

  // The folder the listing is on, found in the tree: every folder from the
  // tree's root down to it opened (and listed, where it never was), then it
  // is the selected row unless a file's tab is in front, whose row that is. A
  // folder outside the tree's root is a new root. No folder yet (the pane
  // expanded before its first listing landed, filesPath still "") is nothing
  // to root at: a root of "" lists the backend's default folder, home, under
  // a path of "", so every child would be "/" + name, and every folder
  // counts as inside it, so the listing's own answer could never re-root it.
  // The first listing's onListing reveals it instead.
  async function reveal(path) {
    if (!path) return;
    if (keepShut !== path) keepShut = null;
    if (!treeRoot || !insideDir(path, treeRoot.path)) reRoot(path);
    const parts = path.slice(treeRoot.path.length).split("/").filter(Boolean);
    let node = treeRoot, at = treeRoot.path;
    if (!node.loaded && !(await expand(node))) return;
    for (const seg of parts) {
      at = joinPath(at, seg);
      node = nodes.get(at);
      if (!node || node.type !== "dir") return;
      if (at === keepShut) break;
      if (!(await expand(node))) return;
    }
    if (on && !frontTab()) select(path);
  }

  function insideDir(path, dir) {
    return path === dir || path.startsWith(dir === "/" ? "/" : dir + "/");
  }

  treeBody.addEventListener("click", (ev) => {
    const row = ev.target.closest(".ws-node");
    if (!row) return;
    const node = nodes.get(row.dataset.path);
    if (!node) return;
    if (node.type === "dir") {
      // The chevron only opens or closes the branch. The name also takes the
      // listing there, out of view, so the bar's crumbs say where it is, and
      // toggles the branch as the chevron does: a second click on an open
      // folder closes it.
      if (ev.target.closest(".ws-twisty")) {
        if (node.expanded) setExpanded(node, false);
        else { if (keepShut === node.path) keepShut = null; expand(node); }
        return;
      }
      // A brief's folder opens the brief by its name (findBrief).
      if (node.brief) { open(node.brief); return; }
      // A closed folder is opened by the listing's answer (onListing fills
      // the node and reveal opens it), not by a list of its own: asking here
      // as well read every folder twice, and a folder that fails failed twice.
      select(node.path);
      if (node.expanded) {
        keepShut = node.path;
        setExpanded(node, false);
      } else if (keepShut === node.path) keepShut = null;
      ctx.navigate(node.path);
      return;
    }
    // The file's letter opens it with its changes marked, whatever the
    // tabs' default; the rest of the row opens it as the default says.
    if (node.type === "file" && ev.target.closest(".ws-badge.ws-marks-link")) {
      open(node.path, undefined, { marks: true });
      return;
    }
    if (node.type === "file") open(node.path);
    else if (node.entry) ctx.openFileActions(Object.assign({}, node.entry, { dir: parentOf(node.path) }));
  });

  // A file row pressed and dragged goes into the file area as a tab would
  // (the drag below); a folder has no tab to become, and stays a row the
  // listing's own drag can drop files onto.
  treeBody.addEventListener("pointerdown", (e) => {
    const row = e.target.closest(".ws-node.file");
    const node = row && nodes.get(row.dataset.path);
    if (node && node.type === "file") startDrag(e, { kind: "path", path: node.path }, row);
  });
  treeBody.addEventListener("click", swallowDragClick, true);
  treeBody.addEventListener("touchmove", (e) => {
    if (drag && drag.lifted && treeBody.contains(drag.src)) e.preventDefault();
  }, { passive: false });

  // The listing's right-click menu, over a row of the tree: the entry carries
  // the folder it is in, which is not the folder the listing is showing.
  treeBody.addEventListener("contextmenu", (ev) => {
    const row = ev.target.closest(".ws-node");
    const node = row && nodes.get(row.dataset.path);
    if (!node || !node.entry) return;
    // Android's long press is this menu as well as the drag's lift: a finger
    // that has carried the row somewhere is dragging, and one still held
    // where it lifted wants the menu, so the drag gives way.
    if (drag && drag.touch && treeBody.contains(drag.src)) {
      if (drag.moved) { ev.preventDefault(); return; }
      endDrag(false);
    }
    ev.preventDefault();
    ctx.openFileActions(Object.assign({}, node.entry, { dir: parentOf(node.path) }),
                        { x: ev.clientX, y: ev.clientY, pane: root });
  });

  function parentOf(path) {
    const i = path.lastIndexOf("/");
    return i > 0 ? path.slice(0, i) : "/";
  }

  // A folder written to (a rename, a delete, a new folder, an upload, a move)
  // read again where the tree has it open. The folder on screen is left to the
  // listing's own reload, whose answer arrives through onListing.
  async function refreshDir(path) {
    const node = nodes.get(path);
    if (!node || !node.loaded || path === ctx.path()) return;
    try {
      const data = await ctx.fsList(path);
      if (nodes.get(path) === node) fill(node, data);
    } catch (e) { dbg("ws tree refresh failed:", e); }
  }

  // ---- the tree's width ----------------------------------------------------
  // On the pane's root, so two panes keep two widths; only the markup's own
  // pane remembers its width, as only it remembers being expanded.
  function applyTreeW(px) {
    const max = root.clientWidth ? root.clientWidth * WS_TREE_MAX : px;
    treeW = Math.round(Math.max(WS_TREE_MIN, Math.min(px, max)));
    root.style.setProperty("--ws-tree-w", treeW + "px");
  }

  // The column seam's drag (sideResize, 26-side-pane.js) moving the other way:
  // the tree is anchored at the left, so rightward travel is a wider tree.
  (function treeResize() {
    let dragging = false, dragOff = 0;
    seam.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      dragging = true;
      dragOff = treeW - e.clientX;
      seam.classList.add("dragging");
      seam.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    seam.addEventListener("pointermove", (e) => {
      if (dragging) applyTreeW(dragOff + e.clientX);
    });
    const finish = () => {
      if (!dragging) return;
      dragging = false;
      seam.classList.remove("dragging");
      if (ctx.id === "files") cfg.wsTreeW = treeW;
      relayout();
    };
    seam.addEventListener("pointerup", finish);
    seam.addEventListener("pointercancel", finish);
  })();

  // ---- the tabs --------------------------------------------------------------

  const groups = [];
  const byPath = new Map();   // one tab per file across every group
  let tabSeq = 0;
  // Where open() puts a new tab: the group last pressed in, VS Code's way;
  // null for the first.
  let focusGroup = null;

  function makeGroup() {
    const rowEl = el("div", { class: "ws-tab-row", role: "tablist" });
    // What a group with no tabs says, under a band that stays, so the band's
    // height and its drop slots do not move when the first file opens.
    const bodyEl = el("div", { class: "ws-body" },
      el("div", { class: "ws-empty" }, "Select a file in the tree to open it"));
    const g = { el: el("div", { class: "ws-group" }, el("div", { class: "ws-tabs" }, rowEl), bodyEl),
                rowEl, bodyEl, tabs: [], active: null, col: null };
    syncEmpty(g);
    return g;
  }

  function syncEmpty(g) { g.el.classList.toggle("ws-none", !g.tabs.length); }

  // The chip is made once and repainted in place, the browser strip's way
  // (renderBrowserTabs, 42-browser.js): the name, a dot while the file has
  // unsaved edits (the cross takes its place under the pointer), and the cross.
  // An editor's chip also carries the key that shows and hides its file's
  // change marks, after the name (wsMarksKeyState says when it is there).
  function makeChip(tab) {
    const name = el("button", { type: "button", class: "tab-name", role: "tab",
                                title: tab.path || "" },
      el("span", { class: "tab-word" }, tab.title));
    name.addEventListener("click", () => activate(tab));
    const chip = el("div", { class: "tab-chip" }, name);
    if (tab.kind === "editor") {
      const key = el("button", { type: "button", class: "ws-marks-key",
                                 "aria-pressed": "false" }, svgIcon("i-m-diff"));
      key.hidden = true;
      key.addEventListener("click", (e) => {
        e.stopPropagation();
        activate(tab);
        setMarks(tab, !tab.marksOn, true);
      });
      chip.appendChild(key);
    }
    chip.appendChild(el("span", { class: "ws-dirty", "aria-hidden": "true" }));
    chip.appendChild(el("button", {
      type: "button", class: "tab-del", "aria-label": "Close " + tab.title,
      onclick: (e) => { e.stopPropagation(); close(tab); },
    }, svgIcon("i-m-x")));
    // The middle button closes a tab, as in every browser and editor; its
    // press is kept from starting the browser's autoscroll.
    chip.addEventListener("mousedown", (e) => { if (e.button === 1) e.preventDefault(); });
    chip.addEventListener("auxclick", (e) => {
      if (e.button !== 1) return;
      e.preventDefault();
      close(tab);
    });
    tab.chip = chip;
    wireTabDrag(tab);
  }

  function paintChip(tab) {
    const word = tab.chip.querySelector(".tab-word");
    if (word.textContent !== tab.title) word.textContent = tab.title;
    tab.chip.classList.toggle("on", tab.group.active === tab);
    tab.chip.classList.toggle("dirty", !!tab.dirty);
    tab.chip.firstElementChild.setAttribute("aria-selected",
                                             tab.group.active === tab ? "true" : "false");
    const key = tab.chip.querySelector(".ws-marks-key");
    if (key) {
      const k = wsMarksKeyState(tab, markOf(tab.path, false), marksWanted(tab.path),
                                tab.marks ? tab.marks.hunks.length : 0);
      key.hidden = !k.show;
      key.title = k.title;
      key.setAttribute("aria-label", k.title);
      key.setAttribute("aria-pressed", k.pressed ? "true" : "false");
      tab.chip.classList.toggle("ws-keyed", k.show);
    }
  }

  function addTab(g, props) {
    // `marksOn` is whether an editor shows its change marks, `marks` what
    // they are drawn from (the inline change marks below). `ref` is the
    // commit an editor's file was read at (a brief's Pinned), `hc` a brief's
    // viewer and what it knows (fillBrief).
    const tab = Object.assign({ id: ++tabSeq, path: "", kind: "editor", title: "",
                                dirty: false, chip: null, body: null,
                                rec: null, mesh: null, closed: false, group: g,
                                marksOn: false, marks: null, ref: "", hc: null }, props);
    tab.key = wsTabKey(tab.path, tab.ref);
    tab.body = el("div", { class: "ws-pane ws-" + tab.kind });
    makeChip(tab);
    g.tabs.push(tab);
    g.rowEl.appendChild(tab.chip);
    g.bodyEl.appendChild(tab.body);
    if (tab.path) byPath.set(tab.key, tab);
    paintChip(tab);
    syncEmpty(g);
    return tab;
  }

  // The tab in front of the group a file would open into.
  function frontTab() {
    const g = focusGroup || groups[0];
    return g ? g.active : null;
  }

  function activate(tab) {
    const g = tab.group;
    const was = g.active;
    // A brief's place is taken while it still shows, since a hidden box has
    // no scroll to read.
    if (was && was !== tab) briefKeep(was);
    g.active = tab;
    for (const t of g.tabs) {
      t.body.classList.toggle("on", t === tab);
      paintChip(t);
    }
    // A film left playing in a tab nobody is looking at is sound from nowhere.
    if (was && was !== tab) pauseMedia(was);
    if (tab.rec) tab.rec.view.requestMeasure();
    if (tab.hc) briefBack(tab);
    if (tab.mesh) tab.mesh.resize();
    select(tab.path);
    // The index can have moved while the tab was behind another.
    if (tab.marksOn && tab.marks) loadBase(tab);
    // A strip wider than the group can leave the tab being switched to off its
    // end; scrolled to only then.
    requestAnimationFrame(() => {
      const r = tab.chip.getBoundingClientRect(), b = g.rowEl.getBoundingClientRect();
      if (r.left < b.left || r.right > b.right) {
        tab.chip.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
    });
  }

  function pauseMedia(tab) {
    const v = tab.body.querySelector("video");
    if (v) v.pause();
  }

  // A file in a tab: the one already open if it is, otherwise a new tab at the
  // end of the strip, filled once what it shows has been read. `kind` is a
  // caller's say over the tap rules (the action sheet's Edit on a page), and
  // `opts.create` a file the first save will make, and `opts.marks` an editor
  // that shows its change marks whatever the tabs' default (a badge's click).
  // `opts.group` and `opts.index` put a new tab in that group at that slot of
  // its band (a tree row's drop), rather than at the end of the last group
  // pressed in. A tap on a brief's line also gives openEditor's `line`,
  // `lineEnd`, `ref` and `root` (29-editor.js): the file at that commit,
  // read-only, in a tab of its own, and the lines to show and tint, which a
  // tab already open is scrolled to as well.
  async function open(path, kind, opts) {
    const have = byPath.get(wsTabKey(path, opts && opts.ref));
    if (have) {
      activate(have);
      if (opts && opts.marks && have.kind === "editor" && !have.marksOn) setMarks(have, true);
      if (opts && opts.line && have.rec) showRange(have, opts);
      return have;
    }
    kind = kind || wsKindOf(path);
    const ref = ctx.refFor(path);
    // A page renders in a tab of the browser's (openRendered): it is served
    // sandboxed into an origin of its own, which a frame in this document would
    // undo. At a ref there is no page on the disk, so it opens as its source;
    // a brief at a ref is its JSON too, the explorer's rule (openEntry).
    if (kind === "html") {
      if (!ref) { ctx.openRendered(path); return null; }
      kind = "editor";
    }
    if (kind === "humancode" && ref) kind = "editor";
    if (ref && (kind === "media" || kind === "pdf" || kind === "mesh")) {
      toast("Can't preview " + (kind === "pdf" ? "PDFs" : kind === "mesh" ? "3D files"
                                                          : "images") + " from " + ref);
      return null;
    }
    if (kind === "pdf" && demoApiOn()) { toast("Not in the demo"); return null; }
    const into = (opts && opts.group) || focusGroup || groups[0];
    const atRef = (opts && opts.ref) || "";
    const tab = addTab(into, {
      path, kind, ref: atRef,
      // A brief is named as its folder's row is (markBrief).
      title: atRef ? baseName(path) + " @ " + edShortRef(atRef)
           : kind === "humancode" ? "Human code" : baseName(path),
      marksOn: kind === "editor" && !atRef && (opts && opts.marks ? true : cfg.wsInlineDiff),
    });
    if (opts && opts.index != null) moveTab(tab, into, opts.index);
    activate(tab);
    const ok = await fillTab(tab, opts || {}).catch((e) => { dbg("ws tab failed:", e); return false; });
    if (!ok && !tab.closed) drop(tab);
    // Not a brief after all, or one this app cannot draw: the file as it is.
    if (!ok && tab.fallback) return open(path, tab.fallback, opts);
    return ok ? tab : null;
  }

  // The lines a brief's tap points at, in a tab already open: the tint moved
  // to them, and the caret and the view after it.
  function showRange(tab, r) {
    const view = tab.rec.view;
    view.dispatch({ effects: edRangeComp.reconfigure(edRangeField(r)) });
    edShowLine(r.line, view);
  }

  // ---- a human-code brief in a tab --------------------------------------------
  // The brief screen's viewer and its side of the app (49-human-code.js), in a
  // tab: one tab per brief, its units reached through the viewer's own nav.
  // A tap on a line opens the file in the group beside the brief (briefOpen),
  // the Pinned/Now pair sits in the viewer's own head, by its search, since a
  // tab has no bar of its own, and what changed since the brief's commit is
  // asked on open and every HC_POLL_MS while the tab is in front.

  async function fillBrief(tab) {
    const data = await ctx.readText(tab.path);
    if (!data || tab.closed) return !!data;
    let brief = null;
    try { brief = JSON.parse(data.content); } catch (e) {}
    if (!hcIsBrief(brief)) { tab.fallback = "editor"; return false; }
    try { await ensureHumanCode(); } catch (e) {
      toast("Couldn't load the brief viewer");
      tab.fallback = "editor";
      return false;
    }
    if (tab.closed) return true;
    if (hcTooNew(brief)) {
      toast("This brief needs a newer PocketTUI");
      tab.fallback = "editor";
      return false;
    }
    const b = hcRecord(tab.path, brief);
    const note = el("div", { class: "ws-hc-note" });
    note.hidden = true;
    const host = el("div", { class: "ws-hc-host" });
    tab.body.append(note, host);
    const view = hcMountIn(host, b, (src) => briefOpen(tab, src), () => false);
    const head = host.querySelector(".hc header");
    head.insertBefore(hcAtPair(b), head.querySelector(".hc-search"));
    tab.hc = { b, view, note, timer: setInterval(() => briefPoll(tab), HC_POLL_MS),
               state: null, edGroup: null };
    hcPaintInto(b, view, note);
    briefPoll(tab);
    return true;
  }

  function briefShown(tab) {
    return on && attached && !tab.closed && tab.group.active === tab;
  }

  async function briefPoll(tab) {
    const hc = tab.hc;
    if (!hc || !briefShown(tab)) return;
    if (await hcAskHunks(hc.b) && tab.hc === hc) hcPaintInto(hc.b, hc.view, hc.note);
  }

  // Where the viewer was, taken while it still shows (a hidden box has no
  // scroll to read) and put back when it shows again: behind another tab,
  // moved to another group, collapsed, or away with its session.
  function briefKeep(tab) {
    if (tab.hc && on && tab.body.classList.contains("on")) tab.hc.state = tab.hc.view.state();
  }

  function briefBack(tab) {
    const hc = tab.hc;
    if (hc && hc.state) { hc.view.restore(hc.state); hc.state = null; }
    if (hc) briefPoll(tab);
  }

  // A tap on a line of a brief: the file in the group beside the brief's, the
  // one the last tap used while it lasts (wsBriefGroup), else a group made
  // beside it as a drop's split makes one, taken away again if the file does
  // not become a tab there. A file already open elsewhere is brought forward
  // where it is; one open in the brief's own group moves over rather than
  // covering the brief.
  async function briefOpen(tab, src) {
    const t = tab.hc && hcTarget(tab.hc.b, src);
    if (!t) return;
    const have = byPath.get(wsTabKey(t.path, t.opts.ref));
    if (have && have.group !== tab.group) {
      focusGroup = have.group;
      tab.hc.edGroup = have.group;
      open(t.path, "editor", t.opts);
      return;
    }
    let g = wsBriefGroup(groups, tab.group, tab.hc.edGroup);
    const made = !g;
    if (made) g = makeSplit("side", tab.group);
    if (!g) return;
    tab.hc.edGroup = g;
    if (have) moveTab(have, g, null);
    focusGroup = g;
    await open(t.path, "editor", Object.assign({ group: g }, t.opts));
    if (made && !g.tabs.length && groups.includes(g)) unsplit();
  }

  async function fillTab(tab, opts) {
    const path = tab.path;
    if (tab.kind === "editor") {
      let content = "", hash = "", readOnly = false, lossy = false;
      if (!opts.create) {
        let data = tab.ref ? await fsReadTextAt(path, tab.ref, opts.root || "")
                           : await ctx.readText(path);
        // The commit has gone from the repo since the brief was read: the
        // file on the disk instead, which is a tab of its own already if it
        // is open.
        if (data && data.error) {
          toast(edShortRef(tab.ref) + " is not in this repo, so this is the file as it is now");
          if (byPath.has(path)) { activate(byPath.get(path)); return false; }
          byPath.delete(tab.key);
          tab.ref = "";
          tab.key = path;
          tab.title = baseName(path);
          byPath.set(path, tab);
          paintChip(tab);
          data = await ctx.readText(path);
        }
        if (!data) return false;
        content = data.content; hash = data.hash;
        lossy = !!data.lossy;
        readOnly = lossy || !!data.readonly;
      }
      try { await ensureCM(); } catch (e) { toast("Couldn't load the editor"); return false; }
      if (tab.closed) return true;
      // The marks live in a compartment of their own, so the chip's key adds
      // and removes them without touching the theme, Wrap or Vim.
      tab.marks = { comp: new window.CM6.Compartment(), ext: null, acts: null,
                    base: null, buf: null, hunks: [], timer: 0, seq: 0, sig: null };
      tab.rec = edMakeView({
        host: tab.body, path, content, readOnly,
        onDirty: (rec, dirty) => { tab.dirty = dirty; paintChip(tab); },
        onSave: () => save(tab),
        onQuit: (rec, bang) => close(tab, bang),
        extensions: [tab.marks.comp.of(tab.marksOn ? marksExt(tab) : []),
                     edRangeExt(opts.line ? { line: opts.line, lineEnd: opts.lineEnd } : null)],
      });
      tab.rec.hash = hash;
      if (opts.line && tab.body.classList.contains("on")) edShowLine(opts.line, tab.rec.view);
      if (tab.marksOn) loadBase(tab);
      if (cfg.editorVimOn) { edDefineVimEx(); edLoadVimrc(); }
      if (lossy) toast("Not valid UTF-8 — opened read-only");
      return true;
    }
    if (tab.kind === "humancode") return fillBrief(tab);
    if (tab.kind === "reader") {
      const data = await ctx.readText(path);
      if (!data || tab.closed) return !!data;
      const page = el("article", { class: "md-body" });
      // A relative image in the document is resolved against readerPath while
      // the document is built (mdSignImage, 33-md-reader.js), and the reader's
      // own file is not this one.
      const keep = readerPath;
      readerPath = path;
      try { page.appendChild(mdRender(data.content)); } finally { readerPath = keep; }
      tab.body.appendChild(el("div", { class: "ws-scroll" }, page));
      mdTypeset(page);
      if (data.lossy) toast("Not valid UTF-8 — some characters are missing");
      return true;
    }
    if (tab.kind === "mesh") {
      tab.mesh = await meshMount(tab.body, path, {});
      if (tab.closed && tab.mesh) { tab.mesh.destroy(); tab.mesh = null; }
      return true;
    }
    // A picture, a film or a PDF: the tag fetches the bytes itself and carries
    // no pairing header, so it is given the signed link the viewer is given
    // (fillViewer, 08-links.js).
    let url;
    try {
      const r = await fetch(apiURL("api/file_link?path=" + encodeURIComponent(path)),
                            { cache: "no-store", headers: authHeaders() });
      if (r.status === 401) { rejectToken(); return false; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      url = apiURL((await r.json()).url);
    } catch (e) {
      toast("Couldn't open " + baseName(path));
      return false;
    }
    if (tab.closed) return true;
    let media;
    if (tab.kind === "pdf") {
      // The src is set before the frame is in the document: a frame's first
      // load spends no history entry, every later navigation of it does
      // (dropFrame, 08-links.js), so each PDF gets a frame of its own.
      media = el("iframe", { class: "ws-frame", title: baseName(path) });
    } else if (VIDEO_EXT_RE.test(path)) {
      media = el("video", { controls: "", playsinline: "", preload: "metadata" });
    } else {
      media = el("img", { alt: baseName(path) });
    }
    media.src = url;
    if (tab.kind === "pdf") tab.body.appendChild(media);
    // Not "ws-media": that is the class of a media tab's own pane (addTab's
    // "ws-" + kind), and the box's display:flex on it would draw every
    // picture's pane at once, stacked, whichever tab is in front.
    else tab.body.appendChild(el("div", { class: "ws-media-box" }, media));
    media.onerror = () => toast("Couldn't load " + baseName(path));
    return true;
  }

  // Ctrl+S, the save button nobody has to find, and vim's :w.
  async function save(tab) {
    if (!tab.rec || tab.rec.readOnly || tab.saving) return;
    tab.saving = true;
    try {
      if (await edViewSave(tab.rec)) kickStatus();
    } finally {
      tab.saving = false;
    }
  }

  // A tab's own close. A file with unsaved edits asks first, in the app's own
  // dialog; `force` is vim's :q!, which has already said discard.
  async function close(tab, force) {
    if (tab.closed) return;
    if (tab.dirty && !force) {
      const ok = await appConfirm("The edits since the last save will be lost.",
        { title: "Discard your unsaved changes to " + tab.title + "?",
          confirmLabel: "Discard" });
      if (!ok || tab.closed) return;
    }
    drop(tab);
  }

  // Everything a tab holds goes with it: the editor's view out of the live set
  // the theme and the toggles walk, the 3D scene's GPU context, the film's
  // decoder. The neighbour to the right takes over, else the one to the left.
  function drop(tab) {
    if (tab.closed) return;
    tab.closed = true;
    if (tab.marks) clearTimeout(tab.marks.timer);
    if (tab.rec) { edDropView(tab.rec); tab.rec = null; }
    if (tab.mesh) { tab.mesh.destroy(); tab.mesh = null; }
    if (tab.hc) {
      clearInterval(tab.hc.timer);
      tab.hc.b.dead = true;
      tab.hc.view.destroy();
      tab.hc = null;
    }
    const v = tab.body.querySelector("video");
    if (v) { v.pause(); v.removeAttribute("src"); }
    const g = tab.group;
    const at = g.tabs.indexOf(tab);
    g.tabs.splice(at, 1);
    if (byPath.get(tab.key) === tab) byPath.delete(tab.key);
    tab.chip.remove();
    tab.body.remove();
    if (g.active === tab) {
      g.active = null;
      const next = g.tabs[at] || g.tabs[at - 1];
      if (next) activate(next);
    }
    syncEmpty(g);
    if (!g.tabs.length) unsplit();
  }

  // ---- the grid of groups ------------------------------------------------------
  // One or two columns, each one group or two stacked (`cols`, each with its
  // `groups`; `groups` is every group in reading order, left column first). A
  // column of one group is its full height. A tab dropped on the right third
  // of a column, while there is only one, makes a column beside it; on the
  // lower half of a column holding one group, a group under that one (the drop
  // target above). The columns share the width by --ws-split, and a column of
  // two shares its height by --ws-split-l or --ws-split-r, as the left or the
  // right column it stands now; all three on the pane's root, remembered as
  // cfg.wsSplit, wsSplitL and wsSplitR by the markup's own pane, as the tree's
  // width is. A seam is in the document only while it has a group either side.

  const cols = [];
  const colSeam = el("div", { class: "ws-seam ws-group-seam ws-col-seam", role: "separator",
                              "aria-orientation": "vertical",
                              "aria-label": "Resize the file columns" });
  let splitShare = 0;
  const rowShare = { l: 0, r: 0 };

  function applySplit(share) {
    splitShare = share;
    root.style.setProperty("--ws-split", String(share));
  }

  function applyRowSplit(side, share) {
    rowShare[side] = share;
    root.style.setProperty("--ws-split-" + side, String(share));
  }

  const sideOf = (c) => (cols.indexOf(c) === 0 ? "l" : "r");

  // The tree seam's drag, turned for groups: the first group (or column) is
  // anchored at the top (or the left), so downward (rightward) travel is a
  // bigger first one. `area` is what the two share, `set` takes the share and
  // `keep` remembers it, for the markup's own pane only.
  function seamDrag(s, across, area, set, keep) {
    let dragging = false, grab = 0;
    s.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      dragging = true;
      const b = s.getBoundingClientRect();
      grab = across ? e.clientX - b.left : e.clientY - b.top;
      s.classList.add("dragging");
      s.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    s.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const r = area.getBoundingClientRect();
      const avail = across ? r.width - s.offsetWidth : r.height - s.offsetHeight;
      if (avail < 2 * WS_GROUP_MIN) return;
      const at = across ? e.clientX - grab - r.left : e.clientY - grab - r.top;
      const first = Math.max(WS_GROUP_MIN, Math.min(at, avail - WS_GROUP_MIN));
      set(Math.round(first / avail * 10000) / 10000);
    });
    const finish = () => {
      if (!dragging) return;
      dragging = false;
      s.classList.remove("dragging");
      if (ctx.id === "files") keep();
      relayout();
    };
    s.addEventListener("pointerup", finish);
    s.addEventListener("pointercancel", finish);
  }

  seamDrag(colSeam, true, groupsEl, applySplit, () => { cfg.wsSplit = splitShare; });

  // A column's seam is its own, and which share it moves is read at the drag:
  // the right column becomes the left one when the left one goes.
  function makeCol() {
    const c = { el: el("div", { class: "ws-col" }), groups: [],
                seam: el("div", { class: "ws-seam ws-group-seam", role: "separator",
                                  "aria-orientation": "horizontal",
                                  "aria-label": "Resize the file groups" }) };
    seamDrag(c.seam, false, c.el, (v) => applyRowSplit(sideOf(c), v), () => {
      if (sideOf(c) === "l") cfg.wsSplitL = rowShare.l;
      else cfg.wsSplitR = rowShare.r;
    });
    return c;
  }

  // After the grid changed: `groups` in reading order again, and a seam that
  // has come into the document at the share last remembered for it.
  function syncGrid() {
    groups.length = 0;
    for (const c of cols) for (const g of c.groups) groups.push(g);
    groups.forEach((g, i) => g.el.setAttribute("data-group", String(i)));
    if (cols.length > 1 && !splitShare) applySplit(cfg.wsSplit);
    for (const c of cols) {
      const side = sideOf(c);
      if (c.groups.length > 1 && !rowShare[side]) {
        applyRowSplit(side, side === "l" ? cfg.wsSplitL : cfg.wsSplitR);
      }
    }
  }

  // A new group beside the grid's one column, or under `at` in its column, and
  // the tab into it. Only what is new is put into the document, so no other
  // group's body moves (a PDF's frame would load again if it did).
  function splitOff(tab, dir, at) {
    const g = makeSplit(dir, at);
    if (g) moveTab(tab, g, null);
  }

  // The new group alone, empty, for a tab to be moved or a file opened into;
  // null when the grid has no room that way.
  function makeSplit(dir, at) {
    if (dir === "side" ? cols.length > 1 : at.col.groups.length > 1) return null;
    const g = makeGroup();
    if (dir === "side") {
      const c = makeCol();
      c.groups.push(g);
      g.col = c;
      c.el.appendChild(g.el);
      cols[0].el.after(colSeam, c.el);
      cols.push(c);
      groupsEl.classList.add("ws-two");
    } else {
      const c = at.col;
      c.groups.push(g);
      g.col = c;
      at.el.after(c.seam, g.el);
      c.el.classList.add("ws-two");
    }
    syncGrid();
    return g;
  }

  // The group nothing is left in goes, and its column with it when that was
  // the column's last; the only group in the grid stays, empty or not.
  function unsplit() {
    const g = groups.find((x) => !x.tabs.length);
    if (!g) return;
    const c = g.col;
    const went = wsDropGroup(cols, g);
    if (!went) return;
    g.el.remove();
    if (went === "col") {
      c.el.remove();
      colSeam.remove();
      groupsEl.classList.remove("ws-two");
    } else {
      c.seam.remove();
      c.el.classList.remove("ws-two");
    }
    if (focusGroup === g) focusGroup = null;
    syncGrid();
    relayout();
  }

  (function firstGroup() {
    const c = makeCol(), g = makeGroup();
    c.groups.push(g);
    g.col = c;
    c.el.appendChild(g.el);
    cols.push(c);
    groupsEl.appendChild(c.el);
    syncGrid();
  })();

  // A tab to `index` of `g` (null for the end), within its group or out of it.
  // It moves rather than being copied, so one tab per path holds by itself;
  // the group it left shows its neighbour, as a close would leave it.
  function moveTab(tab, g, index) {
    const from = tab.group;
    const at = from.tabs.indexOf(tab);
    const moved = wsMoveTab(tab, g, index);
    if (!moved) return;
    const next = g.tabs[moved.index + 1];
    g.rowEl.insertBefore(tab.chip, next ? next.chip : null);
    if (from !== g) {
      briefKeep(tab);
      g.bodyEl.appendChild(tab.body);
      if (from.active === tab) {
        from.active = null;
        const n = from.tabs[at] || from.tabs[at - 1];
        if (n) activate(n);
      }
    }
    activate(tab);
    focusGroup = g;
    syncEmpty(from);
    syncEmpty(g);
    if (moved.emptied) unsplit();
    relayout();
  }

  // On capture, so a press on an editor, a chip or a listing row counts
  // before whatever it does next opens a file.
  groupsEl.addEventListener("pointerdown", (e) => {
    const at = e.target.closest(".ws-group");
    const g = groups.find((x) => x.el === at);
    if (g) focusGroup = g;
  }, true);

  // ---- dragging a tab, or a file from the tree -------------------------------------
  // By pointer events rather than the HTML5 drag the listing's rows move files
  // with: that drag would offer a tab to every folder row as a file to move.
  // One controller carries either a tab or a tree file (the payload, which
  // wsDropPlan reads). A copy of the chip, or for a file its icon and name,
  // follows the pointer and the source stays in its place, dimmed, until the
  // release says where it goes: a slot in a band (a bar between two chips),
  // the right third or the lower half of a column while the grid has room
  // that way (a split beside or below), or another group's body. Escape, or a
  // cancelled pointer, puts it down where it was; a tree file let go outside
  // the file area does nothing.
  let drag = null;
  let swallowClick = false;
  const insBar = el("div", { class: "ws-drop-ins", "aria-hidden": "true" });
  const hint = el("div", { class: "ws-drop-hint", "aria-hidden": "true" });

  // A press on `src` that may become a drag of `payload`: a mouse after the
  // slop, a finger only after holding still WS_LIFT_MS, since the band and
  // the tree both scroll under a finger.
  function startDrag(e, payload, src) {
    if (drag || !e.isPrimary) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const touch = e.pointerType !== "mouse";
    drag = { payload, src, id: e.pointerId, touch, x: e.clientX, y: e.clientY,
             lastX: e.clientX, lastY: e.clientY, lifted: false, moved: false,
             timer: 0, ghost: null, target: null, plan: null };
    window.addEventListener("pointermove", onDragMove);
    window.addEventListener("pointerup", onDragUp);
    window.addEventListener("pointercancel", onDragCancel);
    window.addEventListener("blur", onDragBlur);
    if (touch) drag.timer = setTimeout(lift, WS_LIFT_MS);
  }

  // The click a drag's release makes is not a press on the tab or the row.
  function swallowDragClick(e) {
    if (!swallowClick) return;
    swallowClick = false;
    e.preventDefault();
    e.stopPropagation();
  }

  function wireTabDrag(tab) {
    const chip = tab.chip;
    chip.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".tab-del, .ws-marks-key")) return;
      startDrag(e, { kind: "tab", tab }, chip);
    });
    chip.addEventListener("click", swallowDragClick, true);
    // Once lifted the finger belongs to the tab, and the band must not scroll
    // under it; Android's long press would also open a menu on the same hold.
    chip.addEventListener("touchmove", (e) => {
      if (drag && drag.src === chip && drag.lifted) e.preventDefault();
    }, { passive: false });
    chip.addEventListener("contextmenu", (e) => {
      if (drag && drag.src === chip && drag.touch) e.preventDefault();
    });
  }

  function lift() {
    const d = drag;
    clearTimeout(d.timer);
    d.timer = 0;
    d.lifted = true;
    const src = d.src;
    try { src.setPointerCapture(d.id); } catch (e) {}
    const r = src.getBoundingClientRect();
    let copy, box;
    if (d.payload.kind === "tab") {
      copy = src.cloneNode(true);
      copy.classList.remove("on");
      box = "left:" + r.left + "px;top:" + r.top + "px;width:" + r.width + "px;height:"
            + r.height + "px";
    } else {
      // A tree row is the tree's width; its ghost is a chip of the file's
      // icon and name, set beside the press so it does not jump.
      const name = baseName(d.payload.path);
      copy = el("div", { class: "tab-chip ws-path-ghost" },
        el("span", { class: "file-ic file" }, svgIcon(kindIcon(fileKind(name)))),
        el("span", { class: "tab-name" }, el("span", { class: "tab-word" }, name)));
      box = "left:" + (d.x + 8) + "px;top:" + (d.y - r.height / 2) + "px;height:"
            + r.height + "px";
    }
    d.ghost = el("div", { class: "ws-tab-ghost", "aria-hidden": "true", style: box }, copy);
    document.body.appendChild(d.ghost);
    src.classList.add("dragging");
    document.body.classList.add("ws-tab-dragging");
    window.addEventListener("keydown", onDragKey, true);
    if (d.touch && navigator.vibrate) navigator.vibrate(10);
    follow();
  }

  function follow() {
    const d = drag;
    d.ghost.style.transform = "translate(" + (d.lastX - d.x) + "px," + (d.lastY - d.y) + "px)";
    const t = dropAt(d.lastX, d.lastY);
    d.plan = wsDropPlan(d.payload, t, groups,
                        d.payload.kind === "path" ? byPath.get(d.payload.path) : null);
    showTarget(d.plan ? t : null);
  }

  // The drop target under the pointer, from the boxes on screen now (a band
  // scrolls, and the seam moves). Only inside this workspace's groups: the
  // ghost takes no pointer, so what is under it is what it is over.
  function dropAt(x, y) {
    const hit = document.elementFromPoint(x, y);
    if (!hit || !groupsEl.contains(hit) || hit.closest(".ws-group-seam")) return null;
    const box = (n) => n.getBoundingClientRect();
    const geo = { cols: cols.length, groups: groups.map((g) => ({
      row: box(g.rowEl.parentNode), body: box(g.bodyEl),
      col: box(g.col.el), stack: g.col.groups.length,
      mids: g.tabs.map((t) => { const r = box(t.chip); return r.left + r.width / 2; }),
    })) };
    return wsDropTarget({ x, y }, geo);
  }

  function showTarget(t) {
    if (!t || t.kind !== "insert") insBar.remove();
    if (!t || t.kind === "insert") hint.remove();
    if (!t) return;
    if (t.kind === "insert") {
      const g = groups[t.group];
      const band = g.rowEl.parentNode;
      const tabs = g.tabs;
      const x = t.index < tabs.length ? tabs[t.index].chip.getBoundingClientRect().left
              : tabs.length ? tabs[tabs.length - 1].chip.getBoundingClientRect().right
              : g.rowEl.getBoundingClientRect().left + 2;
      insBar.style.left = Math.round(x - band.getBoundingClientRect().left - 1) + "px";
      if (insBar.parentNode !== band) band.appendChild(insBar);
      return;
    }
    const body = groups[t.group].bodyEl;
    hint.classList.toggle("bot", t.kind === "split" && t.dir === "down");
    hint.classList.toggle("side", t.kind === "split" && t.dir === "side");
    // The split's tint covers what the new group would take: below, from the
    // middle of the column's height rather than of the body under the band;
    // beside, the right half, whose middle is the body's own.
    if (t.kind === "split" && t.dir === "down") {
      const a = groups[t.group].col.el.getBoundingClientRect();
      hint.style.top = Math.round(a.top + a.height / 2 - body.getBoundingClientRect().top) + "px";
    } else hint.style.top = "";
    if (hint.parentNode !== body) body.appendChild(hint);
  }

  function onDragMove(e) {
    const d = drag;
    if (!d || e.pointerId !== d.id) return;
    d.lastX = e.clientX;
    d.lastY = e.clientY;
    const far = Math.hypot(e.clientX - d.x, e.clientY - d.y);
    if (d.lifted) {
      if (far > WS_TOUCH_SLOP) d.moved = true;
      follow();
      return;
    }
    // Before the lift, a finger that moves is scrolling the band or the tree
    // and the press is let go; a mouse that moves is dragging.
    if (d.touch ? far > WS_TOUCH_SLOP : far > WS_DRAG_SLOP) {
      if (d.touch) endDrag(false); else lift();
    }
  }
  function onDragUp(e) { if (drag && e.pointerId === drag.id) endDrag(true); }
  function onDragCancel(e) { if (drag && e.pointerId === drag.id) endDrag(false); }
  function onDragBlur() { endDrag(false); }
  // Escape puts the tab down where it was, and is not also the pane's Escape.
  function onDragKey(e) {
    if (e.key !== "Escape") return;
    e.preventDefault();
    e.stopPropagation();
    endDrag(false);
  }

  function endDrag(commit) {
    const d = drag;
    if (!d) return;
    drag = null;
    clearTimeout(d.timer);
    window.removeEventListener("pointermove", onDragMove);
    window.removeEventListener("pointerup", onDragUp);
    window.removeEventListener("pointercancel", onDragCancel);
    window.removeEventListener("blur", onDragBlur);
    window.removeEventListener("keydown", onDragKey, true);
    if (!d.lifted) return;
    swallowClick = true;
    setTimeout(() => { swallowClick = false; }, 0);
    d.ghost.remove();
    d.src.classList.remove("dragging");
    document.body.classList.remove("ws-tab-dragging");
    showTarget(null);
    const p = commit ? d.plan : null;
    if (!p || (p.tab && p.tab.closed) || !groups.includes(p.group)) return;
    if (p.op === "focus") { activate(p.tab); focusGroup = p.group; return; }
    if (p.op === "move") {
      if (p.split) splitOff(p.tab, p.split, p.group);
      else moveTab(p.tab, p.group, p.index);
      return;
    }
    dropOpen(p);
  }

  // A tree file opened where it was dropped. A split's new group is made
  // first; when the file does not become a tab there (a page opens in the
  // browser, a preview the ref cannot give), that group goes again.
  async function dropOpen(p) {
    const g = p.split ? makeSplit(p.split, p.group) : p.group;
    if (!g) return;
    focusGroup = g;
    await open(p.path, undefined, { group: g, index: p.index });
    if (p.split && !g.tabs.length && groups.includes(g)) unsplit();
  }

  function fileTabs() {
    const out = [];
    for (const g of groups) for (const t of g.tabs) out.push(t);
    return out;
  }

  function dirtyCount() { return fileTabs().filter((t) => t.dirty).length; }

  function askDiscardAll() {
    const n = dirtyCount();
    return appConfirm(n === 1 ? "1 unsaved file will be discarded."
                              : n + " unsaved files will be discarded.",
                      { title: "Close the file pane?", confirmLabel: "Discard" });
  }

  // Whatever lays itself out from its box is told the box moved: CodeMirror
  // measures its viewport, and the 3D scene sizes its canvas.
  function relayout() {
    for (const t of fileTabs()) {
      if (t.rec) t.rec.view.requestMeasure();
      if (t.mesh) t.mesh.resize();
    }
  }

  // ---- git badges ----------------------------------------------------------
  // The working tree's status for the repository the tree's root is in, asked
  // again while the workspace is on screen, at the diff pane's pace: never
  // more often than every 2 s, and four times as long as the last answer took,
  // up to a minute. A write made here asks at once.

  let marks = new Map();
  // The repository the badges are about, as status named it, and its rows by
  // repo-relative path: what an editor's change marks are read and staged by.
  let gitRoot = "";
  let gitFiles = new Map();

  function pollWanted() {
    return on && attached && !document.hidden && !!treeRoot && !demoApiOn()
           && hasCapStrict("git_status") && !ctx.refFor(treeRoot.path);
  }

  // One status question for the tree's root: wsPoller's { ms, data }, false
  // on a 401, null on anything else (a failure, or a root that moved on while
  // it was out).
  async function askStatus() {
    const at = treeRoot.path;
    try {
      const r = await fetch(apiURL("api/git/status?path=" + encodeURIComponent(at)),
                            { cache: "no-store", headers: authHeaders() });
      // The pairing code is being asked for; nothing more is asked until the
      // workspace is shown again.
      if (r.status === 401) { rejectToken(); return false; }
      const data = r.ok ? await r.json().catch(() => null) : null;
      if (!data || !treeRoot || treeRoot.path !== at) return null;
      return { ms: data.elapsed_ms, data };
    } catch (e) {
      dbg("ws git status failed:", e);
      return null;
    }
  }

  function paintStatus(data) {
    marks = data.root ? wsAggregate(data.files, data.root) : new Map();
    gitRoot = String(data.root || "").replace(/\/+$/, "");
    gitFiles = new Map((data.files || []).map((f) => [f.path, f]));
    paintAll();
  }

  const poller = wsPoller(pollWanted, askStatus, paintStatus);
  const kickStatus = poller.kick;

  const onVisibility = () => {
    if (document.hidden) poller.stop();
    else kickStatus();
  };
  document.addEventListener("visibilitychange", onVisibility);

  function markOf(path, isDir) {
    if (!on || ctx.refFor(path)) return null;
    return wsMarkFor(marks, path, isDir);
  }

  function paintNode(node) {
    if (!node.badgeEl) return;
    const isDir = node.type === "dir";
    const m = markOf(node.path, isDir);
    node.badgeEl.className = "ws-badge" + (m ? " " + m.kind + (isDir ? " dot" : "") : "");
    node.badgeEl.textContent = m && !isDir ? m.text : "";
    // A file's letter is also the way to it with its changes marked, and says
    // so under the pointer.
    const live = !!m && !isDir && m.kind !== "del" && wsKindOf(node.path) === "editor"
                 && marksWanted(node.path);
    node.badgeEl.classList.toggle("ws-marks-link", live);
    if (live) node.badgeEl.title = "Open with its changes marked";
    else node.badgeEl.removeAttribute("title");
  }

  // The listing's rows and tiles, by the name fileRow and fileTile stamp on
  // them. A row with nothing to say has no badge at all.
  function paintListing() {
    const dir = ctx.path();
    for (const row of ctx.q("files-list").querySelectorAll("[data-name]")) {
      const isDir = !!row.querySelector(".file-ic.dir");
      const m = markOf(joinPath(dir, row.dataset.name), isDir);
      let b = row.querySelector(".git-badge");
      if (!m) { if (b) b.remove(); continue; }
      if (!b) {
        b = el("span", { class: "git-badge" });
        if (row.classList.contains("file-tile")) row.querySelector(".file-slot").appendChild(b);
        else row.insertBefore(b, row.querySelector(".file-meta"));
      }
      b.className = "git-badge " + m.kind + (isDir ? " dot" : "");
      b.textContent = isDir ? "" : m.text;
    }
  }

  function paintAll() {
    for (const n of nodes.values()) paintNode(n);
    paintListing();
    // An editor's marks compare with the index, which the poll has just seen
    // through status: a file whose row changed (staged from the terminal,
    // committed, new) reads its base again.
    for (const t of fileTabs()) {
      if (t.marksOn && t.marks && t.rec && t.marks.sig !== marksSig(t.path)) loadBase(t);
      else if (t.kind === "editor") paintChip(t);
    }
  }

  // ---- inline change marks -------------------------------------------------
  // An editor tab's own (wsLineDiff and the rest above): tab.marksOn says
  // whether it shows them, tab.marks holds their compartment, the base they
  // compare with (the index's copy, empty for a file git has never seen) and
  // the hunks last counted, recounted 150 ms after the typing stops.

  // The path git knows `path` by, or "" for one outside the repository.
  function relPath(path) {
    return gitRoot && path.startsWith(gitRoot + "/") ? path.slice(gitRoot.length + 1) : "";
  }

  // Whether `path` can have marks here: inside the repository the badges are
  // about, not a ref's read-only copy, on a computer that reads the index and
  // takes a staged hunk.
  function marksWanted(path) {
    return !!relPath(path) && !ctx.refFor(path)
           && hasCapStrict("git") && hasCapStrict("git_index");
  }

  // What says a base read is out of date: the repository and the file's row
  // in the last status answer.
  function marksSig(path) {
    const rel = relPath(path);
    return gitRoot + "\n" + JSON.stringify((rel && gitFiles.get(rel)) || null);
  }

  function marksExt(tab) {
    const m = tab.marks;
    if (!m.ext) m.ext = wsMarksKit().ext(() => {
      clearTimeout(m.timer);
      m.timer = setTimeout(() => drawMarks(tab), 150);
    });
    return m.ext;
  }

  // The chip's key, or a badge's click: marks on or off for this tab, and with
  // `remember` the default for the next changed file opened.
  function setMarks(tab, on, remember) {
    if (remember) cfg.wsInlineDiff = on;
    tab.marksOn = on;
    const m = tab.marks;
    if (m && tab.rec && !tab.closed) {
      clearTimeout(m.timer);
      m.hunks = [];
      tab.rec.view.dispatch({ effects: m.comp.reconfigure(on ? marksExt(tab) : []) });
      if (on) loadBase(tab);
    }
    paintChip(tab);
  }

  // The index's copy of the file. Not in the index: a file git has never seen
  // (badged new) is all added; anything else not there (ignored, or a read
  // that failed) has no marks rather than a wrong whole-file green.
  async function loadBase(tab) {
    const m = tab.marks;
    if (!m || tab.closed || !tab.marksOn) return;
    const seq = ++m.seq;
    m.sig = marksSig(tab.path);
    let base = null;
    if (marksWanted(tab.path)) {
      try {
        const r = await fetch(apiURL("api/fs/read?path=" + encodeURIComponent(tab.path)
          + "&ref=%3A&root=" + encodeURIComponent(gitRoot)),
          { cache: "no-store", headers: authHeaders() });
        if (r.status === 401) { rejectToken(); return; }
        if (r.ok) {
          const data = await r.json();
          base = Object.assign(wsSplitLines(data.content), { tracked: true });
        } else if (r.status === 404) {
          const mk = markOf(tab.path, false);
          if (mk && mk.kind === "new") base = { lines: [], eol: true, tracked: false };
        }
      } catch (e) {
        dbg("ws marks base failed:", e);
      }
    }
    if (seq !== m.seq || tab.closed || !tab.marksOn) return;
    m.base = base;
    drawMarks(tab);
  }

  // Count the hunks between the base and the buffer and draw them.
  function drawMarks(tab) {
    const m = tab.marks;
    if (!m || !tab.rec || tab.closed) return;
    clearTimeout(m.timer);
    m.timer = 0;
    if (!tab.marksOn) return;
    const view = tab.rec.view;
    let hunks = [];
    if (m.base) {
      const buf = wsSplitLines(view.state.doc.toString());
      if (Math.max(buf.lines.length, m.base.lines.length) > WS_DIFF_MAX_LINES) {
        toast("Too long to mark its changes");
        setMarks(tab, false);
        return;
      }
      hunks = wsLineDiff(wsLineKeys(m.base), wsLineKeys(buf));
      m.buf = buf;
    }
    m.hunks = hunks;
    if (!tab.rec.readOnly && !m.acts) m.acts = () => hunkActs(tab);
    view.dispatch({ effects: wsMarksKit().set.of(
      wsMarksKit().build(view.state.doc, hunks, m.base, tab.rec.readOnly ? null : m.acts)) });
    paintChip(tab);
  }

  // A hunk's tick and cross, the diff pane's (diffActs, 37-git-diff.js). Which
  // hunk they act on is read from where they sit when pressed, so a box kept
  // across a recount still acts on its own lines.
  function hunkActs(tab) {
    const box = diffActs([
      ["Stage this change", "i-check", "ok", () => hunkAct(tab, box, "stage")],
      ["Revert this change", "i-x", "danger", () => hunkAct(tab, box, "revert")],
    ]);
    box.classList.add("cm-ws-acts");
    // The press stays off the editor, which would otherwise move its caret.
    box.addEventListener("mousedown", (e) => e.preventDefault());
    return box;
  }

  // The hunk whose first line `line` (1-based) is: its removed block's line,
  // or its first added line.
  function hunkAt(doc, hunks, line) {
    return hunks.find((h) => (h.al ? Math.min(h.b + 1, doc.lines) : h.b + 1) === line) || null;
  }

  async function hunkAct(tab, node, what) {
    const m = tab.marks, view = tab.rec && tab.rec.view;
    if (!m || !view || !m.base) return;
    let pos;
    try { pos = view.posAtDOM(node); } catch (e) { return; }
    // A recount still pending first, so the hunks are the text's.
    if (m.timer) drawMarks(tab);
    const h = hunkAt(view.state.doc, m.hunks, view.state.doc.lineAt(pos).number);
    if (!h) return;
    if (what === "revert") {
      // The buffer, not the disk: a clean editor writes at once, as the diff
      // pane's revert writes the file; with unsaved edits it stays unsaved.
      const clean = !tab.dirty;
      view.dispatch({ changes: wsRevertEdit(m.base, m.buf, h) });
      if (clean) await save(tab);
      return;
    }
    const rel = relPath(tab.path);
    if (m.base.tracked) {
      await diffApplyTo(gitRoot, { path: rel, action: "stage_hunk",
                                   patch: wsHunkPatch(rel, m.base, m.buf, h) }, toast);
    } else if (tab.dirty) {
      // A new file is staged whole, from the disk.
      toast("Save the file to stage it");
      return;
    } else {
      await diffApplyTo(gitRoot, { path: rel, action: "stage_file" }, toast);
    }
    kickStatus();
    loadBase(tab);
  }

  // ---- on and off ------------------------------------------------------------

  // On: the listing moves out of view into the holder — the same element, so
  // its listeners and the bar above it keep working — and the tree catches up
  // with the folder the listing is on. Off puts the listing back
  // where the markup had it and leaves every tab, saved or not, mounted in the
  // hidden workspace: a collapse discards nothing.
  function setOn(v) {
    if (v === on) return;
    if (!v) for (const g of groups) if (g.active) briefKeep(g.active);
    on = v;
    root.classList.toggle("ws-on", v);
    if (v) {
      if (!treeW) applyTreeW(cfg.wsTreeW || WS_TREE_W);
      hold.appendChild(wrap);
      reveal(ctx.path());
      relayout();
      kickStatus();
      for (const g of groups) if (g.active && g.active.hc) briefBack(g.active);
    } else {
      wsEl.before(wrap);
      for (const t of fileTabs()) pauseMedia(t);
      poller.stop();
    }
    // Off, this takes the listing's badges away with the poll that kept them.
    paintListing();
  }

  // Off the pane for a rail switch (filesStash, 28-file-explorer.js): the
  // workspace goes into the leaving session's record whole, views and all, and
  // comes back with it.
  function detach() {
    setOn(false);
    attached = false;
    wsEl.remove();
    root.style.removeProperty("--ws-tree-w");
    root.style.removeProperty("--ws-split");
    root.style.removeProperty("--ws-split-l");
    root.style.removeProperty("--ws-split-r");
  }

  function attach() {
    if (attached) return;
    attached = true;
    root.appendChild(wsEl);
    if (treeW) root.style.setProperty("--ws-tree-w", treeW + "px");
    if (splitShare) root.style.setProperty("--ws-split", String(splitShare));
    for (const side of ["l", "r"]) {
      if (rowShare[side]) root.style.setProperty("--ws-split-" + side, String(rowShare[side]));
    }
  }

  // The end of it: the pane closed, or the computer it was reading switched.
  // Nothing is asked here; the callers that need an answer ask first.
  function destroy() {
    endDrag(false);
    for (const t of fileTabs()) drop(t);
    detach();
    document.removeEventListener("visibilitychange", onVisibility);
    wsLive.delete(api);
  }

  // A listing landed in the pane: the tree shows where it is, and the rows
  // get their badges.
  function onListing(data) {
    if (!on) return;
    if (treeRoot && insideDir(data.path, treeRoot.path)) {
      const node = nodes.get(data.path);
      if (node) fill(node, data);
    }
    reveal(data.path);
    paintListing();
  }

  // The tab whose editor holds `host` saves, if this workspace has it.
  function saveAt(host) {
    for (const t of fileTabs()) {
      if (t.rec && t.rec.view.dom === host) { save(t); return true; }
    }
    return false;
  }

  const api = {
    get on() { return on; },
    groups,
    setOn, onListing, refreshDir, kickStatus, open, close,
    attach, detach, destroy, dirtyCount, askDiscardAll, saveAt, relayout,
  };
  wsLive.add(api);
  return api;
}

// Ctrl+S (Cmd+S) saves the file being typed in, when that is a workspace tab:
// the terminal and the editor screen keep the key as they had it. On capture,
// so the browser's own Save Page never sees it.
document.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey) return;
  if (String(e.key).toLowerCase() !== "s") return;
  const host = e.target && e.target.closest && e.target.closest(".ws-body .cm-editor");
  if (!host) return;
  for (const ws of wsLive) {
    if (ws.saveAt(host)) {
      e.preventDefault();
      e.stopPropagation();
      return;
    }
  }
}, true);
