// ============================================================
// Add-to-home-screen hint
// ============================================================
// Phone/tablet only. iPadOS reports as "Macintosh" unless it's touch-capable,
// which real Macs never are, so that combination still counts as iOS.
function a2hsPlatform() {
  const ua = navigator.userAgent;
  if (/iPhone|iPad|iPod/.test(ua)) return "ios";
  if (/Mac/.test(ua) && navigator.maxTouchPoints > 1) return "ios";
  if (/Android/.test(ua)) return "android";
  return null;
}
function a2hsInstalled() {
  return window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
}
function a2hsShouldShow() {
  return !!a2hsPlatform() && !a2hsInstalled() && !localStorage.getItem("pockettui_a2hs_dismissed");
}
function initA2hsHint() {
  if (!a2hsShouldShow()) return;
  const text = a2hsPlatform() === "ios"
    ? "Add PocketTUI to your home screen: tap Share, then Add to Home Screen."
    : "Add PocketTUI to your home screen: tap the browser menu, then Add to Home screen.";
  $("a2hs-hint-text").textContent = text;
  $("a2hs-hint").classList.add("show");
}
$("a2hs-hint-dismiss").addEventListener("click", () => {
  localStorage.setItem("pockettui_a2hs_dismissed", "1");
  $("a2hs-hint").classList.remove("show");
});

// The app is served both at / and behind `tailscale serve` at /pockettui/, so
// same-origin URLs hang off the directory of the current path rather than the
// site root. With a backend configured (the public static deploy, or a manual
// override) everything targets that absolute origin instead.
const BASE = location.pathname.replace(/[^/]*$/, "");
function apiURL(p) {
  return cfg.backend ? cfg.backend.replace(/\/$/, "") + "/" + p : BASE + p;
}
function wsURL(p) {
  if (cfg.backend) {
    const proto = cfg.backend.startsWith("https:") ? "wss:" : "ws:";
    return cfg.backend.replace(/^https?:/, proto).replace(/\/$/, "") + "/" + p;
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return proto + "//" + location.host + BASE + p;
}

// Every API call needs to prove it holds the pairing code; merge rather than
// clobber so POSTs keep their Content-Type alongside it.
function authHeaders(extra) {
  return Object.assign({ "X-PocketTUI-Token": cfg.token }, extra || {});
}

