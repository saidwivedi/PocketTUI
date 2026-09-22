// ============================================================
// Side pane — the column beside the terminal
// ============================================================
// Three panes want the terminal's right edge: the git changes (37-git-diff.js),
// the file explorer docked rather than opened over the terminal
// (28-file-explorer.js), and the in-app browser (42-browser.js). They share one
// column, up to two of them at a time, stacked one above the other with a seam
// to trade height across — a laptop has room for a terminal and two things
// beside it, and a third row would leave none of them a height worth reading
// at. So the geometry lives here: one width, one split, the two seams to drag
// them by, and whoever opens takes the bottom row off whoever had it.
//
// Wide layouts only, and that gate is the stylesheet's (see GIT DIFF PANE and
// the docked explorer's rules): dropping below the breakpoint stops those
// rules applying and the split disappears without being forgotten.

// What the terminal keeps whatever the drag asks for. 320px is roughly 40
// columns at the default size — a shell that is cramped but still a shell.
const SIDE_TERM_MIN = 320;

// The narrowest each pane is worth drawing at. Below 300 a hunk header wraps
// and the diff stops being readable at all; the explorer's bar carries a path
// plus five controls and needs the extra.
const SIDE_MIN = { diff: 300, files: 340, browser: 360 };

// The shortest either row is worth drawing at. 160px is a bar and three rows of
// what is under it — less than that and the row is a header with a sliver, and
// the drag would let one row swallow the other outright.
const SIDE_ROW_MIN = 160;

// The seam's own thickness, the stylesheet's --side-gutter said again rather
// than read back off the document: the clamp needs the number on every
// pointermove, and it is a constant either way.
const SIDE_GUTTER = 7;

// What each pane type is called in the menu that opens one.
const SIDE_LABEL = { files: "Files", browser: "Browser", diff: "Changes" };

// A row is an instance of a pane, not a pane type: the column can hold two
// explorers, or two browsers, and the two halves of the record, the focus and
// the keys all have to be able to say which of them they mean. The id is the
// type for the pane the markup ships and `type + "#2"` for the one made at
// runtime beside it, and it names a slot rather than a birth order — either can
// be the one that is open, and a record naming only "files#2" restores that one
// alone.
function sideType(id) {
  const at = id.indexOf("#");
  return at < 0 ? id : id.slice(0, at);
}

// Every instance the column knows about, open or not, keyed by id. Each module
// hands its own in at load (and, from the second instance on, at the moment it
// makes one), because everything here that is not geometry belongs to the pane:
// which elements are in its row, how it closes, how its expand is folded away,
// and what a row that just changed height has to be told.
//
//   { type, els(), close(), setExpanded(on), onRowResize?() }
//
// close() ends in sideDrop(id) or refuses — the docked editor with unsaved work
// is the one thing in the app that can say no to the column.
const sideInst = {};

function sideRegister(id, inst) { sideInst[id] = inst; }

function sideEls(id) {
  const inst = sideInst[id];
  return inst ? inst.els() : [];
}

// Which slot of a type is free, top slot first: the id the markup's own pane
// answers to, then the clone's. Null when the column already holds both.
function sideFreeId(type) {
  for (const id of [type, type + "#2"]) if (!sideRows.includes(id)) return id;
  return null;
}

// How a second instance of a type is made. Each module that can duplicate
// itself puts its factory in here; a type with none cannot be opened twice, and
// the menu below leaves it out rather than offering a row that would do
// nothing.
const sideMakers = {};

function sideCreate(type) {
  const make = sideMakers[type];
  if (!make) return null;
  const id = sideFreeId(type);
  return id && make(id) ? id : null;
}

let sideRows = [];      // instance ids — top row first, at most two
let sideWidth = 0;      // 0 until sized — see sideClaim()
let sideSplit = 0;      // the top row's share of the window, 0 until sized
let sideFull = null;    // which row is filling the main area, or null for neither

// The instance of each type a key press means. With one of a kind open it is
// that one; with two, the one last pressed in, because the pane the user is
// working in is the pane a key about that type is about. Set by the press
// itself below and by an open (the pane that just arrived is the one its key
// toggles back away).
const sideFocus = { diff: null, files: null, browser: null };

