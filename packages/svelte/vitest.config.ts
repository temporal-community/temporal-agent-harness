import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vitest/config";

// The client build of Svelte: effects only run there, and the binding is all effects. A DOM
// environment makes Vitest transform modules as client code, so runes compile for the client
// too; nothing here renders.
export default defineConfig({
  plugins: [svelte()],
  resolve: { conditions: ["browser"] },
  test: { include: ["test/**/*.test.ts"], environment: "happy-dom" }
});
