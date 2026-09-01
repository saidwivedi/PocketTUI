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
const PILL_HIDE_MS = 2000;
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
// the grip's face and what a screen reader is told can never disagree.
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
  // Inside the zone keeps it up for as long as the pointer stays — the grip
  // sits inside the grown box too, so reaching for the pin never races the
  // fade. Outside starts the clock, and a pointer that leaves the window
  // simply stops sending moves after its last one out here.
  if (near) pillReveal(); else pillScheduleHide();
}

// The moves land on the document, but crossing into the pill or the grip is
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
