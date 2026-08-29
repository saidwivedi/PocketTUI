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
