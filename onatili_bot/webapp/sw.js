/*
  Ilova sahifasini telefonda saqlab qo'yadi — internet yo'q joyda ham skaner ochiladi.
  Sahifa va skript: avval internetdan (yangi versiya), bo'lmasa saqlangan nusxa.
  API so'rovlariga tegmaydi — test ma'lumotlarini ilovaning o'zi saqlaydi (localStorage).
*/
const CACHE = "onatili-v1";
const SHELL = "/app";
const NETWORK_WAIT = 6000;  // sekin internetda shuncha kutiladi, keyin saqlangan nusxa

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll([SHELL, "/static/omr.js?v=2"]).catch(() => {})));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout")), ms);
    promise.then(value => { clearTimeout(timer); resolve(value); }, error => { clearTimeout(timer); reject(error); });
  });
}

async function networkFirst(request, cacheKey, ignoreSearch = false) {
  const cache = await caches.open(CACHE);
  try {
    const response = await withTimeout(fetch(request), NETWORK_WAIT);
    if (response.ok) cache.put(cacheKey, response.clone());
    return response;
  } catch (error) {
    const saved = await cache.match(cacheKey, { ignoreSearch });  // skript — aynan shu versiyasi
    if (saved) return saved;
    throw error;
  }
}

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin === self.location.origin) {
    if (request.mode === "navigate" && (url.pathname === "/" || url.pathname === "/app")) {
      event.respondWith(networkFirst(request, SHELL, true));  // ?app=1&t=… — hammasi bitta sahifa
    } else if (url.pathname.startsWith("/static/")) {
      event.respondWith(networkFirst(request, request));
    }
    return;
  }
  // Telegram skripti va shriftlar — bo'lsa saqlanganidan (sahifa internetsiz ham chiroyli ochilsin)
  if (url.hostname === "telegram.org" || url.hostname.endsWith("fonts.googleapis.com") || url.hostname.endsWith("fonts.gstatic.com")) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const saved = await cache.match(request);
      const fresh = fetch(request)
        .then(response => { if (response.ok || response.type === "opaque") cache.put(request, response.clone()); return response; })
        .catch(() => saved || Response.error());
      return saved || fresh;
    })());
  }
});
