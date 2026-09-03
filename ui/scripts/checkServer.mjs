// ABOUTME: The vite dev server shared by every check that loads a rune-using module through
// ssrLoadModule(), with its dependency-optimizer cache kept OFF the shared node_modules/.vite.
//
// Sharing that directory with a live `pnpm dev` is what let a FAILING check break the browser.
// Each of these scripts calls `await vite.close()` as its last statement, so an assertion that
// throws earlier kills the process with the optimizer mid-flight: the shared deps/ is left
// half-written and the dev server on :5173 answers `504 Outdated Optimize Dep` to every import,
// which renders as a blank page. The deps_temp_* directories left behind in there go back further
// than any one day's work.
//
// A private cacheDir fixes that at the cause rather than at each exit path. A check cannot corrupt
// a cache it does not share, however it dies — including on a SIGINT, which no `finally` gets to
// run for. Per-script so two checks in flight cannot optimize into each other's cache, and reused
// between runs so nothing accumulates.
import { basename } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const ui = new URL("..", import.meta.url);

/** @param callerUrl the calling check's own `import.meta.url`, which names its cache. */
export function createCheckServer(callerUrl) {
  const check = basename(fileURLToPath(callerUrl), ".mjs");
  return createServer({
    root: fileURLToPath(ui),
    cacheDir: fileURLToPath(new URL(`node_modules/.vite-checks/${check}`, ui)),
    server: { middlewareMode: true },
    appType: "custom",
    logLevel: "silent"
  });
}
