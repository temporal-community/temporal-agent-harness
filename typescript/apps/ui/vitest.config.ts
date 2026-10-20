import { svelte } from "@sveltejs/vite-plugin-svelte";
import { resolve } from "node:path";
import { defineConfig } from "vitest/config";

/* Separate from vite.config.ts rather than a `test` block inside it, because
   Vitest prefers this file wholesale when it exists — the plugin and the alias
   are restated here for that reason, not by accident. The build config has no
   business carrying test settings, and this one has no business carrying the
   dev server or the dist output path. */
export default defineConfig({
  plugins: [svelte()],
  resolve: {
    alias: {
      // From the working directory, as in vite.config.ts and for the same reason.
      $lib: resolve("src/lib")
    }
  },
  test: {
    include: ["src/**/*.test.mjs", "tests/**/*.test.mjs"],
    /* Several of these drive a real controller through catch-up windows and
       backoff ladders measured in whole seconds — the attach retry budget alone
       walks 31.5s by design. The default 5s would fail them for being what they
       are. Individual tests still narrow this where a deadline is the claim. */
    testTimeout: 90_000,
    hookTimeout: 30_000
  }
});
