# Claude Agent SDK integration (exploration)

Status: **exploratory.** Written against
[temporalio/ai-integrations#33](https://github.com/temporalio/ai-integrations/pull/33) (head
`b1cf384`, still a draft), installed via a git pin (see "Verified"). Verified against the PR's
scripted stand-in for Claude only: it has **not** been run with the real Claude Code engine.

## What the PR gives us

`temporalio.claude_agent_sdk` runs the Claude Code engine as Temporal Activities:

- `DurableClaudeAgent` (workflow side) loops *segments*. Each segment is the `run_claude_segment`
  Activity; it stops whenever Claude calls a durable tool (a hook `defer`s the call).
- Each durable tool call, and by default each Claude Code `Bash` / `mcp__*` call, is then its own
  Activity (`tool-<tool_use_id>`), after which the next segment resumes at that call.
- The conversation is held in the Workflow (read by Query, returned as deltas); Continue-As-New,
  History-size guards and live output (`follow_agent`, topic `claude`) are built in.
- It also owns **approvals** (`needs_approval`, `tool_approvals`, `decide`, `pending_approvals`) and
  its **own event vocabulary** (`prompt`, `tool_call`, `approval_needed`, `done`, `text`...).

## Where it overlaps the harness

| Concern | Plugin | Harness | Resolution |
|---|---|---|---|
| Model call as Activity | segment Activity | OpenAI/Gemini plugins | Plugin owns it. |
| Tool dispatch | `activity_as_tool(dict -> dict)` | `@agent.activity_tool_defn` (typed, `Injected`) | Harness tools are the user-facing API; plugin only sees their spec. |
| Approvals | own `decide` / `needs_approval` | `ToolApprovalPolicy`, `tool_approval` update, auto mode | **Harness wins**: one approval path, one UI. Plugin's switched off. |
| Events | own dicts on topic `claude` | `AgentEvent` turn stream | **Harness wins**: translate, do not dual-publish. |
| Conversation state | in the Workflow (plugin) | harness agent state | Plugin holds Claude's conversation; the harness holds nothing of it. |
| Continue-As-New | built in | harness has its own lifecycle | Open question (below). |

## The shape implemented

`temporal_agent_harness/ai_sdks/claude_agent_sdk_harness.py`, a sibling of `openai_agents_harness.py`
(the plugin itself is not vendored: it is a separate package):

- `HarnessClaudeAgent(DurableClaudeAgent)` takes the `AgentWorkflowRunner` and **harness tools**.
- `as_claude_tool(s)` turns a harness tool into a `DurableTool` *spec* (name, docstring, JSON schema
  from the model-facing signature, `Injected` already stripped). The plugin never executes it.
- It overrides `_execute_tool`:
  - harness tool → `runner.run_tool(call.id, tool, **input)`: policy gate + `tool_start/end/error`
    + activity dispatch, exactly as for the OpenAI/Pydantic AI adapters. A denial becomes a
    `ToolOutcome(is_error=True)` with the plugin's own rejection wording.
  - Claude Code tool run as an Activity (`Bash`, `mcp__*`) → `_apply_approval_policy` (the helper
    `as_harness_mcp_server` uses), `ToolStartEvent`, then the plugin's own tool-step Activity,
    then `ToolEnd/ErrorEvent`. Safe-by-default: unknown built-ins are gated unless listed in
    `inherently_safe_builtins`.
- It overrides `_publish` to a no-op: the harness turn stream replaces the plugin's event dicts.

`examples/claude_agent_hello/` shows the wiring (`ClaudeAgentPlugin` on the Worker,
`AgentHarnessPlugin` on the Client, `ToolApprovalPolicy()` so Bash waits for a human).

## What is awkward, and the seams to ask upstream for

Subclassing private members (`_execute_tool`, `_publish`) is the only reason this module can work
without changing the PR. It will break on any refactor. Concrete asks, in priority order:

1. **A tool-execution hook.** `DurableClaudeAgent(tool_executor=async (call) -> ToolOutcome | None)`
   (or public `execute_tool` overridable method), invoked for custom *and* engine calls, falling
   back to the default. Replaces the `_execute_tool` override. Also should expose whether a call is
   custom or engine (`call.kind` exists today).
