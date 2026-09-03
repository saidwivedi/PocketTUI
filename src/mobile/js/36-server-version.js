// ============================================================
// Server version and capabilities
// ============================================================
// The shell at pockettui.com/app/ is redeployed on every release; the server it
// talks to is only updated when its owner runs `pockettui update` on the
// computer. So the two are routinely out of step, and a shell that assumes the
// server is as new as itself shows buttons that answer 404.
//
// /api/version closes that: it says which build the server is, and hands back a
// capability map naming what that build can serve. A server old enough to send
// no map is treated as having everything the shell already had when the map was
// introduced — that is the map's own contract, and it is why hasCap() answers
// true for an unknown server rather than false.

// What the site is publishing, which is what `pockettui update` would install.
const LATEST_VERSION_URL = "https://pockettui.com/version.txt";

// "" until /api/version has answered once; a server too old to carry a VERSION
// file answers "" too, and both read as "unknown" here.
let serverVersion = "";
// null until known, and null also for a server that sent no map at all — see
// hasCap(), which is the only thing that should read this.
let serverCaps = null;
// "" until version.txt has answered, which it may never do.
let latestVersion = "";
let latestVersionAsked = false;

// Ask the computer what it is and what it can do. Any failure leaves the last
// answer standing: a dropped tailnet is not news about the server's build.
async function fetchServerVersion() {
  if (demoMode) return;
  try {
    const r = await fetch(apiURL("api/version"), {
      cache: "no-store", headers: authHeaders(),
    });
    if (!r.ok) return;
    const d = await r.json();
    serverVersion = typeof d.version === "string" ? d.version : "";
    serverCaps = d.capabilities && typeof d.capabilities === "object" ? d.capabilities : null;
  } catch (e) {
    dbg("server version failed:", e);
    return;
  }
  syncVersionRow();
}

// Whether the server on the other end serves this feature. Unknown means yes:
// a server that predates the map is one the shell used to drive with no checks
// at all, so refusing it features it does have would be the worse guess.
function hasCap(name) {
  if (!serverCaps) return true;
  return serverCaps[name] === true;
}

// What the site is publishing, asked once per shell load. Silent on failure by
// design: a self-hosted shell served from the user's own server is a different
// origin from pockettui.com and will be refused by CORS until the site sends a
// header, and "we could not ask" must read the same as "nothing newer".
async function fetchLatestVersion() {
  if (latestVersionAsked || demoMode) return;
  latestVersionAsked = true;
  try {
    const r = await fetch(LATEST_VERSION_URL, { cache: "no-store" });
    if (!r.ok) return;
    const v = (await r.text()).trim();
    // A captive portal answering HTML is not a version, and neither is an
    // error page with a 200 on it.
    if (!/^\d+\.\d+\.\d+$/.test(v)) return;
    latestVersion = v;
  } catch (e) {
    return;
  }
  syncVersionRow();
}

// Is a strictly newer than b? Either side unknown means no — an update hint is
// only worth showing when both ends of the comparison are real.
function versionNewer(a, b) {
  if (!a || !b) return false;
  const x = a.split(".").map(Number), y = b.split(".").map(Number);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const p = x[i] || 0, q = y[i] || 0;
    if (p !== q) return p > q;
  }
  return false;
}

// The Settings block. Hidden wherever there is no server to describe — the
// demo, and a shell still being paired — so it never states a version it is
// guessing at.
function syncVersionRow() {
  const box = $("sheet-version");
  if (!box) return;
  const on = !demoMode && !setupMode && !needsSetup();
  box.hidden = !on;
  if (!on) return;
  $("sheet-version-line").textContent =
    serverVersion ? "Server " + serverVersion : "Server version unknown";
  const stale = versionNewer(latestVersion, serverVersion);
  $("sheet-version-latest").textContent = latestVersion;
  $("sheet-version-update").hidden = !stale;
}

// The same boot moment the push status is fetched at: a paired app, before the
// user has asked for anything.
if (!needsSetup()) {
  fetchServerVersion();
  fetchLatestVersion();
}
