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
