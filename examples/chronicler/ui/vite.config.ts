import { fileURLToPath, URL } from "node:url";
import {
  svelte,
  vitePreprocess
} from "../../../ui/node_modules/@sveltejs/vite-plugin-svelte/src/index.js";
import { defineConfig } from "../../../ui/node_modules/vite/dist/node/index.js";

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  plugins: [svelte({ configFile: false, preprocess: vitePreprocess() })],
  base: "./",
  build: {
    outDir: fileURLToPath(new URL("../ui_dist", import.meta.url)),
    emptyOutDir: true
  },
  resolve: {
    alias: {
      "$lib/bridge": fileURLToPath(new URL("./src/lib/bridge", import.meta.url)),
      $lib: fileURLToPath(new URL("../../../ui/src/lib", import.meta.url)),
      $chronicler: fileURLToPath(new URL("./src/lib", import.meta.url)),
      $ui: fileURLToPath(new URL("../../../ui/src", import.meta.url)),
      "@lucide/svelte": fileURLToPath(
        new URL("../../../ui/node_modules/@lucide/svelte/dist/lucide-svelte.js", import.meta.url)
      ),
      "svelte/transition": fileURLToPath(
        new URL("../../../ui/node_modules/svelte/src/transition/index.js", import.meta.url)
      ),
      "svelte/events": fileURLToPath(
        new URL("../../../ui/node_modules/svelte/src/events/index.js", import.meta.url)
      ),
      "svelte/reactivity": fileURLToPath(
        new URL("../../../ui/node_modules/svelte/src/reactivity/index-client.js", import.meta.url)
      ),
      "svelte/internal/client": fileURLToPath(
        new URL("../../../ui/node_modules/svelte/src/internal/client/index.js", import.meta.url)
      ),
      "svelte/internal/disclose-version": fileURLToPath(
        new URL("../../../ui/node_modules/svelte/src/internal/disclose-version.js", import.meta.url)
      ),
      "svelte/internal/flags/async": fileURLToPath(
        new URL("../../../ui/node_modules/svelte/src/internal/flags/async.js", import.meta.url)
      ),
      "svelte/internal/flags/legacy": fileURLToPath(
        new URL("../../../ui/node_modules/svelte/src/internal/flags/legacy.js", import.meta.url)
      ),
      "svelte/internal/flags/tracing": fileURLToPath(
        new URL("../../../ui/node_modules/svelte/src/internal/flags/tracing.js", import.meta.url)
      ),
      svelte: fileURLToPath(new URL("../../../ui/node_modules/svelte/src/index-client.js", import.meta.url))
    }
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": "http://127.0.0.1:8000"
    }
  }
});
