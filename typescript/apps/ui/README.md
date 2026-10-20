# Shared Example UI

This directory is the shared Svelte frontend for the packaged harness web API.
In development, Vite serves the app and proxies `/api` to the FastAPI server on
`localhost:8000`.

Files:

- `docs/api.md`: HTTP and SSE API reference for the packaged session-manager API.
- `src/lib/api/types.ts`: TypeScript request, response, and stream-event types.
- `src/lib/api/index.ts`: API type exports.

Useful commands from the repo root or the Monty example:

```bash
just app-check     # svelte-check and the tests, under Bazel
just app-install   # installs the workspace for ui-dev
just ui-dev        # Vite dev server; proxies /api to localhost:8000
```

`app-check` runs `//typescript/apps/ui/...`. The production build is `//typescript/apps/ui:dist`,
which `just server` serves and the Python wheel ships. `app-install` and `ui-dev` run
`bazel run //typescript:pnpm -- --dir typescript/apps/ui ...`: the pnpm and Node Bazel pins, so
nothing needs installing. A `pnpm run build` outside Bazel writes `typescript/apps/ui/dist`,
which nothing else reads.

Local testing:

```bash
just server  # builds and serves the Svelte UI on port 8000
just ui-dev  # optional Vite frontend, with /api proxy
```
