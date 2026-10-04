// ============================================================
// Connection profiles — the switch and its two faces
// ============================================================
// The store is in 02-debug-log.js, under cfg: a profile is one computer this
// device is paired with, and exactly one is active. This fragment is everything
// the user sees of that — the switcher over the session list, the list of
// computers in Settings, and the switch itself.
//
// A switch is a reconnect, not a reattach. The sessions on the machine being
// left are that machine's; nothing about them means anything on the next one,
// so the terminal closes, every cache this shell holds about one computer is
// dropped, and the chosen one is asked the same questions from scratch.

// True between "Add computer" and the Save that turns the blank fields into a
// profile. Not a state the sheet can open in — openSettings() clears it — so
// the fields are always either a computer's own or a new one being typed.
let addingProfile = false;

// ---- the Connection tab's fields -------------------------------------------

// The fields as one computer's. With no profile at all it is a first run, where
// the only thing that could be known is an address baked in at build time.
//
// The name field shows the name in force rather than only a typed one: a
// computer is called something from the moment it has an address — its own
// hostname where it reports one, the host out of the address where it does not
// — and a blank field would say it had no name while three other places showed
// one. What is in the field is therefore also what a rename is measured against
// (the Save handler, 05-settings.js).
function fillConnectionFields(p) {
  const parts = backendParts((p ? p.backend : "") || DEFAULT_BACKEND);
  $("backend-url").value = parts.url;
  $("backend-port").value = parts.port;
  $("backend-token").value = formatTokenDisplay(p ? p.token || "" : "");
  $("backend-name").value = profileLabel(p);
}

// Just the name, for the moment the computer answers with one while the sheet
// is open. A field the user is typing in is theirs — never painted over.
function syncConnectionName() {
  if (!$("sheet-settings").classList.contains("show") || addingProfile) return;
  if (document.activeElement === $("backend-name")) return;
  $("backend-name").value = profileLabel(activeProfile());
}

function blankConnectionFields() {
  $("backend-url").value = "";
  $("backend-port").value = "";
  $("backend-token").value = "";
  $("backend-name").value = "";
}

// What the name field says, held to a length a switcher row can show. Empty is
// a real answer: it hands the computer back to whatever name it reports for
// itself, the same way it was named in the first place.
function profileNameField() {
  return $("backend-name").value.trim().slice(0, 40);
}

// ---- whether each computer answers ------------------------------------------

// One authenticated GET of /api/version, built from this profile's own address
// and code and nothing else: cfg and apiURL() are the active computer's, and
// another computer's code must never travel to it. redirect "manual" for the
// same reason — a redirect would carry the code header on to wherever it
// pointed. The timeout is ours (setTimeout + abort) rather than
// AbortSignal.timeout(), which iOS Safari only has from 16.
const PROFILE_CHECK_MS = 4000;

function profileApiURL(p, path) {
  const backend = (p && p.backend) || DEFAULT_BACKEND;
  return backend ? backend.replace(/\/$/, "") + "/" + path : BASE + path;
}

// An http address from an https page, which the browser refuses before any
// packet leaves — the same test classifyFailure() makes for the active one.
function profileMixedContent(p) {
  const backend = (p && p.backend) || DEFAULT_BACKEND;
  return location.protocol === "https:" && /^http:/i.test(backend)
    && !loopbackBackend(backend);
}

// The verdict in FAILURE_COPY's vocabulary plus two of its own: "ok" (answered
// and took the code) and "auth" (answered and refused it, with the server's
// hint). A thrown fetch is a refused connection, a DNS miss, the timeout or a
// mixed-content block, which the browser reports identically, so the last one
// is named from the addresses alone.
async function checkProfile(p, timeoutMs = PROFILE_CHECK_MS) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    let r;
    try {
      r = await fetch(profileApiURL(p, "api/version"), {
        cache: "no-store", redirect: "manual", signal: ctrl.signal,
        headers: { "X-PocketTUI-Token": (p && p.token) || "" },
      });
    } catch (e) {
      if (netOffline()) return { kind: "offline", status: 0 };
      return { kind: profileMixedContent(p) ? "mixed_content" : "unreachable", status: 0 };
    }
    const status = r.status;
    if (r.ok) return { kind: "ok", status };
    if (status === 401 || status === 403) {
      let hint = "";
      try { hint = (await r.json()).hint || ""; } catch (e) {}
      return { kind: "auth", status, hint };
    }
    if (status >= 502 && status <= 504) return { kind: "proxy_dead", status };
    if (status === 404) return { kind: "not_published", status };
    // An opaque redirect reads as status 0: something answers there, and it
    // is not PocketTUI's API.
    if (r.type === "opaqueredirect") return { kind: "not_pockettui", status };
    return { kind: "server_error", status };
  } finally {
    clearTimeout(timer);
  }
}

