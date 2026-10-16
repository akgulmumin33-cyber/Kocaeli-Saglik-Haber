/* Service worker: çevrimdışı çalışma + arka planda bildirim gösterme */
importScripts("./config.js");

const CACHE = "khh-v1";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./config.js",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/apple-touch-icon.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;

  // Haberler: önce internet, olmazsa son kayıtlı liste
  if (url.pathname.endsWith("/news.json")) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put("./news.json", copy));
          }
          return res;
        })
        .catch(() => caches.match("./news.json").then((r) => r || Response.error()))
    );
    return;
  }

  // Uygulama dosyaları: önce önbellek, arkada güncelle
  event.respondWith(
    caches.match(req, { ignoreSearch: true }).then((cached) => {
      const network = fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});

// Firebase bildirimleri: bildirim içeriği otomatik gösterilir,
// dokununca bildirimdeki bağlantı (ilgili haber) açılır.
if (self.APP_CONFIG && self.APP_CONFIG.pushReady) {
  importScripts(
    "https://www.gstatic.com/firebasejs/10.14.1/firebase-app-compat.js",
    "https://www.gstatic.com/firebasejs/10.14.1/firebase-messaging-compat.js"
  );
  firebase.initializeApp(self.APP_CONFIG.firebase);
  firebase.messaging();
}
