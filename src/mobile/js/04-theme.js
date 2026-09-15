// ============================================================
// Theme
// ============================================================
function themePref() {
  return localStorage.getItem("pockettui_theme") || "auto";
}

// The one route a chosen chrome theme takes: stored, resolved against the OS
// preference, painted. The header button cycles through it and the Appearance
// tab names one outright, so the two can never leave the stored value and what
// is on screen disagreeing.
function setThemePref(next) {
  localStorage.setItem("pockettui_theme", next);
  const dark = next === "dark" ||
    (next === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  applyTermTheme();
}

$("btn-theme").addEventListener("click", () => {
  const order = ["auto", "light", "dark"];
  const next = order[(order.indexOf(themePref()) + 1) % order.length];
  setThemePref(next);
  toast("Theme: " + next);
});

