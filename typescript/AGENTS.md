# typescript/

The pnpm workspace for the console ([apps/ui](apps/ui)) and the client SDK ([packages](packages)).
The root [AGENTS.md](../AGENTS.md) covers setup, running the examples, Bazel tests, BUILD files and
regenerating derived files; this file adds the SDK specifics, and
[apps/ui/AGENTS.md](apps/ui/AGENTS.md) the console's. The code and its tests are the source of
truth; these files only point at them, and [docs-drift.test.mjs](apps/ui/tests/docs-drift.test.mjs)
fails, when the console's `check` script runs it, if a path, command or Bazel package they name
stops existing. It's local only; CI doesn't run it.

## Commands

From the repository root, with Bazel's pinned pnpm (`--dir` needs an absolute path under
`bazel run`):

```sh
just app-install # installs the workspace with --frozen-lockfile
bazel test //typescript/... # every package and the console, as CI runs them
# likewise `check` (`typecheck` in client and codegen), `test` and `build` in each SDK package
bazel run //typescript:pnpm -- --dir "$PWD/typescript/packages/client" run test
```

## Which package

| Package | Use it for |
| --- | --- |
| [`@temporalio/agent-harness-client`](packages/client) | Transport, `startSession`, message and state types, schema forms, and `AgentSessionCore` with `PlainSessionState` for code with no framework |
| [`@temporalio/agent-harness-react`](packages/react) | `useAgentSession`, `useAgentSessionSelector` and `HarnessProvider` |
| [`@temporalio/agent-harness-svelte`](packages/svelte) | `AgentSession` and `createHarnessContext` |
| [`@temporal-agent-harness/codegen`](packages/codegen) | `harness-codegen`: an agent's handlers, states and init data as TypeScript types |

Each binding re-exports the client, so an app imports from one package.

## Build an app on the SDK (React or Svelte)

1. **Generate the agent's types.** For an agent in this repo, declare a `harness_client_sdk` beside
   its `harness_agent`, as [tictactoe's BUILD file](../python/examples/tictactoe/BUILD.bazel) does,
   and run its `client_sdk_update` target (`bazel run //python/examples/tictactoe:client_sdk_update`
   writes `python/examples/tictactoe/client_sdk/TicTacToeAgent.ts`). For any other agent, pipe
   `temporal-agent-harness schema module:Class` into `harness-codegen` as
   [packages/codegen/README.md](packages/codegen/README.md) shows. Commit the output; never edit it.
2. **The rest** (connecting, starting or attaching, reading selectively, sending, answering the
   agent, testing) is in the root README's walkthrough,
   [Build a UI in React or Svelte](../README.md#build-a-ui-in-react-or-svelte), and in each
   binding's README: [React](packages/react/README.md), [Svelte](packages/svelte/README.md).

Worked references:

- [tictactoe/ui/App.svelte](../python/examples/tictactoe/ui/App.svelte): a whole Svelte app in one
  component (start, state, send, approvals, optimistic moves).
- [agent_dag/ui/Studio.svelte](../python/examples/agent_dag/ui/Studio.svelte): `callbackTools` the
  page answers, with [App.svelte](../python/examples/agent_dag/ui/App.svelte) (a session list) and
  [RunPanel.svelte](../python/examples/agent_dag/ui/RunPanel.svelte) (subagent messages typed by
  their own generated client) around it.
- [TicTacToeBoard.tsx](packages/react/test/TicTacToeBoard.tsx) and
  [TicTacToeBoard.svelte](packages/svelte/test/TicTacToeBoard.svelte): the same board in each binding.
- The [React](packages/react/README.md) and [Svelte](packages/svelte/README.md) READMEs: Markdown
  replies, per-handler reply cards, forms from a handler's schema, virtual transcripts, TanStack Query.
- An example's page lives in `python/examples/<example>/ui/`, served by a `static_page` target and
  type-checked by a `svelte_check_test` against its generated client; copy
  [tictactoe/ui/BUILD.bazel](../python/examples/tictactoe/ui/BUILD.bazel).
  `bazel run //python/examples/tictactoe/ui:play` serves that one on port 5173.

The console is not a template for an app: it replays runs through its own controller
(`apps/ui/src/lib/state/agentRun.svelte.ts`) and uses the client for types and helpers, not for
its sessions.

## Change the SDK

- Each source and test file has its own target in its directory's `BUILD.bazel`; copy a
  neighbour's (`ts_project`, `js_library` in the Svelte package, and `js_test` or `vitest_test`
  for tests), then run `scripts/check-ts-test-targets.sh`. Export new API from the package's
  entry point, such as [packages/client/src/index.ts](packages/client/src/index.ts), and document
  it in its README.
- Event protocol changes regenerate `packages/client/src/protocol.ts`; see the root AGENTS.md table.
- Publishing, and testing a local build in another app: [packages/README.md](packages/README.md).

## Rules

| Rule | Enforced by |
| --- | --- |
| Core SDK packages have no TanStack deps; TanStack lives in adapters and README recipes only | [coreDeps.test.ts](packages/client/test/coreDeps.test.ts) |
| These docs name only paths, commands and Bazel packages that exist | [docs-drift.test.mjs](apps/ui/tests/docs-drift.test.mjs) (local only, through the console's `check` script; CI doesn't run it) |
