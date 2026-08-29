// PocketTUI PWA service worker — network-first.
// Version is stamped by the server at request time, so a server restart evicts
// the stale shell on the phone's next visit.
const CACHE_VERSION = "__CACHE_VERSION__";
const CACHE_NAME = `pockettui-${CACHE_VERSION}`;

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
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
    data: { session: d.session || "" },
  }));
});

// Focus an existing window if there is one — told which session to open via
// postMessage — else open a fresh one with the session in the fragment, which
// boot parses and strips.
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const session = (e.notification.data && e.notification.data.session) || "";
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      if (wins.length) {
        wins[0].postMessage({ type: "open-session", session: session });
        if (wins[0].focus) return wins[0].focus();
        return;
      }
      return self.clients.openWindow("./#session=" + encodeURIComponent(session));
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
