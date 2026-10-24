// ============================================================
// Explorer workspace: the expanded pane as a folder tree and file tabs
// ============================================================
// When the docked explorer fills the main area (filesSetExpanded,
// 28-file-explorer.js) it lays itself out the way a code editor does: the
// folder tree on the left at a width the user drags, and the files on the right
// as tabs, the folder listing itself pinned as the first of them. Rows in both
// carry the working tree's git status as the diff pane draws it. Nothing here
// exists until the first expand: a phone, the pane at its docked width and the
// full-screen explorer are the listing they always were.
//
// One workspace per explorer pane, made by that pane (workspace() in
// makeFilesPane) and handed the pane's own surface as `ctx`, so a tree and its
// tabs always act on the listing they sit in. The pane loads before this
// fragment, so it reaches makeWorkspace only from a press, never while it loads.
//
// The tabs are in groups: `groups[0]`, which holds the folder tab and so never
// empties, and while a tab has been dragged down into it a second one under
// the first, VS Code's split down, gone again when its last tab leaves.

// The tree's width, in pixels, before the user has dragged it, and the bounds
// the drag is held to: wide enough for a name, never more than most of the pane.
const WS_TREE_W = 220;
const WS_TREE_MIN = 140;
const WS_TREE_MAX = 0.6;

// The least height either of two groups is held to by the seam between them:
// a band of tabs and a few lines of what they show.
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
// bundle; anything else that is not a picture, a PDF, Markdown or a page is
// text for the editor.
function wsKindOf(path) {
  if (typeof MESH_EXT_RE !== "undefined" && MESH_EXT_RE.test(path)
      && typeof meshMount === "function") return "mesh";
  if (FILES_MEDIA_RE.test(path)) return "media";
  if (FILES_PDF_RE.test(path)) return "pdf";
  if (FILES_MD_RE.test(path)) return "reader";
  if (FILES_HTML_RE.test(path)) return "html";
  return "editor";
}

// Where a dragged tab would land, worked out from plain numbers so it reads
// without a page: `pt` is the pointer, `geo.area` the groups' box, and each of
// `geo.groups` its tab band's box (`row`), its body's box (`body`) and the
// middles of its chips in strip order (`mids`). Over a band, the slot before
// the first chip whose middle is right of the pointer. Over a body while there
// is one group, its lower half is a split and its upper half that group; with
// two, a body is its group, the tab added at the end. Anywhere else, nothing.
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
      const half = geo.area.top + (geo.area.bottom - geo.area.top) / 2;
      if (geo.groups.length === 1 && pt.y >= half) return { kind: "split" };
      return { kind: "append", group: g };
    }
  }
  return null;
}

// A tab's move in the model alone: out of its group's list, into `to`'s at
// `index` (a slot counted with the tab still where it was, as the drop target
// counts it; null for the end), never ahead of a pinned folder tab. The folder
// tab itself does not move. Says where it went and, when the move left the
// group it came from with no tabs, that group, for the caller to take away.
function wsMoveTab(tab, to, index) {
  if (tab.pinned) return null;
  const from = tab.group;
  const at = from.tabs.indexOf(tab);
  from.tabs.splice(at, 1);
  if (index == null) index = to.tabs.length;
  else if (from === to && index > at) index--;
  const first = to.tabs[0] && to.tabs[0].pinned ? 1 : 0;
  index = Math.max(first, Math.min(index, to.tabs.length));
  to.tabs.splice(index, 0, tab);
  tab.group = to;
  return { index, emptied: from !== to && !from.tabs.length ? from : null };
}

