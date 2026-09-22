// ============================================================
// Debug log
// ============================================================
// A phone has no console, and an error toast can be gone before it is read. This
// is the whole substitute: an opt-in on-screen tail of what the app just did.
// Off by default and off in every normal session, so the guard below is the
// contract — one boolean read and a return, cheap enough to call from a keydown
// handler or the viewport listener without thinking about it.
//
// The flag is a plain variable rather than a localStorage read per call: dbg()
// sits on paths that fire per keystroke, and localStorage is synchronous.
let dbgOn = localStorage.getItem("pockettui_debug") === "1";
const dbgBuf = [];
const DBG_MAX = 100;

function dbgFormat(p) {
  if (typeof p === "string") return p;
  if (p instanceof Error) return p.name + ": " + p.message;
  if (p === null || p === undefined || typeof p !== "object") return String(p);
  // Cyclic structures and DOM nodes both throw here; a placeholder beats losing
  // the whole line.
  try { return JSON.stringify(p); } catch (e) { return "[unserializable]"; }
}

function dbg(...parts) {
  if (!dbgOn) return;
  const d = new Date();
  const stamp = String(d.getMinutes()).padStart(2, "0") + ":" +
                String(d.getSeconds()).padStart(2, "0") + "." +
                String(d.getMilliseconds()).padStart(3, "0");
  const line = stamp + "  " + parts.map(dbgFormat).join(" ");
  dbgBuf.push(line);
  if (dbgBuf.length > DBG_MAX) dbgBuf.shift();
  dbgSend(line);
  const p = $("dbg-panel");
  if (!p) return;
  p.textContent = dbgBuf.join("\n");
  // Newest at the bottom, so the tail is what stays in view.
  p.scrollTop = p.scrollHeight;
}

// The panel is on the device, and the device is where the bug is: a measurement
// that exists only on that screen cannot be read from the machine running the
// server. So every line the panel gets is also queued for the server's journal,
// batched to at most one request a second — dbg() sits on per-keystroke paths,
// and a fetch per keystroke is not a debug aid. Both the queue and the timer
// only ever exist while the setting is on, because dbg() returns before this on
// the way in.
const dbgQueue = [];
const DBG_SEND_MAX = 50;
let dbgFlushTimer = null;

function dbgSend(line) {
  dbgQueue.push(line);
  // The server takes 50 lines per request; anything past that is dropped here
  // rather than sent to be dropped there.
  if (dbgQueue.length > DBG_SEND_MAX) dbgQueue.shift();
  if (dbgFlushTimer === null) dbgFlushTimer = setTimeout(dbgFlush, 1000);
}

function dbgFlush() {
  clearTimeout(dbgFlushTimer);
  dbgFlushTimer = null;
  if (!dbgQueue.length) return;
  const lines = dbgQueue.splice(0, dbgQueue.length);
  // Swallowed whole: no toast, and above all no dbg() — a line about a failed
  // flush would refill the queue that just failed to flush.
  try {
    fetch(apiURL("api/dbg"), {
      method: "POST",
      keepalive: true,
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ dev: cfg.devname, lines }),
    }).catch(() => {});
  } catch (e) {}
}

// The last second of lines is the interesting one when a phone is being put
// away or reloaded; keepalive is what lets the request outlive the page.
window.addEventListener("pagehide", dbgFlush);

// Built on first enable and then kept — toggling off hides it rather than tearing
// it down, since the only cost while hidden is one detached-from-view element.
function dbgPanel() {
  let p = $("dbg-panel");
  if (!p) {
    p = el("div", { id: "dbg-panel" });
    document.body.appendChild(p);
  }
  return p;
}

// Attached once, unconditionally: dbg() self-gates, so there is nothing to
// detach when the setting goes off and no way for these to leak while it is off.
window.addEventListener("error", (e) => {
  dbg("window.onerror:", e.message, (e.filename || "?") + ":" + e.lineno);
});
window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason;
  dbg("unhandled rejection:", r instanceof Error ? r : dbgFormat(r));
});

