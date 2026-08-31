// ============================================================
// Session list
// ============================================================
// Both kill entry points — a row's trash button and the session sheet's kill
// row — ask the same question through the same themed dialog.
function confirmKill(name) {
  return appConfirm("Kill '" + name + "'? Programs running in it are terminated.",
                    { confirmLabel: "Kill" });
}

function trashBtn(s) {
  return el("button", {
    class: "icon-btn btn-trash", type: "button",
    "aria-label": "Kill session " + s.name,
    onclick: async (e) => {
      e.stopPropagation();
      if (await confirmKill(s.name)) killSession(s.name);
    },
  }, svgIcon("i-trash"));
}

// The bell's three faces — off dim, "on" lit, "quiet" lit but slashed — set
// in one place so the first render and every later tap agree on glyph, class,
// and spoken label alike.
function setBellState(btn, mode, name) {
  btn.classList.toggle("on", mode === "on");
  btn.classList.toggle("quiet", mode === "quiet");
  const icon = mode === "quiet" ? "#i-bell-off" : "#i-bell";
  const use = btn.querySelector("use");
  use.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", icon);
  use.setAttribute("href", icon);
  btn.setAttribute("aria-label",
    mode === "on" ? "Notifications with sound for " + name :
    mode === "quiet" ? "Silent notifications for " + name :
    "Notifications off for " + name);
}

// ---- unread marks -----------------------------------------------------------
// A session whose watcher flipped to "waiting" while it was not the one on
// screen is marked unread until it is opened. Detection is the transition, not
// the state: successive /api/sessions payloads are diffed here, because this
// is the only place another session's state ever reaches this client — the
// terminal's own control frames are always the open session's (seen by
// definition), and a Web Push never hands the page a payload, only the
// notification tap that opens the session anyway. Marks persist so a reload
// keeps them; the in-memory baseline does not, so the first list after a
// reload can never re-mark what is merely still waiting.
const UNREAD_KEY = "pockettui_unread";
function unreadMap() {
  try {
    const m = JSON.parse(localStorage.getItem(UNREAD_KEY));
    if (m && typeof m === "object") return m;
  } catch (e) {}
  return {};
}
function saveUnread(m) {
  try { localStorage.setItem(UNREAD_KEY, JSON.stringify(m)); } catch (e) {}
}
// Opening the session is what marks it read — openTerminal calls this. The
// row's class comes off in place too, so the clear never waits on a reload.
function clearUnread(name) {
  const m = unreadMap();
  if (name in m) { delete m[name]; saveUnread(m); }
  const row = $("list").querySelector('.item[data-name="' + CSS.escape(name) + '"]');
  if (row) row.classList.remove("unread");
}
// Watcher states as of the previous payload; null until one has arrived.
let lastStates = null;
// Diffs a fresh payload against the baseline: prunes marks for sessions that
// no longer exist, marks new arrivals into "waiting" — never the session on
// screen, whose state changes are seen as they happen — and returns the map
// for renderSessions to paint from.
function noteUnread(sessions) {
  const m = unreadMap();
  let changed = false;
  const names = new Set(sessions.map(s => s.name));
  for (const n of Object.keys(m)) {
    if (!names.has(n)) { delete m[n]; changed = true; }
  }
  // The other per-session thing this client holds: a stashed reader or editor
  // (fileViews, 09-image-viewer.js). Pruned against the same payload and for
  // the same reason — a session that is gone takes its marks and its file view
  // with it, however it went.
  keepFileViews(names);
  if (lastStates) {
    for (const s of sessions) {
      if (s.state === "waiting" && lastStates.get(s.name) !== "waiting"
          && s.name !== currentSession && !(s.name in m)) {
        m[s.name] = Date.now();
        changed = true;
      }
    }
  }
  lastStates = new Map(sessions.map(s => [s.name, s.state || ""]));
  if (changed) saveUnread(m);
  return m;
}

