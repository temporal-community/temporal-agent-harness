# Example wiring

Copy the closest example and adapt it; `python/examples/openai_hello` and
`typescript/examples/openai_hello` are the smallest. Keep the workflow name, the registry's
`workflow_type`, the committed schema's `agent`, and the worker's task queue aligned.

## Python example

Files under `python/examples/<example>/`:

| File | Role |
| --- | --- |
| `workflow.py` | The `@agent.defn` class and its tools; imports rooted at `examples.<example>` |
| `worker.py` | Connects, registers the model plugin then `AgentHarnessPlugin`, runs the worker |
| `BUILD.bazel` | `py_library` for the workflow, `harness_module_binary` for the worker, `harness_agent`, `harness_agent_registry` |
| `justfile` | Copy from `openai_hello`; it loads the root `.env.local` |
| `README.md` | What the agent shows and how to run it |
| `agents.toml`, `<Agent>.schema.json` | Generated and committed |

The declarations that gazelle does not generate:

```python
harness_agent(
    name = "MyAgent",                                   # the workflow name
    agent = "examples.my_example.workflow:MyAgentWorkflow",
    key = "my-example",
    label = "My Example",
    description = "One or two sentences shown in the console.",
    task_queue = "my-example",
    deps = [":workflow"],
)

harness_agent_registry(
    name = "agents",
    srcs = ["MyAgent.schema.json", "agents.toml"],
    agents = [":MyAgent"],
)
```

Then:

```bash
just gazelle                                          # generates the py_library/binary deps
bazel run //python/examples/<example>:agents_update   # writes agents.toml and the schema
just buildifier
```

Add the example's directory name to `EXAMPLES` in `python/examples/BUILD.bazel` so `just dev`,
`just server`, and `just workers` know it; those targets expect a `:worker` binary and the
registry's `agents.toml` in the example directory. Put tests in
`python/tests/examples/<example>/` with a `BUILD.bazel` copied from a neighbor.

Required init data: an `@agent.init` that takes a Pydantic model after `AgentConfig` appears in
the schema and the console asks for it before starting a session. Rerun `agents_update` whenever
handlers, state, or init data change.

## TypeScript example

Files under `typescript/examples/<example>/`: `agents.ts`, `worker.ts`, `package.json`,
`tsconfig.json`, `BUILD.bazel`, `justfile`, `README.md`, and the generated `agents.toml` and
schema. The `BUILD.bazel` is hand-written: `npm_link_all_packages`, a `ts_config`, `ts_project`
targets for the agent module and the worker, a `js_binary` worker, `harness_ts_agent`, and a
`harness_agent_registry` named `registry`. Its regeneration target is therefore
`bazel run //typescript/examples/<example>:registry_update`.

A new example is a new pnpm workspace package: add its dependencies to its `package.json`, run
`bazel run //typescript:pnpm -- install` from the repository root to update the single lockfile,
and add the package to `npm_translate_lock`'s `data` in `MODULE.bazel`;
`//typescript:npm_translate_lock_data_matches_workspace_test` fails until it is listed. Wire
tests as `harness_js_test` targets and run `scripts/check-ts-test-targets.sh`.

## Client SDK and a page for the agent

Declare a `harness_client_sdk` beside the `harness_agent`, as `python/examples/tictactoe` does,
and run `bazel run //python/examples/<example>:client_sdk_update` to write
`client_sdk/<Agent>.ts`. Commit it; never edit it. A page lives in `<example>/ui/`, served by a
`static_page` target and type-checked by a `svelte_check_test`; copy
`python/examples/tictactoe/ui/BUILD.bazel`. The application walkthrough is
`docs/guides/client-ui.md`, and `typescript/AGENTS.md` lists worked references.
