/* Service worker: caches the app shell only. API responses are never cached. */
"use strict";

const CACHE = "smoker-shell-v4";
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

// With the password on, a signed-out request for the shell is redirected to the login
// page (or answered 401). Such responses must never be cached as the shell.
const cacheable = (response) => response.ok && !response.redirected;
// Navigations fetch with redirect: "manual", so their redirect is opaque.
const signedOut = (response) =>
  response.redirected || response.type === "opaqueredirect" || response.status === 401;

self.addEventListener("install", (event) => {
  // Cache what we can; a signed-out install must not store the login page.
  const fill = caches.open(CACHE).then((cache) =>
    Promise.all(shellUrls().map((url) =>
      fetch(url, { cache: "no-cache" })
        .then((response) => (cacheable(response) ? cache.put(url, response) : undefined))
        .catch(() => undefined)
    ))
  );
  event.waitUntil(fill.then(() => self.skipWaiting()));
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
        if (cacheable(response)) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(url.href, copy));
        } else if (signedOut(response)) {
          // Logged out: forget the shell so it isn't served offline later.
          caches.delete(CACHE);
        }
        return response;
      })
      .catch(() => caches.match(url.href).then((cached) => cached || Response.error()))
  );
});
