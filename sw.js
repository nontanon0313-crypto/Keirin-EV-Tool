// フロントエンド修正のたびに必ずこのバージョン文字列を更新すること(開発ルール)
const CACHE_NAME = "keirin-ev-v1";

const ASSETS = [
  "./",
  "./index.html",
  "./css/style.css",
  "./js/app.js",
  "./manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  // APIリクエストはキャッシュせず常にネットワークから取得する
  if (event.request.url.includes("/analyze") || event.request.url.includes("/ev") ||
      event.request.url.includes("/purchases") || event.request.url.includes("/simulation") ||
      event.request.url.includes("/races") || event.request.url.includes("/bank")) {
    return;
  }
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
