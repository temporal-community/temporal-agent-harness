// Compiles Svelte as the page loads it, so the page needs no build step: `.svelte` components
// and `.svelte.js` rune modules (the binding ships its own as those) come back as plain modules.
importScripts("https://cdn.jsdelivr.net/npm/svelte@5.57.1/compiler/index.js");

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
  const { pathname } = new URL(event.request.url);
  if (!/\.svelte(\.js)?$/.test(pathname)) return;
  event.respondWith(
    (async () => {
      const source = await (await fetch(event.request)).text();
      const { js } = pathname.endsWith(".svelte")
        ? svelte.compile(source, { filename: pathname })
        : svelte.compileModule(source, { filename: pathname });
      return new Response(js.code, { headers: { "Content-Type": "text/javascript" } });
    })()
  );
});
