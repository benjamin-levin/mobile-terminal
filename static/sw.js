/* Mobile Terminal service worker.
 *
 * Only runs in a secure context (HTTPS or localhost). Served over plain HTTP it
 * never registers, so it's a no-op until the app is fronted by HTTPS (e.g.
 * `tailscale serve`), at which point it activates automatically.
 *
 * Strategy: network-first for navigations so a deployed app shell is visible on
 * the next open, with a short timeout and cached fallback for slow/offline links.
 * Static assets remain stale-while-revalidate for instant repeat loads.
 */
const CACHE = "mobile-terminal-v20";
const NAVIGATION_TIMEOUT_MS = 2500;
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

function cacheNetworkResponse(cache, request, response) {
  if (!response || response.status !== 200 || response.type !== "basic") {
    return Promise.resolve(response);
  }
  return cache.put(request, response.clone()).then(() => response);
}

async function navigationResponse(request) {
  const cache = await caches.open(CACHE);
  const cached = (await cache.match(request)) || (await cache.match("/"));
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  let timeout = null;
  const timedOut = new Promise((_, reject) => {
    timeout = setTimeout(() => {
      controller?.abort();
      reject(new Error("Navigation request timed out"));
    }, NAVIGATION_TIMEOUT_MS);
  });
  try {
    const response = await Promise.race([
      fetch(request, controller ? { signal: controller.signal } : undefined),
      timedOut,
    ]);
    return await cacheNetworkResponse(cache, request, response);
  } catch (error) {
    if (cached) {
      return cached;
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") {
    return;
  }
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || BYPASS.has(url.pathname)) {
    return;
  }
  if (req.mode === "navigate") {
    event.respondWith(navigationResponse(req));
    return;
  }
  const cachePromise = caches.open(CACHE);
  const revalidation = cachePromise.then((cache) =>
    fetch(req).then((response) => cacheNetworkResponse(cache, req, response)),
  );
  event.waitUntil(revalidation.catch(() => {}));
  event.respondWith(
    cachePromise.then((cache) => cache.match(req)).then((cached) => cached || revalidation),
  );
});
