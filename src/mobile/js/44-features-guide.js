// ============================================================
// Features guide
// ============================================================
// "What PocketTUI can do": the hosted page at pockettui.com/features/, which
// render_features.py builds from src/features/catalog.json on every deploy. It
// is framed rather than bundled, so the guide describes the newest release
// whatever build this shell is, and the shell carries none of its 150 records
// or its screenshots. Shown over the whole app, on both layouts: the page has a
// layout of its own for each width.
//
// Two ways in, the session list's row and one in Settings > About, and one
// hint: the first time a device ever gets a session list back, a toast names
// the guide. The toast helper takes no action, so the toast only points at the
// row; it does not open anything itself.
//
// The way out is the page's own cross, which posts a message to this window
// (the page shows the cross only when it is framed), or Escape here. A frame
// that holds focus keeps its keys to itself, so the page answers Escape too, by
// posting the same message.

const FEATURES_HINT_SEEN_KEY = "pockettui_features_hint_seen";
const FEATURES_HINT_MS = 6000;

// Served from pockettui.com, the shell asks its own origin, so a preview
// deploy frames its own guide. Anywhere else (the computer's own address,
// localhost) the guide is only on the public site.
function featuresURL() {
  const own = location.hostname === "pockettui.com";
  return (own ? location.origin : "https://pockettui.com") + "/features/?embed=1";
}

// The element focus goes back to on close, and whether a sheet was already up
// when the guide opened: one opened later is above the guide in the order of
// things to dismiss, so Escape is that sheet's (see the listener below).
let featuresFrom = null;
let featuresScrimAtOpen = false;

function featuresOpen() { return !$("features-overlay").hidden; }

function openFeatures(from) {
  const frame = $("features-frame");
  // Before the first load, which is when the frame's policy is fixed: a
  // self-served install frames the hosted page cross-origin, and its Copy
  // buttons need clipboard-write there.
  if (!frame.getAttribute("src")) {
    frame.allow = "clipboard-write";
    frame.src = featuresURL();
  }
  featuresFrom = from || document.activeElement;
  featuresScrimAtOpen = $("sheet-scrim").classList.contains("show");
  $("features-overlay").hidden = false;
  frame.focus();
}

function closeFeatures() {
  if (!featuresOpen()) return;
  $("features-overlay").hidden = true;
  const from = featuresFrom;
  featuresFrom = null;
  if (from && from.isConnected) from.focus();
}

$("btn-features").addEventListener("click", (e) => openFeatures(e.currentTarget));
$("btn-features-about").addEventListener("click", (e) => openFeatures(e.currentTarget));

// Only from the frame this file put up: any other window (a proxied page in
// the browser pane, a rendered report) could post the same shape.
window.addEventListener("message", (e) => {
  if (!e.data || e.data.type !== "pockettui-features-close") return;
  if (e.source !== $("features-frame").contentWindow) return;
  closeFeatures();
});

// On the window's capture phase, which runs ahead of every capture listener on
// the document: the settings sheet's Escape (05-settings.js) is one of those,
// and opened from About the guide sits over that sheet, so the sheet's handler
// would otherwise put the sheet away under the guide and leave the guide up.
// Stopped here for the reason those handlers give: xterm never consults
// defaultPrevented, and the terminal under the guide is owed no ESC.
window.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !featuresOpen()) return;
  if (!featuresScrimAtOpen && $("sheet-scrim").classList.contains("show")) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  closeFeatures();
}, true);

// Called by loadSessions() on every list that arrives; says something only on
// the first one this device has ever had, remembered on the device the way
// the browser pane's first-page hint is.
function featuresFirstListHint() {
  try {
    if (localStorage.getItem(FEATURES_HINT_SEEN_KEY)) return;
    localStorage.setItem(FEATURES_HINT_SEEN_KEY, "1");
  } catch (e) { return; }
  toast("New here? See what PocketTUI can do", FEATURES_HINT_MS);
}