// Which open instance a node is inside, or null for a node in none of them.
function sideIdAt(node) {
  for (const id of sideRows) {
    for (const el of sideEls(id)) if (el && el.contains(node)) return id;
  }
  return null;
}

function sideFocusedOf(type) {
  const want = sideFocus[type];
  if (want && sideRows.includes(want)) return want;
  // Nothing pressed in yet, or the pressed one has since closed: the topmost
  // row of that type, which with one open is the only answer there is.
  for (const id of sideRows) if (sideType(id) === type) return id;
  return null;
}

// The main pane is the window less the rail and the seam it is drawn on, read
// off the seam itself rather than recomputed from --sidebar-w: the rail's own
// geometry is the truth, and on a phone the handle is display:none and takes
// no width, which reads as "the window" without a branch.
function sideMainW() {
  const r = $("rail-resize").getBoundingClientRect();
  return window.innerWidth - (r.width ? r.right : 0);
}

// Clamped on every read, the way the rail's width is: a stored number is a
// number the window may since have grown or shrunk out from under. The floor is
// the widest pane in the column, since one width serves both rows.
function sideClampW(px) {
  let min = SIDE_MIN.diff;
  for (const id of sideRows) min = Math.max(min, SIDE_MIN[sideType(id)] || 0);
  const max = Math.max(min, sideMainW() - SIDE_TERM_MIN);
  return Math.round(Math.min(max, Math.max(min, px)));
}

function applySideWidth(px) {
  sideWidth = sideClampW(px);
  document.documentElement.style.setProperty("--side-w", sideWidth + "px");
}

// The seam between the rows, as the top row's share of the window's height.
// The window's rather than the column's because the rows are fixed boxes: a
// percentage in their top and bottom resolves against the viewport, so the
// fraction the stylesheet is handed is the fraction this clamps.
function applySideSplit(frac) {
  const h = window.innerHeight || 1;
  const lo = SIDE_ROW_MIN / h;
  const hi = (h - SIDE_ROW_MIN - SIDE_GUTTER) / h;
  sideSplit = Math.min(Math.max(frac, lo), Math.max(lo, hi));
  document.documentElement.style.setProperty(
    "--side-split", (sideSplit * 100).toFixed(3) + "%");
}

// Clearing an expand through the instance that owns it: the flag, the key it is
// remembered under and the icons on its own bar are that pane's, and this knows
// only which row is which. The diff registers a no-op — it has no expand.
function sideSetExpanded(id, on) {
  const inst = sideInst[id];
  if (inst) inst.setExpanded(on);
}

// Puts `id` in the column: a second row under whatever is there, or a row taken
// off whoever had it — through that pane's own close, so it forgets itself
// exactly as its own cross would. False is that close saying no (the docked
// editor asking about unsaved work), and the caller has to stand down with it
// rather than open half a pane. The refit is what carries the new width to xterm
// and on to tmux: the terminal has just changed shape, and it only learns that
// from a fit.
//
// `opts.keep` is the row the press came from — the split key in a pane's own bar
// (sideMenuPick) — and it is the row that stays: what the user pressed in is
// never what the press throws out. The other one goes, and the arrival takes
// its slot rather than the bottom, so a pane opened from the bottom row's bar
// lands on top and the row pressed in stays where it was. Without it the bottom
// row is the one that goes, which is every other way in.
function sideClaim(id, opts) {
  if (sideRows.includes(id)) return true;
  let at = -1;
  if (sideRows.length >= 2) {
    const keep = opts && sideRows.includes(opts.keep) ? opts.keep : null;
    const out = keep ? (sideRows[0] === keep ? sideRows[1] : sideRows[0]) : sideRows[1];
    at = sideRows.indexOf(out);
    const inst = sideInst[out];
    if (inst) inst.close();
    if (sideRows.length >= 2) return false;
  }
  // The evicted row's own slot, which is a push whenever that was the bottom.
  if (at >= 0) sideRows.splice(at, 0, id);
  else sideRows.push(id);
  // The pane that just arrived is the one its key toggles away again, whichever
  // of the two instances it is.
  sideFocus[sideType(id)] = id;
  // A pane filling the main area has no room beside it for the one arriving,
  // so the arrival is what collapses it — the same press would otherwise land
  // a row behind a pane covering it.
  if (sideFull) sideSetExpanded(sideFull, false);
  sideRemember();
  // Half the main pane the first time, and whatever was dragged after that.
  applySideWidth(cfg.sideWidth || Math.round(sideMainW() / 2));
  if (sideRows.length === 2) applySideSplit(cfg.sideSplit || 0.5);
  $("screen-term").classList.add("side-open");
  sideLayout();
  refit(0);
  return true;
}

