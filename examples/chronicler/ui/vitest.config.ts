import { fileURLToPath, URL } from "node:url";
import { svelte } from "../../../ui/node_modules/@sveltejs/vite-plugin-svelte/src/index.js";

export default {
  root: fileURLToPath(new URL("../../../", import.meta.url)),
  cacheDir: fileURLToPath(
    new URL("../../../ui/node_modules/.vite/chronicler-example", import.meta.url)
  ),
  plugins: [svelte()],
  resolve: {
    alias: {
      "$lib/bridge": fileURLToPath(new URL("./src/lib/bridge", import.meta.url)),
      $lib: fileURLToPath(new URL("../../../ui/src/lib", import.meta.url)),
      $chronicler: fileURLToPath(new URL("./src/lib", import.meta.url)),
      "@lucide/svelte": fileURLToPath(
        new URL("../../../ui/node_modules/@lucide/svelte/dist/lucide-svelte.js", import.meta.url)
      ),
      "svelte/server": fileURLToPath(
        new URL("../../../ui/node_modules/svelte/src/server/index.js", import.meta.url)
      ),
      "svelte/internal/server": fileURLToPath(
        new URL("../../../ui/node_modules/svelte/src/internal/server/index.js", import.meta.url)
      ),
      "svelte/transition": fileURLToPath(
        new URL("../../../ui/node_modules/svelte/src/transition/index.js", import.meta.url)
      ),
      svelte: fileURLToPath(new URL("../../../ui/node_modules/svelte/src/index-server.js", import.meta.url))
    }
  },
  ssr: {
    noExternal: ["@lucide/svelte"]
  }
};
