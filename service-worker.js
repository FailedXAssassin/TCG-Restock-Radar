
const CACHE = "tcg-radar-v3-14";
const ASSETS = ["./","index.html","style.css","app.js","manifest.webmanifest","icon-radar-v2.svg"];
self.addEventListener("install", e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting())));
self.addEventListener("activate", e => e.waitUntil(Promise.all([
  caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))),
  self.clients.claim()
])));
self.addEventListener("fetch", e => {
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});

self.addEventListener("push", event => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil(self.registration.showNotification(data.title || "TCG Radar", {body:data.body || "A product changed status.", icon:"./icon-radar-v2.svg", badge:"./icon-radar-v2.svg", tag:data.tag, data:{url:data.url || "./"}}));
});
self.addEventListener("notificationclick", event => { event.notification.close(); event.waitUntil(clients.openWindow(event.notification.data.url)); });

