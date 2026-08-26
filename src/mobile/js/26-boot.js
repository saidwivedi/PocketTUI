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
let wantDemo = false;
try { wantDemo = sessionStorage.getItem(DEMO_INTENT) === "1"; } catch (e) {}
if (wantDemo) {
  openDemo();   // cleared in closeTerminal(), once the demo is actually left
} else if (needsSetup()) {
  // A public build with nothing configured has no backend to query — ask first,
  // then the save handler loads the list.
  openSettings(true);
} else {
  loadSessions();
}
initA2hsHint();

const SW_VERSION = "__CACHE_VERSION__";
if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("sw.js?v=" + SW_VERSION).then(reg => {
    reg.addEventListener("updatefound", () => {
      const nw = reg.installing;
      if (!nw) return;
      nw.addEventListener("statechange", () => {
        if (nw.state === "activated" && navigator.serviceWorker.controller) {
          // New SW took over — reload once so the fresh shell renders.
          if (!sessionStorage.getItem("sw_reloaded_" + SW_VERSION)) {
            sessionStorage.setItem("sw_reloaded_" + SW_VERSION, "1");
            location.reload();
          }
        }
      });
    });
    // The chain above only starts when the page is fetched again, and a standalone
    // PWA resumed from the app switcher is restored frozen — it never re-fetches
    // index.html, so it can run a shell that is builds out of date with no way to
    // find out. Asking on every resume is what closes that door.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) reg.update().catch(()=>{});
    });
  }).catch(()=>{});
}