// quiet suppresses the failure toast: the wide layout's periodic refresh
// reloads a list nobody asked about, and a backend that has gone away already
// reports through the terminal's own banner rather than a toast per tick.
async function loadSessions(spin=false, quiet=false) {
  // Nothing to query yet — prompt instead of failing against the static host.
  if (needsSetup()) { openSettings(true); return; }
  const btn = $("btn-reload");
  if (spin) btn.classList.add("spin");
  try {
    const r = await fetch(apiURL("api/sessions"), { cache: "no-store", headers: authHeaders() });
    if (r.status === 401) { rejectToken(); return; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    renderSessions(data.sessions || []);
    $("list-error").style.display = "none";
    // The fetched list rides back to callers with a session to verify
    // (openSessionByName); everyone else ignores it.
    return data.sessions || [];
  } catch (e) {
    $("list").innerHTML = "";
    if (needsSetup()) $("list").appendChild(demoCard());
    $("list-empty").style.display = "none";
    $("list-error").style.display = "block";
    if (!quiet) toast("Couldn't load sessions");
  } finally {
    btn.classList.remove("spin");
  }
}

// Always last in the list while unpaired: an offline terminal to look around
// in before there is a real backend to reach. Once paired it has a machine of
// its own to show instead, so the demo row drops out — needsSetup() is the
// same paired/configured check the rest of the app uses, so this tracks a
// Forget exactly like every other paired-only surface does.
function demoCard() {
  return el("div", { class: "item demo", onclick: () => openDemo() },
    el("div", { class: "item-head" },
      el("div", { class: "title" }, "Try demo"),
    ),
    el("div", { class: "item-meta" }, el("span", {}, "offline · nothing here is real")),
  );
}

function renderSessions(sessions) {
  const list = $("list");
  const unread = noteUnread(sessions);
  list.innerHTML = "";
  $("list-empty").style.display = sessions.length ? "none" : "block";
  for (const s of sessions) {
    const meta = el("div", { class: "item-meta" });
    if (s.command) {
      meta.appendChild(el("span", { class: "cmd" }, s.command));
      meta.appendChild(el("span", { class: "sep" }, "·"));
    }
    meta.appendChild(el("span", {}, s.windows + (s.windows === 1 ? " window" : " windows")));
    meta.appendChild(el("span", { class: "sep" }, "·"));
    meta.appendChild(el("span", {}, relTime(s.created)));
    // The pane watcher's verdict: amber says the session is waiting on its
    // human, green that it is producing output right now. Idle earns nothing.
    if (s.state === "waiting") {
      meta.appendChild(el("span", { class: "sep" }, "·"));
      meta.appendChild(el("span", { class: "state waiting" }, "needs input"));
    } else if (s.state === "active") {
      meta.appendChild(el("span", { class: "sep" }, "·"));
      meta.appendChild(el("span", { class: "state active" }, "running"));
    }

    // With an alias set it becomes the row's title and the real tmux name moves
    // to a quiet second line — the name their tooling uses is still visible.
    const title = el("div", { class: "title" }, s.alias || s.name);
    if (s.alias) title.appendChild(el("div", { class: "realname" }, s.name));

    const edit = el("button", {
      class: "icon-btn btn-alias", type: "button",
      "aria-label": "Rename or kill " + s.name,
      onclick: (e) => { e.stopPropagation(); openSessionSheet(s); },
    }, svgIcon("i-pencil"));

    // Per-session notification mode, cycled off → with sound → silent. The
    // tap is the user gesture the permission and push-subscription asks need,
    // so both live in the handler.
    const bell = el("button", {
      class: "icon-btn btn-bell", type: "button",
      onclick: (e) => { e.stopPropagation(); toggleNotify(s, e.currentTarget); },
    }, svgIcon("i-bell"));
    setBellState(bell, s.notify, s.name);

    // data-name lets the wide layout's rail mark the open session's row
    // without a rebuild (markSelectedSession below); .selected only renders
    // there — the phone never shows the list and a terminal together.
    const card = el("div", {
      class: "item" + (s.name === currentSession ? " selected" : "")
                    + (unread[s.name] ? " unread" : ""),
      "data-name": s.name,
      onclick: () => openTerminal(s.name),
    },
      el("div", { class: "item-head" },
        el("div", { class: "item-dot" + (s.attached ? " live" : "") }),
        title,
        bell,
        edit,
        trashBtn(s),
      ),
      meta,
    );
    list.appendChild(card);
  }
  if (needsSetup()) list.appendChild(demoCard());
}

// Repaints the rail's selected row in place — openTerminal and closeTerminal
// call it so a switch never waits on a list rebuild. The demo card carries no
// data-name, so it can never read as selected.
function markSelectedSession() {
  for (const row of $("list").children) {
    row.classList.toggle("selected",
      !!currentSession && row.dataset.name === currentSession);
  }
}

// The session sheet edits both names at once and carries the kill row. The
// alias is a display name held on the tmux session itself (it follows the
// session to every device); the tmux name is the real one the user's own
// tooling addresses — renaming changes it everywhere, and the alias survives.
let sheetSession = null;

function openSessionSheet(s) {
  sheetSession = s;
  $("session-title").textContent = s.name;
  $("session-alias").value = s.alias || "";
  $("session-name").value = s.name;
  showSheet(true, "sheet-session");
}

async function saveSessionSheet() {
  const s = sheetSession;
  if (!s) return;
  const alias = $("session-alias").value.trim();
  const name = $("session-name").value.trim();
  // Rename first: if the alias also changed it must be set against the name
  // the session is about to have, not the one it is leaving behind.
  let target = s.name;
  try {
    if (name && name !== s.name) {
      const r = await fetch(apiURL("api/session/rename"), {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ session: s.name, name: name }),
      });
      const data = await r.json().catch(() => null);
      if (!r.ok) { toast(data && data.error ? data.error : "Couldn't rename the session"); return; }
      target = data.session;
      // The session is the same one, under a new name: its stashed file view
      // moves with it. Before the reload below, whose prune would otherwise
      // read the old name's disappearance as the session being gone and throw
      // an unsaved buffer away.
      renameFileView(s.name, target);
    }
    if (alias !== (s.alias || "")) {
      const r = await fetch(apiURL("api/alias"), {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ session: target, alias: alias }),
      });
      const data = await r.json().catch(() => null);
      if (!r.ok) { toast(data && data.error ? data.error : "Couldn't set the name"); return; }
    }
    showSheet(false);
    loadSessions();
  } catch (e) {
    toast("Couldn't save");
  }
}

