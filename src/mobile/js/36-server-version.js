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
// Set while an install is in flight — started from here, started from another
// client (the 409 says so), or found still running at load. The install takes
// minutes and the server answers with the old version for all of them, so the
// notice stays where it is and says "Updating…" instead of vanishing and
// leaving the user to guess whether the tap landed. Deliberately not persisted:
// the tmux session below is the only witness that survives a service restart,
// and it is the truth a reload should be re-reading.
let updating = false;
// The next poll while updating, so nothing can start a second chain, and so
// leaving the state can cancel the one that is pending.
let updateTimer = null;
// When the watch began, for the give-up deadline.
let updateStartedAt = 0;
// Asked once a shell load, the first time the versions say an update is
// waiting — see resumeUpdateIfRunning().
let updateResumeChecked = false;

// The session `pockettui update` runs in, named by the server (UPDATE_SESSION
// in app.py). It outlives the service restart the install triggers, which makes
// it the one thing that can answer "is an install still going" after a reload.
const UPDATE_SESSION = "pockettui-update";
// Three seconds is cheap against a restart's window; five minutes is longer
// than any install has taken and short enough that a wedged one gives the
// button back rather than spinning forever.
const UPDATE_POLL_MS = 3000;
const UPDATE_GIVE_UP_MS = 5 * 60 * 1000;
// Said whenever the install stops being watchable without the server having
// come back newer. It names the session because the pane is where the reason is.
const UPDATE_FAILED = "Update did not finish. Open the pockettui-update session to see why.";

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
  // The diff pane is the one feature that polls: what it needs to know is
  // whether to poll at all, and this is the moment that becomes knowable.
  syncGitCap();
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
  const stale = on && versionNewer(latestVersion, serverVersion);
  box.hidden = !on;
  // The session list carries the same news as this block, and is written from
  // here so the two can never disagree about whether an update is waiting.
  syncUpdatePill(stale);
  // The moment staleness becomes knowable is the moment to ask whether an
  // install this shell has no memory of is already running.
  if (stale && !updating) resumeUpdateIfRunning();
  if (!on) return;
  $("sheet-version-line").textContent =
    serverVersion ? "Server " + serverVersion : "Server version unknown";
  $("sheet-version-latest").textContent = latestVersion;
  $("sheet-version-update").hidden = !stale;
  // The button only where the server said it can honour it; everywhere else the
  // row keeps the line telling the user what to run on the computer, which is
  // the only way an older server gets updated.
  const canUpdate = hasCap("update");
  $("btn-update-server").hidden = !canUpdate;
  $("sheet-version-manual").hidden = canUpdate;
}

// The notice in the session list header. Settings is where an update is
// explained, but nobody opens Settings looking for news, so the screen every
// session starts from is where the offer has to appear.
function syncUpdatePill(stale) {
  const pill = $("btn-update-rail");
  if (!pill) return;
  pill.hidden = !stale;
  if (pill.hidden) return;
  // Two faces, one notice: the offer, and the install it turns into. The
  // working face is unpressable because there is nothing left to ask for, and
  // the pulsing dot is what says the wait is still alive — the label alone
  // could be a screen that stopped updating an hour ago.
  pill.classList.toggle("pill-working", updating);
  pill.disabled = updating;
  pill.textContent = updating ? "Updating…" : "Update " + latestVersion;
}

$("btn-update-rail").addEventListener("click", async () => {
  // A server that cannot update itself has nothing to start here: open Settings,
  // where the version block names the command to run on the computer, and leave
  // the notice standing until that server reports the newer build.
  if (!hasCap("update")) { openSettings(false); return; }
  // The button is disabled while an install runs; a click that arrives anyway
  // must not post a second start.
  if (updating) return;
  await startServerUpdate();
});

