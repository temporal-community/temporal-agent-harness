# What you get

### 🛡️ Durable execution — the foundation
Every agent is a Temporal workflow, so durability isn't a feature you add, it's the ground you
build on. A worker can crash, redeploy, or restart mid-turn and the agent resumes precisely where
it was — no lost state, no double-run tool calls. Model and tool calls retry by policy; an agent
can wait minutes or days for an external event without holding a process open; and every turn,
tool call, and decision is recorded and replayable.

### 📊 Standardized agents, fully observable
The interface to agents built on this harness is **carefully standardized**. Every agent takes the
same configuration contract and emits the same structured **event stream**: a protocol (under
active development) that captures an agent's entire lifecycle — every turn, model interaction, tool
call (start / end / error), reply token, citation, approval decision, subagent hand-off, and
token-usage tally.

That one standardized stream is the foundation of **observability and analytics** for every agent
you build. **Watch an agent live** as it works, or **replay exactly what happened** afterward —
what it decided, which tools it ran, what it cost, and where a human stepped in. You instrument
once; every agent on the harness gets it.

### 🖥️ Your own UI, typed end to end — React and Svelte
Build the product UI for your agent — a game board, a trip planner, a document it edits —
without writing a streaming client. Generate TypeScript types from the agent's Python class, and
`useAgentSession<MyAgent>()` (React) or `new AgentSession<MyAgent>()` (Svelte) gives you one
reactive object holding the session's messages, observable state, subagents, approvals, and
callbacks. `sendMessage` is type-checked against the agent's message handlers, and state you
declare in Python arrives in the browser typed and current. Reloads, extra tabs, and server restarts all pick the session
back up. See [Build a UI in React or Svelte](client-ui.md#build-a-ui-in-react-or-svelte).

### 🙋 Human-in-the-loop, solved
Tool approvals are built in and **safe-by-default**: any tool call can require human sign-off, and
a gated call **pauses inside the workflow and resumes durably** whenever a decision arrives (no
matter how long that takes) — there's no approval queue, state machine, or callback plumbing for
you to build. The policy engine is sophisticated out of the box: layered rules, inherently-safe
auto-approval, per-tool allow-lists, "approve and stop asking," per-session overrides, and runtime
policy updates. When there are more calls than a person can sign off on, **auto mode** puts your
own code — or a model — in the gate, judging each call against reusable criteria an end user can
edit mid-session: see [Auto mode](approvals.md#auto-mode--letting-code-or-a-model-decide).

### 🔌 Bring your own AI SDK
Write turn logic with the SDK you already know. The harness's integrations turn each SDK call into
a durable Temporal activity — so retries and credentials never leak into your workflow code.
Support is growing across the Python AI SDKs and agent frameworks Temporal integrates with:

| AI SDK | Status | Notes |
| --- | --- | --- |
| [Google Gemini](../../python/temporal_agent_harness/ai_sdks/google_genai_plugin) | ✅ Available now | Ships in this repo and is **experimental** - [Python SDK](https://github.com/temporalio/sdk-python) has a fully-supported non-harness integration. |
| [OpenAI Agents SDK](../../python/temporal_agent_harness/ai_sdks/openai_agents) | ✅ Available now | Ships in this repo and is **experimental** - [Python SDK](https://github.com/temporalio/sdk-python) has a fully-supported non-harness integration. |
| [Pydantic AI](../../python/temporal_agent_harness/ai_sdks/pydantic_ai_harness.py) | ✅ Available now | Directly uses Pydantic's Temporal plugin |
| [Google ADK](https://adk.dev/integrations/temporal/) | 🟡 Planned | - |
| [Strands Agents](https://docs.temporal.io/develop/python/integrations/strands-agents) | 🟡 Planned | - |
| [LangGraph](../../python/temporal_agent_harness/ai_sdks/langgraph/_harness.py) | ✅ Available now | Vendors the [`temporalio-langgraph`](https://github.com/temporalio/ai-integrations/tree/main/python/langgraph) plugin as `ai_sdks.langgraph` until its ToolNode fix is released |

### 📡 Durable and inline tools
Tools come in two on-worker flavors — durable, activity-backed tools (`@agent.activity_tool_defn`)
that run as retried, observable Temporal activities, and inline workflow tools
(`@agent.tool_defn`). Each publishes its own start/end lifecycle events onto the agent's
standardized event stream. (A third flavor — **callback tools** — runs on an attached client
instead of the worker; see
[Callback tools](#-callback-tools--let-the-client-run-the-tool).)

### 📞 Callback tools — let the client run the tool
An agent running on a Temporal worker often needs to act somewhere it can't reach — a file on the
user's laptop, a photo from their phone, a device on a private network. A **callback tool**
(`@agent.callback_tool_defn`) has no worker-side body: the agent **pauses inside the workflow**,
publishes the call, and an **attached client executes it on its own machine** and sends the result
back. You declare only the tool's typed contract (its `...` body is enforced) — the harness
supplies the single generic implementation. Because it's dispatched like any other tool, a callback
tool inherits the *same* approval policy, `tool_start`/`tool_end` events, and durable pause/resume:
the workflow simply waits (seconds or days) until a result arrives, and that result is validated
against the tool's declared output type before the turn continues. See
[`python/examples/callback_tools/wiki_agent`](../../python/examples/callback_tools/wiki_agent) — a cloud-shaped agent
that organizes a Markdown wiki on *your* local disk through a thin terminal client.

### 🧩 Agents that are more than chatbots
Most frameworks treat an agent as a single text-in / text-out function. Here, an agent exposes a
**strongly-typed interface**: declare named operations with `@agent.accepts` over pydantic models
(plain text is just one shape). The agent **advertises its own callable surface** — operation
names and input/output schemas — so it's self-describing and ready to be driven programmatically,
by your code or by another agent.

### 🔗 Composable by construction
Because agents have typed, self-describing interfaces, any harness agent can become a
**strongly-typed toolset** another agent drives — start it, call its operations, stop it.
Multi-agent systems compose through real contracts, not strings pasted between prompts.

### 🧑‍💻 Code Mode — give the model code, not just a call menu
Hand a model **one tool that runs a Python script** over your existing tools, instead of a long
menu of individual calls. `agent.code_mode_tool([...tools...])` turns any set of harness tools
into a single run-a-script tool: the model writes Python that calls them as async host functions,
with real control flow — loops, conditionals, `min`/`max`, and `asyncio.gather` concurrency — so
one turn orchestrates many calls. Each host call is still dispatched through the runner as a
durable, approval-gated, observable activity, and the script is statically type-checked against
your tools' signatures **before it runs**. And since a subagent toolset is just a list of tools,
Code Mode composes over subagents for free.
