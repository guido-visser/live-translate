// Makes the listener page installable (an icon on the home screen) and lets it open without a
// connection: it then shows "reconnecting" until the church PC can be reached again.
// Always the network first, so a phone never keeps an old version after an update; the copy kept
// here is only for when the network fails. The text itself (/ws, /api, /audio) is never kept.
"use strict";

const CACHE = "lt-listener-v1";
const SHELL = [
  "/",
  "/manifest.webmanifest",
  "/static/client/style.css",
  "/static/client/i18n.js",
  "/static/client/app.js",
  "/static/client/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .catch(() => {}) // installing still works without the offline copy
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

function kept(request) {
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return false;
  return request.mode === "navigate" || SHELL.includes(url.pathname) || url.pathname.startsWith("/static/client/");
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (!kept(request)) return; // the browser handles it as if there were no service worker
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
        }
        return response;
      })
      .catch(async () => {
        const cached = await caches.match(request, { ignoreSearch: true });
        if (cached) return cached;
        if (request.mode === "navigate") {
          const page = await caches.match("/");
          if (page) return page;
        }
        return Response.error();
      }),
  );
});
