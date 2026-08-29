// ============================================================
// Boot
// ============================================================
syncChrome();
// Debug survives a reload, which is the point — a crash-and-restart is exactly
// when the tail of what happened is wanted.
if (cfg.debug) setDebug(true);
// Take a first full-height reading before any keyboard can appear, so the very
// first focus already has something to compare against.
noteFullHeight();
// pockettui.com/demo redirects here with ?demo=1, so the demo is linkable
// without a backend.
//
// The param is dropped from the URL straight away — openTerminal() pushes
// location.href as its state, so leaving it in would put ?demo=1 behind the back
// gesture and re-enter the demo on any reload. That alone would lose the demo on
// a first https visit, where a newly activated service worker reloads the page
// once to swap in the fresh shell: the reload lands on the already-stripped URL.
// So the intent is parked in sessionStorage, which survives that reload and dies
// with the tab. The PWA's start_url carries no param and is unaffected.
if (new URLSearchParams(location.search).get("demo") === "1") {
  try { sessionStorage.setItem(DEMO_INTENT, "1"); } catch (e) {}
  history.replaceState(null, "", location.pathname + location.hash);
}
// A QR scan lands here as #pair=<payload> — the installer prints a QR whose
// URL carries the address and pairing code so scanning is the whole setup.
// The fragment is stripped before it is even parsed: it must never survive
// into history, a screenshot of the URL bar, or the pushState chain
// openTerminal() starts, since it is exactly what a shoulder-surfer would want.
if (location.hash.indexOf("#pair=") === 0) {
  const raw = location.hash.slice("#pair=".length);
  history.replaceState(null, "", location.pathname + location.search);
  try {
    const p = JSON.parse(atob(raw.replace(/-/g, "+").replace(/_/g, "/")));
    const tok = normalizeToken(p.t);
    if (isValidToken(tok)) {
      cfg.token = tok;
      if (p.a) cfg.backend = normalizeBackend(p.a, "");
      toast("Paired");
    } else {
      toast("Pairing link invalid");
    }
  } catch (e) {
    toast("Pairing link invalid");
  }
}
// A tapped notification with no app window open lands here as
// #session=<name> (an already-open window gets a postMessage instead — see
// 30-notify.js). Stripped with the same replaceState-first discipline as
// #pair= above: the fragment must not survive into history or the pushState
// chain openTerminal() starts.
let deepLinkSession = "";
if (location.hash.indexOf("#session=") === 0) {
  const rawSession = location.hash.slice("#session=".length);
  history.replaceState(null, "", location.pathname + location.search);
  try { deepLinkSession = decodeURIComponent(rawSession); } catch (e) {}
}
let wantDemo = false;
try { wantDemo = sessionStorage.getItem(DEMO_INTENT) === "1"; } catch (e) {}
if (wantDemo) {
  openDemo();   // cleared in closeTerminal(), once the demo is actually left
} else if (needsSetup()) {
  // A public build with nothing configured has no backend to query — ask first,
  // then the save handler loads the list.
  openSettings(true);
} else if (deepLinkSession) {
  // openSessionByName loads the list before opening, so the back gesture out
  // of the terminal lands on a list that is already there. The same tap also
  // parked a copy of the name — take it so it cannot re-fire on resume.
  const target = deepLinkSession;
  takePendingSession().then(() => openSessionByName(target));
} else {
  // An iOS launch from a notification tap can land here on the bare
  // start_url with no fragment at all; the parked copy is the deep link then.
  loadSessions().then(consumePendingSession);
}
initA2hsHint();

const SW_VERSION = "__CACHE_VERSION__";

// A new service worker that activates while a terminal is attached must not
// reload the page out from under the session — the reload is parked here and
// happens once the user is back on the list. In-memory on purpose: if the tab
// dies first, the next launch re-fetches the shell and needs no reload at all.
let swReloadPending = false;
function swReloadNow() {
  sessionStorage.setItem("sw_reloaded_" + SW_VERSION, "1");
  location.reload();
}
// Called by closeTerminal() on the way back to the list; returns whether the
// deferred reload fired, so the caller can skip work the reload will redo.
function applyPendingSwReload() {
  if (!swReloadPending) return false;
  swReloadPending = false;
  swReloadNow();
  return true;
}

if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("sw.js?v=" + SW_VERSION).then(reg => {
    reg.addEventListener("updatefound", () => {
      const nw = reg.installing;
      if (!nw) return;
      nw.addEventListener("statechange", () => {
        if (nw.state === "activated" && navigator.serviceWorker.controller) {
          // New SW took over — reload once so the fresh shell renders, but
          // never mid-session (see swReloadPending above).
          if (!sessionStorage.getItem("sw_reloaded_" + SW_VERSION)) {
            if (currentSession) swReloadPending = true;
            else swReloadNow();
          }
        }
      });
    });
    // The chain above only starts when the page is fetched again, and a standalone
    // PWA resumed from the app switcher is restored frozen — it never re-fetches
    // index.html, so it can run a shell that is builds out of date with no way to
    // find out. Asking on every resume is what closes that door.
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) return;
      // A reload deferred mid-session can fire now if the session was gone by
      // the time the app came back to the foreground.
      if (swReloadPending && !currentSession) { applyPendingSwReload(); return; }
      reg.update().catch(()=>{});
    });
  }).catch(()=>{});
}
