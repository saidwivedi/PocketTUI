// ============================================================
// Wide layout — the session list as a persistent left rail
// ============================================================
// On viewports with room for two panes, the stylesheet's WIDE LAYOUT media
// query keeps the session list on screen as a fixed rail and seats the main
// screens beside it; the screen classes keep governing only the main pane.
// The script half stays deliberately small: one live query for the few paths
// that behave differently beside a rail, a refit when a resize crosses the
// breakpoint, and a refresh loop for a list the phone keeps fresh by
// re-showing it — the rail is never re-shown, so nothing else would ever
// repaint its rows.

// Mirrors the stylesheet's WIDE LAYOUT media query; change both together.
const wideQuery = window.matchMedia("(min-width: 768px) and (min-height: 500px)");
function isWideLayout() { return wideQuery.matches; }

// The rail's width is a preference like the terminal font size: dragged, not
// typed, clamped again on read so a stale or hand-edited value can never
// wedge the rail, and restored at boot. Setting the variable is inert on the
// phone — nothing outside the wide media query reads it.
const RAIL_MIN = 240, RAIL_MAX = 480, RAIL_DEFAULT = 320;
function storedRailW() {
  try {
    const v = parseInt(localStorage.getItem("pockettui_sidebar_w"), 10);
    if (Number.isFinite(v)) return Math.min(RAIL_MAX, Math.max(RAIL_MIN, v));
  } catch (e) {}
  return RAIL_DEFAULT;
}
let railW = storedRailW();
document.documentElement.style.setProperty("--sidebar-w", railW + "px");

// The rail's rows carry their own type scale, and on a laptop nothing else can
// reach it — the pinch that sizes the terminal is a touch gesture. Stored,
// clamped on read and set as a variable exactly like the width above, and the
// number is the row title's size in px: the meta line beneath it derives from
// the same variable in the stylesheet, so the two keep the proportion the rail
// was drawn with however far the scale is taken. Inert on the phone, where the
// rail does not exist and nothing outside the wide media query reads it.
const RAIL_FONT_MIN = 11, RAIL_FONT_MAX = 20, RAIL_FONT_DEFAULT = 14;
function storedRailFont() {
  try {
    const v = parseInt(localStorage.getItem("pockettui_sidebar_font"), 10);
    if (Number.isFinite(v)) return Math.min(RAIL_FONT_MAX, Math.max(RAIL_FONT_MIN, v));
  } catch (e) {}
  return RAIL_FONT_DEFAULT;
}
let railFont = storedRailFont();
document.documentElement.style.setProperty("--rail-title", railFont + "px");

// The one way a chosen rail size lands, the way applyFontSize() is for the
// terminal: clamped, applied live, remembered. Returns what was applied.
function applyRailFont(px) {
  railFont = Math.min(RAIL_FONT_MAX, Math.max(RAIL_FONT_MIN, Math.round(px)));
  document.documentElement.style.setProperty("--rail-title", railFont + "px");
  try { localStorage.setItem("pockettui_sidebar_font", String(railFont)); } catch (e) {}
  return railFont;
}

// Dragging the rail's edge resizes it live: every pane border derives from
// the variable, so the panes track the pointer for free, while the grid
// refit rides its own debounce — landing on pauses and once, forced, at
// release, the way the pinch gesture settles its font size. The handle is a
// fixed strip over the rail's border (see #rail-resize), so its own left
// edge follows the variable too. Pointer events cover mouse and touch alike.
(function railResize() {
  const handle = $("rail-resize");
  let dragging = false, dragOff = 0;
  handle.addEventListener("pointerdown", (e) => {
    dragging = true;
    // Wherever on the gutter the grab landed, the width must not jump to the
    // pointer — the drag moves the width by the pointer's travel instead.
    dragOff = railW - e.clientX;
    handle.classList.add("dragging");
    handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    railW = Math.min(RAIL_MAX, Math.max(RAIL_MIN, Math.round(e.clientX + dragOff)));
    document.documentElement.style.setProperty("--sidebar-w", railW + "px");
    refit();
  });
  const finish = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("dragging");
    try { localStorage.setItem("pockettui_sidebar_w", String(railW)); } catch (e) {}
    // Forced now: the debounce above may have swallowed the last move.
    refit(0);
  };
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
})();

// Crossing the breakpoint reshapes the terminal's pane — the rail claims or
// returns its width — so the grid refits here as well as via the window's own
// resize listener. Entering wide also means the list just appeared after
// arbitrarily long away, so it reloads once, the way a re-shown list would.
wideQuery.addEventListener("change", () => {
  refit(0);
  markSelectedSession();
  if (wideQuery.matches && !demoMode && !needsSetup()) loadSessions();
});

