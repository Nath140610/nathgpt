const CACHE_NAME = "nathgpt-shell-v1";
const APP_SHELL = [
    "/static/style.css",
    "/static/manifest.webmanifest",
    "/logo.png"
];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then((cache) => cache.addAll(APP_SHELL))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(
                keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    const destination = event.notification.data?.url || "/";
    event.waitUntil(
        clients.matchAll({ type: "window", includeUncontrolled: true })
            .then((windows) => {
                const existing = windows.find((windowClient) =>
                    new URL(windowClient.url).origin === self.location.origin
                );
                return existing ? existing.focus() : clients.openWindow(destination);
            })
    );
});

// Les futures notifications push (VAPID) arriveront ici. Le site utilise déjà
// ce service worker pour les alertes de fin tant que l'app est ouverte.
self.addEventListener("push", (event) => {
    const data = event.data?.json() || {};
    event.waitUntil(self.registration.showNotification(data.title || "NathGPT", {
        body: data.body || "Ta génération est prête.",
        icon: "/logo.png",
        badge: "/logo.png",
        data: { url: data.url || "/" }
    }));
});
