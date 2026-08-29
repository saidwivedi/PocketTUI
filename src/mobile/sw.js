// PocketTUI PWA service worker — network-first.
// Version is stamped by the server at request time, so a server restart evicts
// the stale shell on the phone's next visit.
const CACHE_VERSION = "__CACHE_VERSION__";
const CACHE_NAME = `pockettui-${CACHE_VERSION}`;
// A tapped notification's target session, parked where the page can read it
// back — shared with 30-notify.js, which consumes it. Not a shell cache: it
// must survive the activate sweep below.
const DEEPLINK_CACHE = "pockettui-deeplink";
const DEEPLINK_KEY = "./pending-session";
// A tiny event ring next to the parked session: the page's debug log cannot
// see this worker's hops (the SW has no dbg()), so each notificationclick
// step is appended here and the page dumps the ring at startup. Read-modify-
// write is safe: appends only happen inside one click's waitUntil chain.
const SW_EVENTS_KEY = "./sw-events";
function swLog(msg) {
  return caches.open(DEEPLINK_CACHE).then(c =>
    c.match(SW_EVENTS_KEY)
      .then(hit => hit ? hit.json().catch(() => []) : [])
      .then(list => {
        list.push({ at: Date.now(), v: CACHE_VERSION, msg: msg });
        return c.put(SW_EVENTS_KEY, new Response(JSON.stringify(list.slice(-20))));
      })
  ).catch(() => {});
}

// One-shot version exchange: the page's own stamp only says what shell was
// served, not which worker is controlling it — this answers for the worker.
self.addEventListener("message", (e) => {
  const d = e.data || {};
  if (d.type === "version?" && e.ports && e.ports[0]) {
    e.ports[0].postMessage({ version: CACHE_VERSION });
  }
});

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME && k !== DEEPLINK_CACHE)
        .map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Web Push from the pane watcher: show it, carrying the session name so the
// tap can land on that session. The payload is ours (app.py builds it), but a
// push whose data will not parse must still show *something* — iOS revokes
// the subscription of a worker that takes a push and shows nothing.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) {}
  e.waitUntil(self.registration.showNotification(d.title || "PocketTUI", {
    body: d.body || "",
    tag: d.tag || "pockettui",
    icon: "icon-192.png",
    // Quiet-mode sessions ask for a silent banner; a platform that ignores
    // the option just plays its sound, which is the right degradation.
    silent: !!d.silent,
    data: { session: d.session || "" },
  }));
});

// Message a visible window if there is one — told which session to open via
// postMessage — else open one with the session in the fragment, which boot
// parses and strips. The target is parked in DEEPLINK_CACHE first either
// way: iOS drops a postMessage aimed at a frozen standalone page and can
// launch a closed PWA on its bare start_url with the fragment gone, and the
// parked copy is what the page reads back in both cases (30-notify.js
// consumes it on boot, on resume, and in the message handler).
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const session = (e.notification.data && e.notification.data.session) || "";
  const park = !session ? Promise.resolve() :
    caches.open(DEEPLINK_CACHE).then(c => c.put(DEEPLINK_KEY,
      new Response(JSON.stringify({ session: session, at: Date.now(), via: "click", sw: CACHE_VERSION }))
    )).catch(() => {});
  e.waitUntil(
    park.then(() => swLog("click session=" + session))
      .then(() => self.clients.matchAll({ type: "window", includeUncontrolled: true }))
      .then((wins) => {
      // Only a visible (or at least focused) window is trusted with the
      // postMessage path: iOS standalone keeps a frozen, invisible client in
      // matchAll that silently drops messages, and focus() on it is not
      // guaranteed to resume anything. Everything else goes through
      // openWindow, which launches or resumes the PWA and lets the parked
      // entry do the aiming.
      const target = wins.find(w => w.visibilityState === "visible") ||
                     wins.find(w => w.focused);
      if (target) {
        target.postMessage({ type: "open-session", session: session });
        return swLog("postMessage wins=" + wins.length)
          .then(() => { if (target.focus) return target.focus(); })
          .catch(() => {});
      }
      return swLog("openWindow wins=" + wins.length)
        .then(() => self.clients.openWindow("./#session=" + encodeURIComponent(session)))
        .catch(() => {
          // openWindow can refuse when a window already exists — focusing any
          // client still resumes it, and the resume consumes the parked entry.
          return swLog("openWindow failed, focus fallback").then(() => {
            if (wins.length && wins[0].focus) return wins[0].focus();
          }).catch(() => {});
        });
    })
  );
});

self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  // The tailnet backend is cross-origin on the public static deploy — never
  // ours to intercept or cache.
  if (u.origin !== location.origin) return;
  // Live data and the PTY socket must always hit the network untouched.
  if (u.pathname.includes("/api/") || u.pathname.includes("/ws/")) return;
  if (e.request.method !== "GET") return;

  e.respondWith(
    fetch(e.request).then(resp => {
      if (resp.ok && u.origin === location.origin) {
        const clone = resp.clone();
        caches.open(CACHE_NAME).then(c => c.put(e.request, clone)).catch(() => {});
      }
      return resp;
    }).catch(() => caches.match(e.request).then(hit => hit || caches.match("./")))
  );
});
