// ============================================================
// Usage summary
// ============================================================
// One anonymous summary per stint of use, posted to pockettui.com/api/wave as
// the page goes away: which build the shell and the server are, what kind of
// device, how long, how many reconnects and distinct sessions, and how many
// times each pane was opened. Nothing else leaves the device — no address,
// path, command, session name or terminal text, and nothing derived from them.
// The counters live in 01-helpers.js, because fragments that run at load count
// into them; this fragment only reads them.
//
// A stint starts at load and again whenever the page comes back after being
// hidden, so time spent in the background is never counted as use. Each stint
// sends at most once, and one shorter than five seconds is not sent. Offline,
// the summary is simply lost: nothing is queued on the device.
//
// sendBeacon with a text/plain body because that type needs no CORS
// preflight: a self-served shell posts from its own origin, and a preflight a
// beacon cannot make would drop every summary without a word. The endpoint
// never answers anything the client could act on, so nothing is read back.

const USAGE_URL = "https://pockettui.com/api/wave";
// The version of the returning-user question's text. Raising it asks again
// (usageConsentDue), since an answer to older wording is not an answer to this.
const USAGE_TEXT_VERSION = 1;
const USAGE_MIN_SECS = 5;
const USAGE_OS = { iOS: "ios", iPadOS: "ipados", Android: "android", macOS: "mac",
  Windows: "windows", Linux: "linux", ChromeOS: "chromeos" };

let usageStart = Date.now();
let usageSent = false;
let usageHidden = false;

// The endpoint's own rule for a version string; anything else would cost the
// whole summary a 400.
function usageVersion(s) {
  return String(s || "").replace(/[^0-9A-Za-z.\-]/g, "").slice(0, 20);
}

// parseUA's family names (35-report.js), lower-cased into the endpoint's set.
// parseUA already reads a touch-capable "Mac" as iPadOS; an older iPad that
// still names itself in the UA comes back as iOS there and is put right here.
function usageOS(ua, maxTouchPoints) {
  const os = parseUA(ua, maxTouchPoints).os;
  if (os === "iOS" && /iPad/.test(ua)) return "ipados";
  return USAGE_OS[os] || "other";
}

function usageShell() {
  if (SAME_ORIGIN) return "self";
  return location.hostname === "pockettui.com" ? "hosted" : "other";
}

// The summary as it would be sent now. Reads the counters and nothing that
// says where the app is connected to.
function usagePayload(now) {
  const f = {};
  for (const k of Object.keys(usageCounts)) f[k] = usageCounts[k];
  const p = {
    v: 1,
    app: usageVersion(appVersion()),
    srv: usageVersion(serverVersion),
    shell: usageShell(),
    os: usageOS(navigator.userAgent || "", navigator.maxTouchPoints),
    layout: isWideLayout() ? "desktop" : "phone",
    pwa: a2hsInstalled() ? 1 : 0,
    secs: Math.max(0, Math.floor(((now || Date.now()) - usageStart) / 1000)),
    rc: usageReconnects,
    seen: usageSeen.size,
    f: f,
  };
  const id = cfg.usageId;
  if (id) p.id = id;
  return p;
}

function usageSend(body) {
  try {
    if (navigator.sendBeacon &&
        navigator.sendBeacon(USAGE_URL, new Blob([body], { type: "text/plain" }))) return;
  } catch (e) {}
  try {
    fetch(USAGE_URL, { method: "POST", body: body, keepalive: true, mode: "no-cors",
      headers: { "Content-Type": "text/plain" } }).catch(() => {});
  } catch (e) {}
}

// Sends this stint's summary unless it already went, the user turned it off,
// the app is the demo or not paired yet, or the stint was too short to be use.
function usageFlush() {
  if (usageSent || cfg.usageOff || demoMode || needsSetup()) return false;
  const p = usagePayload();
  if (p.secs < USAGE_MIN_SECS) return false;
  usageSent = true;
  usageSend(JSON.stringify(p));
  return true;
}

