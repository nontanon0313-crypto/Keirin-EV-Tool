// フロントエンド修正のたびに必ずこのバージョン文字列を更新すること
const CACHE_NAME = "keirin-ev-v123";

const ASSETS = [
  "./",
  "./index.html",
  "./css/style.css",
  "./js/app.js",
  "./manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = event.request.url;
  // APIは触らない
  const isApi =
    url.includes("/analyze") ||
    url.includes("/ev") ||
    url.includes("/purchases") ||
    url.includes("/simulation") ||
    url.includes("/races") ||
    url.includes("/bank") ||
    url.includes("/health") ||
    url.includes("/revenue") ||
    !url.includes(self.location.origin);
  if (isApi || event.request.method !== "GET") {
    return;
  }

  // index / app.js はネットワーク優先（古いJSで画面が死ぬのを防ぐ）
  const isCritical =
    url.includes("index.html") ||
    url.includes("/js/app.js") ||
    url.endsWith("/") ||
    url.includes("sw.js");

  if (isCritical) {
    event.respondWith(
      fetch(event.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((c) => c.put(event.request, copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
