/* Service worker: caches the app shell only. API responses are never cached. */
"use strict";

const CACHE = "smoker-shell-v1";
const SHELL = [
  "./",
  "static/app.css",
  "static/app.js",
  "manifest.webmanifest",
  "static/icons/icon.svg",
  "static/icons/icon-192.png",
  "static/icons/icon-512.png",
  "static/icons/apple-touch-icon.png",
];

const shellUrls = () => SHELL.map((path) => new URL(path, self.registration.scope).href);

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(shellUrls())).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  url.search = "";
  url.hash = "";
  // Only shell files are handled; /api/*, /docs and everything else go
  // straight to the network untouched.
  if (!shellUrls().includes(url.href)) return;
  // Network first so a running bridge always serves the current UI; the
  // cached copy is the offline fallback.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(url.href, copy));
        }
        return response;
      })
      .catch(() => caches.match(url.href).then((cached) => cached || Response.error()))
  );
});
