---
name: temporal-agent-kit-repo
description: Develop, run, and test example agents inside a temporal-agent-kit repository checkout, where Bazel and just build everything and registries are generated. Use when working in this repo; applications that installed the packages use temporal-agent-kit-python or temporal-agent-kit-typescript instead.
---

# Temporal Agent Kit: working in this repository

This skill applies to a checkout of `temporal-agent-kit`. The packaged skills beside it,
`temporal-agent-kit-python` and `temporal-agent-kit-typescript`, are for applications that installed
the SDK. Their `references/authoring.md`, `advanced.md`, and `verification.md` describe the agent
API and apply here unchanged. Their commands do not: `uv run temporal-agent-kit ...`, `init`,
`schema -o`, `dev`, `skills install`, and `npm run ...` are for installed packages. In this
repository everything builds and runs through Bazel, driven by `just`, and the contributing rules
in the root [AGENTS.md](../../../AGENTS.md) take precedence over anything here.

## How this repository differs from an installed package

- **Imports.** Python imports are absolute and rooted at `temporal_agent_kit`, `examples`, or
  `tests`. TypeScript examples depend on the workspace packages through `:node_modules/...`
  targets, never on published versions.
- **Generated registries.** Each example's `agents.toml` and `<Agent>.schema.json` are written by
  Bazel rules (`agent_kit_agent`, `agent_kit_ts_agent`, `agent_kit_agent_registry`) and committed.
  `bazel test` fails while they are stale. Never run the schema CLI by hand or edit these files.
- **Build files.** Python `BUILD.bazel` targets are generated: run `just gazelle` after adding,
  moving, or changing imports in `.py` files and keep the hand-written `agent_kit_*` declarations.
  TypeScript `BUILD.bazel` files are written by hand. Run `just buildifier` after any BUILD change.
- **Dependencies.** Python: `python/pyproject.toml` and `uv.lock`, then `just gazelle`.
  JavaScript: the `typescript/` pnpm workspace through `bazel run //typescript:pnpm -- <args>`.
  Do not `pip install` or `npm install` into an example.
- **Services.** `just temporal` in one terminal, then `just dev <example>` from the repo root
  runs the session manager, the console server, and that example's worker together. Each
  example's own justfile has `temporal`, `session-manager`, `server`, `worker`, and `dev`.
- **Credentials.** One `.env.local` at the repository root, copied from `.env.example`. The
  recipes load it. Never commit it.

## Choose the task

- Add or change an example agent, its registry entry, client SDK, or UI: read
  [example wiring](references/example-wiring.md).
- Write the agent itself: read the packaged
  [Python authoring](../temporal-agent-kit-python/references/authoring.md) or
  [TypeScript authoring](../temporal-agent-kit-typescript/references/authoring.md), then copy the
  closest example under `python/examples/` or `typescript/examples/`.
- Change the Agent Kit runtime, protocol, or console: follow the regeneration table in
  [AGENTS.md](../../../AGENTS.md) and read `contract/README.md` and `conformance/README.md`
  before touching schemas or goldens.

## Run and verify an example

```bash
cp .env.example .env.local        # once; set OPENAI_API_KEY and/or GEMINI_API_KEY
just temporal                     # terminal 1, unless Temporal is already running
just dev openai_hello             # terminal 2: session manager + console + this example's worker
```

Open <http://localhost:8000>, pick the agent, and verify a complete session: a reply, any tool
trace, and a reload that reconnects. `just dev` lists several examples at once; `just server` and
`just workers` run those parts separately. When the set of served agents changes against a
Temporal server that stays up, run `just reset-manager` before the next `just server`; it only
terminates the session-manager workflow. Do not reset or restart a developer's Temporal server.

## Test

- An example's tests live in `python/tests/examples/<example>/`; run its targets with
  `bazel test //python/tests/examples/<example>/...`. TypeScript examples keep tests beside their
  sources as `agent_kit_js_test` targets.
- Registry freshness is a test: `bazel test //python/examples/<example>/...` fails until
  `agents_update` has been rerun.
- Stream behavior changes need `bazel run //python/tests/conformance:goldens_update` and a
  review of the golden diff.
- Broad changes: `bazel test //...`; console changes: `just app-check`.
