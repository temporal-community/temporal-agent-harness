# Agent annotations reference

The annotations (decorators + type annotation) the harness defines, from the public `agent`
namespace (`temporal_agent_harness/harness/agent.py` `__all__`). There are six true annotations —
five decorators and one type annotation — plus related helpers. For *how* Python decorators work
(and how they differ from Java/Spring annotations), see `python-idioms-for-java-spring-devs.md`.

## Decorators

| Annotation | Kind | For |
|---|---|---|
| `@agent.defn` | class decorator | Stacked *with* `@workflow.defn`. Contract-checks the class is a valid harness agent (`run`/`__init__` takes exactly one `AgentConfig`) and stamps its discovered `@agent.accepts` handlers at import. Returns the class unchanged; fails fast on a malformed agent. |
| `@agent.accepts` | method decorator | Marks a typed, self-describing **operation** — `async def name(self, msg: InputModel) -> OutputModel`. Method name = operation name; input/output pydantic models = the schemas; docstring = the description; the return value becomes the turn's reply. The set of these is the agent's discoverable interface (`agent_interface`). Pure marker (sets an attribute; discovery happens in `@agent.defn`). |
| `@agent.tool_defn` | decorator factory | An **inline** tool — runs in the workflow. |
| `@agent.activity_tool_defn` | decorator factory | A **durable activity-backed** tool — runs as a retried Temporal activity. Returns the in-workflow dispatcher; the generated `@activity.defn` body is registered via `agent.tool_activity(t)`. |
| `@agent.callback_tool_defn` | decorator factory | A **callback** tool — body is a declaration only; fulfilled by an external client (pause → `callback_requested` → result → resume). |

The three tool decorators are the "**where does the tool run**" axis (in the workflow / on a worker
/ on an external client). All three route through `run_tool`, so all three get the approval gate +
`tool_start`/`tool_end` events. `@agent.accepts` and `@agent.defn` are *marker*-style (attach
metadata, return unchanged, discovered later); the tool decorators are *wrapping*-style (a factory
returning a decorator returning a wrapper).

## Type annotation

| Annotation | For |
|---|---|
| `Injected[T]` | Marks a tool parameter as **workflow-supplied and hidden from the model** (`x: Injected[Foo]`) — filled per call via `run_tool(injections=...)` instead of chosen by the LLM. Statically it's just `T`. |

## Related, but not annotations

Same `agent.*` namespace, but helpers/factories/types (not decorators):

- `agent.tool_activity(tool)` — returns the registrable `@activity.defn` body for an
  `activity_tool_defn` tool (for the worker's `activities=[...]`).
- `agent.subagent_toolset(...)` — factory: generates `start_/send_/stop_` tools from another agent's
  interface (agents-as-subagents).
- `agent.code_mode_tool(...)` — factory: the Code Mode tool (model-authored scripts over your tools).
- `ToolApprovalPolicy`, `AgentToolContext`, `ToolApprovalContext`, `CustomApprovalFallback`
  (type alias), and exceptions `ToolApprovalDenied` / `CallbackToolError` — supporting types.

## Notes

- Agents also use Temporal's own decorators — `@workflow.defn`, `@workflow.init`, `@workflow.run`.
  `@agent.defn` is designed to **stack with** `@workflow.defn` (it doesn't replace it). By contrast
  `@agent.activity_tool_defn` **replaces** the need for `@activity.defn` (and forbids stacking it) —
  it generates the activity for you. See `python-idioms-for-java-spring-devs.md`.
- **Slash commands** and the **operator interface** are *not* in this annotation set — they're
  registry/config-based, not `@agent.*` decorators.
- These names are re-exported by `agent.py` from `agent_workflow.py` (and `code_mode` /
  `subagent_toolset`); `agent.defn` and `agent_workflow.defn` are the *same* function object. Prefer
  the `agent.*` facade (the stable public surface).
