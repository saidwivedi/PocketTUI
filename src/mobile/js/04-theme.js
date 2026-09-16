// ============================================================
// Theme
// ============================================================
function themePref() {
  return localStorage.getItem("pockettui_theme") || "auto";
}

// What the app is painted in, from whichever of the two answers is in force. A
// terminal palette is a whole skin — its own tokens and its own light or dark —
// so it wins outright; Paper hands the document back to the stylesheet's two
// :root blocks and resolves the stored preference against the OS. The pair of
// calls is boot-theme.js's, which is also where the derivation lives, so the
// first paint and every later swap can only ever agree.
function applyChrome() {
  const palette = storedTermPalette();
  if (palette) { applyPaletteChrome(palette); return; }
  clearPaletteChrome();
  const pref = themePref();
  const dark = pref === "dark" ||
    (pref === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
}

// The one route a chosen chrome theme takes: stored, then painted. The header
// button cycles through it and the Appearance tab names one outright, so the
// two can never leave the stored value and what is on screen disagreeing.
function setThemePref(next) {
  localStorage.setItem("pockettui_theme", next);
  applyChrome();
  applyTermTheme();
}

$("btn-theme").addEventListener("click", () => {
  const order = ["auto", "light", "dark"];
  const next = order[(order.indexOf(themePref()) + 1) % order.length];
  setThemePref(next);
  // The preference is still recorded under a palette — it is what Paper will
  // come back to — but nothing on screen moves, so the toast says whose
  // setting was just changed rather than leaving the press looking broken.
  toast(storedTermPalette() ? "Theme: " + next + " — Paper only"
                            : "Theme: " + next);
});