// What a row says about its computer, as [text, tone]. The tone picks the
// dot's colour; the text is the whole of the explanation a menu row has room
// for, and the title attribute carries the longer one where there is one.
const PROFILE_STATUS = {
  checking: ["Checking…", ""],
  connected: ["Connected", "good"],
  ok: ["Reachable", "good"],
  auth: ["Code rejected", "bad"],
  mixed_content: ["Blocked by browser", "bad"],
  offline: ["Offline", "warn"],
  unreachable: ["Unreachable", "warn"],
  proxy_dead: ["Not running", "warn"],
  not_published: ["Not published", "warn"],
  not_pockettui: ["Wrong address", "warn"],
  blocked: ["Blocked", "warn"],
  server_error: ["Server error", "warn"],
};
const PROFILE_STATUS_TITLE = {
  auth: "The computer refused this device's pairing code.",
  mixed_content: "This page is https and the browser will not let it call a plain-http address.",
  unreachable: "Nothing answered at this address.",
  proxy_dead: "The computer answered, but PocketTUI is not running.",
};

// The computer in force is not checked from here: the app is talking to it
// already, and whatever last failed against it (activeVerdict) or succeeded is
// the truer answer. Every other row reads its own last check, and keeps that
// answer on screen while a recheck is out.
function profileStatusKind(p) {
  if (p.id === activeProfileId()) {
    if (activeVerdict) return activeVerdict;
    const open = typeof WebSocket !== "undefined" && sock && sock.readyState === WebSocket.OPEN;
    return sessionsEverLoaded || open ? "connected" : "";
  }
  const rec = profileChecks.get(p.id);
  if (!rec) return "";
  return rec.kind || (rec.pending ? "checking" : "");
}

function profileStatusEl(p) {
  const s = el("span", { class: "profile-menu-status" });
  paintStatusEl(s, profileStatusKind(p));
  return s;
}

function paintStatusEl(s, kind) {
  const [text, tone] = PROFILE_STATUS[kind] || ["", ""];
  s.textContent = text;
  s.hidden = !text;
  s.dataset.tone = tone;
  if (PROFILE_STATUS_TITLE[kind]) s.title = PROFILE_STATUS_TITLE[kind];
  else s.removeAttribute("title");
}

// Both menus' rows, repainted in place: a check landing must not rebuild a
// menu under the finger that is about to tap it.
function paintProfileStatuses() {
  const byId = new Map(readProfiles().map((p) => [p.id, p]));
  for (const row of document.querySelectorAll(".profile-row[data-profile-id]")) {
    const p = byId.get(row.dataset.profileId);
    const s = row.querySelector(".profile-menu-status");
    if (p && s) paintStatusEl(s, profileStatusKind(p));
  }
}

// The failure paths' report on the computer in force (null = it just worked).
function noteActiveVerdict(kind) {
  if (activeVerdict === kind) return;
  activeVerdict = kind;
  paintProfileStatuses();
}

// Checks run only while a chooser is on screen. The Settings one can be left
// "open" under a sheet that has closed, which counts as shut.
function profileMenusOpen() {
  return $("profile-wrap").classList.contains("open") ||
    ($("profile-pick-wrap").classList.contains("open") &&
     $("sheet-settings").classList.contains("show"));
}

// Rechecks while a chooser stays open, herdr's cadence: every 30 s while a
// computer answers, doubling from there while it does not, up to four minutes.
// A rejected code and an http address in an https page are not rechecked at
// all — no wait changes either, and every wrong code counts towards the
// server's lockout of this device — until the chooser is opened again.
const PROFILE_RECHECK_MS = 30000;
const PROFILE_RECHECK_MAX_MS = 240000;

async function runProfileCheck(id) {
  const p = readProfiles().find((x) => x.id === id);
  if (!p || id === activeProfileId()) return;
  let rec = profileChecks.get(id);
  if (!rec) { rec = { kind: "", at: 0, fails: 0, timer: null, seq: 0, pending: false }; profileChecks.set(id, rec); }
  clearTimeout(rec.timer);
  rec.timer = null;
  const seq = ++rec.seq;
  rec.pending = true;
  paintProfileStatuses();
  const c = await checkProfile(p);
  if (profileChecks.get(id) !== rec || seq !== rec.seq) return;
  rec.pending = false;
  // Edited, forgotten or switched to while the answer was out: it is about an
  // address or code that is no longer this row's.
  const now = readProfiles().find((x) => x.id === id);
  if (!now || now.backend !== p.backend || now.token !== p.token || id === activeProfileId()) {
    profileChecks.delete(id);
    paintProfileStatuses();
    return;
  }
  rec.kind = c.kind;
  rec.at = Date.now();
  rec.fails = c.kind === "ok" ? 0 : rec.fails + 1;
  paintProfileStatuses();
  scheduleProfileRecheck(id, rec);
}

