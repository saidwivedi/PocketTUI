// ============================================================
// Notifications — bell toggle, Web Push, prompt chips, deep links
// ============================================================
// The server's pane watcher decides when a session needs its human (a y/n
// question, a numbered menu, a long run finishing); this fragment is the
// phone half: the list rows' bell opting a session in, this device's Web
// Push subscription, the chips row that renders a detected prompt's answers,
// and landing a tapped notification on the right session.

// What /api/push/status last answered. Fetched once a paired app boots and
// kept, so the bell tap can act inside the user's own gesture instead of
// waiting on a round-trip before it may ask for permission.
let pushStatus = null;

async function fetchPushStatus() {
  try {
    const r = await fetch(apiURL("api/push/status"), { cache: "no-store", headers: authHeaders() });
    if (r.ok) pushStatus = await r.json();
  } catch (e) {}
  return pushStatus;
}

// The server's VAPID public key, base64url, as the BufferSource
// pushManager.subscribe wants.
function pushKeyBytes(v) {
  const pad = "=".repeat((4 - (v.length % 4)) % 4);
  const raw = atob((v + pad).replace(/-/g, "+").replace(/_/g, "/"));
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr;
}

// Get this browser a stored push subscription, if it can hold one at all.
// Returns whether it did — false is not an error: @notify still turns on and
// the ntfy transport still fires; push is one transport, never the gate.
async function ensurePushSubscription() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) ||
      !("Notification" in window) || location.protocol !== "https:") return false;
  try {
    // Permission first — it is the one step that must run inside the tap's
    // transient activation, so nothing is awaited ahead of it.
    if (Notification.permission === "denied") return false;
    if (Notification.permission !== "granted") {
      if ((await Notification.requestPermission()) !== "granted") return false;
    }
    const status = pushStatus || await fetchPushStatus();
    if (!status || !status.push || !status.vapid_key) return false;
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: pushKeyBytes(status.vapid_key),
    });
    const r = await fetch(apiURL("api/push/subscribe"), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ subscription: sub.toJSON(), dev: cfg.devname }),
    });
    return r.ok;
  } catch (e) {
    dbg("push subscribe:", e);
    return false;
  }
}

// The list row's bell cycles the session's mode: off → with sound → silent →
// off. Leaving off rides the tap through permission + subscribe (iOS grants
// both only from a user gesture), then sets @notify either way — on iOS
// outside the home-screen app there is no push to be had, so the toast says
// what would fix it while the ntfy transport keeps working; otherwise the
// toast names the mode the tap just landed on.
async function toggleNotify(s, btn) {
  const mode = s.notify === "off" ? "on" : s.notify === "on" ? "quiet" : "off";
  let hinted = false;
  if (s.notify === "off") {
    const pushed = await ensurePushSubscription();
    if (!pushed && a2hsPlatform() === "ios" && !a2hsInstalled()) {
      toast("Add to Home Screen to get push notifications on this phone", 3500);
      hinted = true;
    }
  }
  try {
    const r = await fetch(apiURL("api/notify"), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ session: s.name, mode: mode }),
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) { toast(data && data.error ? data.error : "Couldn't update notifications"); return; }
    s.notify = data.notify;
    setBellState(btn, s.notify, s.name);
    if (!hinted) {
      toast(s.notify === "on" ? "Notifications with sound" :
            s.notify === "quiet" ? "Silent notifications" : "Notifications off");
    }
  } catch (e) {
    toast("Couldn't update notifications");
  }
}

// ------------------------------------------------------------
// Prompt chips
// ------------------------------------------------------------
function hideChips() {
  const bar = $("chips");
  if (!bar.classList.contains("show")) return;
  bar.classList.remove("show");
  bar.textContent = "";
  // The row is a flex sibling of #term-host: its height comes out of the
  // terminal's rows, which xterm only learns from a fit.
  refit(0);
}

// Renders a prompt frame's answers, plus ⏎ and esc always — confirm and back
// out are what every prompt understands. A tap sends the literal key(s);
// send() itself hides the row, so answering by any other key clears it too.
// The empty frame (options and line both blank) is the watcher saying the
// session is busy again.
function showPromptChips(ctl) {
  const options = Array.isArray(ctl.options) ? ctl.options : [];
  const line = typeof ctl.line === "string" ? ctl.line : "";
  if (!options.length && !line) { hideChips(); return; }
  const bar = $("chips");
  bar.textContent = "";
  const chips = options.map(o => ({ label: String(o), seq: String(o) }))
    .concat([{ label: "⏎", seq: "\r" }, { label: "esc", seq: "\x1b" }]);
  for (const c of chips) {
    const b = el("button", { type: "button" }, c.label);
    // Focus stays exactly where it is, like the key bar's keys: stealing it
    // would drop the soft keyboard mid-typing.
    b.addEventListener("pointerdown", (e) => e.preventDefault());
    b.addEventListener("mousedown", (e) => e.preventDefault());
    b.addEventListener("click", (e) => { e.preventDefault(); send(c.seq); });
    bar.appendChild(b);
  }
  bar.classList.add("show");
  refit(0);
}