// The mirror, and a no-op for a pane that is not in the column — the two close
// paths (its own cross, and another pane claiming) both land here.
function sideDrop(id) {
  const at = sideRows.indexOf(id);
  if (at < 0) return;
  sideRows.splice(at, 1);
  // A closed row is nothing for a key to toggle: the focus goes back to
  // whichever of that type is still open, which sideFocusedOf works out.
  if (sideFocus[sideType(id)] === id) sideFocus[sideType(id)] = null;
  if (!sideRows.length) {
    sideFull = null;
    cfg.sidePane = null;
    $("screen-term").classList.remove("side-open", "side-full", "side-two");
  } else {
    sideRemember();
    // The row that left may have been the one holding the width's floor up, so
    // the remaining one gets its say on a width it was never asked about.
    applySideWidth(sideWidth);
    sideLayout();
  }
  refit(0);
}

// Remembered against the session it was opened in, not against the app: the
// column is that session's (see fileViews, 09-image-viewer.js), and a reload
// only puts the rows back when that same session is opened again. The order is
// written out with them: the two rows come back one at a time and in whatever
// order their own opens resolve, and this is what says which way up they were
// (sideOrder). The browser's page and tabs are the browser's own half of the
// record (browserRemember, 42-browser.js) and only carry over while it is
// still one of the rows, and it is kept per instance: `panes` is that half, one
// entry per row that has one, so two browsers do not write over each other's
// tabs. An entry for a row that has just closed is dropped with it — a pane the
// column no longer holds has nothing to come back to.
function sideRemember() {
  if (!sideRows.length) return;
  const prev = cfg.sidePane;
  const prevPanes = prev && prev.panes ? prev.panes : {};
  const rec = { rows: sideRows.slice(), session: currentSession || "", panes: {} };
  for (const id of sideRows) if (prevPanes[id]) rec.panes[id] = prevPanes[id];
  cfg.sidePane = rec;
}

// How each row of a remembered column is put back: the record names ids, and
// every type has its own way in — the changes of this session's repo, the
// explorer at its cwd, the browser on the tabs the record carries. The pane's
// own half of the record goes with it, and the promise (the explorer's open is a
// cwd round trip away) comes back so the caller can order the rows once they
// have all landed. An id no module has an instance for is a no-op: a record can
// name the second of a kind, and a build whose panes do not duplicate has
// nothing to open for it.
function sideBootOpen(id, rec) {
  const type = sideType(id);
  if (type === "diff") return diffSetOpen(true, id, rec);
  if (type === "browser") {
    const p = rec || {};
    return openBrowser(p.url, p.tabs, p.tab, id);
  }
  return filesFollowSession(id);
}

// The two rows trade places. Which is on top is a preference and nothing else
// reads it, so this is the order, the record and a repaint — no box changed
// width, so the terminal has nothing to be re-fitted to.
function sideSwap() {
  if (sideRows.length !== 2) return;
  sideRows.reverse();
  sideRemember();
  sideLayout();
}

// The order a restore asks for, once the rows it names are all up: each pane
// comes back through its own open path and claims whenever that path resolves,
// which is not the order they were left in. A no-op unless the rows asked for
// are exactly the rows that are there, so a restore that only half happened
// leaves whatever did open where it landed.
function sideOrder(rows) {
  if (!Array.isArray(rows) || rows.length !== sideRows.length) return;
  const asked = rows.slice().sort();
  const have = sideRows.slice().sort();
  if (asked.some((t, i) => t !== have[i])) return;
  if (rows.every((t, i) => t === sideRows[i])) return;
  sideRows = rows.slice();
  sideRemember();
  sideLayout();
}

