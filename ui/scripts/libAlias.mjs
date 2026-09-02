/**
 * Teach plain node the `$lib/` alias, so a self-check can import the same
 * module the app does rather than a copy reachable by relative path.
 *
 * Import this for its side effect before importing anything under `src/lib`.
 */
import { register } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const libRoot = pathToFileURL(
  resolve(dirname(fileURLToPath(import.meta.url)), "../src/lib")
).href;

register(
  `data:text/javascript,${encodeURIComponent(`
    export async function resolve(specifier, context, nextResolve) {
      if (specifier.startsWith("$lib/")) {
        const path = ${JSON.stringify(libRoot)} + specifier.slice(4);
        const url = /\\.[a-z]+$/i.test(path) ? path : path + ".ts";
        return nextResolve(url, context);
      }
      return nextResolve(specifier, context);
    }
  `)}`,
  import.meta.url
);
