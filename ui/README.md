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

## Extension seams

`AgentUiExtensions` lets an example add presentation without putting example contracts in the
generic UI:

- `headerControl` supplies a header component with no application-specific props.
- `workspaceComponent` receives the current transcript items, session ID, following/closed state,
  and optional `onSend` callback through `AgentWorkspaceProps`.
- `toolPresentation` supplies an attachment component for a tool activity and an `isHost` predicate
  that selects its host row from the available activity rows.
- `presentation` is an `AgentPresentationAdapter`: `messageText` formats inbound messages, and an
  optional `replyText` formats reply data.

Examples own their extension implementations and pass them to `App` through this interface.
