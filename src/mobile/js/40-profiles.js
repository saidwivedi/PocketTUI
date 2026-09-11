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
    },
      el("span", { class: "view-check", "aria-hidden": "true" }, "✓"),
      // Name over address rather than beside it: on one line the two split the
      // row between them and both came out elided, and the name is the half
      // being chosen.
      el("span", { class: "profile-menu-text" },
        el("span", { class: "profile-menu-name" }, profileLabel(p)),
        // Which address that name resolves to, for the two computers whose names
        // do not tell them apart on their own.
        el("span", { class: "profile-menu-addr" }, profileHost(p.backend)),
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
  if (!on) return;
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
  if (!on) return;
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
  sessionsResetForProfile();
  versionResetForProfile();
  pairResetForProfile();
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
  if (id) removeProfile(id);
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
