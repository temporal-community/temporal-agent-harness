# Build a UI in React or Svelte

A UI on a harness agent should be about the product, not the protocol. The client SDKs take care
of the event stream, reconnects, JSON Patch state, pairing replies with messages, and the
subagent tree, and they type all of it from the agent's schema. The integration is one
line:

```ts
const agent = useAgentSession<TicTacToeAgent>({ sessionId });                      // React
const agent = new AgentSession<TicTacToeAgent>({ get sessionId() { return id; } }); // Svelte
```

| Package | What it is |
| --- | --- |
| [`@temporalio/agent-harness-react`](../../typescript/packages/react) | `useAgentSession`, a hook for React 18.2+ |
| [`@temporalio/agent-harness-svelte`](../../typescript/packages/svelte) | `AgentSession`, a reactive class for Svelte 5 |
| [`@temporalio/agent-harness-client`](../../typescript/packages/client) | the framework-free core both bindings share; re-exported by each |

**1. Declare state on the agent.** `agent.state(...)` makes a model observable: every
committed `mutate()` block is published on the event stream as JSON Patch ops. A tool reaches
the state as an injected `StateRef`, which the workflow supplies and the model never sees, so
the browser watches the board change as the model plays.

```python
from agents import Agent, Runner

from temporal_agent_harness.ai_sdks.openai_agents_harness import as_openai_agent_tool
from temporal_agent_harness.harness.state import HarnessState, StateRef


class Board(HarnessState):
    """Nine cells, read left to right, top to bottom."""

    cells: list[Literal["X", "O"] | None] = [None] * 9


class PlayMove(BaseModel):
    """The player's move."""

    cell: int


# The model supplies `cell` and `mark`; the workflow injects `board`.
@agent.tool_defn(inherently_safe=True)
async def place_mark(
    cell: int, mark: Literal["X", "O"], board: agent.Injected[StateRef[Board]]
) -> str:
    """Place a mark on an empty cell, 1-9."""
    with board.mutate() as draft:
        draft.cells[cell - 1] = mark
    return f"{mark} is on cell {cell}."


@agent.defn(name="TicTacToeAgent")
class TicTacToeAgentWorkflow:
    board = agent.state(Board)

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._sdk_agent = Agent(
            name="TicTacToe",
            instructions="You play O. Record the player's X with place_mark, then place your O.",
            model="gpt-5.1",
            tools=[as_openai_agent_tool(self._runner, place_mark, injections={"board": self.board})],
        )

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def play(self, move: PlayMove) -> TextReply:
        """Play X on a cell; the agent answers with its O."""
        prompt = f"Board: {self.board.current.cells}. I play X on cell {move.cell}."
        result = await Runner.run(self._sdk_agent, input=prompt)
        return TextReply(text=str(result.final_output))
```

**2. Generate the types.** `temporal-agent-harness schema` prints an agent's handlers, state,
and init data as JSON Schema, and [`harness-codegen`](../../typescript/packages/codegen) turns that
into TypeScript. Install the generator in your application (available starting with the next
npm release), then generate from either SDK's schema:

```bash
npm install --save-dev --save-exact @temporalio/agent-harness-codegen@0.2.0
uv run temporal-agent-harness schema my_agent.workflow:TicTacToeAgentWorkflow -o agent.schema.json
# For TypeScript, use agent-harness-schema on your compiled agent instead.
npx harness-codegen agent.schema.json -o TicTacToeAgent.ts
```

Repository contributors can also use `bazel run //python/examples/tictactoe:client_sdk_update`.

Commit the output and regenerate it in CI. Renaming a field on `Board` without regenerating then
fails the type check, not production.

**3. Render the session.**

```bash
npm install @temporalio/agent-harness-react    # or @temporalio/agent-harness-svelte
```

```tsx
import { useAgentSession } from "@temporalio/agent-harness-react";
import type { TicTacToeAgent } from "./client_sdk/TicTacToeAgent";

function Game({ sessionId }: { sessionId: string }) {
  const agent = useAgentSession<TicTacToeAgent>({ sessionId });
  const board = agent.states.board; // typed as the Python `Board`

  return (
    <>
      {board?.cells.map((cell, i) => (
        // the handler name and its payload are both checked against the agent
        <button key={i} onClick={() => agent.sendMessage("play", { cell: i + 1 })}>{cell ?? ""}</button>
      ))}

      {agent.messages.map((m) => (
        <p key={m.id}>{m.handler} · {m.status} {m.output?.text}</p>
      ))}

      {agent.pendingApprovals.map(({ part }) => (
        <div key={part.toolId}>
          <strong>{part.toolName}</strong>
          <button onClick={() => agent.respondToApproval(part.toolId, { approved: true })}>Approve</button>
          <button onClick={() => agent.respondToApproval(part.toolId, { approved: false })}>Deny</button>
        </div>
      ))}
    </>
  );
}
```

The Svelte binding has the same fields and actions; a component reads `agent.states.board` in a
`$derived` and the connection stays open while anything reads it. Both packages' READMEs have the
full example.

**What the session gives you**

- **Typed messages.** `agent.messages` narrows on `handler`, so `input` and `output` are that
  handler's models. Each message's `parts` are its model calls, tool calls, and reply deltas, as
  they stream.
- **The whole agent tree.** Every subagent, at any depth, gets its own messages and state
  through `agent.agents` and `agent.agent(id)`. `pendingApprovals` and `pendingCallbacks`
  collect waiting tool calls from all of them and answer the right workflow.
- **Callback tools in the browser.** Tools listed in `callbackTools` are answered by the page
  itself (`get_timezone: () => Intl.DateTimeFormat().resolvedOptions().timeZone`); any other
  callback, such as an `ask_user`, waits in `pendingCallbacks` for
  `agent.provideToolOutput(toolId, output)`.
- **Durable by default.** A reload returns the same session, two tabs both stay live, and after a
  server restart the stream resumes where it left off.
- **One connection per session.** Components following the same session share one connection
  under a `<HarnessProvider>` (React) or `createHarnessContext()` (Svelte).
- **Typed starts.** `startSession(transport, TicTacToeAgent, { data })` creates a session, with
  `data` required, optional, or refused to match the agent's init data.

[`python/examples/tictactoe/ui`](../../python/examples/tictactoe/ui) is a complete board in one Svelte component, and
[`python/examples/agent_dag/ui`](../../python/examples/agent_dag/ui) is a larger studio. The design and its reasoning
are in [`docs/design/typed-agent-sessions.md`](../design/typed-agent-sessions.md).