function setDebug(on) {
  dbgOn = !!on;
  if (dbgOn) {
    dbgPanel().style.display = "";
    // The cache version is the only build stamp the app carries, and it is what
    // identifies which shell a report came from. Read through a function because
    // the const itself is declared with the service-worker code at the far end of
    // this script, after the boot path that restores this setting has run.
    dbg("debug on — build", buildVersion());
  } else {
    const p = $("dbg-panel");
    if (p) { p.style.display = "none"; p.textContent = ""; }
    dbgBuf.length = 0;
    dbgQueue.length = 0;
    clearTimeout(dbgFlushTimer);
    dbgFlushTimer = null;
  }
}
function buildVersion() {
  try { return SW_VERSION; } catch (e) { return "(booting)"; }
}
function relTime(ts) {
  const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if (s < 60) return s + "s ago";
  const m = Math.floor(s/60); if (m < 60) return m + "m ago";
  const h = Math.floor(m/60); if (h < 24) return h + "h ago";
  const d = Math.floor(h/24); if (d < 30) return d + "d ago";
  return Math.floor(d/30) + "mo ago";
}

// Build-time placeholder. Three cases:
//   "same-origin" — served by the backend itself (app.py substitutes this), so
//                   the API lives on this origin and no setup is needed.
//   "https://..." — a backend was baked in at build time.
//   ""            — public static build: the backend is unknown, so the app
//                   asks for it on first run and remembers it in localStorage.
// An unsubstituted placeholder (opening the template directly) behaves as
// same-origin, which is what a local dev server wants.
const BUILD_BACKEND = "__BACKEND_URL__";
const SAME_ORIGIN = BUILD_BACKEND === "same-origin" || BUILD_BACKEND.indexOf("__") === 0;
const DEFAULT_BACKEND = SAME_ORIGIN ? "" : BUILD_BACKEND;

// ============================================================
// Connection profiles
// ============================================================
// One phone, several computers. A profile is one machine this device is paired
// with — {id, name, backend, token} — and exactly one of them is active: every
// URL the app builds, every header it sends and every session it lists belongs
// to whichever that is. cfg.backend and cfg.token below are views onto the
// active profile rather than keys of their own, so the forty-odd places that
// ask for "the backend" keep asking the same question and get this one's answer.
//
// Switching is a reconnect and never a reattach — see switchProfile()
// (40-profiles.js), which closes whatever session is open, drops every piece of
// per-machine state this shell is holding, and loads the chosen computer's list.
const PROFILES_KEY = "pockettui_profiles";
const ACTIVE_PROFILE_KEY = "pockettui_profile";

// Bumped by every switch. Anything that asks one computer a question and paints
// the answer (loadSessions, fetchServerVersion) carries the generation it asked
// under and drops an answer that lands after the machine changed under it — the
// same guard sockGen is for the terminal's socket.
let profileGen = 0;