// The name a folder goes by in the tree's head: ~ for home, as the crumbs say.
function wsFolderLabel(path, home) {
  if (home && path === home) return "~";
  return baseName(path) || "/";
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
  const wsEl = el("div", { class: "ws", tabindex: "-1" },
    el("aside", { class: "ws-tree" }, treeHead, treeBody), seam, groupsEl);
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
    }
    nodes.set(path, node);
    paintNode(node);
    return node;
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
  // is the selected row. A folder outside the tree's root is a new root.
  async function reveal(path) {
    if (!treeRoot || !insideDir(path, treeRoot.path)) reRoot(path);
    const parts = path.slice(treeRoot.path.length).split("/").filter(Boolean);
    let node = treeRoot, at = treeRoot.path;
    if (!node.loaded && !(await expand(node))) return;
    for (const seg of parts) {
      at = joinPath(at, seg);
      node = nodes.get(at);
      if (!node || node.type !== "dir") return;
      if (!(await expand(node))) return;
    }
    if (on && activeTab() === folderTab) select(path);
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
      // The chevron only opens or closes the branch. The name also lists the
      // folder, which is the folder tab's job, so that tab comes to the front.
      if (ev.target.closest(".ws-twisty")) {
        if (node.expanded) setExpanded(node, false);
        else expand(node);
        return;
      }
      activate(folderTab);
      ctx.navigate(node.path);
      expand(node);
      return;
    }
    if (node.type === "file") open(node.path);
    else if (node.entry) ctx.openFileActions(Object.assign({}, node.entry, { dir: parentOf(node.path) }));
  });

  // The listing's right-click menu, over a row of the tree: the entry carries
  // the folder it is in, which is not the folder the listing is showing.
  treeBody.addEventListener("contextmenu", (ev) => {
    const row = ev.target.closest(".ws-node");
    const node = row && nodes.get(row.dataset.path);
    if (!node || !node.entry) return;
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
    const bodyEl = el("div", { class: "ws-body" });
    const g = { el: el("div", { class: "ws-group", "data-group": String(groups.length) },
                    el("div", { class: "ws-tabs" }, rowEl), bodyEl),
                rowEl, bodyEl, tabs: [], active: null };
    groupsEl.appendChild(g.el);
    groups.push(g);
    return g;
  }

  // The chip is made once and repainted in place, the browser strip's way
  // (renderBrowserTabs, 42-browser.js): the name, a dot while the file has
  // unsaved edits (the cross takes its place under the pointer), and the cross.
  function makeChip(tab) {
    const name = el("button", { type: "button", class: "tab-name", role: "tab",
                                title: tab.path || "" },
      tab.pinned ? el("span", { class: "ws-tab-ic" }, svgIcon("i-m-folder")) : null,
      el("span", { class: "tab-word" }, tab.title));
    name.addEventListener("click", () => activate(tab));
    const chip = el("div", { class: "tab-chip" + (tab.pinned ? " ws-pinned" : "") }, name);
    if (!tab.pinned) {
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
    }
    tab.chip = chip;
    if (!tab.pinned) wireTabDrag(tab);
  }

  function paintChip(tab) {
    const word = tab.chip.querySelector(".tab-word");
    if (word.textContent !== tab.title) word.textContent = tab.title;
    tab.chip.classList.toggle("on", tab.group.active === tab);
    tab.chip.classList.toggle("dirty", !!tab.dirty);
    tab.chip.firstElementChild.setAttribute("aria-selected",
                                             tab.group.active === tab ? "true" : "false");
  }

  function addTab(g, props) {
    const tab = Object.assign({ id: ++tabSeq, path: "", kind: "editor", title: "",
                                dirty: false, pinned: false, chip: null, body: null,
                                rec: null, mesh: null, closed: false, group: g }, props);
    tab.body = el("div", { class: "ws-pane ws-" + tab.kind });
    makeChip(tab);
    g.tabs.push(tab);
    g.rowEl.appendChild(tab.chip);
    g.bodyEl.appendChild(tab.body);
    if (tab.path) byPath.set(tab.path, tab);
    paintChip(tab);
    return tab;
  }

  function activeTab() { return groups[0].active; }

  function activate(tab) {
    const g = tab.group;
    const was = g.active;
    g.active = tab;
    for (const t of g.tabs) {
      t.body.classList.toggle("on", t === tab);
      paintChip(t);
    }
    // A film left playing in a tab nobody is looking at is sound from nowhere.
    if (was && was !== tab) pauseMedia(was);
    if (tab.rec) tab.rec.view.requestMeasure();
    if (tab.mesh) tab.mesh.resize();
    select(tab.pinned ? ctx.path() : tab.path);
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

  const folderTab = addTab(makeGroup(), { kind: "folder", pinned: true,
                                          title: wsFolderLabel(ctx.path(), ctx.home()) });
  activate(folderTab);

  // A file in a tab: the one already open if it is, otherwise a new tab at the
  // end of the strip, filled once what it shows has been read. `kind` is a
  // caller's say over the tap rules (the action sheet's Edit on a page), and
  // `opts.create` a file the first save will make.
  async function open(path, kind, opts) {
    const have = byPath.get(path);
    if (have) { activate(have); return have; }
    kind = kind || wsKindOf(path);
    const ref = ctx.refFor(path);
    // A page renders in a tab of the browser's (openRendered): it is served
    // sandboxed into an origin of its own, which a frame in this document would
    // undo. At a ref there is no page on the disk, so it opens as its source.
    if (kind === "html") {
      if (!ref) { ctx.openRendered(path); return null; }
      kind = "editor";
    }
    if (ref && (kind === "media" || kind === "pdf" || kind === "mesh")) {
      toast("Can't preview " + (kind === "pdf" ? "PDFs" : kind === "mesh" ? "3D files"
                                                          : "images") + " from " + ref);
      return null;
    }
    if (kind === "pdf" && demoApiOn()) { toast("Not in the demo"); return null; }
    const tab = addTab(focusGroup || groups[0], { path, kind, title: baseName(path) });
    activate(tab);
    const ok = await fillTab(tab, opts || {}).catch((e) => { dbg("ws tab failed:", e); return false; });
    if (!ok && !tab.closed) drop(tab);
    return ok ? tab : null;
  }

  async function fillTab(tab, opts) {
    const path = tab.path;
    if (tab.kind === "editor") {
      let content = "", hash = "", readOnly = false, lossy = false;
      if (!opts.create) {
        const data = await ctx.readText(path);
        if (!data) return false;
        content = data.content; hash = data.hash;
        lossy = !!data.lossy;
        readOnly = lossy || !!data.readonly;
      }
      try { await ensureCM(); } catch (e) { toast("Couldn't load the editor"); return false; }
      if (tab.closed) return true;
      tab.rec = edMakeView({
        host: tab.body, path, content, readOnly,
        onDirty: (rec, dirty) => { tab.dirty = dirty; paintChip(tab); },
        onSave: () => save(tab),
        onQuit: (rec, bang) => close(tab, bang),
      });
      tab.rec.hash = hash;
      if (cfg.editorVimOn) { edDefineVimEx(); edLoadVimrc(); }
      if (lossy) toast("Not valid UTF-8 — opened read-only");
      return true;
    }
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
    else tab.body.appendChild(el("div", { class: "ws-media" }, media));
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
    if (tab.pinned || tab.closed) return;
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
    if (tab.rec) { edDropView(tab.rec); tab.rec = null; }
    if (tab.mesh) { tab.mesh.destroy(); tab.mesh = null; }
    const v = tab.body.querySelector("video");
    if (v) { v.pause(); v.removeAttribute("src"); }
    const g = tab.group;
    const at = g.tabs.indexOf(tab);
    g.tabs.splice(at, 1);
    if (byPath.get(tab.path) === tab) byPath.delete(tab.path);
    tab.chip.remove();
    tab.body.remove();
    if (g.active === tab) {
      g.active = null;
      const next = g.tabs[at] || g.tabs[at - 1];
      if (next) activate(next);
    }
    if (!g.tabs.length) unsplit();
  }

  // ---- the second group --------------------------------------------------------
  // Made by the first tab dragged to the lower half of the files, under the
  // first group with a seam between them; the seam's drag sets the top group's
  // share of the height (--ws-split on the pane's root, remembered as
  // cfg.wsSplit by the markup's own pane, as the tree's width is).

  const groupSeam = el("div", { class: "ws-seam ws-group-seam", role: "separator",
                                "aria-orientation": "horizontal",
                                "aria-label": "Resize the file groups" });
  let splitShare = 0;

  function applySplit(share) {
    splitShare = share;
    root.style.setProperty("--ws-split", String(share));
  }

  function splitDown(tab) {
    if (tab.pinned) return;
    if (groups.length < 2) {
      const g = makeGroup();
      g.el.before(groupSeam);
      groupsEl.classList.add("ws-two");
      if (!splitShare) applySplit(cfg.wsSplit);
    }
    moveTab(tab, groups[1], null);
  }

  // The second group goes once nothing is left in it; the first never does.
  function unsplit() {
    const g = groups[1];
    if (!g || g.tabs.length) return;
    groups.splice(1, 1);
    g.el.remove();
    groupSeam.remove();
    groupsEl.classList.remove("ws-two");
    if (focusGroup === g) focusGroup = null;
    relayout();
  }

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
      g.bodyEl.appendChild(tab.body);
      if (from.active === tab) {
        from.active = null;
        const n = from.tabs[at] || from.tabs[at - 1];
        if (n) activate(n);
      }
    }
    activate(tab);
    focusGroup = g;
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

  // The tree seam's drag turned on its side: the top group is anchored at the
  // top, so downward travel is a taller top group.
  (function groupResize() {
    let dragging = false, grab = 0;
    groupSeam.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      dragging = true;
      grab = e.clientY - groupSeam.getBoundingClientRect().top;
      groupSeam.classList.add("dragging");
      groupSeam.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    groupSeam.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const r = groupsEl.getBoundingClientRect();
      const avail = r.height - groupSeam.offsetHeight;
      if (avail < 2 * WS_GROUP_MIN) return;
      const top = Math.max(WS_GROUP_MIN, Math.min(e.clientY - grab - r.top, avail - WS_GROUP_MIN));
      applySplit(Math.round(top / avail * 10000) / 10000);
    });
    const finish = () => {
      if (!dragging) return;
      dragging = false;
      groupSeam.classList.remove("dragging");
      if (ctx.id === "files") cfg.wsSplit = splitShare;
      relayout();
    };
    groupSeam.addEventListener("pointerup", finish);
    groupSeam.addEventListener("pointercancel", finish);
  })();

  // ---- dragging a tab ------------------------------------------------------------
  // By pointer events rather than the HTML5 drag the listing's rows move files
  // with: that drag would offer a tab to every folder row as a file to move.
  // A copy of the chip follows the pointer and the chip stays in its place,
  // dimmed, until the release says where it goes: a slot in a band (a bar
  // between two chips), the lower half of the files while there is one group
  // (a split), or the other group's body. Escape, or a cancelled pointer, puts
  // it down where it was.
  let tabDrag = null;
  let swallowClick = false;
  const insBar = el("div", { class: "ws-drop-ins", "aria-hidden": "true" });
  const hint = el("div", { class: "ws-drop-hint", "aria-hidden": "true" });

  function wireTabDrag(tab) {
    const chip = tab.chip;
    chip.addEventListener("pointerdown", (e) => {
      if (tabDrag || !e.isPrimary) return;
      if (e.pointerType === "mouse" && e.button !== 0) return;
      if (e.target.closest(".tab-del")) return;
      const touch = e.pointerType !== "mouse";
      tabDrag = { tab, id: e.pointerId, touch, x: e.clientX, y: e.clientY,
                  lastX: e.clientX, lastY: e.clientY,
                  lifted: false, timer: 0, ghost: null, target: null };
      window.addEventListener("pointermove", onDragMove);
      window.addEventListener("pointerup", onDragUp);
      window.addEventListener("pointercancel", onDragCancel);
      window.addEventListener("blur", onDragBlur);
      if (touch) tabDrag.timer = setTimeout(liftTab, WS_LIFT_MS);
    });
    // The click a drag's release makes is not a press on the tab.
    chip.addEventListener("click", (e) => {
      if (!swallowClick) return;
      swallowClick = false;
      e.preventDefault();
      e.stopPropagation();
    }, true);
    // Once lifted the finger belongs to the tab, and the band must not scroll
    // under it; Android's long press would also open a menu on the same hold.
    chip.addEventListener("touchmove", (e) => {
      if (tabDrag && tabDrag.tab === tab && tabDrag.lifted) e.preventDefault();
    }, { passive: false });
    chip.addEventListener("contextmenu", (e) => {
      if (tabDrag && tabDrag.tab === tab && tabDrag.touch) e.preventDefault();
    });
  }

  function liftTab() {
    const d = tabDrag;
    clearTimeout(d.timer);
    d.timer = 0;
    d.lifted = true;
    const chip = d.tab.chip;
    try { chip.setPointerCapture(d.id); } catch (e) {}
    const r = chip.getBoundingClientRect();
    const copy = chip.cloneNode(true);
    copy.classList.remove("on");
    d.ghost = el("div", { class: "ws-tab-ghost", "aria-hidden": "true",
                          style: "left:" + r.left + "px;top:" + r.top + "px;width:"
                                 + r.width + "px;height:" + r.height + "px" }, copy);
    document.body.appendChild(d.ghost);
    chip.classList.add("dragging");
    document.body.classList.add("ws-tab-dragging");
    window.addEventListener("keydown", onDragKey, true);
    if (d.touch && navigator.vibrate) navigator.vibrate(10);
    followTab();
  }

  function followTab() {
    const d = tabDrag;
    d.ghost.style.transform = "translate(" + (d.lastX - d.x) + "px," + (d.lastY - d.y) + "px)";
    d.target = dropAt(d.lastX, d.lastY);
    showTarget(d.target);
  }

  // The drop target under the pointer, from the boxes on screen now (a band
  // scrolls, and the seam moves). Only inside this workspace's groups: the
  // ghost takes no pointer, so what is under it is what it is over.
  function dropAt(x, y) {
    const hit = document.elementFromPoint(x, y);
    if (!hit || !groupsEl.contains(hit) || hit.closest(".ws-group-seam")) return null;
    const box = (n) => n.getBoundingClientRect();
    const geo = { area: box(groupsEl), groups: groups.map((g) => ({
      row: box(g.rowEl.parentNode), body: box(g.bodyEl),
      mids: g.tabs.map((t) => { const r = box(t.chip); return r.left + r.width / 2; }),
    })) };
    const t = wsDropTarget({ x, y }, geo);
    if (!t) return null;
    // Dropped on the body of its own group, a tab stays where it is.
    if (t.kind === "append" && groups[t.group] === tabDrag.tab.group) return null;
    if (t.kind === "insert") {
      const first = groups[t.group].tabs[0];
      if (first && first.pinned) t.index = Math.max(1, t.index);
    }
    return t;
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
                                      : tabs[tabs.length - 1].chip.getBoundingClientRect().right;
      insBar.style.left = Math.round(x - band.getBoundingClientRect().left - 1) + "px";
      if (insBar.parentNode !== band) band.appendChild(insBar);
      return;
    }
    const body = (t.kind === "split" ? groups[0] : groups[t.group]).bodyEl;
    hint.classList.toggle("bot", t.kind === "split");
    // The split's tint starts where the split starts, the middle of the
    // groups' height rather than of the body under the band.
    if (t.kind === "split") {
      const a = groupsEl.getBoundingClientRect();
      hint.style.top = Math.round(a.top + a.height / 2 - body.getBoundingClientRect().top) + "px";
    } else hint.style.top = "";
    if (hint.parentNode !== body) body.appendChild(hint);
  }

  function onDragMove(e) {
    const d = tabDrag;
    if (!d || e.pointerId !== d.id) return;
    d.lastX = e.clientX;
    d.lastY = e.clientY;
    if (d.lifted) { followTab(); return; }
    // Before the lift, a finger that moves is scrolling the band and the press
    // is let go; a mouse that moves is dragging.
    const far = Math.hypot(e.clientX - d.x, e.clientY - d.y);
    if (d.touch ? far > WS_TOUCH_SLOP : far > WS_DRAG_SLOP) {
      if (d.touch) endTabDrag(false); else liftTab();
    }
  }
  function onDragUp(e) { if (tabDrag && e.pointerId === tabDrag.id) endTabDrag(true); }
  function onDragCancel(e) { if (tabDrag && e.pointerId === tabDrag.id) endTabDrag(false); }
  function onDragBlur() { endTabDrag(false); }
  // Escape puts the tab down where it was, and is not also the pane's Escape.
  function onDragKey(e) {
    if (e.key !== "Escape") return;
    e.preventDefault();
    e.stopPropagation();
    endTabDrag(false);
  }

  function endTabDrag(commit) {
    const d = tabDrag;
    if (!d) return;
    tabDrag = null;
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
    d.tab.chip.classList.remove("dragging");
    document.body.classList.remove("ws-tab-dragging");
    showTarget(null);
    const t = commit ? d.target : null;
    if (!t || d.tab.closed) return;
    if (t.kind === "split") splitDown(d.tab);
    else moveTab(d.tab, groups[t.group], t.kind === "insert" ? t.index : null);
  }

  function fileTabs() {
    const out = [];
    for (const g of groups) for (const t of g.tabs) if (!t.pinned) out.push(t);
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
  let pollTimer = null, pollBusy = false, pollAgain = false;
  let pollEvery = DIFF_POLL_MS;

  function pollWanted() {
    return on && attached && !document.hidden && !!treeRoot && !demoApiOn()
           && hasCapStrict("git_status") && !ctx.refFor(treeRoot.path);
  }

  function schedule() {
    clearTimeout(pollTimer);
    pollTimer = pollWanted() ? setTimeout(poll, pollEvery) : null;
  }

  async function poll() {
    clearTimeout(pollTimer);
    pollTimer = null;
    if (!pollWanted()) return;
    if (pollBusy) { pollAgain = true; return; }
    pollBusy = true;
    const at = treeRoot.path;
    let again = true;
    try {
      const r = await fetch(apiURL("api/git/status?path=" + encodeURIComponent(at)),
                            { cache: "no-store", headers: authHeaders() });
      // The pairing code is being asked for; nothing more is asked until the
      // workspace is shown again.
      if (r.status === 401) { rejectToken(); again = false; return; }
      const data = r.ok ? await r.json().catch(() => null) : null;
      if (data && treeRoot && treeRoot.path === at) {
        marks = data.root ? wsAggregate(data.files, data.root) : new Map();
        const ms = Number(data.elapsed_ms) || 0;
        pollEvery = Math.min(DIFF_POLL_MAX_MS,
                             Math.max(DIFF_POLL_MS, Math.round(ms * DIFF_POLL_FACTOR)));
        paintAll();
      }
    } catch (e) {
      dbg("ws git status failed:", e);
    } finally {
      pollBusy = false;
      if (pollAgain && again) { pollAgain = false; poll(); }
      else if (again) schedule();
    }
  }

  function kickStatus() {
    if (pollBusy) { pollAgain = true; return; }
    poll();
  }

  const onVisibility = () => {
    if (document.hidden) { clearTimeout(pollTimer); pollTimer = null; }
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
  }

  // ---- on and off ------------------------------------------------------------

  // On: the listing moves into the folder tab — the same element, so its
  // listeners, its drop zone and its thumbnails' observer come with it — and
  // the tree catches up with the folder on screen. Off puts the listing back
  // where the markup had it and leaves every tab, saved or not, mounted in the
  // hidden workspace: a collapse discards nothing.
  function setOn(v) {
    if (v === on) return;
    on = v;
    root.classList.toggle("ws-on", v);
    if (v) {
      if (!treeW) applyTreeW(cfg.wsTreeW || WS_TREE_W);
      folderTab.body.appendChild(wrap);
      reveal(ctx.path());
      relayout();
      kickStatus();
    } else {
      wsEl.before(wrap);
      for (const t of fileTabs()) pauseMedia(t);
      clearTimeout(pollTimer);
      pollTimer = null;
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
  }

  function attach() {
    if (attached) return;
    attached = true;
    root.appendChild(wsEl);
    if (treeW) root.style.setProperty("--ws-tree-w", treeW + "px");
    if (splitShare) root.style.setProperty("--ws-split", String(splitShare));
  }

  // The end of it: the pane closed, or the computer it was reading switched.
  // Nothing is asked here; the callers that need an answer ask first.
  function destroy() {
    endTabDrag(false);
    for (const t of fileTabs()) drop(t);
    detach();
    document.removeEventListener("visibilitychange", onVisibility);
    wsLive.delete(api);
  }

  // A listing landed in the pane: the folder tab takes its name, the tree
  // shows where it is, and its rows get their badges.
  function onListing(data) {
    folderTab.title = root.querySelector(".files-title-name").textContent
                      || wsFolderLabel(data.path, ctx.home());
    paintChip(folderTab);
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
