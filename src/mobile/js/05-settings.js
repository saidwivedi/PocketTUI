// ============================================================
// Settings
// ============================================================
// One scrim serves every sheet, so closing means closing whichever is open.
const SHEET_IDS = ["sheet-settings", "sheet-new", "sheet-session",
                   "sheet-file-actions", "sheet-files-add"];
function showSheet(on, id="sheet-settings") {
  // Closing the settings sheet ends the first-run voice step however it was
  // closed — Confirm, Cancel or the scrim. The device is paired by then, so
  // dismissing it is a real answer: keep the resolved engine unstored and let
  // it keep tracking the backend, which is what unset has always meant.
  if (voiceStep && !(on && id === "sheet-settings")) showVoiceStep(false);
  for (const s of SHEET_IDS) $(s).classList.toggle("show", on && id === s);
  $("sheet-scrim").classList.toggle("show", on);
}

// Turn what someone actually types ("my-host", "my-host:5560/pockettui",
// "https://my-host/pockettui/") into a URL the app can use.
function normalizeBackend(url, port) {
  let u = (url || "").trim();
  if (!u) return "";
  if (!/^https?:\/\//i.test(u)) u = "https://" + u;
  u = u.replace(/\/+$/, "");
  const p = (port || "").trim();
  if (p) {
    // Only add a port if the authority does not already carry one AND the
    // address has no path. A path like /pockettui is a reverse-proxy route on
    // 443; gluing a leftover backend port onto it builds a URL nothing answers
    // on — the port field is for bare hosts only.
    const m = u.match(/^(https?:\/\/)([^/]+)(.*)$/i);
    if (m && !/:\d+$/.test(m[2]) && !m[3]) u = m[1] + m[2] + ":" + p;
  }
  return u;
}

// Split a stored URL back into the two fields for editing.
function backendParts(v) {
  const m = (v || "").match(/^(https?:\/\/)([^/]+?)(?::(\d+))?(\/.*)?$/i);
  if (!m) return { url: v || "", port: "" };
  return { url: m[1] + m[2] + (m[4] || ""), port: m[3] || "" };
}

// Accepts the code typed with or without the dash, any case, with stray
// whitespace — normalizes down to the bare 10-char canonical form. 0 and 1 are
// mapped to O and I: the alphabet excludes them for being look-alikes, so a
// typed 0 can only ever mean the letter O read off a screen.
function normalizeToken(v) {
  return (v || "").toUpperCase().replace(/[\s-]/g, "")
    .replace(/0/g, "O").replace(/1/g, "I");
}

// True for exactly 10 base32 chars — the alphabet excludes 0/1/8/9 so those
// never pass even if someone's autocorrect or a look-alike keyboard slips one in.
function isValidToken(v) {
  return /^[A-Z2-7]{10}$/.test(v);
}

// Re-groups a canonical 10-char token as XXXXX-XXXXX for display in the field.
function formatTokenDisplay(v) {
  const t = normalizeToken(v);
  return t.length > 5 ? t.slice(0, 5) + "-" + t.slice(5) : t;
}

let setupMode = false;
// True between a first-run save and its Confirm: the sheet is still open, but
// on the voice step rather than the address one.
let voiceStep = false;

function openSettings(firstRun) {
  setupMode = !!firstRun;
  const parts = backendParts(cfg.backend);
  $("backend-url").value = parts.url;
  $("backend-port").value = parts.port;
  $("backend-token").value = formatTokenDisplay(cfg.token);
  $("backend-devname").value = cfg.devname;
  syncVoicePicker();
  // Lazy, like the mic key's own use of it: the sheet is the other place the
  // answer matters, and asking on open is what makes a fresh setup_voice.sh run
  // show up without a reload.
  fetchVoiceStatus(true).then(syncVoicePicker);
  $("dbg-toggle").checked = cfg.debug;
  $("search-toggle").checked = cfg.scrollbackSearchOn;
  $("alt-toggle").checked = cfg.altKeyOn;
  $("snip-toggle").checked = cfg.snippetsOn;
  $("snip-text").value = cfg.snippets;
  $("snip-edit").classList.toggle("show", cfg.snippetsOn);
  $("sheet-title").textContent = firstRun ? "Connect your computer" : "Settings";
  $("sheet-note").classList.toggle("hide", !firstRun);
  // First run has nothing to go back to, so there is no cancelling out of it.
  $("btn-settings-cancel").style.display = firstRun ? "none" : "";
  // The demo is the way out of a first run with no backend to enter; past setup
  // the list screen carries its own entry point.
  $("btn-sheet-demo").classList.add("show");
  // Nothing to forget on a first run, and nothing to forget if the backend was
  // baked in at build time and no code has been entered yet.
  $("btn-settings-forget").classList.toggle("show", !firstRun && !!(cfg.backend || cfg.token));
  $("sheet-install").classList.toggle("show", !!firstRun);
  $("sheet-faq").classList.toggle("show", !!firstRun);
  $("sheet-settings").classList.toggle("setup", !!firstRun);
  // The voice step is something a save opens, never something the sheet opens
  // with: until the code is entered there is nothing to confirm.
  showVoiceStep(false);
  showSheet(true);
}

// The first-run voice step: on or off. Off is also what every ordinary visit to
// Settings looks like, so this is what returns the sheet to normal.
function showVoiceStep(on) {
  voiceStep = on;
  $("voice-confirm-row").classList.toggle("show", on);
  $("btn-voice-confirm").classList.toggle("primary", on);
  if (on) syncVoiceConfirm();
}

// Brings the picker to the top of the sheet. The address fields above it are
// answered by now and the FAQ below is about connecting, so what the step is
// asking should be the first thing in view rather than something to scroll for.
function revealVoiceStep() {
  $("voice-label").scrollIntoView({ behavior: "smooth", block: "start" });
}

// Confirm commits whatever the picker is showing, so it is only inert while the
// picker is showing nothing — which the auto-resolved pre-selection means is
// nearly never. It stays a real tap rather than a formality: the engine it
// confirms is one the user has now actually seen.
function syncVoiceConfirm() {
  const sel = $("voice-engine").querySelector("input:checked");
  $("btn-voice-confirm").disabled = !sel;
}

// The address was reachable but the code was wrong — send them straight back
// to re-enter it rather than showing the generic can't-connect toast.
function rejectToken() {
  openSettings(needsSetup());
  $("backend-token").value = "";
  $("backend-token").focus();
  toast("Pairing code rejected");
}

$("btn-settings").addEventListener("click", () => openSettings(false));
$("btn-sheet-demo").addEventListener("click", () => {
  // Leaves setupMode set so the sheet comes back on the way out of the demo,
  // and writes nothing to localStorage.
  showSheet(false);
  openDemo();
});
$("btn-copy-install").addEventListener("click", () => {
  const cmd = $("install-cmd").textContent;
  if (!navigator.clipboard || !navigator.clipboard.writeText) {
    toast("Clipboard unavailable");
    return;
  }
  navigator.clipboard.writeText(cmd)
    .then(() => toast("Install command copied"))
    .catch(() => toast("Clipboard blocked"));
});
// Drops the stored address and pairing code, so this device is no longer paired
// with that computer. The device name stays: it names this device, not the
// computer, and keeping it means re-pairing lands on the same view session.
$("btn-settings-forget").addEventListener("click", () => {
  localStorage.removeItem("pockettui_backend");
  localStorage.removeItem("pockettui_token");
  $("backend-url").value = "";
  $("backend-port").value = "";
  $("backend-token").value = "";
  toast("Computer forgotten");
  // Re-open in whichever mode we are now in: a public build with nothing stored
  // is back to first-run setup, which is not dismissible.
  openSettings(needsSetup());
});
$("btn-settings-cancel").addEventListener("click", () => showSheet(false));
$("sheet-scrim").addEventListener("click", () => {
  if (!setupMode) showSheet(false);   // the setup sheet is not dismissible
});
$("btn-settings-save").addEventListener("click", () => {
  const v = normalizeBackend($("backend-url").value, $("backend-port").value);
  if (setupMode && !v) { toast("Enter your computer address"); return; }
  const tok = normalizeToken($("backend-token").value);
  if (setupMode && !isValidToken(tok)) { toast("Enter the 10-character pairing code"); return; }
  // Asked before the write, because the write is what makes it false.
  const wasUnpaired = needsSetup();
  // Also asked before the write: a re-pair onto a different backend/token needs
  // the same voice step as a first pairing, even though wasUnpaired is false.
  const credsChanged = cfg.backend !== v || cfg.token !== tok;
  cfg.backend = v;
  cfg.token = tok;
  // Cleaning can empty the field outright ("!!!"), which would leave this device
  // unnamed — keep the previous name in that case rather than writing a blank.
  const dev = cleanDevName($("backend-devname").value);
  if (dev) cfg.devname = dev;
  setupMode = false;
  // A save that paired this device, or re-paired it onto a different backend or
  // token, stays open on the voice step. A new backend's engines are unknown
  // even if this device was already paired to some other install, so the choice
  // is re-asked; everything the picker knows was learned before there were
  // credentials to ask with, so the status is re-fetched from scratch rather
  // than reused.
  const pairing = (wasUnpaired || credsChanged) && !needsSetup();
  if (pairing) {
    $("sheet-settings").classList.remove("setup");
    $("sheet-title").textContent = "Dictation";
    $("sheet-note").classList.add("hide");
    $("btn-settings-cancel").style.display = "";
    showVoiceStep(true);
    syncVoicePicker();
    revealVoiceStep();
    fetchVoiceStatus(true).then(() => {
      syncVoicePicker();
      syncVoiceConfirm();
      // Again once the answer is in: the notes that appear and disappear with it
      // change the section's height, and the first scroll aimed at the old one.
      revealVoiceStep();
    });
  } else {
    showSheet(false);
  }
  // Reload against the new backend so a bad URL surfaces straight away. Not
  // awaited either way: the list is being built behind the voice step, and is
  // there the moment it closes.
  loadSessions(true);
});
// Ends the first-run voice step. The tap is the choice — including the one the
// picker had already resolved on the user's behalf, which is written here so an
// engine confirmed during setup is stored exactly like one picked later from
// Settings, rather than left unset and re-resolved on every reload.
$("btn-voice-confirm").addEventListener("click", () => {
  const sel = $("voice-engine").querySelector("input:checked");
  if (!sel) return;
  if (cfg.voiceEngine !== sel.value) cfg.voiceEngine = sel.value;
  showVoiceStep(false);
  showSheet(false);
});
// Paints the picker from the stored choice and whatever the backend last said.
// Called twice per open — once from what is already known so the sheet is never
// blank, once when the status fetch lands.
function syncVoicePicker() {
  const rows = {
    parakeet: $("voice-row-parakeet"),
    whisper: $("voice-row-whisper"),
  };
  // No status is not the same as "not installed": an unreachable computer has
  // told us nothing. Both read as unavailable either way — there is nothing to
  // send audio to — but the note says which it is.
  const reachable = !!voiceStatus && !demoMode;
  // And "not paired yet" is a third thing again. On a first run there is no
  // computer to have been asked, so "not installed" and "unreachable" are both
  // claims about a machine we have never spoken to — and the user, reading
  // them, concludes their setup is broken before it has begun. The demo is left
  // on the unreachable wording: it genuinely has a backend it cannot use.
  const unpaired = !demoMode && needsSetup();
  for (const name of ["parakeet", "whisper"]) {
    const row = rows[name];
    const input = row.querySelector("input");
    const ok = reachable && voiceStatus.engines[name];
    row.classList.toggle("off", !ok);
    // Disabled rather than merely dimmed, except where it is the current choice:
    // an engine the user picked and the computer has since lost still has to
    // show as picked, or the sheet reads as having silently changed the setting
    // — which is exactly what the session fallback deliberately does not do.
    input.disabled = !ok && cfg.voiceEngine !== name;
    row.querySelector(".note").textContent = ok ? ""
      : unpaired ? "pair with your computer first"
      : reachable ? "not installed — run setup_voice.sh"
      : "computer unreachable";
  }
  // The stored choice when there is one, resolved otherwise: an unset device
  // shows the answer it is actually using rather than nothing selected at all.
  // Said out loud on the row too, because a selected radio with no explanation
  // reads as something the user chose and forgot rather than something the
  // computer answered — and the whole point of the unset state is that it
  // tracks the backend.
  const auto = !cfg.voiceEngine;
  const sel = cfg.voiceEngine || settledVoiceEngine();
  for (const input of $("voice-engine").querySelectorAll("input")) {
    const on = input.value === sel;
    input.checked = on;
    const note = input.parentNode.querySelector(".note");
    const hint = input.value === "phone" ? "the keyboard's own mic" : note.textContent;
    note.textContent = on && auto ? (hint ? hint + " · auto" : "auto") : hint;
  }
}

// Applies on the tap rather than on Save, for the same reason as the debug
// switch below: it is reached for when the current voice path is getting in the
// way, and Cancel must not put it back. Changing it mid-capture stops the
// recording outright — a microphone still running for the engine just switched
// away from would be the choice not having taken.
//
// Shared by both listeners below so they can never drift: a click and a change
// on the same tap must resolve to exactly the same state.
function applyVoicePick(v) {
  if (v !== "phone" && v !== "parakeet" && v !== "whisper") return;
  cfg.voiceEngine = v;
  // An explicit choice clears the session's fallback: the user is answering the
  // failure that set it, and the next tap must honour what they picked rather
  // than the verdict it replaced. Same for the not-installed cache, which the
  // next tap's probe re-establishes if it was right.
  voiceForcedPhone = false;
  recVoiceChecked = false;
  recVoiceNotSetup = false;
  stopListening();
  cancelRecording();
  // cancelRecording() already resyncs through recClearUI(); a change made with
  // nothing running is the case that needs telling.
  recSyncMic();
  // The picked row is now the stored one, so the auto hint has to come off it.
  syncVoicePicker();
  if (voiceStep) syncVoiceConfirm();
}
// click always fires on a tap; change only fires when the tap actually
// changed which radio is checked. A tap on a radio not already checked
// therefore fires both, in that order — the flag lets change recognize its
// own click and skip re-applying what click just applied.
let voicePickerClickedValue = null;
$("voice-engine").addEventListener("click", (e) => {
  const input = e.target && e.target.closest("input");
  if (!input || !$("voice-engine").contains(input)) return;
  voicePickerClickedValue = input.value;
  applyVoicePick(input.value);
});
$("voice-engine").addEventListener("change", (e) => {
  const v = e.target && e.target.value;
  if (v === voicePickerClickedValue) { voicePickerClickedValue = null; return; }
  applyVoicePick(v);
});
// Applies on the tap rather than on Save: the reason to reach for it is that
// something is already going wrong, and Cancel must not be able to lose it.
$("dbg-toggle").addEventListener("change", (e) => {
  cfg.debug = e.target.checked;
  setDebug(e.target.checked);
});
// Applies on the tap, same as the switches above: the key it adds or drops is
// this sheet's scrim sitting over the key bar, so nothing can be mid-press
// underneath when the rebuild lands.
$("search-toggle").addEventListener("change", (e) => {
  cfg.scrollbackSearchOn = e.target.checked;
  if (!e.target.checked) closeSearch();
  buildKeybar();
});
$("alt-toggle").addEventListener("change", (e) => {
  cfg.altKeyOn = e.target.checked;
  buildKeybar();
});
// Applies on the tap, like the voice picker and debug switch: the row appears
// and disappears behind the sheet as the switch moves, and Cancel must not
// take that back.
$("snip-toggle").addEventListener("change", (e) => {
  cfg.snippetsOn = e.target.checked;
  $("snip-edit").classList.toggle("show", e.target.checked);
  syncSnipbar();
});
// Saved as typed — the box is the storage, line for line.
$("snip-text").addEventListener("input", (e) => {
  cfg.snippets = e.target.value;
  syncSnipbar();
});
$("backend-token").addEventListener("input", (e) => {
  const input = e.target;
  // 0→O and 1→I before filtering: the code on screen has no zeros or ones, so
  // typing one means the user read a look-alike letter — swallowing it made the
  // field appear stuck. One mapper for both counts keeps the caret math honest.
  const keep = (s) => s.toUpperCase().replace(/0/g, "O").replace(/1/g, "I")
    .replace(/[^A-Z2-7]/g, "");
  const rawBefore = keep(input.value.slice(0, input.selectionStart)).length;
  const raw = keep(input.value).slice(0, 10);
  const formatted = raw.length > 5 ? raw.slice(0, 5) + "-" + raw.slice(5) : raw;
  input.value = formatted;
  let pos = rawBefore;
  if (rawBefore > 5) pos += 1;   // account for the dash now sitting before the caret
  input.setSelectionRange(pos, pos);
});

