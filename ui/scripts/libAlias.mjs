/**
 * Teach plain node the `$lib/` alias, so a self-check can import the same
 * module the app does rather than a copy reachable by relative path.
 *
 * Import this for its side effect before importing anything under `src/lib`.
 */
import { registerHooks } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const libRoot = pathToFileURL(
  resolve(dirname(fileURLToPath(import.meta.url)), "../src/lib")
).href;

/* In-thread hooks, so this closes over libRoot directly. The older register()
   runs hooks on a separate loader thread, which is why that form had to ship
   its source as a data: URL with the path baked in by hand. */
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (!specifier.startsWith("$lib/")) return nextResolve(specifier, context);
    const path = libRoot + specifier.slice(4);
    return nextResolve(/\.[a-z]+$/i.test(path) ? path : `${path}.ts`, context);
  }
});
