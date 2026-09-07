// ============================================================
// Scrollback search
// ============================================================
// Ctrl+Shift+F, and the key pill's magnifier, which opens the same bar for the
// pointer that never learned the chord. Both belong to the laptop: the chord is
// claimed in 31-wide-layout.js beside the diff pane's, the key is shown only in
// pill mode, and pill mode is the wide layout plus a real pointer — so search
// exists exactly where there is a keyboard to press it on. A phone has neither,
// and no room for a bar over the grid: the markup below stays display:none
// there, and so does the key.
//
// The bar is absolutely positioned inside #screen-term, which is the terminal's
// own column: the rail moves that box's left edge and the side pane its right,
// so the bar can never reach over the diff or the explorer without a rule of its
// own. It docks at the top because the prompt is at the bottom — scrollback is
// what a find is looking at anyway.
//
// The find itself is tmux's, run on the computer through /api/search: this
// terminal is a tmux client, so xterm's buffer holds only what tmux last
// painted and a find here would see the visible screen and nothing else, while
// the history the user is looking for is tmux's. So the pane sits in copy mode
// for as long as matches are up — tmux draws the highlights and the attach
// socket carries them like any other repaint — and closing the bar leaves it.
let searchOpen = false;
let searchTypeTimer = null;
// Requests overlap while the field is typed in and answer out of order; only
// the newest one may write the counter.
let searchSeq = 0;
// True once this bar has put the pane into copy mode. A copy mode the user
// entered themselves in tmux is not this bar's to cancel.
let searchInMode = false;
// And which session that pane belongs to. The cancel has to name it rather
// than read the current one, because the closers that carry the bar out —
// closing the terminal, switching session — reach here after the app has
// stopped calling it current, and a cancel with no session cancels nothing:
// the pane would stay in copy mode wearing this bar's match colours.
let searchSession = "";

// Long enough that a held-down key is one find rather than one per character,
// short enough that a pause in typing answers before it is noticed. Every find
// is a fresh one from the bottom: the query changed, so where the last one
// walked the cursor to is not where this one starts.
const SEARCH_TYPE_MS = 250;

// One call, and the counter the answer carries. Every path in goes through
// here, so a stale answer is dropped in exactly one place.
async function searchPost(action) {
  if (demoMode) return;
  const q = $("search-input").value;
  const seq = ++searchSeq;
  if (action !== "cancel") {
    searchInMode = true;
    searchSession = currentSession || "";
  }
  try {
    const r = await fetch(apiURL("api/search"), {
      method: "POST", cache: "no-store",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ session: searchSession, dev: cfg.devname,
                             query: q, action: action }),
    });
    if (r.status === 401) { rejectToken(); return; }
    const d = await r.json().catch(() => null);
    if (!d || seq !== searchSeq || !searchOpen) return;
    searchShowCount(d, q);
  } catch (e) {
    // Quiet, like the diff pane's poll: an unreachable backend already nags
    // from the terminal's banner.
  }
}

// tmux counts its matches but does not number the one it is sitting on, so the
// counter says how many there are rather than which of them this is. `partial`
// is tmux saying it stopped counting at its own limit, which is what the plus
// stands for. A count is a tmux 3.2 format: an older computer sends null and
// the counter stays blank while the search itself still works.
function searchShowCount(d, q) {
  const el = $("search-count");
  if (!q) { el.textContent = ""; return; }
  if (d.present === false) { el.textContent = "No matches"; return; }
  const n = d.count;
  if (typeof n !== "number") { el.textContent = ""; return; }
  el.textContent = n + (d.partial ? "+" : "")
    + (n === 1 && !d.partial ? " match" : " matches");
}

// Leaving copy mode, which is what puts the pane back under the prompt and
// takes the highlights down — and, on the computer, unsets the match colours
// the find painted the window in. Only ever sent for a copy mode this bar
// opened: the user's own is theirs to leave.
function searchCancel() {
  $("search-count").textContent = "";
  if (!searchInMode) return;
  searchInMode = false;
  searchPost("cancel");
}

// Typing is not a request per keystroke: each one restarts the find from the
// bottom of the history, and a held-down key would run the walk once per
// character.
function searchLive() {
  clearTimeout(searchTypeTimer);
  searchTypeTimer = setTimeout(() => {
    if ($("search-input").value) searchPost("restart");
    else searchCancel();
  }, SEARCH_TYPE_MS);
}

function searchStep(back) {
  const q = $("search-input").value;
  if (!q) return;
  clearTimeout(searchTypeTimer);
  searchPost(back ? "prev" : "next");
}

function openSearch() {
  // A computer too old to have the route serves 404s to it, so the chord does
  // nothing there rather than opening a bar that can never count anything.
  if (!hasCap("search")) return;
  const input = $("search-input");
  // A second press is a return to the field, not a toggle — the same thing a
  // browser's own find does, and the query is still there to be replaced.
  if (!searchOpen) {
    searchOpen = true;
    $("search-bar").classList.add("open");
  }
  input.focus();
  input.select();
  if (input.value) searchPost("restart");
}

// The single close routine, the shape setCompose() has: every path in — Escape,
// the cross, a window that stopped being wide, a session switch — closes through
// here, so copy mode and the focus always land the same way. The query survives
// it: reopening with the last one selected is one keystroke from repeating the
// find and none from replacing it.
function closeSearch() {
  if (!searchOpen) return;
  searchOpen = false;
  clearTimeout(searchTypeTimer);
  $("search-bar").classList.remove("open");
  searchCancel();
  // Focus back to the terminal the way the image viewer and the edge-swipe
  // gesture hand it back — but only while the terminal is still the screen on
  // show, since the two lifecycle callers below reach here on the way off it.
  if (term && $("screen-term").classList.contains("active")) term.focus();
}

// A computer without the route also stops offering the chord, the way the diff
// pane's rail row goes. Called when the capability map lands (36-server-version
// .js) — before that hasCap() answers yes, which is the map's own contract for
// a server too old to send one.
function syncSearchCap() {
  const on = hasCap("search");
  for (const id of ["rk-search", "key-search"]) {
    const row = $(id);
    if (row) row.hidden = !on;
  }
  // And the pill's key, the one button this feature has. A class rather than
  // the rows' `hidden`, because the pill's display rules are ID-strength and
  // would outrank the attribute — the report key carries its own .show for
  // exactly that reason. Looked up rather than held, since buildKeybar()
  // replaces it.
  const key = $("keybar").querySelector(".k-search");
  if (key) key.classList.toggle("show", on);
}

// Escape is the bar's only while its own field has the focus. The three other
// Escape handlers on this document (05-settings.js, 09-image-viewer.js,
// 28-file-explorer.js) are capture listeners registered ahead of this one, so
// they are asked first and each stands aside here: no sheet is up (the chord
// refuses to open under one), the viewer is not shown, and the docked
// explorer's only answers a press inside its own pane. Claiming it immediately
// is what stops the same press reaching xterm, which reads keydown off its
// textarea and never consults defaultPrevented. A press with the focus back in
// the terminal is the terminal's — vim is owed its Escape — and it is also
// tmux's own way out of copy mode, which is the mode the pane is left in.
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
// invisible, with the pane still in copy mode behind it.
wideQuery.addEventListener("change", () => { if (!wideQuery.matches) closeSearch(); });