// The state badges ("needs input" / "running") only move when /api/sessions
// is asked again. On the phone that happens every time the list comes back;
// beside the rail the user can sit in a terminal for an hour with the list in
// view, so it polls — modestly, foreground only, and quiet so an unreachable
// backend nags from the terminal's own banner rather than a toast per tick.
const WIDE_REFRESH_MS = 15000;
setInterval(() => {
  if (!isWideLayout() || document.hidden || demoMode || needsSetup()) return;
  loadSessions(false, true);
}, WIDE_REFRESH_MS);

// ============================================================
// Key pill — the key bar, where a hardware keyboard is
// ============================================================
// Beside a real keyboard most of the bar is redundant, so the stylesheet's KEY
// PILL query floats what is left over the terminal's top-right corner and gives
// the rows back (see it for the reasoning and for the surviving keys). The DOM
// is the same #keybar throughout — no second bar, no second mic — so all this
// half owns is whether the pill is on screen: a pin the user sets once, and,
// while it is off, a corner that answers the pointer and clears again after it.

// Mirrors the stylesheet's KEY PILL media query; change both together. Wider
// than isWideLayout(): a touch-only tablet in landscape is two-pane but has no
// hardware keys to make the bar redundant, so it keeps the bar.
const pillQuery = window.matchMedia(
  "(min-width: 768px) and (min-height: 500px) and (hover: hover) and (pointer: fine)");

// Pinned is a preference like the rail's width, and pinned is the default:
// arriving to an empty corner would leave the folder and the microphone with
// nowhere on screen that says they exist. Anything else stored — absent,
// hand-edited, half-written — reads as the default rather than as "off".
const PILL_PIN_KEY = "pockettui_keypill_pinned";
// Short on purpose: the pointer has already left, and an unpinned pill is a
// thing the user asked to be out of the way. Long enough to survive a pointer
// clipping the corner on its way somewhere else, no longer.
const PILL_HIDE_MS = 800;
// How far outside the pill still counts as reaching for it. Generous on
// purpose: the target is a corner, not a control, and the pointer is aiming at
// something it cannot currently see.
const PILL_REVEAL_PAD = 90;

function storedPillPinned() {
  try {
    const v = localStorage.getItem(PILL_PIN_KEY);
    if (v === "0") return false;
    if (v === "1") return true;
  } catch (e) {}
  return true;
}

let pillPinned = storedPillPinned();
let pillRevealed = false;      // transient: the pointer came looking for it
let pillHover = false;
let pillHideTimer = null;

// The one write path — every change of pin or reveal lands here, so the class,
// the pin's face and what a screen reader is told can never disagree.
function syncPill() {
  const pin = $("keybar-pin");
  pin.setAttribute("aria-pressed", pillPinned ? "true" : "false");
  pin.setAttribute("aria-label", pillPinned ? "Unpin the key pill" : "Pin the key pill");
  $("screen-term").classList.toggle(
    "pill-hidden", pillQuery.matches && !pillPinned && !pillRevealed);
}

// Reasons the pill may not go away even though its idle time is up. The mic is
// the sharp one: while a take is running that key IS the stop control, and
// fading it out would leave the recording with no way to end it. An open
// compose strip is the same errand a moment earlier. A pointer resting on the
// pill is the plain one — a control must not walk away from the hand on it.
function pillHeld() {
  return pillHover || composeOpen || recStarting || recording() || recBusy;
}

function pillScheduleHide() {
  clearTimeout(pillHideTimer);
  pillHideTimer = setTimeout(() => {
    pillHideTimer = null;
    if (pillHeld()) { pillScheduleHide(); return; }
    pillRevealed = false;
    syncPill();
  }, PILL_HIDE_MS);
}

function pillReveal() {
  clearTimeout(pillHideTimer);
  pillHideTimer = null;
  if (pillRevealed) return;
  pillRevealed = true;
  syncPill();
}

// The reveal zone is the pill's own box grown by a margin, measured live rather
// than declared here — the pill's corner moves with the safe area, the rail's
// width and whatever chrome is pushing it down, and a second copy of those
// offsets in script would drift from the stylesheet's. Measured off the hidden
// pill too: it is faded, not removed, so it still has a box to grow. A
// document-level move rather than a hit-catching element over the grid, which
// would swallow every click on terminal text.
function pillPointer(e) {
  if (pillPinned) return;
  const r = $("keybar").getBoundingClientRect();
  const near = e.clientX >= r.left - PILL_REVEAL_PAD && e.clientX <= r.right + PILL_REVEAL_PAD
            && e.clientY >= r.top - PILL_REVEAL_PAD && e.clientY <= r.bottom + PILL_REVEAL_PAD;
  // Inside the zone keeps it up for as long as the pointer stays. Outside
  // starts the clock, and a pointer that leaves the window simply stops
  // sending moves after its last one out here.
  if (near) pillReveal(); else pillScheduleHide();
}

