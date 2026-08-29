// ============================================================
// Session list
// ============================================================
// A row's trash button asks with the same confirm() dialog the session
// sheet's kill row uses — one idiom for "sure?" wherever a kill can start.
function trashBtn(s) {
  return el("button", {
    class: "icon-btn btn-trash", type: "button",
    "aria-label": "Kill session " + s.name,
    onclick: (e) => {
      e.stopPropagation();
      if (confirm("Kill '" + s.name + "'? Programs running in it are terminated.")) {
        killSession(s.name);
      }
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

async function loadSessions(spin=false) {
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
  } catch (e) {
    $("list").innerHTML = "";
    if (needsSetup()) $("list").appendChild(demoCard());
    $("list-empty").style.display = "none";
    $("list-error").style.display = "block";
    toast("Couldn't load sessions");
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
      el("div", { class: "chev" }, svgIcon("i-fwd")),
    ),
    el("div", { class: "item-meta" }, el("span", {}, "offline · nothing here is real")),
  );
}

function renderSessions(sessions) {
  const list = $("list");
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

    const card = el("div", { class: "item", onclick: () => openTerminal(s.name) },
      el("div", { class: "item-head" },
        el("div", { class: "item-dot" + (s.attached ? " live" : "") }),
        title,
        bell,
        edit,
        trashBtn(s),
        el("div", { class: "chev" }, svgIcon("i-fwd")),
      ),
      meta,
    );
    list.appendChild(card);
  }
  if (needsSetup()) list.appendChild(demoCard());
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
// button. Confirmation is each caller's own concern (both put up the same
// confirm() dialog) — by the time this runs the user has already said yes.
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
  if (!confirm("Kill '" + s.name + "'? Programs running in it are terminated.")) return;
  if (await killSession(s.name)) showSheet(false);
}

$("btn-session-cancel").addEventListener("click", () => showSheet(false));
$("btn-session-save").addEventListener("click", saveSessionSheet);
$("btn-session-kill").addEventListener("click", killSheetSession);

// The folder is sticky because the next session is usually started in the same
// project as the last one.
function openNewSession() {
  $("new-name").value = "";
  $("new-dir").value = cfg.newdir;
  showSheet(true, "sheet-new");
  $("new-name").focus();
}

async function createSession() {
  const name = $("new-name").value.trim();
  if (!name) { toast("Enter a name"); return; }
  // tmux reads these as window/pane separators, so a name carrying one never
  // addresses the session it looks like. The server rejects them too.
  for (const c of [".", ":"]) {
    if (name.includes(c)) { toast("Names can't contain " + c); return; }
  }
  const dir = $("new-dir").value.trim();
  try {
    const r = await fetch(apiURL("api/session"), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ name: name, dir: dir }),
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) { toast(data && data.error ? data.error : "Couldn't create the session"); return; }
    cfg.newdir = dir;
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
    startY = window.scrollY <= 0 ? e.touches[0].clientY : null;
  }, { passive: true });
  scr.addEventListener("touchend", (e) => {
    if (startY !== null && e.changedTouches[0].clientY - startY > 70) loadSessions(true);
    startY = null;
  }, { passive: true });
})();