// Which row, if any, is filling the main area. The class lives on #screen-term
// — the seams it hides and the width it overrides are all read from there — and
// the flag lives in the module whose pane it is, so this is the one place that
// knows both, and the one place that can hold the column to one expanded row:
// expanding either folds the other's expand away with it.
function sideSetFull(id, on) {
  if (on) {
    if (sideFull && sideFull !== id) sideSetExpanded(sideFull, false);
    sideFull = id;
  } else if (sideFull === id) sideFull = null;
  $("screen-term").classList.toggle("side-full", sideFull !== null);
  sideLayout();
}

// Which row each pane's elements are in, which is three classes and the way the
// swap keys point. Everything else about their boxes is the stylesheet's, and a
// pane on its own is in no row at all — it keeps the full-height rules it has
// always had.
function sideLayout() {
  const two = sideRows.length === 2;
  $("screen-term").classList.toggle("side-two", two);
  // Every instance, not only the open ones: a pane that has just left the
  // column is a pane whose elements are still wearing the class of the row it
  // was in, and nothing else takes it off them.
  for (const id of Object.keys(sideInst)) {
    const at = two ? sideRows.indexOf(id) : -1;
    // Expanded, the row under the full pane is not a shorter row but none at
    // all: there is nothing of it left to see behind a pane covering the column.
    const cls = at < 0 ? ""
      : sideFull ? (sideFull === id ? "" : "side-hidden")
      : at === 0 ? "side-top" : "side-bot";
    for (const el of sideEls(id)) {
      if (!el) continue;
      el.classList.remove("side-top", "side-bot", "side-hidden");
      if (cls) el.classList.add(cls);
      if (!two) continue;
      // Where the pane this key is in would land, rather than that there is a
      // swap at all: the top row's points down and the bottom row's up.
      for (const btn of el.querySelectorAll(".dock-swap")) {
        btn.querySelector("use").setAttribute("href",
          at === 0 ? "#i-chev-down" : "#i-chev-up");
        btn.setAttribute("aria-label",
          at === 0 ? "Move this pane down" : "Move this pane up");
      }
    }
  }
}

// ---- the keys in a pane's own bar -------------------------------------------
// Both of them are in every bar a row can wear one in — the diff's head, the
// explorer's, the browser's, and the three file views seated in the explorer's
// row — and each does the same one thing in all of them. Wired under a root
// rather than once over the document, because a second instance of a pane is a
// runtime copy of its markup: the copy brings its own keys, and they are wired
// here the same way the shipped ones are.
function sideWireBar(root) {
  for (const btn of root.querySelectorAll(".dock-swap")) {
    btn.addEventListener("click", sideSwap);
  }
  for (const btn of root.querySelectorAll(".dock-split")) {
    btn.addEventListener("click", () => {
      const wrap = btn.closest(".dock-split-wrap");
      showSplitMenu(wrap, !wrap.classList.contains("open"));
    });
  }
}

// Which types the split key's menu offers: the ones that are not up at all, and
// the ones that are but can be opened a second time in the free slot. A type
// with no factory (sideMakers) cannot, so it is left out rather than offered as
// a row that would do nothing.
function sideSplitTypes() {
  const out = [];
  for (const type of ["files", "browser", "diff"]) {
    const up = sideRows.some((id) => sideType(id) === type);
    if (!up || (sideMakers[type] && sideFreeId(type))) out.push(type);
  }
  return out;
}

// The wrap whose menu is open, or null. One at a time, which is what the scrim
// below belongs to.
let sideSplitAt = null;

// The header menu's recipe (showThemeMenu, 04-theme.js): open and closed are one
// class on the wrap, the rows are written at open rather than kept in step —
// the column changes under them and a menu is only ever read open — and the
// scrim takes the press that closes it.
function showSplitMenu(wrap, on) {
  if (sideSplitAt && sideSplitAt !== wrap) showSplitMenu(sideSplitAt, false);
  wrap.classList.toggle("open", on);
  $("side-split-scrim").classList.toggle("show", on);
  const btn = wrap.querySelector(".dock-split");
  btn.setAttribute("aria-expanded", on ? "true" : "false");
  sideSplitAt = on ? wrap : null;
  if (!on) return;
  const from = sideIdAt(btn);
  const menu = wrap.querySelector(".dock-split-menu");
  menu.textContent = "";
  for (const type of sideSplitTypes()) {
    const row = el("button", { type: "button", class: "view-row", role: "menuitem" },
                   el("span", {}, SIDE_LABEL[type]));
    row.addEventListener("click", () => {
      showSplitMenu(wrap, false);
      sideMenuPick(from, type);
    });
    menu.appendChild(row);
  }
}

