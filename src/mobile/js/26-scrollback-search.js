// ============================================================
// Scrollback search
// ============================================================
// Opened by the key bar's magnifier key. The overlay docks to the top of the
// terminal screen — the key bar and compose strip already own the bottom edge,
// so search takes the only edge left rather than covering either of them.
//
// searchAddon (07-terminal.js) is null when the addon failed to load or the
// terminal has not been built yet; every entry point below is a no-op in that
// case, same defensive shape as useWebgl()/useSearch().
let searchOpen = false;

// The umber accent already carries the app's light/dark themes (TERM_THEME_*
// in 07-terminal.js); reading it live rather than hardcoding a second copy of
// the ramp keeps the highlight color a single source of truth. Decorations are
// re-read each open so a theme toggle while search was last used is honoured
// on the next open rather than baked in at load.
function searchDecorations() {
  const accent = getComputedStyle(document.documentElement)
    .getPropertyValue("--umber").trim() || "#b85c38";
  return {
    matchBackground: "rgba(255, 224, 120, 0.55)",
    matchBorder: accent,
    matchOverviewRuler: accent,
    activeMatchBackground: "rgba(255, 180, 60, 0.75)",
    activeMatchBorder: accent,
    activeMatchColorOverviewRuler: accent,
  };
}

// @xterm/addon-search 0.15.0, the version vendored here, supports decorations
// (highlight-all plus a distinct active-match style); an unsupported build
// would just ignore the extra option key rather than throw, so no feature
// check is needed against a future vendor bump.
function searchOptions() {
  return { decorations: searchDecorations() };
}

function openSearch() {
  if (searchOpen) { $("search-input").focus(); return; }
  searchOpen = true;
  $("search-bar").classList.add("open");
  $("search-input").focus();
}

// The single close routine, same shape as setCompose(): every path in closes
// through here so highlights and focus always land the same way regardless of
// what triggered the close.
function closeSearch() {
  if (!searchOpen) return;
  searchOpen = false;
  $("search-bar").classList.remove("open");
  $("search-input").value = "";
  if (searchAddon) { try { searchAddon.clearDecorations(); } catch (e) {} }
  // Hands focus back to the terminal exactly the way the image viewer and the
  // edge-swipe gesture already do (09-image-viewer.js, 19-voice-capture.js) —
  // the soft keyboard search raised goes away with the blur this causes, same
  // as the compose strip's own close.
  if (term) term.focus();
}

function searchNext() {
  if (!searchAddon) return;
  const q = $("search-input").value;
  if (!q) return;
  try { searchAddon.findNext(q, searchOptions()); } catch (e) {}
}

function searchPrev() {
  if (!searchAddon) return;
  const q = $("search-input").value;
  if (!q) return;
  try { searchAddon.findPrevious(q, searchOptions()); } catch (e) {}
}

(function bindSearchBar() {
  const input = $("search-input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); searchNext(); return; }
    if (e.key === "Escape") { e.preventDefault(); closeSearch(); }
  });
  $("search-prev").addEventListener("click", searchPrev);
  $("search-next").addEventListener("click", searchNext);
  $("search-close").addEventListener("click", closeSearch);
})();
