import { svelte } from "@sveltejs/vite-plugin-svelte";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig } from "vite";

const client = resolve("../../packages/client/src/index.ts");

export default defineConfig({
  plugins: [svelte()],
  base: "./",
  // The Python package ships Bazel's build of this (//typescript/apps/ui:dist). A build outside
  // Bazel names dependencies' scoped CSS classes from pnpm's node_modules layout, so it stays
  // here.
  build: {
    outDir: "dist",
    emptyOutDir: true
  },
  resolve: {
    alias: {
      // From the working directory (this package, where vite runs), not from import.meta.url:
      // vite bundles this file before running it and writes in its real path, which under
      // Bazel lies outside the sandbox the build runs in.
      $lib: resolve("src/lib"),
      /* The client's exports point at its dist/, which nothing builds before the console.
         Bazel links the built package instead and leaves the sources out of the sandbox. */
      ...(existsSync(client) && { "@temporalio/agent-harness-client": client })
    }
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": "http://127.0.0.1:8000"
    }
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: false
  }
});