// The next check, timed from the last answer rather than from now, so a
// chooser reopened a moment after a check keeps the same cadence.
function scheduleProfileRecheck(id, rec) {
  if (rec.timer || rec.pending || !profileMenusOpen()) return;
  if (rec.kind === "auth" || rec.kind === "mixed_content") return;
  const delay = rec.kind === "ok" ? PROFILE_RECHECK_MS
    : Math.min(PROFILE_RECHECK_MS * Math.pow(2, rec.fails - 1), PROFILE_RECHECK_MAX_MS);
  rec.timer = setTimeout(() => {
    rec.timer = null;
    if (profileMenusOpen()) runProfileCheck(id);
  }, Math.max(0, rec.at + delay - Date.now()));
}

// On every open: each other computer is asked now, unless it answered in the
// last few seconds (a menu closed and reopened) or is being asked already.
function startProfileChecks() {
  const active = activeProfileId();
  for (const p of readProfiles()) {
    if (p.id === active) continue;
    const rec = profileChecks.get(p.id);
    if (rec && (rec.pending || Date.now() - rec.at < 5000)) { scheduleProfileRecheck(p.id, rec); continue; }
    runProfileCheck(p.id);
  }
}

function stopProfileChecksIfClosed() {
  if (profileMenusOpen()) return;
  for (const rec of profileChecks.values()) { clearTimeout(rec.timer); rec.timer = null; }
}

// The active computer's socket has failed `n` times in a row
// (scheduleReconnect, 09-image-viewer.js): say which way, in the banner the
// terminal already has. A computer that answers and takes the code leaves the
// socket as the thing failing — the toast at 3, the server-error banner from
// 6, as before. A refused code goes the way a 4401 close does. Anything else
// is named now rather than after six tries, with the health descriptor asked
// for the finer verdicts (a proxy with nothing behind it, an unrouted path).
async function classifyDrop(n) {
  const pgen = profileGen;
  const stale = () => pgen !== profileGen || !currentSession || retries === 0;
  const c = await checkProfile({ backend: cfg.backend, token: cfg.token });
  if (stale()) return;
  if (c.kind === "ok") {
    if (n === 3) toast("Reconnecting…");
    if (n >= 6) renderConnBanner(classifyFailure(null, null, { kind: "ok" }));
    return;
  }
  if (c.kind === "auth") {
    clearTimeout(retryTimer);
    closeTerminal(true);
    rejectToken(c.hint);
    return;
  }
  const probe = c.kind === "offline" || c.kind === "mixed_content" ? null : await probeServer();
  if (stale()) return;
  renderConnBanner(classifyFailure(null, null, probe));
}

// ---- the menu, drawn in two places -----------------------------------------

// The trash at the end of a Settings row. It asks first, through the native
// confirm() the explorer's delete uses rather than appConfirm(): that one is a
// sheet, and the single-sheet rule would close the Settings sheet this was
// asked from. stopPropagation, so forgetting a computer never also picks it.
function profileTrashBtn(p) {
  return el("button", {
    type: "button", class: "icon-btn profile-menu-trash",
    "aria-label": "Forget " + profileLabel(p),
    onclick: (e) => {
      e.stopPropagation();
      if (!confirm("Forget " + profileLabel(p) +
                   "? This device will need to be paired with it again.")) return;
      forgetProfile(p.id);
      // The row that was tapped is gone, so what is left is drawn again — and
      // with nothing left the menu goes too, the sheet behind it being first-run
      // setup by then.
      showProfilePick(readProfiles().length > 0);
    },
  }, svgIcon("i-trash"));
}

