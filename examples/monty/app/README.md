# Server App

This directory is the Svelte frontend for the Monty example. In development,
Vite serves the app and proxies `/api` to the shared FastAPI server on
`localhost:8000`.

Files:

- `docs/api.md`: HTTP and SSE API reference for the shared session-manager API.
- `src/lib/api/types.ts`: TypeScript request, response, and stream-event types.
- `src/lib/api/index.ts`: API type exports.

Useful commands from `examples/monty`:

```bash
just app-install   # one-time install, or after package changes
just app-check     # svelte-check + local Svelte 5 syntax guard
just app-build     # writes app/dist
just app-dev       # Vite dev server; proxies /api to localhost:8000
```

Local testing:

```bash
just server   # backend and /api
just app-dev  # frontend on Vite, with /api proxy
```
