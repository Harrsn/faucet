// Faucet's service worker. Deliberately small:
//  * /app/<version>/… (scripts, styles, icons) are content-hashed and never
//    change, so they're served from a cache once fetched — pages open fast,
//    even on a weak signal. Each deploy has a new version; old caches are
//    dropped when this worker activates.
//  * Page loads go to the network first; only if that fails is a small
//    "you're offline" page shown.
//  * The API is never cached: Faucet's data is always live.
const VERSION = "__ASSET_VERSION__";
const ASSETS = `faucet-assets-${VERSION}`;

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith("faucet-") && k !== ASSETS) await caches.delete(k);
    await self.clients.claim();
  })());
});

const OFFLINE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Faucet — offline</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0E1013;color:#E9EAEC;
font:16px/1.5 system-ui,sans-serif;text-align:center;padding:24px}b{font-size:22px;display:block;margin-bottom:6px}
a{color:#D9A441}</style></head><body><div><b>Faucet can't be reached</b>
You're offline, or the server is. <a href="/">Try again</a></div></body></html>`;

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith(`/app/${VERSION}/`)) {
    e.respondWith((async () => {
      const cache = await caches.open(ASSETS);
      const hit = await cache.match(req);
      if (hit) return hit;
      const res = await fetch(req);
      if (res.ok) cache.put(req, res.clone());
      return res;
    })());
    return;
  }
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(() => new Response(OFFLINE, {
      status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } })));
  }
});