// Start the update the server offered. The install restarts the service part
// way through, which drops this app's socket; the shell's own reconnect is what
// brings it back, so there is nothing to block on here — the session named in
// the toast is where the install can actually be read. What is watched from
// here is only whether it landed, which is what the notice reports.
async function startServerUpdate() {
  const btn = $("btn-update-server");
  btn.disabled = true;
  try {
    const r = await fetch(apiURL("api/update"), {
      method: "POST", headers: authHeaders(),
    });
    if (r.status === 409) {
      // Another client got there first. This one has the same install to wait
      // for, so it joins the wait rather than re-offering a button that would
      // only earn the same refusal.
      btn.disabled = false;
      toast("An update is already running");
      beginUpdateWatch();
      return;
    }
    if (!r.ok) throw new Error("HTTP " + r.status);
    showSheet(false);
    toast("Updating. Open the pockettui-update session to watch.");
    loadSessions();
    beginUpdateWatch();
  } catch (e) {
    dbg("update: start failed", e);
    btn.disabled = false;
    toast("Could not start the update");
  }
}

// Watch an install through to its end. Idempotent — a click, a 409 and a reload
// all arrive here, and only the first starts a chain of polls.
function beginUpdateWatch() {
  if (updating) return;
  updating = true;
  updateStartedAt = Date.now();
  syncVersionRow();
  updateTimer = setTimeout(pollUpdate, UPDATE_POLL_MS);
}

// Stop watching, saying why. The notice re-renders from the plain state, which
// means it disappears if the server came back newer and goes back to offering
// the update if it did not.
function endUpdateWatch(message) {
  updating = false;
  clearTimeout(updateTimer);
  updateTimer = null;
  syncVersionRow();
  toast(message);
}

// True once the server answers with a build that is no longer behind the site's,
// which is the only proof the install worked — and it ends the watch when it
// does. A server that names no version at all proves nothing: mid-restart the
// question simply cannot be answered yet.
function finishIfLanded() {
  if (!serverVersion || versionNewer(latestVersion, serverVersion)) return false;
  endUpdateWatch("Updated to " + serverVersion);
  return true;
}

// Is the install's session still there? null when the question could not be
// asked — the server is unreachable for most of an install, and silence is not
// an answer.
async function updateSessionAlive() {
  try {
    const r = await fetch(apiURL("api/sessions"), {
      cache: "no-store", headers: authHeaders(),
    });
    if (!r.ok) return null;
    const d = await r.json();
    return (d.sessions || []).some(s => s.name === UPDATE_SESSION);
  } catch (e) {
    return null;
  }
}

// One round of "is it done yet". Most rounds during an install fail outright —
// the service is restarting under them — and fetchServerVersion already treats
// that as no news, which is exactly right here.
async function pollUpdate() {
  await fetchServerVersion();
  if (!updating || finishIfLanded()) return;
  // The session ending is a hint, not a verdict: the wrapper can exit having
  // failed, and it can also finish a beat before the restarted server is
  // answering again. So the version gets one more ask, and only a server that
  // is up and still behind means the install did not take.
  if (await updateSessionAlive() === false) {
    await fetchServerVersion();
    if (!updating || finishIfLanded()) return;
    endUpdateWatch(UPDATE_FAILED);
    return;
  }
  // Nothing has been heard for long enough that waiting is no longer useful.
  // The offer comes back so the user can try again; the pane says why it needs
  // trying again.
  if (Date.now() - updateStartedAt > UPDATE_GIVE_UP_MS) {
    endUpdateWatch(UPDATE_FAILED);
    return;
  }
  updateTimer = setTimeout(pollUpdate, UPDATE_POLL_MS);
}

// An install started before this page load is still the same install, and the
// shell's flag did not survive the reload — but the tmux session did. Asked at
// most once, and only where there is a version gap to explain and a server that
// could have started one.
async function resumeUpdateIfRunning() {
  if (updateResumeChecked || demoMode || !hasCap("update")) return;
  updateResumeChecked = true;
  if (await updateSessionAlive() === true) beginUpdateWatch();
}

$("btn-update-server").addEventListener("click", startServerUpdate);

// The same boot moment the push status is fetched at: a paired app, before the
// user has asked for anything.
if (!needsSetup()) {
  fetchServerVersion();
  fetchLatestVersion();
}
