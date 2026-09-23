import { fileURLToPath } from "node:url";

import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

const repoRoot = fileURLToPath(new URL("../../..", import.meta.url));

export default defineConfig({
  plugins: [svelte()],
  // The harness packages are linked from packages/, each with its own node_modules. One Svelte
  // runtime for the page: a second copy would keep its own reactivity graph.
  resolve: { dedupe: ["svelte"] },
  server: { port: 5173, fs: { allow: [repoRoot] } }
});
