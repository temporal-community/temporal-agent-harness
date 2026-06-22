# Shared Example UI

This directory is the shared Svelte frontend for the examples. In development,
Vite serves the app and proxies `/api` to the shared session-manager FastAPI
server on `localhost:8000`.

Files:

- `docs/api.md`: HTTP and SSE API reference for the shared session-manager API.
- `src/lib/api/types.ts`: TypeScript request, response, and stream-event types.
- `src/lib/api/index.ts`: API type exports.

Useful commands from any example that imports `examples/session_manager/justfile`:

```bash
just app-install   # one-time install, or after package changes
just app-check     # svelte-check + local Svelte 5 syntax guard
just app-build     # writes temporal_agent_harness/ui/dist for packaging
just ui-dev        # Vite dev server; proxies /api to localhost:8000
```

The same commands can be run from the repo root with `pnpm --dir ui ...`.

Local testing:

```bash
just server  # builds and serves the Svelte UI on port 8000
just ui-dev  # optional Vite frontend, with /api proxy
```
