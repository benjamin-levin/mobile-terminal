/* Mobile Terminal service worker.
 *
 * Only runs in a secure context (HTTPS or localhost). Served over plain HTTP it
 * never registers, so it's a no-op until the app is fronted by HTTPS (e.g.
 * `tailscale serve`), at which point it activates automatically.
 *
 * Strategy: stale-while-revalidate for same-origin GETs. The app shell (HTML,
 * xterm, app.js, CSS, icons) is served from the local Cache instantly with zero
 * network round-trips, then refreshed in the background — so a client far from
 * the server (e.g. Japan -> US) boots the UI immediately and only the WebSocket
 * pays the distance. At most one load is stale after a deploy; the next is fresh.
 */
const CACHE = "mobile-terminal-v18";
// The app shell (xterm, app.js, CSS) is inlined into the HTML, so caching "/"
// caches everything needed to boot; icons/manifest are cached on demand.
const PRECACHE = ["/"];

// Never intercept live endpoints — these must always hit the network.
const BYPASS = new Set(["/_ws", "/config", "/stats", "/health"]);

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE).catch(() => {})),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") {
    return;
  }
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || BYPASS.has(url.pathname)) {
    return;
  }
  event.respondWith(
    caches.open(CACHE).then((cache) =>
      cache.match(req).then((cached) => {
        const network = fetch(req)
          .then((resp) => {
            if (resp && resp.status === 200 && resp.type === "basic") {
              cache.put(req, resp.clone());
            }
            return resp;
          })
          .catch(() => cached);
        // Serve cache immediately when present; fall back to network on a miss.
        return cached || network;
      }),
    ),
  );
});