// One row per computer, the one in force ticked. The two dropdowns differ in a
// single row: Settings ends with the way to add a computer, because adding one
// is a settings job, and the session list's does not, because a pill over a
// list of sessions is for getting between machines and nothing else.
function fillProfileMenu(menu, pick, withAdd) {
  menu.innerHTML = "";
  const active = addingProfile ? "" : activeProfileId();
  for (const p of readProfiles()) {
    const on = p.id === active;
    // A div and not a button: the Settings menu hangs a trash button off the end
    // of each row, and a button inside a button is invalid markup that iOS
    // Safari lays out and taps its own way. Enter and Space are wired below,
    // since a div answers to neither on its own.
    const row = el("div", {
      class: "view-row profile-row" + (on ? " on" : ""),
      role: "menuitemradio", "aria-checked": on ? "true" : "false", tabindex: "0",
      "data-profile-id": p.id,
    },
      el("span", { class: "view-check", "aria-hidden": "true" }, "✓"),
      // Name over address rather than beside it: on one line the two split the
      // row between them and both came out elided, and the name is the half
      // being chosen.
      el("span", { class: "profile-menu-text" },
        el("span", { class: "profile-menu-name" }, profileLabel(p)),
        // Which address that name resolves to, for the two computers whose names
        // do not tell them apart on their own, and whether it answers there.
        el("span", { class: "profile-menu-sub" },
          el("span", { class: "profile-menu-addr" }, profileHost(p.backend)),
          profileStatusEl(p),
        ),
      ),
    );
    row.addEventListener("click", () => pick(p.id));
    row.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      pick(p.id);
    });
    // Forgetting a computer is managing them, which is a Settings job — hence
    // the same test as the "Add another computer…" row below.
    if (withAdd) row.appendChild(profileTrashBtn(p));
    menu.appendChild(row);
  }
  if (!withAdd) return;
  const add = el("button", { type: "button", class: "view-row profile-menu-add", role: "menuitem" },
    el("span", { class: "view-check", "aria-hidden": "true" }, "✓"),
    // "another", because every row above it is already a computer.
    el("span", {}, "Add another computer…"),
  );
  add.addEventListener("click", () => {
    showProfilePick(false);
    beginAddProfile();
  });
  menu.appendChild(add);
}

// ---- the chooser at the top of Settings > Connection ------------------------

// What the fields below are about: the computer in force, or the new one being
// typed. Present from the first computer on — it is the only way to add a
// second — and gone only where there is none at all, which is the first run.
function syncProfilePick() {
  const list = readProfiles();
  $("profile-block").hidden = !list.length || setupMode;
  $("profile-pick-name").textContent = addingProfile
    ? "New computer" : (profileLabel(activeProfile()) || "This computer");
}

function showProfilePick(on) {
  $("profile-pick-wrap").classList.toggle("open", on);
  $("btn-profile-pick").setAttribute("aria-expanded", on ? "true" : "false");
  if (!on) { stopProfileChecksIfClosed(); return; }
  startProfileChecks();
  fillProfileMenu($("profile-pick-menu"), (id) => {
    showProfilePick(false);
    // The computer already on the other end: nothing to switch to, and the tap
    // only ends an add the user thought better of.
    if (id === activeProfileId() && !addingProfile) return;
    if (id === activeProfileId()) {
      addingProfile = false;
      fillConnectionFields(activeProfile());
      syncProfilePick();
      return;
    }
    switchProfile(id);
  }, true);
}

// The blank fields a computer is typed into. Save is what turns them into a
// profile — until then nothing is stored, so backing out of the sheet leaves
// this device exactly as it was.
function beginAddProfile() {
  addingProfile = true;
  blankConnectionFields();
  syncProfilePick();
  $("backend-url").focus();
}

$("btn-profile-pick").addEventListener("click", () => {
  showProfilePick(!$("profile-pick-wrap").classList.contains("open"));
});
// This one floats inside the sheet rather than over the session list, so it has
// no scrim of its own to be dismissed by: anything pressed outside it closes
// it, which is what the scrim does for the other one. On capture, so a control
// under the open panel still gets its own click.
document.addEventListener("click", (e) => {
  if (!$("profile-pick-wrap").classList.contains("open")) return;
  if (e.target.closest && e.target.closest("#profile-pick-wrap")) return;
  showProfilePick(false);
}, true);

// ---- the session list's switcher -------------------------------------------

// Which computer the list belongs to, and the way between them. Only from two
// computers on: with one there is nowhere to go, and this menu does not add
// them — Settings does. What it carries is that computer's name rather than its
// address, so where it does show it says whose sessions these are.
function syncProfileSwitcher() {
  const list = readProfiles();
  $("btn-profile").hidden = list.length < 2;
  $("profile-name").textContent = profileLabel(activeProfile()) || "This computer";
}

