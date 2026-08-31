/* Sure Weather service worker.
   - App shell: precached, versioned; network-first so deploys always land.
   - /weather: network-first, cached fallback for offline.
   - /tile + /wind-grid: cache-first runtime cache (offline map, upstream
     hiccups), trimmed to a bounded number of entries. */
const CACHE = "sure-weather-v9";
const RUNTIME = "sure-weather-tiles-v1";
const RUNTIME_MAX = 600;
const APP_SHELL = ["/", "/static/app.css", "/static/app.js", "/static/i18n.js", "/static/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE && k !== RUNTIME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

async function trimRuntime() {
  const cache = await caches.open(RUNTIME);
  const keys = await cache.keys();
  if (keys.length <= RUNTIME_MAX) return;
  // Oldest-inserted first (approximation: insertion order of keys()).
  for (const k of keys.slice(0, keys.length - RUNTIME_MAX)) {
    await cache.delete(k);
  }
}

async function cacheFirst(e) {
  const cached = await caches.match(e.request);
  if (cached) return cached;
  const resp = await fetch(e.request);
  if (resp.ok) {
    const cache = await caches.open(RUNTIME);
    await cache.put(e.request, resp.clone());
    trimRuntime();
  }
  return resp;
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/tile/") || url.pathname === "/wind-grid") {
    e.respondWith(cacheFirst(e));
    return;
  }
  if (url.pathname.startsWith("/weather")) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
    return;
  }
  if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => caches.match("/")));
    return;
  }
  // Assets (JS/CSS/icons): network-first — a stale service-worker cache
  // must never pin old code. Offline falls back to the cached shell.
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request).then((hit) => hit || Promise.reject()))
  );
});

self.addEventListener("push", (e) => {
  let data = { title: "Sure Weather", body: "Mise à jour météo" };
  try { data = e.data.json(); } catch {}
  e.waitUntil(
    self.registration.showNotification(data.title || "Sure Weather", {
      body: data.body || "",
      icon: "/static/icon-192.png",
      badge: "/static/icon-192.png",
      data: { url: data.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(clients.openWindow(e.notification.data.url || "/"));
});