// A row of that menu pressed. The type's own way in, with the pane the menu
// belongs to named as the row to keep: picked from a full column, this is the
// one press in the app that must not throw out the pane it was made from
// (sideClaim). A type already up gets a second instance made for it first, and a
// build that cannot make one has not offered the row.
function sideMenuPick(from, type) {
  const up = sideRows.some((id) => sideType(id) === type);
  const id = up ? sideCreate(type) : type;
  if (!id) return;
  const opts = { keep: from };
  if (type === "diff") diffSetOpen(true, id, null, opts);
  else if (type === "files") filesOpenAtCwd(id, opts);
  else openBrowser("", null, 0, id, opts);
}

// Which pane a press landed in, and the press that closes an open split menu.
// On capture, so a press the pane's own handlers act on is still seen here.
//
// The menu's scrim cannot take every press that should close it: the menu drops
// out of a bar inside a pane, and the panes are stacked above the scrim (the
// stylesheet's z-index — a scrim over them would be a scrim over the menu). So a
// press elsewhere in a pane is closed here, and a press anywhere else is the
// scrim's own, which swallows it rather than letting it through to the terminal.
document.addEventListener("pointerdown", (e) => {
  const id = sideIdAt(e.target);
  if (id) sideFocus[sideType(id)] = id;
  if (sideSplitAt && !sideSplitAt.contains(e.target)
      && e.target !== $("side-split-scrim")) {
    showSplitMenu(sideSplitAt, false);
  }
}, true);

$("side-split-scrim").addEventListener("click", () => {
  if (sideSplitAt) showSplitMenu(sideSplitAt, false);
});

// A hardware keyboard's Escape closes the menu, showThemeMenu's own rule: on
// capture and ahead of every handler that would otherwise read the press as
// leaving the pane under it.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !sideSplitAt) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  showSplitMenu(sideSplitAt, false);
}, true);

sideWireBar(document);

// Dragging the seam resizes the pane live, the rail's railResize() mirrored:
// the width moves by the pointer's travel rather than jumping to it, the
// variable does the layout for free, and the fit rides its own debounce until
// the release forces one. Anchored to the right edge, so leftward travel is
// a wider pane — hence the sum where the rail takes a difference.
(function sideResize() {
  const handle = $("side-gutter");
  let dragging = false, dragOff = 0;
  handle.addEventListener("pointerdown", (e) => {
    dragging = true;
    dragOff = sideWidth + e.clientX;
    handle.classList.add("dragging");
    handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    applySideWidth(dragOff - e.clientX);
    refit();
  });
  const finish = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("dragging");
    cfg.sideWidth = sideWidth;
    refit(0);
  };
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
})();

// The seam between the rows, the one above turned on its side. No refit
// anywhere in it: this drag divides the column and never touches the
// terminal's box, so xterm and tmux have nothing to be told.
(function sideRowResize() {
  const handle = $("side-hsplit");
  let dragging = false, dragOff = 0;
  handle.addEventListener("pointerdown", (e) => {
    dragging = true;
    dragOff = sideSplit * window.innerHeight - e.clientY;
    handle.classList.add("dragging");
    handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    applySideSplit((dragOff + e.clientY) / window.innerHeight);
  });
  const finish = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("dragging");
    cfg.sideSplit = sideSplit;
    // Whatever a pane has to redo when its row changes height, asked of the
    // panes in the column rather than known here: the diff's own list/body seam
    // is a height inside the row, and its clamp is against that row — a list
    // left as tall as a taller row allowed would push the diff past the bottom
    // of this one.
    for (const id of sideRows) {
      const inst = sideInst[id];
      if (inst && inst.onRowResize) inst.onRowResize();
    }
  };
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
})();