// Open and closed are one class on the wrap, and the scrim rides along so a tap
// anywhere off the menu closes it without the list underneath taking that tap —
// the file explorer's two dropdowns work the same way, and for the same reasons.
// Written on open rather than kept in step: a computer can be added, renamed or
// forgotten while this is closed, and the menu is only ever looked at open.
function showProfileMenu(on) {
  $("profile-wrap").classList.toggle("open", on);
  $("profile-scrim").classList.toggle("show", on);
  $("btn-profile").setAttribute("aria-expanded", on ? "true" : "false");
  if (!on) { stopProfileChecksIfClosed(); return; }
  startProfileChecks();
  fillProfileMenu($("profile-menu"), (id) => {
    showProfileMenu(false);
    if (id !== activeProfileId()) switchProfile(id);
  }, false);
}

$("btn-profile").addEventListener("click", () => {
  showProfileMenu(!$("profile-wrap").classList.contains("open"));
});
$("profile-scrim").addEventListener("click", () => showProfileMenu(false));

// Both faces of the same fact, plus the sheet's fields when it happens to be
// open — a switch made from the session list changes what those are about.
function syncProfileUI() {
  syncProfileSwitcher();
  syncProfilePick();
  if ($("sheet-settings").classList.contains("show") && !addingProfile) {
    fillConnectionFields(activeProfile());
  }
}

// ---- the switch -------------------------------------------------------------

// Everything this shell holds that is about one computer rather than about the
// app: the stashed file views and the folders they were in, the diff pane's
// repo, the list's unread baseline, the server's build and capability map, the
// pairing QR. All of it is the machine being left's, and none of it means
// anything on the next one.
//
// `probe` is for an address typed a moment ago: the failure of that one is
// worth naming, which is what loadSessionsAfterProbe() does and a plain load
// does not.
function switchProfile(id, probe) {
  const p = readProfiles().find((x) => x.id === id);
  if (!p) return;
  // First, so that every answer already in flight — a session list, a version,
  // a push status — is the old computer's and is dropped when it lands.
  profileGen++;
  showProfileMenu(false);
  if (currentSession || $("screen-term").classList.contains("active")) {
    // skipReload: the list is loaded below, against the computer being
    // switched to rather than the one being left.
    closeTerminal(true);
  }
  dropAllFileViews();
  filesResetForProfile();
  diffResetForProfile();
  browserResetForProfile();
  sessionsResetForProfile();
  versionResetForProfile();
  pairResetForProfile();
  // The verdict was the computer being left's. The one switched to has its own
  // check record, which is about it as a row and not as the active one.
  activeVerdict = null;
  profileChecks.delete(id);
  setActiveProfile(id);
  // Whatever was on screen, a switch lands on the session list — the one screen
  // that is about the computer as a whole.
  $("screen-list").classList.add("active");
  syncChrome();
  syncProfileUI();
  fetchServerVersion();
  if (probe) loadSessionsAfterProbe();
  else loadSessions();
  // The subscription this browser holds is registered with the machine being
  // left, so the new one has to be told about it too.
  pushResetForProfile();
}

// ---- forgetting one ---------------------------------------------------------

// This device stops being paired with that computer — the Forget button's job
// and a chooser row's trash alike. Forgetting one it was not talking to changes
// nothing but the two lists it was in; forgetting the one in force means the app
// has to land somewhere, which is the next computer saved or, with none left,
// the first run.
function forgetProfile(id) {
  const wasActive = !id || id === activeProfileId();
  if (id) {
    removeProfile(id);
    const rec = profileChecks.get(id);
    if (rec) clearTimeout(rec.timer);
    profileChecks.delete(id);
  }
  toast("Computer forgotten");
  if (!wasActive) {
    // The fields are the active computer's and stay its — including anything
    // typed into them and not yet saved.
    syncProfileSwitcher();
    syncProfilePick();
    return;
  }
  blankConnectionFields();
  // With another computer saved, this device is not unpaired — it is now
  // talking to that one, which is a switch like any other. With none left it is
  // back to first-run setup, which is not dismissible.
  const next = readProfiles()[0];
  if (next) { switchProfile(next.id); return; }
  // Nothing left to switch between, so the switcher goes with the last row.
  syncProfileSwitcher();
  openSettings(needsSetup());
}

// A scanned pairing link (27-boot.js), applied to this device's profiles. The
// same computer scanned twice is the profile it already made — matched on the
// address, the only thing in the payload that names a machine — so a re-pair
// replaces that one's code rather than leaving two rows for one computer.
function adoptPairedBackend(backend, token) {
  const same = readProfiles().find((x) => (x.backend || "") === backend);
  if (same && same.id === activeProfileId()) {
    updateProfile(same.id, { token: token });
    syncProfileUI();
    return;
  }
  if (same) {
    updateProfile(same.id, { token: token });
    switchProfile(same.id);
    return;
  }
  switchProfile(addProfile({ backend: backend, token: token }).id);
}

syncProfileSwitcher();
