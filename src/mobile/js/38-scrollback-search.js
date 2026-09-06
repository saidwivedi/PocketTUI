// ============================================================
// Scrollback search
// ============================================================
// Ctrl+Shift+F, and nothing else: no key, no setting, no button anywhere. The
// chord is claimed in 31-wide-layout.js beside the diff pane's, so search only
// exists where there is a keyboard to press it on — a phone has no chord and no
// room for a bar over the grid, and the markup below stays display:none there.
//
// The bar is absolutely positioned inside #screen-term, which is the terminal's
// own column: the rail moves that box's left edge and the side pane its right,
// so the bar can never reach over the diff or the explorer without a rule of its
// own. It docks at the top because the prompt is at the bottom — scrollback is
// what a find is looking at anyway.
//
// searchAddon (07-terminal.js) is null when the addon failed to load or the
// terminal has not been built yet; every entry point below is a no-op in that
// case, the same defensive shape useWebgl()/useSearch() have.
let searchOpen = false;
let searchTypeTimer = null;

// The umber accent already carries the app's light/dark themes (TERM_THEME_*
// in 07-terminal.js); reading it live rather than hardcoding a second copy of
// the ramp keeps the highlight colour a single source of truth. Read at every
// find, so a theme toggled while the bar sits open is honoured by the next one.
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

// Plain text, either case — a terminal find is looking for a path or a word it
// just saw scroll past, not writing a pattern. The decorations key is also what
// turns the addon's result events on, which is where the counter comes from.
function searchOptions() {
  return { caseSensitive: false, regex: false, decorations: searchDecorations() };
}

// The addon's own count, fired on every find and again whenever output or a
// resize moves the matches (07-terminal.js hands it here). resultIndex is -1
// while nothing is the active match — after a clear, or when the last active
// one scrolled out of the buffer.
function searchResults(r) {
  if (!searchOpen) return;
  const count = r ? r.resultCount : 0;
  const idx = r ? r.resultIndex : -1;
  if (!$("search-input").value) $("search-count").textContent = "";
  else if (!count) $("search-count").textContent = "No matches";
  else $("search-count").textContent = idx >= 0 ? (idx + 1) + " of " + count
    : count + (count === 1 ? " match" : " matches");
}

function searchClear() {
  $("search-count").textContent = "";
  if (searchAddon) { try { searchAddon.clearDecorations(); } catch (e) {} }
}

// One find, always upwards for the first one: the prompt is at the bottom, so
// the match the user means is the nearest one above it. `incremental` is what
// keeps a growing query from walking that match further up the buffer on every
// keystroke — the anchor stays where the search started and only the highlight
// grows, which is what a find field is expected to do while it is typed in.
function searchRun() {
  if (!searchAddon) return;
  const q = $("search-input").value;
  if (!q) { searchClear(); return; }
  const opts = searchOptions();
  opts.incremental = true;
  try { searchAddon.findPrevious(q, opts); } catch (e) {}
}

// Typing is not a keypress-per-find: highlighting all matches walks the whole
// buffer, so a held-down key would run it once per character.
function searchLive() {
  clearTimeout(searchTypeTimer);
  searchTypeTimer = setTimeout(searchRun, 120);
}

function searchStep(back) {
  if (!searchAddon) return;
  const q = $("search-input").value;
  if (!q) return;
  clearTimeout(searchTypeTimer);
  const opts = searchOptions();
  try { back ? searchAddon.findPrevious(q, opts) : searchAddon.findNext(q, opts); } catch (e) {}
}

function openSearch() {
  if (!searchAddon) return;
  const input = $("search-input");
  // A second press is a return to the field, not a toggle — the same thing a
  // browser's own find does, and the query is still there to be replaced.
  if (!searchOpen) {
    searchOpen = true;
    $("search-bar").classList.add("open");
  }
  input.focus();
  input.select();
  if (input.value) searchRun();
}

// The single close routine, the shape setCompose() has: every path in — Escape,
// the cross, a window that stopped being wide, a session switch — closes through
// here, so the highlights and the focus always land the same way. The query
// survives it: reopening with the last one selected is one keystroke from
// repeating the find and none from replacing it.
function closeSearch() {
  if (!searchOpen) return;
  searchOpen = false;
  clearTimeout(searchTypeTimer);
  $("search-bar").classList.remove("open");
  searchClear();
  // Focus back to the terminal the way the image viewer and the edge-swipe
  // gesture hand it back — but only while the terminal is still the screen on
  // show, since the two lifecycle callers below reach here on the way off it.
  if (term && $("screen-term").classList.contains("active")) term.focus();
}

// Escape is the bar's only while its own field has the focus. The three other
// Escape handlers on this document (05-settings.js, 09-image-viewer.js,
// 28-file-explorer.js) are capture listeners registered ahead of this one, so
// they are asked first and each stands aside here: no sheet is up (the chord
// refuses to open under one), the viewer is not shown, and the docked
// explorer's only answers a press inside its own pane. Claiming it immediately
// is what stops the same press reaching xterm, which reads keydown off its
// textarea and never consults defaultPrevented. A press with the focus back in
// the terminal is the terminal's — vim is owed its Escape — and leaves the bar
// and its highlights up, which is what makes a highlighted path clickable.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !searchOpen) return;
  if (e.target !== $("search-input")) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  closeSearch();
}, true);

(function bindSearchBar() {
  const input = $("search-input");
  input.addEventListener("input", searchLive);
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    searchStep(!e.shiftKey);
  });
  // The chevrons must not take the focus off the field — the next Enter belongs
  // there. Same preventDefault the key pill's pin uses for the terminal.
  for (const [id, back] of [["search-prev", true], ["search-next", false]]) {
    $(id).addEventListener("mousedown", e => e.preventDefault());
    $(id).addEventListener("click", () => searchStep(back));
  }
  $("search-close").addEventListener("click", closeSearch);
})();

// Below the breakpoint the bar's rules stop applying and it would sit open and
// invisible, with the buffer still highlighted behind it.
wideQuery.addEventListener("change", () => { if (!wideQuery.matches) closeSearch(); });
