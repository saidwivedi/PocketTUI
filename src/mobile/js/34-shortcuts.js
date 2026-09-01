// ============================================================
// Keyboard shortcuts sheet
// ============================================================
// A sheet of its own rather than one more block in Settings, which is already a
// long flat scroll, and reachable only where there is a keyboard to use it with
// — the row that opens it is gated in the stylesheet, on the same test the
// editor's Vim button wears.
//
// It carries the two text sizes because a laptop has no other way to reach
// them: the terminal's size is a pinch gesture, which is touch-only, and the
// rail's has never had a control at all. Both apply on the press rather than on
// a save — the sheet floats over the rail and the terminal, so the change is
// visible while it is being made, which is the only way to choose a size.

// One writer per stepper: the number on its face and whether either end button
// is still pressable are the same fact, so nothing sets one without the other.
function syncStepper(name, value, min, max) {
  $(name + "-value").textContent = value + "px";
  $("btn-" + name + "-down").disabled = value <= min;
  $("btn-" + name + "-up").disabled = value >= max;
}

// The live terminal's size is the truth while one exists; before that — the
// sheet is reachable from the session list — the stored size is what the next
// terminal will open at, which is the same number the user is aiming at.
function shortcutsFontSize() {
  return term ? term.options.fontSize : storedFontSize();
}

function syncShortcuts() {
  syncStepper("termfont", shortcutsFontSize(), FONT_MIN, FONT_MAX);
  syncStepper("railfont", railFont, RAIL_FONT_MIN, RAIL_FONT_MAX);
}

$("btn-shortcuts").addEventListener("click", () => {
  syncShortcuts();
  showSheet(true, "sheet-shortcuts");
});

// A press moves the size by one pixel through the same apply the pinch settles
// through, then reads back what actually landed — the clamp lives in there, so
// a press at the end of the range is a no-op the buttons already showed.
for (const by of [-1, 1]) {
  $("btn-termfont-" + (by < 0 ? "down" : "up")).addEventListener("click", () => {
    applyFontSize(shortcutsFontSize() + by);
    syncShortcuts();
  });
  $("btn-railfont-" + (by < 0 ? "down" : "up")).addEventListener("click", () => {
    applyRailFont(railFont + by);
    syncShortcuts();
  });
}
