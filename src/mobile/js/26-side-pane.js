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

// Which elements each pane type puts in its row. The explorer's row is four of
// them because a file opened from the docked pane opens inside the pane — the
// editor, the reader and the viewer seat themselves over its listing
// (28-file-explorer.js) and belong to whichever row the listing is in.
const SIDE_ELS = {
  diff: ["diff-pane"],
  files: ["screen-files", "screen-editor", "screen-reader", "viewer"],
  browser: ["screen-browser"],
};

let sideRows = [];      // "diff", "files", "browser" — top row first, at most two
let sideWidth = 0;      // 0 until sized — see sideClaim()
let sideSplit = 0;      // the top row's share of the window, 0 until sized
let sideFull = null;    // which row is filling the main area, or null for neither

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
  for (const t of sideRows) min = Math.max(min, SIDE_MIN[t] || 0);
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

// Clearing an expand through the module that owns it: the flag, the key it is
// remembered under and the icons on its own bar are that module's, and this
// knows only which pane is which. The diff has no expand of its own.
function sideSetExpanded(who, on) {
  if (who === "files") filesSetExpanded(on);
  else if (who === "browser") browserSetExpanded(on);
}

// Puts `who` in the column: a second row under whatever is there, or the bottom
// row taken off whoever had it — through that pane's own close, so it forgets
// itself exactly as its own cross would. False is that close saying no (the
// docked editor asking about unsaved work), and the caller has to stand down
// with it rather than open half a pane. The refit is what carries the new width
// to xterm and on to tmux: the terminal has just changed shape, and it only
// learns that from a fit.
function sideClaim(who) {
  if (sideRows.includes(who)) return true;
  if (sideRows.length >= 2) {
    const out = sideRows[1];
    if (out === "diff") diffSetOpen(false);
    else if (out === "files") closeDockedFiles();
    else if (out === "browser") closeDockedBrowser();
    if (sideRows.length >= 2) return false;
  }
  sideRows.push(who);
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
function sideDrop(who) {
  const at = sideRows.indexOf(who);
  if (at < 0) return;
  sideRows.splice(at, 1);
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
// only puts the rows back when that same session is opened again. `owner` is
// the top row said again for the record's older shape, which is what the shell
// still reads back at boot; the browser's page and tabs are the browser's own
// half of the record (browserRemember, 42-browser.js) and only carry over while
// it is still one of the rows.
function sideRemember() {
  if (!sideRows.length) return;
  const prev = cfg.sidePane;
  const rec = { owner: sideRows[0], rows: sideRows.slice(),
                session: currentSession || "" };
  if (sideRows.includes("browser") && prev) {
    rec.url = prev.url;
    rec.tabs = prev.tabs;
    rec.tab = prev.tab;
  }
  cfg.sidePane = rec;
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
function sideSetFull(who, on) {
  if (on) {
    if (sideFull && sideFull !== who) sideSetExpanded(sideFull, false);
    sideFull = who;
  } else if (sideFull === who) sideFull = null;
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
  for (const type of Object.keys(SIDE_ELS)) {
    const at = two ? sideRows.indexOf(type) : -1;
    // Expanded, the row under the full pane is not a shorter row but none at
    // all: there is nothing of it left to see behind a pane covering the column.
    const cls = at < 0 ? ""
      : sideFull ? (sideFull === type ? "" : "side-hidden")
      : at === 0 ? "side-top" : "side-bot";
    for (const id of SIDE_ELS[type]) {
      const el = $(id);
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

// The key is in every bar a row can wear one in — the diff's head, the
// explorer's, the browser's, and the three file views seated in the explorer's
// row — and it does the same one thing in all of them.
for (const btn of document.querySelectorAll(".dock-swap")) {
  btn.addEventListener("click", sideSwap);
}

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
    // The diff's own list/body seam is a height inside a row that has just
    // changed height, and its clamp is against that row: a list left as tall as
    // a taller row allowed would push the diff past the bottom of this one.
    if (sideRows.includes("diff") && diffListH) applyDiffListH(diffListH);
  };
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
})();
