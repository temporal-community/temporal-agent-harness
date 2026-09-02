/**
 * Teach plain node to import a `.svelte` file, so a self-check can assert what a
 * component actually renders rather than mirroring its markup into the check and
 * hoping the copy stays honest. Compiles to the server target and hands node the
 * result; the compiler strips `lang="ts"` on its own, so there is no preprocessor
 * to install.
 *
 * Import this for its side effect before importing any component. Pair it with
 * libAlias.mjs when the component reaches for `$lib/`.
 */
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { fileURLToPath } from "node:url";
import { compile } from "svelte/compiler";

registerHooks({
  load(url, context, nextLoad) {
    if (!url.startsWith("file:") || !url.endsWith(".svelte")) return nextLoad(url, context);
    const path = fileURLToPath(url);
    const { js } = compile(readFileSync(path, "utf8"), {
      generate: "server",
      name: path.split("/").pop().replace(".svelte", "")
    });
    return { format: "module", shortCircuit: true, source: js.code };
  }
});
