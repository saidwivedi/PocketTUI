// ============================================================
// Theme
// ============================================================
$("btn-theme").addEventListener("click", () => {
  const order = ["auto", "light", "dark"];
  const cur = localStorage.getItem("pockettui_theme") || "auto";
  const next = order[(order.indexOf(cur) + 1) % order.length];
  localStorage.setItem("pockettui_theme", next);
  const dark = next === "dark" ||
    (next === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  if (term) term.options.theme = currentTermTheme();
  syncChrome();
  toast("Theme: " + next);
});

