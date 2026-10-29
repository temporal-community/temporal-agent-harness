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
`bazel run //typescript:pnpm -- --dir "$PWD/typescript/apps/ui" ...`: the pnpm and Node Bazel pins, so
nothing needs installing. A `pnpm run build` outside Bazel writes `typescript/apps/ui/dist`,
which nothing else reads.

Local testing:

```bash
just server  # builds and serves the Svelte UI on port 8000
just ui-dev  # optional Vite frontend, with /api proxy
```

## Building a pane

To add a pane, follow [AGENTS.md](AGENTS.md#add-a-pane).

## Embedding the console

The build is path-independent: Vite's `base` is `./` and every API call is
relative (`api/agents`, not `/api/agents`), so `dist` can be served under any
prefix. Serve it at `/harness/` and the console asks for `/harness/assets/...`
and `/harness/api/...`; route that `api/` to the harness API. Link to the
prefix with its trailing slash, because `/harness` without one resolves those
relative URLs against `/`.

`?mock=demo` (also `worst`, `empty`, `transcript5k`, `sessions10k`, and the
others in `src/main.ts`) runs the console on fixture data with no API. It is a
development-only switch, so it works under `just ui-dev` or a build made with
`NODE_ENV=development vite build --mode development`, not the shipped `dist`.

The console is an app, not a library. To put an agent in your own UI, build on
the client packages instead: `@temporalio/agent-harness-client` for the
transport and types, plus `@temporalio/agent-harness-svelte` or
`@temporalio/agent-harness-react` for the bindings (`python/examples/agent_dag/ui`
uses the Svelte one).
