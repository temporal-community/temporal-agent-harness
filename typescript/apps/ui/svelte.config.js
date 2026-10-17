import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath, URL } from "node:url";

export default {
  preprocess: vitePreprocess(),
  compilerOptions: {
    /* Svelte names each component's scoped CSS classes from its path relative to
       rootDir (the working directory by default), or from its absolute path when it
       lies outside rootDir. Dependencies live in the workspace's node_modules, above
       this package, so rootDir is the workspace root: the committed build then does
       not depend on where the checkout is. */
    rootDir: fileURLToPath(new URL("../..", import.meta.url))
  }
};
