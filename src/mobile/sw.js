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
