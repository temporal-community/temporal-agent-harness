# Run the example agents

## Requirements

For Python agents and the shared console:

- Python **3.11+**
- [uv](https://docs.astral.sh/uv/) for dependency management
- A Temporal service — a local dev server (`temporal server start-dev`) or Temporal Cloud

For TypeScript agents in your own project, see the
[TypeScript requirements and installation](../../typescript/packages/core/README.md#build-with-it--install-from-npm).

To run the bundled examples from a checkout, additionally:

- [just](https://just.systems/) for the example recipes
- [Bazelisk](https://github.com/bazelbuild/bazelisk), installed as `bazel` (e.g.
  `brew install bazelisk`), which the recipes build and run with. It fetches the pinned Bazel,
  which brings its own Python, Node and pnpm.
- The [`temporal` CLI](https://docs.temporal.io/cli), which `just temporal` starts a local dev
  server with. The Nexus example (`examples/nexus_hello`) needs **1.9.1 or later** — older
  builds reject the Nexus dynamic config its `just temporal` recipe sets. Its recipes check the
  version and fail with a message. The other examples work with any recent release.

A separate Node or [pnpm](https://pnpm.io/) installation is **not** required for the checkout
recipes: they use the Node and pnpm Bazel pins — see
[UI development](../internal/development.md#ui-development).


## Run the examples

These run from a checkout or an unpacked
[release archive](../../python/README.md#try-the-example-agents) — the steps are identical.

One `.env.local` at the **project root** serves every example. Create it once:

```bash
cp .env.example .env.local
```

Set the creds for whichever agents you'll run: `OPENAI_API_KEY` (react_agent, openai_hello,
pydantic_ai_hello) and/or `GEMINI_API_KEY` (monty, wiki, coding, code_mode_memory).
The default committed `temporal.local.toml` profile points at a local Temporal dev server.

### One example, standalone

Each example runs on its own from its directory — [`python/examples/monty`](../../python/examples/monty) (a
conversational Code Mode travel agent + a subagent variant) is the best starting point:

```bash
cd python/examples/monty
just temporal          # local Temporal dev server; skip if you bring your own
just session-manager   # worker hosting the packaged SessionManagerWorkflow
just server            # serves the Svelte UI + /api on :8000 (this example's agents only)
just worker            # this example's agent worker
```

Open <http://localhost:8000> and pick an agent. Every example follows the same recipe set
(`temporal` / `session-manager` / `server` / `worker`, plus `client` where noted). `just server`
builds the UI from `typescript/apps/ui/` with the Node and pnpm Bazel pins, so a change there
shows up on the next `just server`.

### All examples behind one UI

The **root** justfile runs every example agent at once so the UI lists them all. From the project root,
each in its own terminal:

```bash
just temporal          # start FRESH (or `just reset-manager` first — see the gotcha)
just dev               # starts the session manager, the server, and all eight workers in one terminal; Ctrl-C stops them all
```


`server`, `workers`, and `dev` also take example names (the directory under `python/examples/`, e.g.
`monty` or `auto_mode`) to run only those: `just dev monty auto_mode` serves just their agents and
starts just their workers, and `just workers tictactoe` adds one more worker beside a running
stack. From an example's own directory, `just dev` does the same for that example. Switching the
set of agents you serve is a registry change, so `just reset-manager` first (see the gotcha below).

Then create a session for any agent in the UI. A few need extra setup or a client:

| Agent | Needs |
|---|---|
| OpenAI Hello · Pydantic AI Hello | `OPENAI_API_KEY`; chat directly in the UI |
| Monty (both) | `GEMINI_API_KEY`; chat directly in the UI |
| Travel agent (Jev Auto mode) | `GEMINI_API_KEY` **and** `TYPESAFE_API_KEY`; Monty with [auto mode](approvals.md#auto-mode--letting-code-or-a-model-decide) judging its gated calls ([readme](../../python/examples/auto_mode/README.md)); chat directly in the UI |
| Tic-Tac-Toe (TypeSafe) | `TYPESAFE_API_KEY`; no LLM — every move is a [TypeSafe](https://docs.typesafe.ai) System One judgment ([readme](../../python/examples/tictactoe/README.md)); send `new_game` then `play` in the UI, or `just tictactoe-play` for its board |
| ReAct Agent | `OPENAI_API_KEY`; the **F1 MCP server** at `F1_MCP_SERVER_HOME` ([setup](../../python/examples/react_agent/README.md#the-f1-mcp-server)); `just react-client` to answer its `ask_user` (chat alone works in the UI) |
| Code Mode Memory | `GEMINI_API_KEY`; long-term memory on the worker's disk, shared by sessions given the same `memory_dir` ([readme](../../python/examples/code_mode_memory/README.md)); chat directly in the UI |
| Agent DAG Studio | `OPENAI_API_KEY`; an agent writes a Python flow of agents that Code Mode runs as subagents ([readme](../../python/examples/agent_dag/README.md)); best in its own UI, **`just agent-dag-studio`** |

**Gotcha — the session manager caches its registry.** The server seeds the `session-manager`
workflow with the registry on first start and reuses the existing one after that. So when you switch
between a single-example server and the all-agents server (or change the set), run
`just reset-manager` before the next `just server`, or start a fresh Temporal dev server. Also: an
agent whose worker isn't running will accept a created session but never progress (it parks) — start
its worker.