2. **An event sink.** `on_event: Callable[[dict], None]` on the workflow side, and an equivalent
   for the segment Activity's `emit` (assistant text), so Claude's text becomes `ReplyDelta`.
   Today live text is published from the Activity to a Workflow Stream topic; the harness would
   need `AgentWorkflowRunner.publisher_from_activity(...)` there, which needs a
   `TurnStreamContext` in `SegmentInput` (a pass-through `extra: dict` on the segment input would do).
   **Not implemented here: with this sketch Claude's text arrives only as the final `TextReply`,
   no `reply_delta` streaming and no `model_interaction_*` / `TokenUsage` events.**
3. **Typed tool args.** `activity_as_tool` takes `dict -> dict` activities with an open schema.
   The harness would pass `input_schema` itself, which this sketch already does; validation of
   Claude's arguments against it is still left to the tool (harness tools validate through their
   own signature).
4. **Approval-free mode.** An explicit "approvals handled by caller" flag, so the dormant
   `decide` / `pending_approvals` surface is not left attached to the workflow.

## Open questions

- **Continue-As-New.** `DurableClaudeAgent` can continue as new itself (`auto_continue_as_new`),
  carrying `AgentState`. The harness has its own session lifecycle and a `SessionManagerWorkflow`.
  Whether the harness runner can be handed over across a CAN, and who owns `run()`'s arguments, is
  unresolved; this sketch leaves `auto_continue_as_new` off.
- **One task at a time.** `DurableClaudeAgent.run()` is not reentrant. The harness `@agent.accepts`
  handlers can overlap (`MidTurn`); the example relies on the default queueing. A `MidTurn.join`
  handler would raise "runs one task at a time".
- **Subagents.** Claude Code's own `Agent` subagents run inside the segment and cannot call
  durable tools. That is unrelated to the harness's `subagent_toolset`, which should remain the way
  to give Claude another harness agent (expose it as a tool).
- **Tool Activity IDs.** The plugin's `tool-<tool_use_id>` idempotency key is lost for harness
  activity tools, whose dispatcher assigns its own Activity ID. Engine tools keep it.
- **temporalio floor.** The PR needs `temporalio>=1.33`; this repo locks 1.32.
- **Worker footprint.** Each segment is a Claude Code process (~270 MB, 0.7-0.9 s start) with
  disk the Worker must share across a queue for `cwd`-based tools.
- **Where Claude Code runs.** It needs Anthropic/Bedrock credentials on the Worker, outside the
  harness's own model-credential handling.

## Verified

`tests/ai_sdks/claude_agents/` (6 tests, on the Temporal time-skipping server, using the PR's
`ScriptedClaude`; no engine or API key):

- a harness tool runs ungated with `tool_start` -> `tool_end`;
- `Bash` (an engine call, run as the plugin's own tool-step Activity) waits for a harness
  `tool_approval`, never runs before it, then emits
  `approval_requested -> resolved -> tool_start -> tool_end`;
- a denied `Bash` never runs, Claude sees the plugin's rejection wording, and no start/end
  bracket is published;
- `as_claude_tool` produces the model-facing schema and rejects non-harness tools;
  `tool_approvals`/`approvers` are refused.

The rest of the suite passes with the extra installed, except
`test_code_mode_vfs::test_a_rename_across_mounts_is_refused`, which fails on macOS only (the OS
error string for `EXDEV` differs from Linux's) and is unrelated.

Setup notes: the PR's `pyproject.toml` needs a recent `uv` (dotted `module-name` for
`uv_build`); uv 0.7.12 cannot parse it, 0.12.x can. The extra moves `temporalio` from 1.32 to
1.34 in `uv.lock`. Test packages must not be named `claude_agent_sdk` (it shadows the real
library).

## Not done

- **The real engine.** Nothing ran Claude Code itself: segment text, streaming, the `PreToolUse`
  defer hook, Continue-As-New and parallel tool calls in one message are untested here.
- **Live streaming** (seam 2 above): not implemented.
- **Dependency pin.** The extra points at a fork's commit; swap it for the released package when
  #33 ships.
