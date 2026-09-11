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

// ---- the list of computers in Settings -------------------------------------

// One row per computer, the active one marked, above the fields that edit it.
// Selecting a row is a switch: picking a computer here means what picking one
// from the session list's switcher means, and the fields below are always about
// whichever computer the app is actually talking to.
function renderProfileList() {
  const list = readProfiles();
  // Nothing to say on a device that has never paired, and the first run asks
  // one question — this is not it.
  $("profile-block").hidden = !list.length || setupMode;
  // The rows are a choice, and one computer is not a choice: below two of them
  // the section is only the way to add another, and the fields underneath are
  // that one computer's. Most people have one machine, and a row naming it says
  // nothing the fields do not already say.
  $("profile-rows").hidden = list.length < 2;
  const box = $("profile-list");
  box.innerHTML = "";
  const active = activeProfileId();
  for (const p of list) {
    const on = p.id === active && !addingProfile;
    const row = el("button", {
      type: "button", class: "profile-item" + (on ? " on" : ""),
      "aria-pressed": on ? "true" : "false",
    },
      el("span", { class: "profile-item-name" }, profileLabel(p)),
      el("span", { class: "profile-item-addr" }, profileHost(p.backend)),
    );
    row.addEventListener("click", () => {
      // The computer already on the other end: the tap has nothing to switch
      // to, and only ends an "Add computer" the user thought better of.
      if (p.id === activeProfileId()) {
        addingProfile = false;
        fillConnectionFields(activeProfile());
        renderProfileList();
        return;
      }
      switchProfile(p.id);
    });
    box.appendChild(row);
  }
}

// The blank fields a computer is typed into. Save is what turns them into a
// profile — until then nothing is stored, so backing out of the sheet leaves
// this device exactly as it was.
function beginAddProfile() {
  addingProfile = true;
  blankConnectionFields();
  renderProfileList();
  $("backend-url").focus();
}

$("btn-profile-add").addEventListener("click", beginAddProfile);

// ---- the session list's switcher -------------------------------------------

// Which computer the list belongs to. Only from two computers on: with one
// there is nothing to switch to, and a pill naming the only machine there is
// would sit on every session list for the sake of a feature that user is not
// using. Adding a second brings it out and forgetting back down to one puts it
// away, both through syncProfileUI().
function syncProfileSwitcher() {
  const list = readProfiles();
  $("btn-profile").hidden = list.length < 2;
  $("profile-name").textContent = profileLabel(activeProfile()) || "This computer";
}

// Open and closed are one class on the wrap, and the scrim rides along so a tap
// anywhere off the menu closes it without the list underneath taking that tap —
// the file explorer's two dropdowns work the same way, and for the same reasons.
function showProfileMenu(on) {
  $("profile-wrap").classList.toggle("open", on);
  $("profile-scrim").classList.toggle("show", on);
  $("btn-profile").setAttribute("aria-expanded", on ? "true" : "false");
  if (on) renderProfileMenu();
}

// Written on open rather than kept in step: a computer can be added, renamed or
// forgotten while this is closed, and the menu is only ever looked at open.
function renderProfileMenu() {
  const menu = $("profile-menu");
  menu.innerHTML = "";
  const active = activeProfileId();
  for (const p of readProfiles()) {
    const on = p.id === active;
    const row = el("button", {
      type: "button", class: "view-row" + (on ? " on" : ""),
      role: "menuitemradio", "aria-checked": on ? "true" : "false",
    },
      el("span", { class: "view-check", "aria-hidden": "true" }, "✓"),
      el("span", { class: "profile-menu-name" }, profileLabel(p)),
    );
    row.addEventListener("click", () => {
      showProfileMenu(false);
      if (p.id !== activeProfileId()) switchProfile(p.id);
    });
    menu.appendChild(row);
  }
  const add = el("button", { type: "button", class: "view-row profile-menu-add", role: "menuitem" },
    el("span", { class: "view-check", "aria-hidden": "true" }, "✓"),
    el("span", {}, "Add computer…"),
  );
  add.addEventListener("click", () => {
    showProfileMenu(false);
    openSettings(false, "connection");
    beginAddProfile();
  });
  menu.appendChild(add);
}

$("btn-profile").addEventListener("click", () => {
  showProfileMenu(!$("profile-wrap").classList.contains("open"));
});
$("profile-scrim").addEventListener("click", () => showProfileMenu(false));

// Both faces of the same fact, plus the sheet's fields when it happens to be
// open — a switch made from the session list changes what those are about.
function syncProfileUI() {
  syncProfileSwitcher();
  renderProfileList();
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
