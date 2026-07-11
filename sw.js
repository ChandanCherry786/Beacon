// Minimal service worker. Its only purpose is to make the app installable as
// a desktop app so it gets its own Windows taskbar icon. It does not cache
// anything: the fetch handler is a no-op, so every request goes to the network
// as usual and the app always serves fresh.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
