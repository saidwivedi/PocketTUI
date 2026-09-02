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

const cfg = {
  get backend() { return localStorage.getItem("pockettui_backend") || DEFAULT_BACKEND; },
  set backend(v) { localStorage.setItem("pockettui_backend", v); },
  get token() { return localStorage.getItem("pockettui_token") || ""; },
  set token(v) { localStorage.setItem("pockettui_token", v); },
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
  // The snippet row's commands, one per line, exactly as typed in Settings.
  // Never-saved falls back to the starter set; a deliberately emptied box is
  // kept empty rather than resurrected.
  get snippets() {
    const v = localStorage.getItem("pockettui_snippets");
    return v === null ? "git status\nls -la\nhtop" : v;
  },
  set snippets(v) { localStorage.setItem("pockettui_snippets", v); },
  // Whether the snippet row shows at all. Off by default: a second row costs
  // terminal height, so it is opt-in from Settings.
  get snippetsOn() { return localStorage.getItem("pockettui_snippets_on") === "1"; },
  set snippetsOn(v) {
    if (v) localStorage.setItem("pockettui_snippets_on", "1");
    else localStorage.removeItem("pockettui_snippets_on");
  },
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

