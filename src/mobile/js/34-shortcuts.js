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

function openShortcuts() {
  syncShortcuts();
  showSheet(true, "sheet-shortcuts");
}

$("btn-shortcuts").addEventListener("click", openShortcuts);

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

// ============================================================
// Shortcuts card in the rail
// ============================================================
// The sheet above is complete and unread: nobody opens Settings looking for a
// chord they do not know exists, and the chord they get wrong is Ctrl+C, which
// off a Mac copies a selection and interrupts when there is none. So the four
// worth knowing sit at the foot of the rail, in the keys of the platform
// actually in front of the user — the same /Mac/ test the terminal binds copy
// on, so the card cannot promise a key the terminal does not answer to. No
// copy key is among them: a drag is copied the moment it lets go, and on a
// trackpad the selection rarely survives the reach for the keyboard — any
// scroll is a mouse report to tmux, and xterm drops the selection on input.
//
// Rail-only and keyboard-only: a phone has no chords to press and no room to
// spare, and both gates are the stylesheet's (see RAIL SHORTCUTS CARD). The
// collapse is remembered because a card you have read is one you want folded
// away, and a rail is a thing you look at every day.
const RAIL_KEYS_KEY = "pockettui_rail_keys";
const railKeysMac = /Mac/.test(navigator.userAgent);

$("rk-paste").textContent = railKeysMac ? "Cmd+V" : "Ctrl+V";
$("rk-interrupt").textContent = railKeysMac
  ? "Interrupt the program"
  : "Interrupt — copies instead if text is selected";

// One writer: the chevron's direction, the body's presence and what a screen
// reader is told are the same fact.
function syncRailKeys(open) {
  $("rail-keys").classList.toggle("closed", !open);
  $("rail-keys-body").hidden = !open;
  const btn = $("btn-rail-keys");
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  btn.setAttribute("aria-label", open ? "Collapse the shortcuts" : "Expand the shortcuts");
}

// Open unless it was closed on purpose: anything else stored — absent,
// hand-edited — reads as the default, the way the key pill's pin does.
let railKeysOpen = true;
try { railKeysOpen = localStorage.getItem(RAIL_KEYS_KEY) !== "closed"; } catch (e) {}
syncRailKeys(railKeysOpen);

$("btn-rail-keys").addEventListener("click", () => {
  railKeysOpen = !railKeysOpen;
  syncRailKeys(railKeysOpen);
  try { localStorage.setItem(RAIL_KEYS_KEY, railKeysOpen ? "open" : "closed"); } catch (e) {}
});

$("btn-rail-keys-all").addEventListener("click", openShortcuts);