// The actual kill, shared by the sheet's own button and each row's trash
// button. Confirmation is each caller's own concern (both ask through
// confirmKill()) — by the time this runs the user has already said yes.
// Returns whether it succeeded, so a caller with its own UI to close (the
// sheet) only closes it once the kill actually lands.
async function killSession(name) {
  try {
    const r = await fetch(apiURL("api/session/kill"), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ session: name }),
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) { toast(data && data.error ? data.error : "Couldn't kill the session"); return false; }
    // The name is free again the moment the kill lands, so a stashed file view
    // for it is dropped here rather than left to the refresh below: the next
    // session created under this name must not open onto this one's buffer.
    dropFileView(name);
    loadSessions();
    return true;
  } catch (e) {
    toast("Couldn't kill the session");
    return false;
  }
}

async function killSheetSession() {
  const s = sheetSession;
  if (!s) return;
  if (!(await confirmKill(s.name))) return;
  if (await killSession(s.name)) showSheet(false);
}

$("btn-session-cancel").addEventListener("click", () => showSheet(false));
$("btn-session-save").addEventListener("click", saveSessionSheet);
$("btn-session-kill").addEventListener("click", killSheetSession);

// "sess-0831-2143" — no "." or ":" so it clears the tmux separator rule below.
function defaultSessionName() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return "sess-" + p(d.getMonth() + 1) + p(d.getDate()) + "-" +
         p(d.getHours()) + p(d.getMinutes());
}

function openNewSession() {
  $("new-name").value = "";
  // Show the name an empty field will produce, not a made-up example.
  $("new-name").placeholder = defaultSessionName();
  showSheet(true, "sheet-new");
  $("new-name").focus();
}

async function createSession() {
  const typed = $("new-name").value.trim();
  // tmux reads these as window/pane separators, so a name carrying one never
  // addresses the session it looks like. The server rejects them too.
  for (const c of [".", ":"]) {
    if (typed.includes(c)) { toast("Names can't contain " + c); return; }
  }
  const base = typed || defaultSessionName();
  try {
    let data = null, r = null;
    // Two auto-named sessions in the same minute collide; suffix past it rather
    // than blaming the user for a name they never chose.
    for (let attempt = 1; attempt <= 5; attempt++) {
      const name = attempt === 1 ? base : base + "-" + attempt;
      r = await fetch(apiURL("api/session"), {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ name: name, dir: "" }),
      });
      data = await r.json().catch(() => null);
      const dup = data && data.error && data.error.includes("already exists");
      if (r.ok || typed || !dup) break;
    }
    if (!r.ok) { toast(data && data.error ? data.error : "Couldn't create the session"); return; }
    showSheet(false);
    $("new-name").value = "";
    openTerminal(data.session);
  } catch (e) {
    toast("Couldn't create the session");
  }
}

$("btn-new").addEventListener("click", openNewSession);
$("btn-new-cancel").addEventListener("click", () => showSheet(false));
$("btn-new-create").addEventListener("click", createSession);

$("btn-reload").addEventListener("click", () => loadSessions(true));

// Pull-down to refresh — only when already scrolled to the top of the list.
(function() {
  let startY = null;
  const scr = $("screen-list");
  scr.addEventListener("touchstart", (e) => {
    // In the wide layout the rail scrolls itself rather than the page.
    const top = isWideLayout() ? scr.scrollTop : window.scrollY;
    startY = top <= 0 ? e.touches[0].clientY : null;
  }, { passive: true });
  scr.addEventListener("touchend", (e) => {
    if (startY !== null && e.changedTouches[0].clientY - startY > 70) loadSessions(true);
    startY = null;
  }, { passive: true });
})();

