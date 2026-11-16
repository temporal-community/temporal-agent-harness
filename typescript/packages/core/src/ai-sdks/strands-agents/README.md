# Strands Agents on the TypeScript harness

Run a [Strands Agents](https://strandsagents.com/) agent as a harness agent. Every model call runs
as a Temporal activity and streams live onto the agent's turn: replies, reasoning, tool requests
and token usage. Every tool call goes through the harness: the approval policy, `tool_start` /
`tool_end` / `tool_error` events, injected parameters, and mid-turn steering. This is the
TypeScript side of the Python integration (`temporal_agent_harness.ai_sdks.strands_agents`); both
work the same way.

The integration has four modules:

| Module | Runs | What |
|---|---|---|
| `@temporalio/agent-harness/ai-sdks/strands-agents-harness` | workflow | `harnessStrandsAgent`, `asStrandsTool(s)`, `harnessStreamRoute` |
| `@temporalio/agent-harness-worker/ai-sdks/strands-agents-harness` | worker | `harnessObserverFactory`, `StrandsStreamObserver` |
| `@temporalio/agent-harness/ai-sdks/strands-agents` | workflow | the vendored plugin's workflow half: `TemporalAgent`, `TemporalModel`, … |
| `@temporalio/agent-harness-worker/ai-sdks/strands-agents` | worker | the vendored plugin's worker half: `StrandsPlugin` |

`typescript/examples/strands_hello` is a complete agent using everything below.

> **Experimental.** This integration may change in future versions.

## Installation

```sh
pnpm add @temporalio/agent-harness @temporalio/agent-harness-worker @strands-agents/sdk
```

`@strands-agents/sdk` is an optional peer of both harness packages. Install the model provider
you use too, e.g. `openai` for `@strands-agents/sdk/models/openai`.

## Quickstart

### Workflow

```ts
import { z } from "zod";
import {
  MidTurn, TextMessage, TextReply, ToolApprovalPolicy, defineAgent, handler, tool,
} from "@temporalio/agent-harness";
import {
  asStrandsTool, harnessStrandsAgent,
} from "@temporalio/agent-harness/ai-sdks/strands-agents-harness";

const getWeather = tool({
  name: "get_weather",
  description: "Return the current weather for a city.",
  inputSchema: z.object({ city: z.string() }),
  outputSchema: z.string(),
  inherentlySafe: true,
  execute: async ({ city }) => `It's 72°F and sunny in ${city}.`,
});

export const WeatherAgent = defineAgent({
  name: "WeatherAgent",
  approvalPolicyDefault: ToolApprovalPolicy.allowInherentlySafe(),
  handlers: {
    ask: handler({
      description: "Chat with the assistant; sent mid-turn, the message steers it.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ACCEPT,
    }),
  },
  create: ({ runner }) => {
    const agent = harnessStrandsAgent(runner, {
      model: "openai", // a name registered in StrandsPlugin({ models })
      activityOptions: { startToCloseTimeout: "2 minutes" },
      systemPrompt: "You are a friendly assistant.",
      tools: [asStrandsTool(runner, getWeather)],
      printer: false, // replies stream through the turn, not to stdout
    });
    return {
      async ask({ text }, ctx) {
        if (ctx.joined_turn) {
          runner.steer(text);
          return { text: "" };
        }
        const result = await agent.invoke(text);
        return {
          text: result.lastMessage.content
            .flatMap((block) => (block.type === "textBlock" ? [block.text] : []))
            .join(""),
        };
      },
    };
  },
});
```

Build the agent in `create`: it keeps the conversation for as long as the workflow runs. Read the
reply from `result.lastMessage`'s text blocks: a Strands `AgentResult`'s `toString()` also prints
the model's reasoning.

### Worker

```ts
import { OpenAIModel } from "@strands-agents/sdk/models/openai";
import { AgentHarnessPlugin, toolActivity } from "@temporalio/agent-harness-worker";
import { StrandsPlugin } from "@temporalio/agent-harness-worker/ai-sdks/strands-agents";
import { harnessObserverFactory } from "@temporalio/agent-harness-worker/ai-sdks/strands-agents-harness";

