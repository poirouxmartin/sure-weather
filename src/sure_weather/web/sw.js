const CACHE = "sure-weather-v7";
const APP_SHELL = ["/", "/static/app.css", "/static/app.js", "/static/i18n.js", "/static/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/weather")) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request).catch(() => caches.match("/"))
    );
    return;
  }
  // Network-first for assets: a stale service-worker cache must never pin
  // old JS/CSS during development. Offline falls back to the cached shell.
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request).then((hit) => hit || Promise.reject()))
  );
});