// ------------------------------------------------------------
// Notification deep links
// ------------------------------------------------------------
// The session may be gone by the time the tap lands — refresh the list first
// (which the landing wants anyway) and stay on it with a toast when the name
// is missing. A list that would not load proves nothing, so that case falls
// through to the terminal's own connect errors.
async function openSessionByName(name) {
  dbg("deeplink: open", name || "(empty)");
  if (!name || demoMode || needsSetup()) return;
  if (currentSession === name && $("screen-term").classList.contains("active")) return;
  const sessions = await loadSessions();
  if (sessions && !sessions.some((s) => s.name === name)) {
    dbg("deeplink: session gone:", name);
    toast("Session '" + name + "' is gone");
    return;
  }
  openTerminal(name);
}

// The parked deep link: notificationclick writes the tapped session into
// DEEPLINK_CACHE before it postMessages or opens a window, because iOS drops
// a message aimed at a frozen standalone page and can launch a closed PWA on
// its bare start_url with the #session= fragment gone. Reading takes the
// entry, so whichever consumer gets there first — the message handler, boot,
// or the resume listener below — fires it once. Freshness-gated: the entry is
// written by the tap itself, so anything older is debris from a launch that
// died mid-flight, not an instruction to teleport a later visit.
const DEEPLINK_FRESH_MS = 2 * 60 * 1000;
// `src` names the consumer for the debug log only — every hop of the chain
// logs itself so a tap that lands on the list is diagnosable from the phone.
async function takePendingSession(src) {
  try {
    const c = await caches.open("pockettui-deeplink");
    const hit = await c.match("./pending-session");
    if (!hit) { dbg("deeplink take(" + src + "): miss"); return ""; }
    await c.delete("./pending-session");
    const d = await hit.json();
    const age = Date.now() - (d.at || 0);
    if (!d.session || age > DEEPLINK_FRESH_MS) {
      dbg("deeplink take(" + src + "): stale age=" + Math.round(age / 1000) + "s");
      return "";
    }
    dbg("deeplink take(" + src + "): " + d.session + " age=" + Math.round(age / 1000) +
        "s via=" + (d.via || "?") + " sw=" + (d.sw || "?"));
    return String(d.session);
  } catch (e) {
    dbg("deeplink take(" + src + "):", e);
    return "";
  }
}
async function consumePendingSession(src) {
  const name = await takePendingSession(src);
  if (name) openSessionByName(name);
}

// A tapped notification reaching an already-open window arrives as a message
// from the service worker (a fresh window gets the #session= fragment
// instead, which boot parses). The parked copy is taken first so it cannot
// re-fire on the next resume; the message's own name covers the case where
// another consumer already took it.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.addEventListener("message", (e) => {
    const d = e.data || {};
    if (d.type === "open-session" && d.session) {
      dbg("deeplink: sw message", d.session);
      takePendingSession("message").then((name) => openSessionByName(name || String(d.session)));
    }
  });
}

// iOS resuming the frozen app after a notification tap is exactly the state
// where the postMessage above goes missing — on the way back to the
// foreground, the parked entry is the truth.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) consumePendingSession("resume");
});

// One startup dump of the service worker's notification-click event ring
// (sw.js appends its hops there — the page's debug log cannot see them live).
// Read-only on purpose: it is a ring the SW prunes itself, and clearing it
// here would erase history the next launch's dump still wants.
if (cfg.debug && "caches" in window) {
  caches.open("pockettui-deeplink")
    .then((c) => c.match("./sw-events"))
    .then((hit) => hit ? hit.json() : [])
    .then((list) => {
      for (const ev of list.slice(-20)) {
        dbg("sw-ring", relTime((ev.at || 0) / 1000), "v=" + (ev.v || "?"), ev.msg || "");
      }
    })
    .catch(() => {});
}

// Warm the status cache so the first bell tap needs no round-trip before the
// permission ask. Fire-and-forget; the toggle re-fetches if this missed.
if (!needsSetup()) fetchPushStatus();