const worker = await Worker.create({
  connection,
  taskQueue: "weather",
  workflowsPath: fileURLToPath(new URL("./agents.js", import.meta.url)),
  plugins: [
    new StrandsPlugin({
      models: { openai: () => new OpenAIModel({ modelId: "gpt-5.1" }) },
      observerFactory: harnessObserverFactory,
    }),
    new AgentHarnessPlugin({ tools: [/* toolActivity(...) for each activityTool */] }),
  ],
});
```

`StrandsPlugin` registers the model activities and adjusts the workflow bundle for Strands (see
[Bundling](#bundling)). Without `observerFactory: harnessObserverFactory`, model calls run but
nothing streams onto the turn. The harness's plugin goes last, as in Python.

## Streaming

`harnessStrandsAgent` routes every model call to the turn in progress. Inside the model activity,
the harness observer translates the Strands stream:

| Strands stream event | Turn event |
|---|---|
| call starts | `model_interaction_started` (the worker model's `modelId`) |
| `textDelta` | `reply_delta` |
| `reasoningContentDelta` | `thought_summary` |
| a `toolUseStart` block (`toolUseId`, name, streamed input) | one `tool_requested`, keyed by `toolUseId` |
| `modelMetadataEvent.usage` | the usage on `model_interaction_ended`, which is always published |

## Tools

Every tool call of a harness Strands agent gets the agent's approval policy, lifecycle events and
steering:

| Tool | Declare with | Runs |
|---|---|---|
| Workflow tool | `tool({ ..., execute })` | inline, in the workflow |
| Activity tool | `activityTool(...)` | on a worker, as a durable activity; register its body with `AgentHarnessPlugin({ tools: [toolActivity(tool, body)] })` |
| Callback tool | `tool(...)` without `execute` | on a client: the call waits in the workflow until a client posts the result |
| MCP tools | `new TemporalMCPClient({ server })` in `tools` | one activity per call, against the MCP server the worker registers |

Pass each declared tool through `asStrandsTool(runner, tool)` (or `asStrandsTools(runner, [...])`).
Each MCP tool call runs as a harness `tool(...)` named and described as the MCP tool is, so it is
gated and bracketed exactly like your own tools. `tool_requested`, `tool_start` and `tool_end`
share Strands' `toolUseId`. `harnessStrandsAgent` refuses anything else: Strands `tool(...)`s and
`workflow.activityAsTool`.

### Injected parameters

A parameter the workflow fills, not the model, is left out of the tool's schema. Pass the values,
keyed by parameter name, when you adapt the tool:

```ts
asStrandsTool(runner, recordStop, { injections: { board } });
```

Every call hands the mapping to `runner.runTool(..., { injections })`. The mapping is bound when the
agent is built, so inject long-lived values; an activity tool's injected values must be
serializable, since they travel in its activity input.

## MCP servers

Register each MCP server on the worker by name, and pass a `TemporalMCPClient` for it in the
agent's `tools`:

```ts
new StrandsPlugin({
  models: { ... },
  mcpClients: { "f1-data": () => new McpClient({ transport: ... }) },
  observerFactory: harnessObserverFactory,
});
```

```ts
const agent = harnessStrandsAgent(runner, {
  model: "openai",
  tools: [
    asStrandsTool(runner, getWeather),
    new TemporalMCPClient({ server: "f1-data", cacheTools: true }),
  ],
});
```

Listing the server's tools and each tool call are activities, so they replay from history. The
agent's approval policy covers every MCP call: under a policy that asks a person, each call waits
for approval, and a denied call reaches the model as an error result without reaching the server.
Address an MCP tool in the policy by the name the model sees (with the client's `prefix`, if any).
`cacheTools: true` lists the tools once per workflow instead of before every model call.

## Human input and approvals

Strands pauses an agent for a person with interrupts: the run stops, and the caller resumes it
with answers, which runs the interrupted tool again. A harness agent uses the harness's own
mechanisms instead, which keep the turn running: a **callback tool** to ask a person or client for
something (its call waits durably in the workflow), and the **approval policy** to approve a tool
call. `harnessStrandsAgent` refuses Strands interrupts: if a tool or a hook interrupts the run, the
call fails with a non-retryable `ApplicationFailure` of type `StrandsInterruptNotSupported`, the
turn publishes `message_handler_error`, and the turn ends.

## Mid-turn steering

Accept messages mid-turn (`midTurn: MidTurn.ACCEPT`) and, when `ctx.joined_turn`, queue the text
with `runner.steer(text)`, as in the quickstart. The harness appends it to the next tool result the
model receives. `tool_end` keeps the tool's own output.

## Models

`StrandsPlugin({ models: { name: factory } })` maps names to model factories. Each factory runs
lazily on the worker, inside the model activity, and its model is cached for the worker's lifetime.
`harnessStrandsAgent(runner, { model: name })` selects one. Read credentials in the factory, never
in workflow code. Strands' own retries are off (`retryStrategy` is refused): set `retry`, timeouts
and the other activity options in `activityOptions`.

## Bundling

A TypeScript worker bundles its workflow code. `StrandsPlugin` adjusts the bundle for Strands:

- The Node built-ins, model-provider SDKs, AWS clients and MCP transports that
  `@strands-agents/sdk` and `@modelcontextprotocol/*` import, which workflow code never uses, are
  replaced with empty modules. Only imports made from those packages are replaced, so your own
  workflow code, and another integration's, keeps every library it imports, and Temporal's check on
  Node built-ins still covers it.
- The workflow half installs the `Headers`, web-streams and `structuredClone` polyfills Strands
  needs in the workflow sandbox. The package marks those modules as having side effects, so
  webpack keeps them.

A worker can run harness Strands agents beside OpenAI Agents or Gemini agents.

## Not available in a harness agent

These upstream features work only on a plain `TemporalAgent`, which is still exported: Strands
tools and `workflow.activityAsTool`, interrupts, and raw `streamingTopic` streaming. Upstream's TypeScript package
has no sandbox, worker env refs or sync-call guard, so neither does this one.

## Vendoring

This package and its worker half are `temporalio/ai-integrations` `typescript/strands-agents` at
`a18192838b2390ed2186d68018ff6adea657461a`, the commit the Python integration vendors. Upstream's
style is kept (quotes, formatting) so a sync diffs cleanly. Every local change is marked with a
`// Harness:` comment:

- **Split:** one upstream package becomes a workflow half (core) and a worker half (worker). The
  model activity's wire types and activity names live in core; `ModelActivity` in the worker.
  `temporal-mcp-client.ts` splits the same way.
- **The streaming seam:** `StrandsPlugin({ observerFactory })` and `ModelActivity`'s observer,
  `TemporalModel` / `TemporalAgent({ streamRoute })`, and a `streamTo` token on the streaming
  activity input.
- **Bundling:** the issuer-scoped replacement above, in place of upstream's bundle-wide
  `ignoreModules` and its MCP-transport stub module. Temporal's own check on Node built-ins in
  workflow code skips only those imported from `@strands-agents/sdk` or `@modelcontextprotocol/*`
  (its file session storage imports `fs` and `path`).
- **ES modules:** `.js` / `.ts` import extensions, `import` in place of `require` (the polyfills install
  only inside a workflow and only where missing; the MCP client and tool import each other), and
  `StrandsPlugin.version` read from the harness worker package.
- **The repo's compiler settings:** no constructor parameter properties, a typed view of
  `globalThis` in the polyfills, the MCP transport type from `@modelcontextprotocol/client`, and
  a private static `apply` renamed `applyListings`.
- **Exports:** core's index also exports what the worker half needs: `rewriteError`, the model
  activities' wire types and activity names.
