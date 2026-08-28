// フロントエンド修正のたびに必ずこのバージョン文字列を更新すること(開発ルール)
const CACHE_NAME = "keirin-ev-v84";

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
  // APIリクエスト(ヘルスチェック含む)はキャッシュせず常にネットワークから取得する
  const url = event.request.url;
  const isApiRequest = url.includes("/analyze") || url.includes("/ev") ||
      url.includes("/purchases") || url.includes("/simulation") ||
      url.includes("/races") || url.includes("/bank") || url.includes("/health") ||
      !url.includes(self.location.origin); // 自分のオリジン以外(=Render等の外部API)は常にスルー
  if (isApiRequest) {
    return;
  }
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
