# Should the Monty CodeAct scaffolding become a generic harness toolset?

> Status: open question / discussion note for the harness author. Not a proposal to
> implement yet — floating it to see whether promotion has been considered and, if not,
> whether it's intentionally staying in example-land.

Working through the Monty example, most of the script-execution machinery isn't actually
travel- or Monty-agent-specific — it feels like it wants to live in the harness. Floating it
as a question.

## What looks generic

The clearly-generic parts: the interpreter-stepping activities (`monty_start_batch` /
`monty_resume_batch` + `_drive_to_batch` in `examples/monty/monty_activities.py`) and the
batch loop in `MontyHostDriver.run_script` (`examples/monty/_host_driver.py`) have no domain
content.

And the two parts that *are* hand-wired today seem auto-derivable:

- `HOST_FUNCTION_STUBS` (`examples/monty/travel_models.py`) — the type-check stubs are
  hand-maintained, but the harness already introspects every tool's signature to build the
  model schema (`_tool_signatures` / `model_json_schema` in `harness/agent_workflow.py`), so
  the stubs could be generated from the agent's registered tools.
- `_dispatch_host_call` (`examples/monty/_host_driver.py`) — the `match name:` table maps host
  function names to activities, but those host functions are just the agent's registered
  tools, so the dispatch could be a lookup in the agent's tool set.

## Why it feels like a natural fit rather than a bolt-on

The script's host calls already go through `run_tool` (via `_run_activity_tool`), so
approvals, `tool_start` / `tool_end` events, retries, and durability already compose with no
special handling — and the snapshot-in-history concern is already covered by the
large-payload offload util (`utils/large_payload.py`). So this looks less like "make CodeAct
work with the harness" and more like "stop hand-wiring what the harness can generate."

Shape-wise it'd mirror `subagent_toolset` — an opt-in `code_act_toolset(runner)` that exposes
one `run_script` tool and generates the stubs/dispatch from the agent's registered tools.
Same "generate a model-facing surface from typed declarations" philosophy.

## Arguments against promoting it (which may well win)

- **Dependency coupling.** It'd couple the core to `pydantic-monty` (experimental, compiled,
  v0.0.x). Today it's deliberately example-only (`pyproject.toml` `examples` group). Probably
  an optional extra at most (e.g. `temporal-agent-harness[codeact]`).
- **The harness is deliberately unopinionated about the agentic loop.** CodeAct is one
  strategy among several (ReAct, plan-execute). As an optional toolset it stays agnostic; as
  core machinery it takes a side.
- **Sandbox + contract are arguably author policy** — which sandbox, what capabilities are in
  scope, type-check or not, how the script-writing prompt is tuned. A generic version has to
  make these configurable.
- **It's probably just early.** N=1 usage (only the Monty example). Better to see it across a
  couple of agents before fixing a generic API, so the abstraction is shaped by real
  variation rather than guessed from one example.

## What stays agent-specific regardless

The actual tools (`search_flights`, etc.), the agent's persona/system prompt, and any domain
framing in how it talks to the user. Those belong to the agent. The *scaffolding* —
interpreter stepping, batch loop, stub generation, name→tool dispatch — is what looks
generic.

## Ask

Mostly curious whether this has been considered, and if there's a reason it's intentionally
staying in example-land. Happy to prototype the `code_act_toolset` + stub-generation-from-
tool-signatures if it's worth exploring.
