import { defineConfig } from "vitest/config";

// A DOM environment for React Testing Library; nothing here needs a browser beyond that.
export default defineConfig({
  test: { include: ["test/**/*.test.{ts,tsx}"], environment: "happy-dom" }
});
