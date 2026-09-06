// ============================================================
// Side pane — the one slot beside the terminal
// ============================================================
// Two panes want the terminal's right edge: the git changes (37-git-diff.js)
// and the file explorer docked rather than opened over the terminal
// (28-file-explorer.js). They are one slot, not two — a laptop has room for a
// terminal and one thing beside it, and two seams to drag would leave neither
// pane a width worth reading at. So the geometry lives here: one width, one
// remembered width, one gutter to drag it by, and whoever opens takes the slot
// off whoever had it.
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
const SIDE_MIN = { diff: 300, files: 340 };

let sideOwner = null;   // "diff", "files" or null — who holds the slot
let sideWidth = 0;      // 0 until sized — see sideClaim()

// The main pane is the window less the rail and the seam it is drawn on, read
// off the seam itself rather than recomputed from --sidebar-w: the rail's own
// geometry is the truth, and on a phone the handle is display:none and takes
// no width, which reads as "the window" without a branch.
function sideMainW() {
  const r = $("rail-resize").getBoundingClientRect();
  return window.innerWidth - (r.width ? r.right : 0);
}

// Clamped on every read, the way the rail's width is: a stored number is a
// number the window may since have grown or shrunk out from under.
function sideClampW(px) {
  const min = SIDE_MIN[sideOwner] || SIDE_MIN.diff;
  const max = Math.max(min, sideMainW() - SIDE_TERM_MIN);
  return Math.round(Math.min(max, Math.max(min, px)));
}

function applySideWidth(px) {
  sideWidth = sideClampW(px);
  document.documentElement.style.setProperty("--side-w", sideWidth + "px");
}

// Hands the slot to `who`, taking it off whoever had it — through that pane's
// own close, so it forgets itself exactly as its own cross would. The refit is
// what carries the new width to xterm and on to tmux: the terminal has just
// changed shape, and it only learns that from a fit.
function sideClaim(who) {
  if (sideOwner === who) return;
  if (sideOwner === "diff") diffSetOpen(false);
  else if (sideOwner === "files") closeDockedFiles();
  sideOwner = who;
  cfg.sidePane = who;
  // Half the main pane the first time, and whatever was dragged after that.
  applySideWidth(cfg.sideWidth || Math.round(sideMainW() / 2));
  $("screen-term").classList.add("side-open");
  refit(0);
}

// The mirror, and a no-op for a pane that no longer holds the slot — the two
// close paths (its own cross, and the other pane claiming) both land here.
function sideDrop(who) {
  if (sideOwner !== who) return;
  sideOwner = null;
  cfg.sidePane = "";
  $("screen-term").classList.remove("side-open", "side-full");
  refit(0);
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
