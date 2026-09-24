// ============================================================
// Theme
// ============================================================
// The stored choice, or with none, light while unpaired and the OS once
// paired: the rule lives with the first paint (themePrefInForce,
// boot-theme.js), so the two can never resolve differently.
function themePref() {
  return themePrefInForce();
}

// The moment a first run pairs: a device that never chose keeps the light it
// was onboarded in rather than flipping to the OS's answer as the rule above
// hands over, so "light" is stored as its choice — the key the header menu
// writes, and changed the same way. A device with a choice keeps it.
function keepLightAfterPairing() {
  try {
    if (localStorage.getItem("pockettui_theme")) return;
    localStorage.setItem("pockettui_theme", "light");
  } catch (e) {}
}

// What the app is painted in. The preference picks one half of the chosen pair
// — auto asks the OS, light and dark pin one — and that half is either Paper,
// which hands the document back to the stylesheet's two :root blocks, or a
// palette, which writes its own derived tokens over them. Both the reading and
// the paint are boot-theme.js's, which is also where the derivation lives, so
// the first frame and every later swap can only ever agree.
function applyChrome() {
  const dark = prefIsDark(themePref());
  applyEntryChrome(termPair()[dark ? "dark" : "light"], dark);
  // The framed guide paints itself in whatever this just resolved to.
  featuresSyncTheme();
}

// The one route a chosen theme takes: stored, then painted. The header menu and
// the Appearance tab both name one outright, so the two can never leave the
// stored value and what is on screen disagreeing.
function setThemePref(next) {
  localStorage.setItem("pockettui_theme", next);
  applyChrome();
  applyTermTheme();
  // Which half of the pair is live has just changed, so the tab's two ticks say
  // something else now — and this is the other way into the same setting.
  syncAppearance();
}

// Auto hands the choice to the OS, and the OS can change its mind while the app
// is open: the other half of the pair becomes the live one, so the chrome and
// the terminal are repainted then rather than at the next load.
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (themePref() !== "auto") return;
  applyChrome();
  applyTermTheme();
  syncAppearance();
});

// ---- the header's menu ------------------------------------------------------
// Three named choices rather than a cycle. With a light palette chosen and a
// dark one, a press has to say which of the two it is switching to, and a cycle
// that shows neither name cannot. Button and panel are the session list's
// profile switcher (40-profiles.js) — the app has one dropdown, drawn wherever
// one is needed — down to the scrim that takes the tap that closes it.
//
// Written on open rather than kept in step: either half can be re-chosen in
// Settings while this is closed, and the menu is only ever read open.
function showThemeMenu(on) {
  $("theme-wrap").classList.toggle("open", on);
  $("theme-scrim").classList.toggle("show", on);
  $("btn-theme").setAttribute("aria-expanded", on ? "true" : "false");
  if (!on) return;
  const pair = termPair(), pref = themePref();
  const menu = $("theme-menu");
  menu.textContent = "";
  for (const r of [{ pref: "auto", label: "Auto" },
                   { pref: "light", label: "Light · " + pair.light.name },
                   { pref: "dark", label: "Dark · " + pair.dark.name }]) {
    const on_ = r.pref === pref;
    const row = el("button", {
      type: "button", class: "view-row" + (on_ ? " on" : ""),
      role: "menuitemradio", "aria-checked": on_ ? "true" : "false",
    }, el("span", { class: "view-check" }, "✓"), el("span", {}, r.label));
    row.addEventListener("click", () => {
      showThemeMenu(false);
      setThemePref(r.pref);
    });
    menu.appendChild(row);
  }
  // The rest of the choosing is the Appearance tab's: a footnote under the
  // last row's hairline, in the umber of the rail card's "All shortcuts", not a
  // fourth choice. The rows draw that line themselves once one is not last.
  const more = el("button", { type: "button", class: "menu-foot", role: "menuitem" },
                  el("span", {}, "More in Settings"), svgIcon("i-back"));
  more.addEventListener("click", () => {
    showThemeMenu(false);
    openSettings(false, "appearance");
  });
  menu.appendChild(more);
}

$("btn-theme").addEventListener("click", () => {
  showThemeMenu(!$("theme-wrap").classList.contains("open"));
});
$("theme-scrim").addEventListener("click", () => showThemeMenu(false));

// A hardware keyboard's Escape closes the menu, the way it closes the sheet.
// On capture and ahead of the sheet's own handler, which is the next listener
// on this same document: nothing else can be open over this menu, and the guard
// leaves the press alone whenever it is not.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !$("theme-wrap").classList.contains("open")) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  showThemeMenu(false);
}, true);