function newProfileId() {
  try { return crypto.randomUUID(); } catch (e) {}
  return "p" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

// Anything unreadable reads as "no profiles", which is first-run territory —
// the same answer an empty store gives, and the only safe one when the store
// is something this build cannot parse.
function readProfiles() {
  try {
    const list = JSON.parse(localStorage.getItem(PROFILES_KEY));
    if (Array.isArray(list)) return list.filter((p) => p && typeof p === "object" && p.id);
  } catch (e) {}
  return [];
}

function writeProfiles(list) {
  try { localStorage.setItem(PROFILES_KEY, JSON.stringify(list)); } catch (e) {}
}

function activeProfileId() { return localStorage.getItem(ACTIVE_PROFILE_KEY) || ""; }

// The profile every cfg.backend/cfg.token read answers from, or null when this
// device is paired with nothing at all: a first run, or the last computer
// forgotten.
function activeProfile() {
  const id = activeProfileId();
  return readProfiles().find((p) => p.id === id) || null;
}

function setActiveProfile(id) {
  localStorage.setItem(ACTIVE_PROFILE_KEY, id);
  mirrorLegacyKeys();
}

function updateProfile(id, patch) {
  const list = readProfiles();
  const p = list.find((x) => x.id === id);
  if (!p) return null;
  Object.assign(p, patch);
  writeProfiles(list);
  if (id === activeProfileId()) mirrorLegacyKeys();
  return p;
}

// `name` is the user's rename and nothing else; `host` is what the computer
// called itself the last time it said (see learnProfileHost). Two fields rather
// than one because they answer to different people: a name typed here must
// survive whatever the server says next, and a server's own name must not be
// mistaken for a rename nobody made.
function addProfile(fields) {
  const p = Object.assign({ id: newProfileId(), name: "", host: "", backend: "", token: "" },
                          fields || {});
  const list = readProfiles();
  list.push(p);
  writeProfiles(list);
  return p;
}

function removeProfile(id) {
  writeProfiles(readProfiles().filter((p) => p.id !== id));
  // The marks were about that machine's sessions, and nothing will ever ask
  // about them again.
  dropProfileUnread(id);
  if (id === activeProfileId()) localStorage.removeItem(ACTIVE_PROFILE_KEY);
  mirrorLegacyKeys();
}

// The address a profile is reached at, written the short way: host and port,
// no scheme. With the port, since two of these can be one machine serving
// twice and the port is then the only thing telling them apart. An empty
// address is a shell the backend served itself, so the page's own host is the
// honest answer there.
function profileHost(backend) {
  if (!backend) return location.host;
  try { return new URL(backend).host; } catch (e) { return backend; }
}

// The same address read as a name: the first label of the host. A port is part
// of where a computer is rather than of what it is called, and so is the rest of
// the domain — "studio.example.net" is an address, "studio" is the
// computer, and the whole string in a name field reads as the address it is.
// A literal is kept entire: an IP has no first label worth taking, and a
// quarter of one names nothing. Anything with nothing to cut — "localhost", a
// bare hostname — comes back as itself.
function profileHostname(backend) {
  let host = location.hostname;
  if (backend) {
    try { host = new URL(backend).hostname; } catch (e) { return backend; }
  }
  // URL.hostname hands back IPv6 bracketed, which is what makes it recognisable
  // in one character; IPv4 is four numbers and nothing else.
  if (host.indexOf("[") === 0 || /^\d{1,3}(\.\d{1,3}){3}$/.test(host)) return host;
  return host.split(".")[0] || host;
}

// What a profile is called, everywhere it is named — the switcher, the menu,
// the list in Settings and the name field itself, so the four always agree.
// Three answers in order of who said it: the user, who renamed it; the computer,
// which reported its own hostname (`host`, /api/version); and failing both the
// address, which is the one thing there always is. Never blank for a profile
// with an address, which is why the name field can be filled from it.
function profileLabel(p) {
  return p ? (p.name || p.host || profileHostname(p.backend)) : "";
}

// Setting a credential with no profile to hold it makes one: the first Save of
// a first run, and a pairing link that arrives before any Save.
function writeActiveProfile(field, v) {
  const p = activeProfile();
  if (p) { updateProfile(p.id, { [field]: v }); return; }
  setActiveProfile(addProfile({ [field]: v }).id);
}

// A host -> zoom-factor map with everything that is not one of those thrown
// away, used on the way in and on the way out of cfg.browserZoom: what is read
// back is whatever survived the last write, and a hand-edited or older record
// must not put a factor the pane cannot use into the frame.
function browserZoomClean(v) {
  const out = {};
  if (!v || typeof v !== "object") return out;
  for (const h of Object.keys(v)) {
    const z = v[h];
    if (typeof z === "number" && Number.isFinite(z) && z >= 0.25 && z <= 5 && z !== 1) {
      out[h] = z;
    }
  }
  return out;
}

// The column's rows as the column can hold them: instance ids of the three pane
// types and no other string, each of them once, and two at the most — the column
// beside the terminal stacks no deeper, and it holds at most two of a kind, so
// "files" and "files#2" are the only two names an explorer row can have
// (26-side-pane.js). Both halves of the record go through this, because a record
// is only worth writing in the shape it will be read back in, and a hand-edited
// or older one must not name a row the column cannot draw.
const SIDE_ID_RE = /^(diff|files|browser)(#2)?$/;

function sideRowsClean(v) {
  const out = [];
  if (!Array.isArray(v)) return out;
  for (const t of v) {
    if (typeof t !== "string" || !SIDE_ID_RE.test(t)) continue;
    if (out.includes(t)) continue;
    out.push(t);
    if (out.length === 2) break;
  }
  return out;
}

// One browser row's own half of that record: the tabs in the strip's order,
// which of them was on screen, and the address that one was on. The cap is the
// strip's own (BROWSER_TAB_MAX, 42-browser.js) spelled out rather than read:
// this runs while the shell is still loading its fragments, and that one is
// declared in a later fragment than this.
//
// Cleaned with the index rather than beside it: dropping an entry moves every
// tab after it up one, and an index left counting against the record as it was
// written names the tab next to the one that was on screen — the pane then sends
// that one to `url` and shows the same page twice.
function browserRecClean(v) {
  const raw = v && Array.isArray(v.tabs) ? v.tabs : [];
  const want = v && typeof v.tab === "number" && v.tab >= 0 ? Math.trunc(v.tab) : 0;
  const tabs = [];
  let at = 0;
  // Either shape an entry may have been written in: the address alone, or the
  // address with the mode that tab was left in (the computer's own network,
  // 42-browser.js). The pair comes back out of here whichever went in, so the
  // pane has one shape to read.
  for (let i = 0; i < raw.length && tabs.length < 8; i++) {
    const e = raw[i];
    const u = typeof e === "string" ? e.trim()
            : (e && typeof e.url === "string" ? e.url.trim() : "");
    if (!u) continue;
    if (i <= want) at = tabs.length;
    tabs.push({ url: u, lan: !!(e && e.lan) });
  }
  return {
    url: v && typeof v.url === "string" ? v.url : "",
    tabs: tabs,
    tab: Math.min(at, Math.max(0, tabs.length - 1)),
  };
}

// The same, written out: the pair only where there is something more to say than
// the address, so a shell too old to know about the mode still reads those tabs
// back as strings.
function browserRecWrite(v) {
  if (!v || typeof v !== "object") return null;
  const out = {};
  if (typeof v.url === "string" && v.url) out.url = v.url;
  if (Array.isArray(v.tabs)) {
    out.tabs = v.tabs.map((t) => {
      if (typeof t === "string") return t || null;
      if (!t || typeof t.url !== "string" || !t.url) return null;
      return t.lan ? { url: t.url, lan: true } : t.url;
    }).filter((t) => t).slice(0, 8);
    out.tab = Math.min(Math.max(0, Math.trunc(v.tab) || 0),
                       Math.max(0, out.tabs.length - 1));
  }
  return out.url || out.tabs ? out : null;
}

// A diff row's half is which of its two lists was on screen, and nothing else:
// the changes themselves are the repo's and are re-read at every open.
function diffRecClean(v) {
  return { tab: v && v.tab === "untracked" ? "untracked" : "tracked" };
}

// Only what differs from what the getter would have assumed, so a column of
// panes left as they open writes no halves at all.
function diffRecWrite(v) {
  return v && v.tab === "untracked" ? { tab: "untracked" } : null;
}

// The active profile's credentials, mirrored into the two keys this app used
// before profiles existed. Nothing here reads them back — it is cheap insurance
// for an older shell still cached on the same origin, which knows only those.
function mirrorLegacyKeys() {
  const p = activeProfile();
  localStorage.setItem("pockettui_backend", p ? p.backend || "" : "");
  localStorage.setItem("pockettui_token", p ? p.token || "" : "");
}

// One-time, and before anything reads cfg: a device paired the old way holds one
// backend and one token under those keys, and that pairing becomes profile one
// rather than being asked for all over again. Empty on both is a device that was
// never paired, which is a first run either way and needs no profile invented
// for it. Drop once no old installs are left, the way boot-theme.js's rename is.
function migrateProfiles() {
  if (localStorage.getItem(PROFILES_KEY) !== null) return;
  const backend = localStorage.getItem("pockettui_backend") || "";
  const token = localStorage.getItem("pockettui_token") || "";
  if (!backend && !token) return;
  setActiveProfile(addProfile({ backend: backend, token: token }).id);
}
migrateProfiles();

const cfg = {
  // The active profile's address, or whatever the build itself knows when this
  // device is paired with nothing yet — the fallback this getter always had. A
  // profile with no address of its own means same-origin, which on a build with
  // nothing baked in is the same empty string.
  get backend() {
    const p = activeProfile();
    return (p ? p.backend : "") || DEFAULT_BACKEND;
  },
  set backend(v) { writeActiveProfile("backend", v); },
  get token() {
    const p = activeProfile();
    return (p ? p.token : "") || "";
  },
  set token(v) { writeActiveProfile("token", v); },
  // Names this device's own grouped view session (<devname>-<target>), so
  // two devices watching one session never detach each other. Minted once and
  // kept, because a name that changed per visit would strand a view per reload.
  get devname() {
    let v = localStorage.getItem("pockettui_devname");
    if (!v) {
      const r = crypto.getRandomValues(new Uint8Array(4));
      v = "device-" + Array.from(r, (b) => "abcdefghijklmnopqrstuvwxyz0123456789"[b % 36]).join("");
      localStorage.setItem("pockettui_devname", v);
    }
    return v;
  },
  set devname(v) { localStorage.setItem("pockettui_devname", v); },
  // Which engine the mic key talks to: "phone", "parakeet" or "whisper". Empty
  // when the user has never chosen, which is not the same as any of the three —
  // an unset device asks the backend which engine it is actually running and
  // follows that, so a computer with voice installed needs no visit to Settings
  // at all. resolveVoiceEngine() is where that resolution lives.
  get voiceEngine() {
    const v = localStorage.getItem("pockettui_voice_engine");
    return v === "phone" || v === "parakeet" || v === "whisper" ? v : "";
  },
  set voiceEngine(v) { localStorage.setItem("pockettui_voice_engine", v); },
  // The switch this replaced, read once and never written. A device that had
  // turned local transcription off was saying "phone dictation", and that answer
  // survives the upgrade rather than being silently reversed by the new
  // setting's ask-the-backend default.
  get legacyLocalVoiceOff() { return localStorage.getItem("pockettui_localvoice") === "0"; },
  get debug() { return localStorage.getItem("pockettui_debug") === "1"; },
  set debug(v) {
    if (v) localStorage.setItem("pockettui_debug", "1");
    else localStorage.removeItem("pockettui_debug");
  },
  // Whether the alt modifier key appears in the key bar. Off by default: it is
  // a key-bar slot most sessions never reach for.
  get altKeyOn() { return localStorage.getItem("pockettui_alt_on") === "1"; },
  set altKeyOn(v) {
    if (v) localStorage.setItem("pockettui_alt_on", "1");
    else localStorage.removeItem("pockettui_alt_on");
  },
  // Which panes, if any, are split out beside the terminal on a wide layout —
  // the git changes ("diff"), the file explorer ("files"), the in-app browser
  // ("browser") — as the column's rows, the top one first, and the session
  // they were open in. One key rather than one per pane because it is one
  // column (26-side-pane.js), and the order is half of what has to come back:
  // two rows restored the other way up are not the column that was left. The
  // browser keeps the page it was on in the same record, since a pane restored
  // to a blank frame would have lost the whole of what it was showing. The
  // session is half the answer because the column is that session's own: a
  // reload brings it back for that session and for no other. Null by default:
  // the whole pane is the terminal's until something asks for the split. The
  // width and the seam between the rows are shared for the same reason, and
  // are 0 until one has been dragged, which reads as "half" at the next open.
  //
  // Two older shapes come back through here. The first build wrote the owner
  // as a bare string, with no session to reopen it in — JSON.parse rejects it,
  // and a pane belonging to nobody is a pane that is not reopened. The
  // one-slot build after it wrote { owner, session }: one pane and no order,
  // which is this record with a single row. The build after that kept the
  // browser's half in flat keys beside the rows, which is instance "browser"'s
  // half and is read back as that.
  //
  // `panes` always has an entry for every row that can carry one, filled with
  // defaults where the record said nothing, so a reader never has to null-check
  // the half of the record it came for.
  get sidePane() {
    let v = null;
    try { v = JSON.parse(localStorage.getItem("pockettui_side_pane")); } catch (e) {}
    if (!v || typeof v !== "object") return null;
    const rows = sideRowsClean(Array.isArray(v.rows) ? v.rows : [v.owner]);
    if (!rows.length) return null;
    const src = v.panes && typeof v.panes === "object" ? v.panes : { browser: v };
    const panes = {};
    for (const id of rows) {
      const t = sideType(id);
      if (t === "browser") panes[id] = browserRecClean(src[id]);
      else if (t === "diff") panes[id] = diffRecClean(src[id]);
    }
    // The first browser's half said again in the flat keys, where the shell that
    // wrote this record before the column could hold two of a kind reads it —
    // and where an older shell still cached on this origin reads it too.
    const flat = panes.browser || { url: "", tabs: [], tab: 0 };
    return {
      rows: rows,
      session: typeof v.session === "string" ? v.session : "",
      panes: panes,
      url: flat.url,
      tabs: flat.tabs,
      tab: flat.tab,
    };
  },
  set sidePane(v) {
    const rows = v ? sideRowsClean(v.rows) : [];
    if (rows.length && v.session) {
      // The top row's type said again under the key the one-slot build reads.
      // Nothing here reads it back — it is the same cheap insurance as
      // mirrorLegacyKeys' above, for an older shell still cached on this origin,
      // which would otherwise find a record it cannot parse and reopen no pane
      // at all. sideType is a function declaration in a later fragment, which is
      // hoisted and callable here; the constants over there are not.
      const rec = { owner: sideType(rows[0]), rows: rows, session: v.session };
      const src = v.panes && typeof v.panes === "object" ? v.panes : {};
      const panes = {};
      for (const id of rows) {
        const t = sideType(id);
        const p = t === "browser" ? browserRecWrite(src[id])
                : t === "diff" ? diffRecWrite(src[id]) : null;
        if (p) panes[id] = p;
      }
      if (Object.keys(panes).length) rec.panes = panes;
      // The flat mirror of the first browser's half, for the readers above.
      if (panes.browser) Object.assign(rec, panes.browser);
      localStorage.setItem("pockettui_side_pane", JSON.stringify(rec));
    } else localStorage.removeItem("pockettui_side_pane");
  },
  get sideWidth() {
    const v = parseInt(localStorage.getItem("pockettui_side_w"), 10);
    return Number.isFinite(v) ? v : 0;
  },
  set sideWidth(v) { localStorage.setItem("pockettui_side_w", String(v)); },
  // Where the seam between the column's two rows was last dragged to, as the
  // top row's share of the window's height. 0 until one has been dragged,
  // which reads as an even split at the next second row; a share of none or
  // all of it is no column at all, so anything outside those ends reads the
  // same as never dragged.
  get sideSplit() {
    const v = parseFloat(localStorage.getItem("pockettui_side_split"));
    return Number.isFinite(v) && v > 0 && v < 1 ? v : 0;
  },
  set sideSplit(v) { localStorage.setItem("pockettui_side_split", String(v)); },
  // How tall the pane's file list was last dragged to. 0 until one has been
  // dragged, which leaves the list sized by the files in it under its cap.
  get diffListHeight() {
    const v = parseInt(localStorage.getItem("pockettui_diff_list_h"), 10);
    return Number.isFinite(v) ? v : 0;
  },
  set diffListHeight(v) { localStorage.setItem("pockettui_diff_list_h", String(v)); },
  // Which of the pane's two lists it opens on. Tracked by default: it is the
  // one git answers without walking everything the repo has never seen, and on
  // a network checkout that walk is the difference between a second and a
  // minute.
  get diffTab() {
    return localStorage.getItem("pockettui_diff_tab") === "untracked"
      ? "untracked" : "tracked";
  },
  set diffTab(v) {
    if (v === "untracked") localStorage.setItem("pockettui_diff_tab", "untracked");
    else localStorage.removeItem("pockettui_diff_tab");
  },
  // The editor's line-wrap toggle. Off by default: long lines scroll sideways,
  // same as before this existed.
  get editorWrapOn() { return localStorage.getItem("pockettui_editor_wrap") === "1"; },
  set editorWrapOn(v) {
    if (v) localStorage.setItem("pockettui_editor_wrap", "1");
    else localStorage.removeItem("pockettui_editor_wrap");
  },
  // The editor's vim keybindings. Off by default: the plain editor is what
  // most taps want, and vim needs a real Escape key to be usable at all.
  get editorVimOn() { return localStorage.getItem("pockettui_editor_vim") === "1"; },
  set editorVimOn(v) {
    if (v) localStorage.setItem("pockettui_editor_vim", "1");
    else localStorage.removeItem("pockettui_editor_vim");
  },
  // How the file explorer lays a folder out: "list" (a row per entry, the
  // default) or "grid" (icon tiles). One choice for every folder, not one per
  // folder — a view is how you like to read, not a property of the directory.
  get filesView() {
    return localStorage.getItem("pockettui_files_view") === "grid" ? "grid" : "list";
  },
  set filesView(v) {
    if (v === "grid") localStorage.setItem("pockettui_files_view", "grid");
    else localStorage.removeItem("pockettui_files_view");
  },
  // Whether the docked explorer fills the main pane instead of sharing it with
  // the terminal. Remembered beside the width for the same reason: the pane
  // comes back the size it was left.
  get filesExpanded() { return localStorage.getItem("pockettui_files_expanded") === "1"; },
  set filesExpanded(v) {
    if (v) localStorage.setItem("pockettui_files_expanded", "1");
    else localStorage.removeItem("pockettui_files_expanded");
  },
  // The same for the browser pane, kept apart from the explorer's: a page read
  // full width and a folder read full width are separate habits.
  get browserExpanded() { return localStorage.getItem("pockettui_browser_expanded") === "1"; },
  set browserExpanded(v) {
    if (v) localStorage.setItem("pockettui_browser_expanded", "1");
    else localStorage.removeItem("pockettui_browser_expanded");
  },
  // How big the browser pane draws a host's pages, as a factor per host: a dev
  // server read at 125% is still at 125% the next time it is opened, which is
  // what a desktop browser's per-site zoom does. Only the hosts that are not
  // at 1 are written, so the record stays the size of the handful of pages
  // someone has actually zoomed. The bounds are the pane's own steps, widened
  // a little: anything outside them is not a factor this app wrote.
  get browserZoom() {
    let v = null;
    try { v = JSON.parse(localStorage.getItem("pockettui_browser_zoom")); } catch (e) {}
    return browserZoomClean(v);
  },
  set browserZoom(v) {
    const rec = browserZoomClean(v);
    if (Object.keys(rec).length) {
      localStorage.setItem("pockettui_browser_zoom", JSON.stringify(rec));
    } else localStorage.removeItem("pockettui_browser_zoom");
  },
  // What order the file explorer lists a folder in: "name" (the backend's own
  // dirs-first, alphabetical order, the default), "newest", "oldest" or
  // "size". Global for the same reason filesView is — an outputs folder read
  // newest-first stays that way wherever you open it.
  get filesSort() {
    const v = localStorage.getItem("pockettui_files_sort");
    return v === "newest" || v === "oldest" || v === "size" ? v : "name";
  },
  set filesSort(v) {
    if (v === "newest" || v === "oldest" || v === "size") {
      localStorage.setItem("pockettui_files_sort", v);
    } else {
      localStorage.removeItem("pockettui_files_sort");
    }
  },
};

// The name becomes a tmux session name, so it is held to what tmux and the
// backend's `dev` check both accept: lowercase, [a-z0-9-], no runs of dashes,
// no dashes at either end. Empty after cleaning means keep the existing name.
function cleanDevName(v) {
  return String(v).toLowerCase().replace(/[^a-z0-9-]/g, "")
    .replace(/-+/g, "-").replace(/^-+|-+$/g, "").slice(0, 16).replace(/-+$/, "");
}

// True when we have no idea where the API is: not same-origin, nothing baked
// in, nothing stored. That is the first run of a public build.
function needsSetup() {
  return (!SAME_ORIGIN && !cfg.backend) || !cfg.token;
}