// The moves land on the document, but crossing into the pill or its pin is
// worth knowing exactly: a still pointer sends nothing, and the pill must not
// fade out from under one that has stopped on it. buildKeybar() empties the bar
// rather than replacing it, so a listener bound here outlives every rebuild.
function pillEntered() { pillHover = true; pillReveal(); }
function pillLeft() { pillHover = false; if (!pillPinned) pillScheduleHide(); }

// Bound only where the pill exists: on a phone these are listeners that could
// only ever run to decline.
function pillListeners(on) {
  const parts = [$("keybar"), $("keybar-pin")];
  if (on) {
    document.addEventListener("mousemove", pillPointer, { passive: true });
    for (const p of parts) {
      p.addEventListener("mouseenter", pillEntered);
      p.addEventListener("mouseleave", pillLeft);
    }
    return;
  }
  document.removeEventListener("mousemove", pillPointer);
  pillHover = false;
  for (const p of parts) {
    p.removeEventListener("mouseenter", pillEntered);
    p.removeEventListener("mouseleave", pillLeft);
  }
}

(function bindPillPin() {
  const pin = $("keybar-pin");
  // Focus stays in the terminal, same as every key in the bar below it.
  pin.addEventListener("pointerdown", e => e.preventDefault());
  pin.addEventListener("mousedown", e => e.preventDefault());
  pin.addEventListener("click", () => {
    pillPinned = !pillPinned;
    try { localStorage.setItem(PILL_PIN_KEY, pillPinned ? "1" : "0"); } catch (e) {}
    // Unpinning must not pull the pill out from under the pointer that just
    // clicked it — nothing would be left to click to put it back. It stays up
    // and takes the same idle timeout a reveal gets.
    pillRevealed = !pillPinned;
    syncPill();
    if (!pillPinned) pillScheduleHide();
  });
})();

// Crossing this breakpoint is the bar leaving or rejoining the flex column, so
// the terminal gains or loses the rows it was docked in — xterm only learns
// that from a fit. The reveal is dropped on the way through either direction:
// it is an answer to a pointer that was in a corner this layout no longer has.
pillQuery.addEventListener("change", () => {
  pillListeners(pillQuery.matches);
  clearTimeout(pillHideTimer);
  pillHideTimer = null;
  pillRevealed = false;
  syncPill();
  refit(0);
});

pillListeners(pillQuery.matches);
syncPill();

// ============================================================
// Rail shortcuts — Control and Shift and a digit
// ============================================================
// Only beside the rail: the number means a row on screen, and the phone never
// shows the list and a terminal together. Three keys rather than two, because
// a plain Command or Control digit goes to the browser's tab strip, which takes
// it before the page sees it and does not hand it back for a preventDefault,
// and because Ctrl+Alt+digit is what GNOME, KDE and Xfce switch workspaces
// with — the window manager wins that one long before a keydown reaches here.
// Shift is what makes the rest safe: the control codes the shell is owed come
// out of xterm's no-shift branch — Ctrl+3 is Escape, Ctrl+8 is Delete — so a
// shifted digit takes nothing away from the terminal. The listener sits on the
// document rather than on the rail, so one listener covers a focused terminal
// and a focused rail alike, and on capture, because xterm does not let every
// chord it has no use for go past: Ctrl+Shift+2 is Ctrl+@ on a US layout, which
// it reads as NUL, and a key it claims it also cancels — preventDefault and
// stopPropagation together — at its own textarea, so a bubble listener here was
// never told that key had been pressed at all. Claiming a chord stops it
// immediately for the reason the sheet's Escape gives: stopPropagation only
// keeps an event from the next node, not from the listeners beside it on this
// one. A chord pointing at a row that is not there is left alone rather than
// swallowed — nothing prevented and nothing stopped — so capture costs the
// terminal and the browser none of the keys this does not use.
document.addEventListener("keydown", (e) => {
  if (!e.ctrlKey || !e.shiftKey || e.altKey || e.metaKey) return;
  if (!isWideLayout() || $("sheet-scrim").classList.contains("show")) return;
  // The digit row's code is fixed where its key is not: with Shift down a US
  // layout puts !@#$%^&*( in e.key, and other layouts put their own symbols
  // there. Where a remapped or soft keyboard sends no code at all, the old
  // keyCode still carries the digit — deprecated, but implemented everywhere,
  // and moved by neither Shift nor the layout.
  const m = /^Digit([1-9])$/.exec(e.code || "");
  const n = m ? +m[1] : (e.keyCode >= 49 && e.keyCode <= 57 ? e.keyCode - 48 : 0);
  if (!n) return;
  // Rows only — the demo card carries no data-name.
  const row = $("list").querySelectorAll(".item[data-name]")[n - 1];
  if (!row) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  if (row.dataset.name !== currentSession) openTerminal(row.dataset.name);
}, true);