// A new stint. The session on screen is part of it from the start, the same
// as one opened after it.
function usageResume() {
  usageStart = Date.now();
  usageSent = false;
  for (const k of Object.keys(usageCounts)) usageCounts[k] = 0;
  usageSeen.clear();
  usageReconnects = 0;
  if (currentSession && !demoMode) usageSeen.add(currentSession);
}

// ---- the two settings ------------------------------------------------------

function usageMintId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // randomUUID exists only in a secure context, and a LAN shell is plain http.
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" +
    h.slice(16, 20) + "-" + h.slice(20);
}

// True when the returning-user question has never been answered, or was
// answered to older wording than the app now shows.
function usageConsentDue() {
  const r = cfg.usageConsent;
  return !r || typeof r.v !== "number" || r.v < USAGE_TEXT_VERSION;
}

// Yes to being counted as returning. A fresh id every time: an earlier one was
// deleted when the answer was no, and is not brought back. Being counted means
// the summary is on, so a yes turns statistics back on as well.
function usageGrant() {
  cfg.usageOff = false;
  cfg.usageId = usageMintId();
  cfg.usageConsent = { consent: true, at: new Date().toISOString(), v: USAGE_TEXT_VERSION };
}

function usageRevoke() {
  cfg.usageId = "";
  cfg.usageConsent = { consent: false, at: new Date().toISOString(), v: USAGE_TEXT_VERSION };
}

// Statistics off sends nothing, and takes the id with it: an id kept while off
// would be a way to link the use before to the use after.
function usageSetOff(off) {
  cfg.usageOff = !!off;
  if (off && cfg.usageId) usageRevoke();
}

// ---- the returning-user question -------------------------------------------

let usageAsked = false;

// Shows the question when it is due, once per page load, and only onto a quiet
// screen: not with statistics off (the answer would mean nothing), not in the
// demo or before pairing, and not over the first run, its voice step, or any
// other sheet. A screen that is busy now is asked on a later load instead of
// having a second sheet stacked on it. The reason is for the debug log only.
function usageAskIfDue(reason) {
  if (usageAsked || !usageConsentDue() || cfg.usageOff || demoMode || needsSetup()) return false;
  if (setupMode || voiceStep || $("sheet-scrim").classList.contains("show")) return false;
  usageAsked = true;
  dbg("usage: ask", reason);
  showSheet(true, "sheet-usage");
  return true;
}

// The About rows show the answer the next time Settings opens either way
// (openSettings syncs them); painting them now keeps the hidden sheet honest.
function usageAnswered() {
  showSheet(false);
  syncUsageRows();
}

$("btn-usage-yes").addEventListener("click", () => {
  usageGrant();
  usageAnswered();
});
// A no to the id only: statistics stay as they were. On a first ask there is no
// id; on a re-ask after new wording an earlier yes may have left one, and a no
// to the new text has to stop it being sent.
$("btn-usage-no").addEventListener("click", () => {
  usageRevoke();
  usageAnswered();
});
// Closing is not an answer, so nothing is stored and the next load asks again.
$("btn-usage-close").addEventListener("click", () => showSheet(false));

// After boot (27-boot.js runs after this fragment) has opened whatever it
// opens, so the guards above see the first run, the demo or a deep-linked
// session already on screen. Covers a pairing link, which pairs without the
// first run's Confirm, and a device paired before the question existed.
setTimeout(() => usageAskIfDue("boot"), 800);

// ---- when it is sent -------------------------------------------------------

// Both, because a backgrounding standalone PWA on iOS fires pagehide and not
// visibilitychange (see the voice-capture note); the sent flag makes the second
// a no-op.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    usageFlush();
    usageHidden = true;
  } else if (usageHidden) {
    usageHidden = false;
    usageResume();
  }
});
window.addEventListener("pagehide", () => {
  usageFlush();
  usageHidden = true;
});
window.addEventListener("pageshow", (e) => {
  if (!e.persisted) return;
  usageHidden = false;
  usageResume();
});
